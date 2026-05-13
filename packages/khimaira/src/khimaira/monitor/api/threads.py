"""`/api/threads` — paginated thread list + single-thread state inspection.

Backend dispatch:
  - Postgres (`AsyncPostgresSaver`): JSONB columns, fast jsonb-extract
    queries pull just the keys we need without deserializing whole blobs.
  - SQLite (`AsyncSqliteSaver`): BLOB columns (msgpack-encoded), so we
    pull every row and deserialize in Python. Fine at khimaira-scale;
    revisit if it ever becomes a bottleneck.

A project may have multiple SQLite databases (khimaira has one per graph).
List queries union all of them; detail queries probe each in order until
the requested thread_id is found.

Polling-friendly: callers pass `since` (a checkpoint_id watermark) and
the endpoint uses an indexed cursor so polls stay cheap.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import re

from starlette.requests import Request

from .._optional import require
from ..discovery import ast_walker
from ..discovery.connections import (
    Connections,
    PostgresConnection,
    SqliteConnection,
    discover_sqlite,
)
from ..discovery.project import Project
from ..discovery.redaction import redact
from ..discovery.state_decoder import decode, to_jsonable
from ..discovery.thread_grouping import parse_grouping
from ..metadata import cache as meta_cache
from ..metadata import observations as obs_cache
from ..metadata.schema import (
    ProjectMetadata,
    RunClustering,
    RuntimeObservations,
    ThreadGrouping,
)

# ---------------------------------------------------------------------------
# Postgres SQL
# ---------------------------------------------------------------------------
# Two-phase query so LIMIT applies to MOST-RECENT threads, not the
# alphabetically-first ones. Without the outer ORDER BY, busy projects
# (jeevy with 100+ threads) had their newly-spawned runs invisible
# from the dashboard because alphabetically-earlier idle threads
# filled the page and pushed live ones off the bottom.
_PG_LIST_SQL = """
SELECT *
FROM (
    SELECT DISTINCT ON (thread_id)
           thread_id,
           checkpoint_id                                          AS latest_checkpoint_id,
           checkpoint->>'ts'                                      AS last_updated,
           (checkpoint->'channel_values' ? '__interrupt__')       AS is_paused,
           checkpoint->'channel_values'->>'agent_profile'         AS agent_profile,
           checkpoint->'channel_values'->>'phase'                 AS phase,
           (metadata->>'step')::int                               AS step,
           metadata->>'source'                                    AS source,
           metadata->'writes'                                     AS writes,
           checkpoint->'versions_seen'                            AS versions_seen
    FROM checkpoints
    ORDER BY thread_id, checkpoint_id DESC
) AS latest_per_thread
ORDER BY latest_checkpoint_id DESC
LIMIT %s OFFSET %s
"""

_PG_DETAIL_SQL = """
SELECT checkpoint_id,
       parent_checkpoint_id,
       type,
       checkpoint,
       metadata,
       checkpoint->>'ts'                                      AS ts,
       checkpoint->'versions_seen'                            AS versions_seen,
       (metadata->>'step')::int                               AS step,
       metadata->'writes'                                     AS writes
FROM checkpoints
WHERE thread_id = %s
ORDER BY checkpoint_id DESC
LIMIT %s
"""

# ---------------------------------------------------------------------------
# SQLite SQL
# ---------------------------------------------------------------------------
# Same column names but checkpoint + metadata are BLOB. We extract the
# fields in Python after decoding.
_SQLITE_LIST_SQL = """
SELECT thread_id, checkpoint_id, type, checkpoint, metadata
FROM checkpoints
WHERE checkpoint_id = (
  SELECT MAX(checkpoint_id) FROM checkpoints c2
  WHERE c2.thread_id = checkpoints.thread_id
)
ORDER BY checkpoint_id DESC
LIMIT ? OFFSET ?
"""

_SQLITE_DETAIL_SQL = """
SELECT checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata
FROM checkpoints
WHERE thread_id = ?
ORDER BY checkpoint_id DESC
LIMIT ?
"""


def build_router(
    connections_by_project: dict[Path, Connections],
    projects: list[Project] | None = None,
):
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    # Cache Postgres URLs (static — they come from .env and don't change
    # without a daemon restart). SQLite files come and go (graphs create
    # their .db on first run), so SQLite discovery happens per-request.
    name_to_path: dict[str, Path] = {}
    name_to_postgres: dict[str, list[PostgresConnection]] = {}
    for path, conns in connections_by_project.items():
        name_to_path[path.name] = path
        name_to_postgres[path.name] = conns.postgres
    # Also fold in projects that exist but had no connections at startup.
    if projects:
        for p in projects:
            name_to_path.setdefault(p.name, p.path)
            name_to_postgres.setdefault(p.name, [])

    def _live_connections(name: str) -> Connections | None:
        path = name_to_path.get(name)
        if path is None:
            return None
        # Re-glob SQLite on every request — khimaira-style projects create
        # per-graph .db files lazily. This is a few file syscalls; cheap.
        return Connections(
            postgres=name_to_postgres.get(name, []),
            sqlite=discover_sqlite(path),
        )

    def _metadata_for(name: str) -> ProjectMetadata | None:
        """Load the project's metadata cache. Returns None when no scan
        has landed yet — callers fall back to heuristic defaults."""
        path = name_to_path.get(name)
        if path is None:
            return None
        return meta_cache.load(path)

    def _grouping_for(name: str) -> ThreadGrouping | None:
        meta = _metadata_for(name)
        return meta.thread_grouping if meta else None

    def _run_clustering_for(name: str) -> RunClustering | None:
        meta = _metadata_for(name)
        return meta.run_clustering if meta else None

    def _running_threshold_for(name: str) -> float:
        meta = _metadata_for(name)
        if meta and meta.running_threshold_seconds is not None:
            # Clamp to the documented [60, 1800] range so a stray
            # scan response can't flatten the heuristic into "always
            # running" or "never running".
            return float(max(60, min(1800, meta.running_threshold_seconds)))
        return _RUNNING_THRESHOLD_SECONDS

    # Per-node thresholds derived from the observation collector's
    # p95 stats. When a thread's `current_node` has accumulated stats,
    # use `p95 * margin` as the threshold instead of the project-wide
    # default. This adapts the dashboard to each app's actual node
    # latencies — drawing_extract gets a generous threshold, persist
    # gets a tight one.
    _OBSERVATION_MARGIN = 2.0  # how far past p95 before we flip to idle
    _MIN_VISITS_FOR_ADAPTIVE = 5  # require some signal before trusting stats

    def _per_node_thresholds_for(name: str) -> dict[str, float]:
        path = name_to_path.get(name)
        if path is None:
            return {}
        obs: RuntimeObservations | None = obs_cache.load(path)
        if obs is None:
            return {}
        out: dict[str, float] = {}
        for graph_obs in obs.graphs.values():
            for node_name, stats in graph_obs.nodes.items():
                if stats.visits < _MIN_VISITS_FOR_ADAPTIVE:
                    continue
                # Use max(p95*margin, max+small_buffer) so a thread that
                # hits the observed worst-case isn't immediately idle'd.
                # Floor at 30s — even fast nodes deserve a tolerance window.
                threshold = max(
                    stats.duration_p95 * _OBSERVATION_MARGIN,
                    stats.duration_max * 1.2,
                    30.0,
                )
                # Cap at 1 hour — anything legitimately slower is a
                # design problem, not a monitoring problem.
                out[node_name] = min(threshold, 3600.0)
        return out

    # Topology-derived terminal-node names per project. A node is
    # "terminal" when its only outgoing edges go to `__end__` (or it
    # has no outgoing edges at all in any graph). Used by _derive_status
    # to flip a thread to idle the moment it reaches the graph's end —
    # without this, threads sit at "running" until the heuristic 5min
    # window expires, which lies for runs that have actually finished.
    #
    # Cached lazily per project; AST extraction is fast (<200ms) but
    # not free.
    _terminal_cache: dict[str, frozenset[str]] = {}

    def _terminal_nodes_for(name: str) -> frozenset[str]:
        cached = _terminal_cache.get(name)
        if cached is not None:
            return cached
        path = name_to_path.get(name)
        if path is None:
            _terminal_cache[name] = frozenset()
            return _terminal_cache[name]
        try:
            results = ast_walker.extract_from_path(path)
        except Exception:
            _terminal_cache[name] = frozenset()
            return _terminal_cache[name]
        terminal: set[str] = set()
        for r in results:
            if not r.nodes or not r.edges:
                continue
            # Build outgoing-edges map for this graph.
            out: dict[str, set[str]] = {}
            for src, dst in r.edges:
                out.setdefault(src, set()).add(dst)
            for node in r.nodes:
                if node in {"__start__", "__input__", "__end__", "__interrupt__"}:
                    continue
                targets = out.get(node)
                # No outgoing edges = terminal. Or every outgoing edge
                # goes to __end__ = terminal.
                if not targets or targets <= {"__end__"}:
                    terminal.add(node)
        result = frozenset(terminal)
        _terminal_cache[name] = result
        return result

    @router.get("/threads/{name}")
    async def list_threads(
        name: str, limit: int = 50, offset: int = 0, since: str | None = None
    ):
        conns = _live_connections(name)
        if conns is None or (not conns.postgres and not conns.sqlite):
            raise fastapi.HTTPException(
                status_code=404,
                detail=f"no checkpointer connection discovered for project: {name}",
            )

        rows = await _list_threads(conns, since, limit, offset)
        grouping = _grouping_for(name)
        run_clustering = _run_clustering_for(name)
        terminals = _terminal_nodes_for(name)
        running_threshold = _running_threshold_for(name)
        per_node = _per_node_thresholds_for(name)
        return {
            "project": name,
            "limit": limit,
            "offset": offset,
            "since": since,
            "scope_label": (grouping.scope_label if grouping else "Run"),
            # When absent, the frontend applies its built-in heuristic
            # (trailing-UUID + 5min proximity). Always serialized so the
            # frontend can tell "no rule yet" from "explicit no-cluster".
            "run_clustering": (run_clustering.model_dump() if run_clustering else None),
            # Per-project running threshold so the frontend's stale/
            # stuck badge thresholds can scale with it. Apps with slow
            # nodes (jeevy: 900s, khimaira-pipeline: 600s+) shouldn't
            # show "stale" at a fixed 5min when the backend legitimately
            # classifies them running for longer.
            "running_threshold_seconds": running_threshold,
            "threads": [
                _serialize_thread(r, grouping, terminals, running_threshold, per_node)
                for r in rows
            ],
        }

    @router.get("/threads/{name}/{thread_id}")
    async def thread_detail(name: str, thread_id: str, limit: int = 20):
        conns = _live_connections(name)
        if conns is None or (not conns.postgres and not conns.sqlite):
            raise fastapi.HTTPException(
                status_code=404,
                detail=f"no checkpointer connection discovered for project: {name}",
            )

        rows = await _thread_detail(conns, thread_id, limit)
        if not rows:
            raise fastapi.HTTPException(
                status_code=404, detail=f"thread not found: {thread_id}"
            )

        return {
            "project": name,
            "thread_id": thread_id,
            "checkpoints": [_serialize_checkpoint(r) for r in rows],
        }

    @router.get("/threads/{name}/{thread_id}/wait")
    async def wait_thread(
        name: str,
        thread_id: str,
        until_status: str | None = None,
        until_node: str | None = None,
        timeout_s: float = 300.0,
        poll_interval_s: float = 0.5,
    ):
        """**Long-poll** — block server-side until a thread reaches a target
        state, then return ONE response.

        Replaces the agent-side `sleep(N) → list_threads → check status`
        polling loop with a single MCP-friendly call. The daemon does the
        polling (every poll_interval_s, default 500ms) and returns when:

          - status transitions to `until_status` (default behavior: any
            non-running, non-starting status — i.e. terminal),
          - `until_node` matches `current_node` (e.g. wait until the run
            reaches a specific node in the graph),
          - `timeout_s` exceeded,
          - the thread is not found (returns reason=not_found).

        Args:
          until_status: target status. None = wait until not-in-flight
            (terminal: idle / paused). Common values: "idle", "paused",
            "running".
          until_node: optional. Returns when current_node matches this.
          timeout_s: max wall time before returning reason=timeout.
          poll_interval_s: daemon-side poll cadence. Default 500ms.
        """
        conns = _live_connections(name)
        if conns is None or (not conns.postgres and not conns.sqlite):
            raise fastapi.HTTPException(
                status_code=404,
                detail=f"no checkpointer connection discovered for project: {name}",
            )

        grouping = _grouping_for(name)
        terminals = _terminal_nodes_for(name)
        running_threshold = _running_threshold_for(name)
        per_node = _per_node_thresholds_for(name)

        # Default: wait until run leaves the in-flight set. Caller can
        # override with until_status="paused" to wait for HITL gates etc.
        in_flight = frozenset({"running", "starting"})
        # Clamp poll cadence to a sane range to prevent abuse / hot loops.
        poll_interval_s = max(0.1, min(5.0, poll_interval_s))

        deadline = asyncio.get_event_loop().time() + timeout_s
        start = asyncio.get_event_loop().time()
        last_summary: dict[str, Any] | None = None

        while True:
            # Reuse the list-path code to derive status; limit large enough
            # to find the target in typical projects. If you have >500
            # threads per project, raise this.
            rows = await _list_threads(conns, None, 500, 0)
            target_row = next(
                (r for r in rows if r.get("thread_id") == thread_id), None
            )
            if target_row is None:
                return {
                    "thread_id": thread_id,
                    "project": name,
                    "reason": "not_found",
                    "elapsed_s": asyncio.get_event_loop().time() - start,
                    "summary": None,
                }

            summary = _serialize_thread(
                target_row, grouping, terminals, running_threshold, per_node
            )
            last_summary = summary
            status = summary["status"]
            current_node = summary["current_node"]

            if until_status is not None and status == until_status:
                return {
                    "thread_id": thread_id,
                    "project": name,
                    "reason": "until_status_match",
                    "elapsed_s": asyncio.get_event_loop().time() - start,
                    "summary": summary,
                }
            if until_node is not None and current_node == until_node:
                return {
                    "thread_id": thread_id,
                    "project": name,
                    "reason": "until_node_match",
                    "elapsed_s": asyncio.get_event_loop().time() - start,
                    "summary": summary,
                }
            if until_status is None and status not in in_flight:
                # Default terminal-detection branch.
                return {
                    "thread_id": thread_id,
                    "project": name,
                    "reason": "terminal",
                    "elapsed_s": asyncio.get_event_loop().time() - start,
                    "summary": summary,
                }

            if asyncio.get_event_loop().time() >= deadline:
                return {
                    "thread_id": thread_id,
                    "project": name,
                    "reason": "timeout",
                    "elapsed_s": asyncio.get_event_loop().time() - start,
                    "summary": last_summary,
                }

            await asyncio.sleep(poll_interval_s)

    @router.get("/threads/{name}/{thread_id}/stream")
    async def thread_stream(name: str, thread_id: str, request: Request):
        """SSE stream of checkpoints as they appear.

        Each SSE connection runs its own 250ms poll loop. A multi-subscriber
        registry would share polls across tabs — current loopback-single-user
        usage doesn't justify the complexity yet.

        Events:
          - `initial`  : one event with the latest checkpoint at subscribe time
          - `checkpoint`: new checkpoint(s) since last tick, oldest→newest
          - `keepalive`: every 15s, so proxies don't reap idle connections
          - `idle_timeout`: after 30min of no new checkpoints; client should
                           reconnect if still interested
        """
        from sse_starlette.sse import EventSourceResponse

        async def _gen():
            async for event in _stream_checkpoints(
                name, thread_id, _live_connections, request
            ):
                yield event

        return EventSourceResponse(_gen())

    return router


# ---------------------------------------------------------------------------
# SSE stream
# ---------------------------------------------------------------------------
_STREAM_POLL_INTERVAL_S = 0.25
_STREAM_KEEPALIVE_INTERVAL_S = 15.0
_STREAM_IDLE_TIMEOUT_S = 1800.0  # 30min — client reconnects if needed


async def _stream_checkpoints(
    name: str,
    thread_id: str,
    conns_resolver,
    request,
):
    """Async generator yielding SSE event dicts for one thread.

    Resolves connections every tick (cheap; SQLite re-globs catch new
    .db files for khimaira's per-graph databases). Emits an `initial`
    event so clients can render their starting state without a separate
    REST call, then `checkpoint` events for every new row.
    """
    import json
    import time

    last_id: str | None = None
    last_emit = time.monotonic()
    last_keepalive = time.monotonic()

    while True:
        if await request.is_disconnected():
            return

        conns = conns_resolver(name)
        if conns is None or (not conns.postgres and not conns.sqlite):
            await asyncio.sleep(_STREAM_POLL_INTERVAL_S)
            continue

        try:
            rows = await _thread_detail(conns, thread_id, 10)
        except Exception:
            rows = []

        if rows:
            latest_id = rows[0]["checkpoint_id"]
            if last_id is None:
                # First tick — emit initial state, then watch for changes.
                payload = _serialize_checkpoint(rows[0])
                yield {
                    "event": "initial",
                    "data": json.dumps(payload, default=str),
                }
                last_id = latest_id
                last_emit = time.monotonic()
            elif latest_id != last_id:
                # Walk back through `rows` until we hit our last-seen id;
                # everything before that is new. Reverse to emit
                # oldest→newest so the client renders the trajectory in
                # natural order.
                new_rows = []
                for r in rows:
                    if r["checkpoint_id"] == last_id:
                        break
                    new_rows.append(r)
                new_rows.reverse()
                for r in new_rows:
                    payload = _serialize_checkpoint(r)
                    yield {
                        "event": "checkpoint",
                        "data": json.dumps(payload, default=str),
                    }
                last_id = latest_id
                last_emit = time.monotonic()

        now = time.monotonic()
        if now - last_keepalive >= _STREAM_KEEPALIVE_INTERVAL_S:
            yield {"event": "keepalive", "data": "{}"}
            last_keepalive = now

        if now - last_emit >= _STREAM_IDLE_TIMEOUT_S:
            yield {"event": "idle_timeout", "data": "{}"}
            return

        await asyncio.sleep(_STREAM_POLL_INTERVAL_S)


# ---------------------------------------------------------------------------
# Backend dispatch
# ---------------------------------------------------------------------------
async def _list_threads(
    conns: Connections, since: str | None, limit: int, offset: int
) -> list[dict[str, Any]]:
    if conns.postgres:
        return await _pg_list(conns.postgres[0], since, limit, offset)
    # SQLite — union across every discovered DB.
    union: list[dict[str, Any]] = []
    for sqlite_conn in conns.sqlite:
        rows = await asyncio.to_thread(_sqlite_list_sync, sqlite_conn, limit + offset)
        union.extend(rows)
    union.sort(key=lambda r: r.get("last_updated") or "", reverse=True)
    return union[offset : offset + limit]


async def _thread_detail(
    conns: Connections, thread_id: str, limit: int
) -> list[dict[str, Any]]:
    if conns.postgres:
        return await _pg_detail(conns.postgres[0], thread_id, limit)
    for sqlite_conn in conns.sqlite:
        rows = await asyncio.to_thread(
            _sqlite_detail_sync, sqlite_conn, thread_id, limit
        )
        if rows:
            return rows
    return []


# ---------------------------------------------------------------------------
# Postgres path
# ---------------------------------------------------------------------------
async def _pg_list(
    conn: PostgresConnection, since: str | None, limit: int, offset: int
) -> list[dict[str, Any]]:
    psycopg = require("psycopg")
    rows: list[dict[str, Any]] = []
    sql = _PG_LIST_SQL
    params: tuple = (limit, offset)
    if since:
        sql = sql.replace(
            "LIMIT %s OFFSET %s", "WHERE checkpoint_id > %s LIMIT %s OFFSET %s"
        )
        # Note: simple form — re-emit with `since` in the WHERE clause when needed
    async with await psycopg.AsyncConnection.connect(conn.url) as pg:
        async with pg.cursor() as cur:
            await cur.execute(sql, params)
            cols = [d[0] for d in cur.description] if cur.description else []
            async for row in cur:
                rows.append(dict(zip(cols, row)))
    return rows


async def _pg_detail(
    conn: PostgresConnection, thread_id: str, limit: int
) -> list[dict[str, Any]]:
    psycopg = require("psycopg")
    rows: list[dict[str, Any]] = []
    async with await psycopg.AsyncConnection.connect(conn.url) as pg:
        async with pg.cursor() as cur:
            await cur.execute(_PG_DETAIL_SQL, (thread_id, limit))
            cols = [d[0] for d in cur.description] if cur.description else []
            async for row in cur:
                rows.append(dict(zip(cols, row)))
    return rows


# ---------------------------------------------------------------------------
# SQLite path
# ---------------------------------------------------------------------------
def _sqlite_list_sync(conn: SqliteConnection, fetch_limit: int) -> list[dict[str, Any]]:
    """Read latest checkpoint per thread from one SQLite DB. Decodes blobs
    in Python and projects to the same row shape the Postgres path emits."""
    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(f"file:{conn.path}?mode=ro", uri=True, timeout=2.0) as db:
            db.row_factory = sqlite3.Row
            cur = db.execute(_SQLITE_LIST_SQL, (fetch_limit, 0))
            for row in cur.fetchall():
                decoded = _decode_checkpoint(row["type"], row["checkpoint"])
                meta = _decode_metadata(row["type"], row["metadata"])
                channel_values = (
                    decoded.get("channel_values") if isinstance(decoded, dict) else None
                )
                out.append(
                    {
                        "thread_id": row["thread_id"],
                        "latest_checkpoint_id": row["checkpoint_id"],
                        "last_updated": (
                            decoded.get("ts") if isinstance(decoded, dict) else None
                        ),
                        "is_paused": isinstance(channel_values, dict)
                        and "__interrupt__" in channel_values,
                        "agent_profile": (
                            channel_values.get("agent_profile")
                            if isinstance(channel_values, dict)
                            else None
                        ),
                        "phase": (
                            channel_values.get("phase")
                            if isinstance(channel_values, dict)
                            else None
                        ),
                        "step": (meta.get("step") if isinstance(meta, dict) else None),
                        "source": (
                            meta.get("source") if isinstance(meta, dict) else None
                        ),
                        "writes": (
                            meta.get("writes") if isinstance(meta, dict) else None
                        ),
                        "versions_seen": (
                            decoded.get("versions_seen")
                            if isinstance(decoded, dict)
                            else None
                        ),
                        "_db_path": conn.path,  # kept for debugging; not serialized to client
                    }
                )
    except sqlite3.Error:
        return []
    return out


def _sqlite_detail_sync(
    conn: SqliteConnection, thread_id: str, limit: int
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    try:
        with sqlite3.connect(f"file:{conn.path}?mode=ro", uri=True, timeout=2.0) as db:
            db.row_factory = sqlite3.Row
            cur = db.execute(_SQLITE_DETAIL_SQL, (thread_id, limit))
            for row in cur.fetchall():
                decoded = _decode_checkpoint(row["type"], row["checkpoint"])
                meta = _decode_metadata(row["type"], row["metadata"])
                out.append(
                    {
                        "checkpoint_id": row["checkpoint_id"],
                        "parent_checkpoint_id": row["parent_checkpoint_id"],
                        "type": row["type"],
                        "checkpoint": decoded,
                        "metadata": meta,
                        "ts": decoded.get("ts") if isinstance(decoded, dict) else None,
                        "versions_seen": (
                            decoded.get("versions_seen")
                            if isinstance(decoded, dict)
                            else None
                        ),
                        "step": meta.get("step") if isinstance(meta, dict) else None,
                        "writes": (
                            meta.get("writes") if isinstance(meta, dict) else None
                        ),
                    }
                )
    except sqlite3.Error:
        return []
    return out


def _decode_checkpoint(type_str: str | None, blob: bytes | None) -> Any:
    """Decode a SQLite checkpoint blob and pass through dicts (after Python
    msgpack libs return raw types). The state_decoder handles the wire-format
    cases; this wrapper exists so we can normalize None / non-dict results."""
    if blob is None:
        return None
    return decode(type_str, blob)


def _decode_metadata(type_str: str | None, blob: bytes | None) -> Any:
    if blob is None:
        return None
    return decode(type_str, blob)


# ---------------------------------------------------------------------------
# Shared serialization (works for both backends — row shapes are normalized)
# ---------------------------------------------------------------------------
_SPECIAL_NODES = frozenset({"__input__", "__start__", "__interrupt__", "__end__"})

# Default activity window for "is this thread still in flight?" — used
# when no per-project metadata override exists. See _derive_status's
# step 4 docstring for the trade-offs. Per-project value comes from
# `ProjectMetadata.running_threshold_seconds`.
_RUNNING_THRESHOLD_SECONDS = 300.0


def _serialize_thread(
    row: dict[str, Any],
    grouping: ThreadGrouping | None = None,
    terminal_nodes: frozenset[str] = frozenset(),
    running_threshold_seconds: float = _RUNNING_THRESHOLD_SECONDS,
    per_node_thresholds: dict[str, float] | None = None,
) -> dict[str, Any]:
    current_node, recent_nodes = _derive_nodes(row)
    grouping_fields = _resolve_grouping(row["thread_id"], grouping)
    # Effective threshold for THIS thread: use per-node stats when this
    # node has enough observed visits, else fall back to project-wide.
    effective_threshold = running_threshold_seconds
    if per_node_thresholds and current_node:
        node_threshold = per_node_thresholds.get(current_node)
        if node_threshold is not None:
            effective_threshold = node_threshold
    return {
        "thread_id": row["thread_id"],
        "latest_checkpoint_id": row["latest_checkpoint_id"],
        "last_updated": row.get("last_updated"),
        "step": row.get("step"),
        "status": _derive_status(
            row,
            current_node=current_node,
            terminal_nodes=terminal_nodes,
            running_threshold_seconds=effective_threshold,
        ),
        "current_node": current_node,
        "recent_nodes": recent_nodes,
        "agent_profile": row.get("agent_profile"),
        "phase": row.get("phase"),
        # Generic grouping fields the UI consumes blindly. App-agnostic.
        "scope_kind": grouping_fields["scope_kind"],
        "scope_id": grouping_fields["scope_id"],
        "stage": grouping_fields["stage"],
        "stage_detail": grouping_fields["stage_detail"],
    }


def _resolve_grouping(
    thread_id: str, grouping: ThreadGrouping | None
) -> dict[str, str]:
    """Apply the metadata-provided regex patterns first; fall back to the
    generic heuristic if no pattern matches (or no metadata exists yet)."""
    if grouping and grouping.patterns:
        for rule in grouping.patterns:
            try:
                m = re.match(rule.pattern, thread_id)
            except re.error:
                continue
            if not m:
                continue
            captured = m.groupdict()
            return {
                "scope_kind": rule.scope_kind or captured.get("scope_kind") or "thread",
                "scope_id": captured.get("scope_id") or thread_id,
                "stage": rule.stage
                or captured.get("stage")
                or rule.scope_kind
                or "thread",
                "stage_detail": captured.get("stage_detail") or "",
            }
    # Fallback heuristic
    return dict(parse_grouping(thread_id))


def _serialize_checkpoint(row: dict[str, Any]) -> dict[str, Any]:
    # to_jsonable runs AFTER redact so the redacted payload (still a
    # mix of dicts, Pydantic models, dataclasses, Send objects, …)
    # gets normalized into something FastAPI's JSON encoder can
    # traverse without crashing on objects whose `__iter__` raises.
    return {
        "checkpoint_id": row["checkpoint_id"],
        "parent_checkpoint_id": row.get("parent_checkpoint_id"),
        "created_at": row.get("ts"),
        "step": row.get("step"),
        "node": _derive_nodes(row)[0],
        "state": to_jsonable(redact(_unwrap_state(row.get("checkpoint")))),
        "metadata": (
            to_jsonable(redact(row["metadata"])) if row.get("metadata") else None
        ),
    }


def _derive_nodes(row: dict[str, Any]) -> tuple[str | None, list[str]]:
    writes = row.get("writes")
    if isinstance(writes, dict) and writes:
        nodes = sorted(k for k in writes.keys() if k not in _SPECIAL_NODES)
        if nodes:
            return nodes[0], nodes

    versions_seen = row.get("versions_seen")
    if not isinstance(versions_seen, dict):
        return None, []

    candidates: list[tuple[str, str]] = []
    for node, channels in versions_seen.items():
        if node in _SPECIAL_NODES:
            continue
        if not isinstance(channels, dict):
            continue
        max_v = ""
        for v in channels.values():
            v_str = str(v)
            if v_str > max_v:
                max_v = v_str
        candidates.append((max_v, node))

    candidates.sort(reverse=True)
    recent = [name for _, name in candidates]
    current = recent[0] if recent else None
    return current, recent


def _derive_status(
    row: dict[str, Any],
    current_node: str | None = None,
    terminal_nodes: frozenset[str] = frozenset(),
    running_threshold_seconds: float = _RUNNING_THRESHOLD_SECONDS,
) -> str:
    """Classify a thread's status using every signal available from the
    checkpoint schema. Decision tree, in priority order:

      1. `__interrupt__` channel set     → paused (HITL — time-independent;
                                             a paused run can sit at the gate
                                             for hours/days, that's normal)
      2. source=input, step ≤ 0          → starting (graph just kicked off)
      3a. writes contains `__end__`      → idle (some LangGraph versions
                                             populate metadata.writes)
      3b. current_node is a terminal     → idle (topology-derived: nodes
          (only edge → __end__)              whose only outgoing edges go to
                                             __end__. This is the load-
                                             bearing terminal signal because
                                             metadata.writes is `null` in
                                             jeevy's LangGraph — the
                                             writes-based detection in 3a
                                             never fires there.)
      4. activity within 5 min           → running (heuristic for in-flight,
                                             tolerates slow LLM nodes between
                                             checkpoint writes)
      5. else                            → idle (no recent activity, no
                                             terminal marker — likely
                                             abandoned/errored; frontend's
                                             staleness classifier gives the
                                             user a "stuck" badge if the
                                             situation warrants attention)

    Note: We can't detect "Python is currently executing this node" from
    the checkpoint table alone — between checkpoint commits the database
    looks identical to "node finished a moment ago." The 5min window is
    the practical floor; pair it with terminal detection so completed
    runs flip to idle the instant the last node fires rather than waiting
    out the window.
    """
    # 1. HITL pause — time-independent, can persist indefinitely.
    if row.get("is_paused"):
        return "paused"

    # 2. Just-started graph.
    source = row.get("source")
    step = row.get("step")
    if source == "input" and (step is None or step <= 0):
        return "starting"

    # 3a. Terminal via writes metadata. Some LangGraph versions populate
    # this; jeevy's doesn't, so 3b below catches it.
    writes = row.get("writes")
    if isinstance(writes, dict) and "__end__" in writes:
        return "idle"

    # 3b. Terminal via topology — current_node has only outgoing edges
    # to __end__ (or no outgoing edges at all).
    #
    # Tension: one-shot graphs at terminal = done forever. Orchestrator-
    # style apps (jeevy's chat_lane / ingest_lane / digest_lane /
    # output_lane) hit terminal at the END of every cycle but
    # re-invoke within seconds. We can't tell the two apart from the
    # checkpoint alone — both look identical at the moment they hit
    # terminal.
    #
    # Heuristic: tolerate a SHORT between-cycles window. In jeevy,
    # cycle-to-cycle gap is typically <3s; 30s gives generous slack
    # without dragging the "running" classification long after a run
    # actually completes (90s was too long — it kept finished runs
    # flagged as running for the full 1.5min after their final cycle).
    #
    # Trade-off accepted: if a real orchestrator pause exceeds 30s,
    # we'll briefly mis-classify as idle. The 5-min running threshold
    # below catches it on the next cycle when activity resumes.
    if current_node and current_node in terminal_nodes:
        last_updated = row.get("last_updated")
        if last_updated and _within_seconds(last_updated, 30.0):
            return "running"
        return "idle"

    # 4. Recent activity → in-flight (best-effort, tolerates slow nodes).
    # Threshold is per-project when the metadata scan provided one;
    # the default 300s applies otherwise. Apps with slower nodes
    # (khimaira's pipeline does 8min Claude calls) push it up; apps
    # with fast graphs bring it down for tighter idle detection.
    last_updated = row.get("last_updated")
    if last_updated and _within_seconds(last_updated, running_threshold_seconds):
        return "running"

    # 5. Default — no clear signal of activity, treat as idle. The
    # frontend's staleness classifier will flag this as "stuck" if a
    # paused/running thread crosses the 15min threshold.
    return "idle"


def _within_seconds(ts_iso: str, seconds: float) -> bool:
    from datetime import datetime, timezone

    try:
        ts = datetime.fromisoformat(ts_iso.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return False
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    delta = (datetime.now(timezone.utc) - ts).total_seconds()
    return 0 <= delta <= seconds


def _unwrap_state(decoded: object) -> object:
    if isinstance(decoded, dict) and "channel_values" in decoded:
        return decoded["channel_values"]
    return decoded
