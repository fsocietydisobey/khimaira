"""Knowledge-graph layer over the Claude native-memory indexes (memory-kg).

Nodes are the memory entries already indexed by ``claude_memory_retrieval``
(MEMORY.md / MEMORY_ARCHIVE.md bullets for the khimaira and jeevy corpora),
addressed by the SAME ``uuid5(project:link)`` ids the Qdrant vector layer
uses — the two layers name identical entities. Edges are typed, explicit,
non-destructive relationships (``SUPERSEDES`` / ``RELATES_TO`` / ``CAUSED_BY``)
stored in a small SQLite table; the memory markdown files are NEVER written
by anything in this module — originals stay untouched forever (the design
rejected AI-consolidation as lossy; a graph edge gives the same "this
supersedes that" value without rewriting content).

Serving: the monitor daemon mounts ``monitor/api/memory_kg.py`` at
``/internal/memory-kg/{graph,node/<id>,schema,health}`` and registers itself
as the ``khimaira`` project's KG adapter — memory is a khimaira feature, not
a separate project, so the sidebar's khimaira → kg tab is the entry point.
The existing generic proxy (``/api/graph/khimaira``), graph viewer, and
``kg_*`` MCP tools work with zero UI/tool changes; when the khimaira repo is
attached (the normal case) the adapter annotates that real registry entry,
falling back to a virtual placeholder otherwise. Nodes are derived live from the memory files
on each request (they're tiny); only edges live in SQLite.

Edge writes happen ONLY through :func:`link_entries` (the ``memory_link``
MCP tool) — no automatic edge inference anywhere. AI may *suggest* an edge
in conversation; only an explicit tool call commits it.
"""

from __future__ import annotations

import os
import re
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from khimaira.claude_memory_retrieval import (
    MemorySource,
    _point_id,
    _source_entries,
    canonical_project,
    configured_sources,
)
from khimaira.log import get_logger

log = get_logger("monitor.memory_kg")

ADAPTER_LABEL = "khimaira"

EDGE_TYPES = ("SUPERSEDES", "RELATES_TO", "CAUSED_BY")

# The memory convention's entry types (frontmatter `metadata: type:`); anything
# unrecognized falls back to the generic "memory" node type.
_ENTRY_TYPES = ("user", "feedback", "project", "reference")
_FALLBACK_TYPE = "memory"

# `type: <value>` inside a leading `--- ... ---` frontmatter block. The memory
# files nest it under `metadata:`, but matching the key line alone keeps this
# tolerant of indentation drift across hand-written topic files.
_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---", re.DOTALL)
_TYPE_LINE_RE = re.compile(r"^\s*type:\s*([A-Za-z_-]+)\s*$", re.MULTILINE)

_DB_ENV = "KHIMAIRA_MEMORY_KG_DB"


# ---------------------------------------------------------------------------
# Edge store (SQLite — the ONLY writable state in this feature)
# ---------------------------------------------------------------------------


def db_path() -> Path:
    """Resolve the edge-store path at call time (env-injectable for tests).

    Resolution is deliberately lazy — a module-level constant would freeze the
    production path before test fixtures can monkeypatch the environment.
    """
    override = os.environ.get(_DB_ENV)
    if override:
        return Path(override).expanduser()
    state_home = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    return state_home / "khimaira" / "memory_kg.sqlite3"


def _connect() -> sqlite3.Connection:
    path = db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS edges (
            id TEXT PRIMARY KEY,
            from_id TEXT NOT NULL,
            to_id TEXT NOT NULL,
            type TEXT NOT NULL,
            created_at TEXT NOT NULL,
            note TEXT NOT NULL DEFAULT '',
            UNIQUE (from_id, to_id, type)
        )
        """
    )
    return conn


def _edge_id(from_id: str, to_id: str, edge_type: str) -> str:
    """Deterministic edge id — stable across re-adds of the same triple."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"memory-edge:{from_id}:{to_id}:{edge_type}"))


def add_edge(from_id: str, to_id: str, edge_type: str, note: str = "") -> dict[str, Any]:
    """Insert one typed edge. Idempotent on (from_id, to_id, type).

    Raises ValueError on an unknown edge type or a self-loop — reject loudly
    at the boundary rather than persisting garbage.
    Returns the stored row plus ``created`` (False when it already existed).
    """
    if edge_type not in EDGE_TYPES:
        raise ValueError(f"unknown edge type {edge_type!r}; must be one of {list(EDGE_TYPES)}")
    if from_id == to_id:
        raise ValueError("self-loop rejected: from and to resolve to the same memory entry")

    created_at = datetime.now(UTC).isoformat()
    with _connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO edges (id, from_id, to_id, type, created_at, note) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (_edge_id(from_id, to_id, edge_type), from_id, to_id, edge_type, created_at, note),
        )
        created = cur.rowcount == 1
        row = conn.execute(
            "SELECT id, from_id, to_id, type, created_at, note FROM edges "
            "WHERE from_id = ? AND to_id = ? AND type = ?",
            (from_id, to_id, edge_type),
        ).fetchone()
    return {**dict(row), "created": created}


def list_edges() -> list[dict[str, Any]]:
    """All stored edges, oldest first (full rows, including note/created_at)."""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, from_id, to_id, type, created_at, note FROM edges ORDER BY created_at, id"
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Node derivation (read-only over the memory markdown files)
# ---------------------------------------------------------------------------


def _entry_type(memory_dir: Path, link: str) -> str:
    """Node type for an entry: topic-file frontmatter → filename prefix → fallback.

    The frontmatter is authoritative when the linked topic file exists and
    declares a known type; the ``<type>_slug.md`` naming convention is the
    deterministic fallback for entries whose topic file is missing/unreadable.
    """
    target = memory_dir / link
    try:
        head = target.read_text(encoding="utf-8")
    except OSError:
        head = ""
    if head:
        fm = _FRONTMATTER_RE.match(head)
        if fm:
            m = _TYPE_LINE_RE.search(fm.group(1))
            if m and m.group(1) in _ENTRY_TYPES:
                return m.group(1)
    prefix = Path(link).name.split("_", 1)[0]
    if prefix in _ENTRY_TYPES:
        return prefix
    return _FALLBACK_TYPE


def _collect_node_entries(sources: list[MemorySource] | None = None) -> list[dict[str, Any]]:
    """One record per (project, link) across live + archive files.

    Mirrors ``claude_memory_retrieval._collect_entries``' dedup semantics
    (archive loaded first so a duplicated live entry wins deterministically)
    but keeps the memory directory and an ``archived`` flag, which the vector
    layer doesn't need.
    """
    sources = configured_sources() if sources is None else sources
    records: dict[tuple[str, str], dict[str, Any]] = {}
    for source in sources:
        for path, archived in ((source.archive_path, True), (source.index_path, False)):
            for entry in _source_entries(source, path):
                records[(entry["project"], entry["link"])] = {
                    **entry,
                    "archived": archived,
                    "memory_dir": source.index_path.parent,
                }
    return list(records.values())


def _node_for(record: dict[str, Any]) -> dict[str, Any]:
    """Project one entry record onto the generic graph-node contract.

    ONLY contract fields (id/type/label + optional badge) — the daemon's
    contract gate drops nodes carrying extra fields.
    """
    node: dict[str, Any] = {
        "id": _point_id(record["project"], record["link"]),
        "type": _entry_type(record["memory_dir"], record["link"]),
        "label": record["title"] or record["link"],
    }
    if record["archived"]:
        node["badge"] = "archived"
    return node


# ---------------------------------------------------------------------------
# Adapter payloads (graph / node / schema / health)
# ---------------------------------------------------------------------------


def graph_payload(scope: str = "", sources: list[MemorySource] | None = None) -> dict[str, Any]:
    """The `{data: {nodes, edges}}` graph-contract payload.

    ``scope`` optionally filters to one memory corpus (``khimaira``/``jeevy``);
    scoped edges keep anything touching a scoped node. Dangling edges (an
    endpoint no longer present in either file) are reported as-is — the viewer
    tolerates them; ``health`` counts them.
    """
    records = _collect_node_entries(sources)
    canonical = canonical_project(scope) if scope else None
    if scope and canonical is not None:
        records = [r for r in records if r["project"] == canonical]
    nodes = [_node_for(r) for r in records]
    node_ids = {n["id"] for n in nodes}

    edges = [
        {"id": e["id"], "from": e["from_id"], "to": e["to_id"], "type": e["type"]}
        for e in list_edges()
        # Unscoped: everything (dangling included). Scoped: touching a scoped node.
        if canonical is None or e["from_id"] in node_ids or e["to_id"] in node_ids
    ]
    return {"data": {"nodes": nodes, "edges": edges}}


def node_payload(node_id: str, sources: list[MemorySource] | None = None) -> dict[str, Any]:
    """Single-node detail: full entry fields + every edge touching it.

    ``found: false`` for an unknown id is a graceful-empty case, not an error —
    edges touching the id are still listed so a dangling edge can be inspected
    from either end.
    """
    record = next(
        (
            r
            for r in _collect_node_entries(sources)
            if _point_id(r["project"], r["link"]) == node_id
        ),
        None,
    )
    edges = [e for e in list_edges() if node_id in (e["from_id"], e["to_id"])]
    if record is None:
        return {"data": {"found": False, "id": node_id, "edges": edges}}
    return {
        "data": {
            "found": True,
            **_node_for(record),
            "project": record["project"],
            "link": record["link"],
            "body": record["body"],
            "source_file": record["source_file"],
            "archived": record["archived"],
            "edges": edges,
        }
    }


def schema_payload(sources: list[MemorySource] | None = None) -> dict[str, Any]:
    """Type meta-graph: one node per entry type (count as badge), one edge per
    observed (from-type, to-type, edge-type) triple. Endpoints of dangling
    edges aggregate under a ``missing`` type so they stay visible here too."""
    records = _collect_node_entries(sources)
    type_by_id = {
        _point_id(r["project"], r["link"]): _entry_type(r["memory_dir"], r["link"]) for r in records
    }

    type_counts: dict[str, int] = {}
    for t in type_by_id.values():
        type_counts[t] = type_counts.get(t, 0) + 1

    triples: dict[tuple[str, str, str], None] = {}
    for e in list_edges():
        from_t = type_by_id.get(e["from_id"], "missing")
        to_t = type_by_id.get(e["to_id"], "missing")
        triples[(from_t, to_t, e["type"])] = None

    seen_types = set(type_counts) | {t for (a, b, _t) in triples for t in (a, b)}
    return {
        "data": {
            "nodes": [
                {
                    "id": f"type:{t}",
                    "type": "memory_type",
                    "label": t,
                    "badge": type_counts.get(t, 0),
                }
                for t in sorted(seen_types)
            ],
            "edges": [
                {"from": f"type:{a}", "to": f"type:{b}", "type": t} for (a, b, t) in sorted(triples)
            ],
        }
    }


def health_payload(sources: list[MemorySource] | None = None) -> dict[str, Any]:
    """Aggregate counts, including the dangling-edge count the design requires."""
    records = _collect_node_entries(sources)
    node_ids = {_point_id(r["project"], r["link"]) for r in records}
    nodes_by_type: dict[str, int] = {}
    for r in records:
        t = _entry_type(r["memory_dir"], r["link"])
        nodes_by_type[t] = nodes_by_type.get(t, 0) + 1

    edges = list_edges()
    edges_by_type: dict[str, int] = {}
    dangling = 0
    for e in edges:
        edges_by_type[e["type"]] = edges_by_type.get(e["type"], 0) + 1
        if e["from_id"] not in node_ids or e["to_id"] not in node_ids:
            dangling += 1

    return {
        "data": {
            "nodes": len(records),
            "edges": len(edges),
            "dangling_edges": dangling,
            "archived_nodes": sum(1 for r in records if r["archived"]),
            "nodes_by_type": nodes_by_type,
            "edges_by_type": edges_by_type,
        }
    }


# ---------------------------------------------------------------------------
# Explicit edge writes (the memory_link MCP tool's core)
# ---------------------------------------------------------------------------


def link_entries(
    project: str,
    from_link: str,
    to_link: str,
    edge_type: str,
    note: str = "",
    sources: list[MemorySource] | None = None,
) -> dict[str, Any]:
    """Resolve two memory-entry links to their uuid5 ids and store one edge.

    Fail-loud validation at the boundary: unknown project, unknown edge type,
    self-loop, and links that don't resolve to an existing entry (live OR
    archive) are all rejected with a specific ValueError — an edge should only
    ever be born non-dangling.
    """
    canonical = canonical_project(project)
    if canonical is None:
        raise ValueError(f"unknown project {project!r}; expected 'khimaira' or 'jeevy'")

    known_links = {r["link"] for r in _collect_node_entries(sources) if r["project"] == canonical}
    missing = [link for link in (from_link, to_link) if link not in known_links]
    if missing:
        raise ValueError(
            f"no {canonical} memory entry with link(s) {missing!r} in MEMORY.md or "
            f"MEMORY_ARCHIVE.md — check the link with memory_search first"
        )

    result = add_edge(
        _point_id(canonical, from_link),
        _point_id(canonical, to_link),
        edge_type,
        note=note,
    )
    return {**result, "project": canonical, "from_link": from_link, "to_link": to_link}


# ---------------------------------------------------------------------------
# Registration (virtual adapter entry → existing /api/graph proxy)
# ---------------------------------------------------------------------------


def register_adapter(port: int) -> None:
    """Idempotently register this daemon's own memory-kg routes as a KG adapter.

    Runs at daemon startup (serve()). Localhost, same process — deliberately no
    token_env: graph.py sends no auth header when token_env is absent (verified:
    `_auth_headers` only builds a header when the adapter declares one).
    """
    from khimaira.attach.registry import set_virtual_kg_adapter

    url = f"http://127.0.0.1:{port}/internal/memory-kg/graph"
    try:
        set_virtual_kg_adapter(ADAPTER_LABEL, url=url)
        log.info("memory-kg: registered virtual KG adapter %r → %s", ADAPTER_LABEL, url)
    except Exception as exc:
        # Registration failure must not stop the daemon from serving.
        log.warning("memory-kg: adapter registration failed: %s", exc)
