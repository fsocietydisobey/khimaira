"""Prune, index, and search Claude Code native memory files.

This is deliberately independent of mnemosyne and the notebook collection. The
corpus is four tiny append-only Markdown indexes (live + archive for khimaira and
jeevy), embedded into a dedicated ``khimaira_memory`` Qdrant collection.

All trigger paths call :func:`refresh_memories`: prune first, then rebuild only
when the combined file fingerprint changed. Qdrant/embedder failures are reported
without breaking hooks, daemon loops, or MCP callers.

Automatic Stop/daemon refresh is disabled unless
``KHIMAIRA_MEMORY_AUTO_REFRESH=1``. The manual CLI is the explicit path for a
one-shot configured-source refresh.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from khimaira.claude_memory_index import _atomic_write_text, _parse_index, prune
from khimaira.log import get_logger

log = get_logger("claude_memory_retrieval")

_RAG_ENABLED = os.environ.get("KHIMAIRA_MEMORY_RAG", "1") != "0"
_QDRANT_URL = os.environ.get(
    "KHIMAIRA_MEMORY_QDRANT_URL",
    os.environ.get("KHIMAIRA_NOTEBOOK_QDRANT_URL", "http://localhost:6343"),
)
_COLLECTION = "khimaira_memory"
EMBED_MODEL = os.environ.get("KHIMAIRA_MEMORY_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DIM = int(os.environ.get("KHIMAIRA_MEMORY_EMBED_DIM", "384"))
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

DEFAULT_MAX_BYTES = int(os.environ.get("KHIMAIRA_MEMORY_MAX_BYTES", "12000"))
DEFAULT_TOP_K = 8
_FINGERPRINT_FILE = ".khimaira_memory_fingerprint"
_AUTO_REFRESH_ENV = "KHIMAIRA_MEMORY_AUTO_REFRESH"


@dataclass(frozen=True)
class MemorySource:
    project: str
    index_path: Path
    archive_path: Path
    pins: tuple[str, ...] = ()

    @property
    def fingerprint_path(self) -> Path:
        return self.archive_path.parent / _FINGERPRINT_FILE


def configured_sources(home: Path | None = None) -> list[MemorySource]:
    """Return the two known project memory locations.

    Environment overrides make the same production code safe to exercise against
    synthetic fixtures and portable across usernames/home directories.
    """
    home = Path.home() if home is None else Path(home)
    khimaira_index = Path(
        os.environ.get(
            "KHIMAIRA_MEMORY_KHIMAIRA_INDEX",
            home / ".claude/projects/-home--3ntropy-dev-khimaira/memory/MEMORY.md",
        )
    ).expanduser()
    jeevy_index = Path(
        os.environ.get(
            "KHIMAIRA_MEMORY_JEEVY_INDEX",
            home / ".claude-jeevy/projects/-home--3ntropy-work-jeevy-portal/memory/MEMORY.md",
        )
    ).expanduser()
    return [
        MemorySource(
            project="khimaira",
            index_path=khimaira_index,
            archive_path=khimaira_index.with_name("MEMORY_ARCHIVE.md"),
        ),
        MemorySource(
            project="jeevy",
            index_path=jeevy_index,
            archive_path=jeevy_index.with_name("MEMORY_ARCHIVE.md"),
            pins=("user_profile.md",),
        ),
    ]


def canonical_project(project: str) -> str | None:
    """Map session/project labels onto the two supported memory corpora."""
    normalized = project.strip().lower().replace("-", "_")
    if normalized in {"khimaira", "khimaira_dev"}:
        return "khimaira"
    if normalized in {"jeevy", "jeevy_portal"}:
        return "jeevy"
    return None


def auto_refresh_enabled() -> bool:
    """Whether lifecycle/daemon callers may mutate configured live memory.

    Hooks have no deployment boundary: saving their source activates them in
    every live Claude session. Keep automatic mutation opt-in so an unreviewed
    edit cannot silently become production behavior.
    """
    return os.environ.get(_AUTO_REFRESH_ENV) == "1"


@lru_cache(maxsize=1)
def _embedder():
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=EMBED_MODEL)


@lru_cache(maxsize=1)
def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=_QDRANT_URL)


def _embed(texts: list[str]) -> list[list[float]]:
    return [vector.tolist() for vector in _embedder().embed(texts)]


def _embed_query(query: str) -> list[float]:
    vector = next(iter(_embedder().embed([_BGE_QUERY_PREFIX + query])))
    return vector.tolist()


def _point_id(project: str, link: str) -> str:
    """Stable across live→archive relocation of the same project/link."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{project}:{link}"))


def _source_entries(source: MemorySource, path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    try:
        parsed = _parse_index(path.read_text(encoding="utf-8"))
    except OSError as exc:
        log.warning("memory retrieval: could not read %s: %s", path, exc)
        return []
    return [
        {
            "project": source.project,
            "source_file": path.name,
            "title": entry.title or entry.link or "Untitled",
            "link": entry.link or "",
            "body": entry.body or "",
        }
        for entry in parsed
        if entry.is_entry
    ]


def _collect_entries(sources: list[MemorySource]) -> list[dict[str, str]]:
    # A link is the stable identity within a project. Archive is loaded first so
    # an accidentally duplicated live entry wins deterministically, while the
    # UUID remains stable as entries move between files.
    entries: dict[tuple[str, str], dict[str, str]] = {}
    for source in sources:
        for path in (source.archive_path, source.index_path):
            for entry in _source_entries(source, path):
                entries[(entry["project"], entry["link"])] = entry
    return list(entries.values())


def _content_fingerprint(sources: list[MemorySource]) -> str:
    digest = hashlib.sha256()
    for source in sorted(sources, key=lambda item: item.project):
        for label, path in (
            ("live", source.index_path),
            ("archive", source.archive_path),
        ):
            digest.update(f"{source.project}:{label}\0".encode())
            try:
                digest.update(path.read_bytes())
            except FileNotFoundError:
                digest.update(b"<missing>")
            digest.update(b"\0")
    return digest.hexdigest()


def _existing_fingerprint_paths(sources: list[MemorySource]) -> list[Path]:
    return [
        source.fingerprint_path
        for source in sources
        if source.index_path.exists() or source.archive_path.exists()
    ]


def _fingerprint_matches(sources: list[MemorySource], fingerprint: str) -> bool:
    paths = _existing_fingerprint_paths(sources)
    if not paths:
        return False
    try:
        return all(path.read_text(encoding="utf-8").strip() == fingerprint for path in paths)
    except OSError:
        return False


def _write_fingerprints(sources: list[MemorySource], fingerprint: str) -> None:
    for path in _existing_fingerprint_paths(sources):
        _atomic_write_text(path, fingerprint + "\n")


def _collection_is_empty(client: Any) -> bool:
    return int(client.count(collection_name=_COLLECTION, exact=True).count) == 0


def rebuild_memory_index(
    *,
    sources: list[MemorySource] | None = None,
    force: bool = False,
) -> dict[str, Any]:
    """Full-wipe and rebuild the memory collection when content changed."""
    sources = configured_sources() if sources is None else sources
    if not _RAG_ENABLED:
        return {"status": "disabled", "changed": False, "indexed": 0}
    if not any(source.index_path.exists() or source.archive_path.exists() for source in sources):
        return {"status": "no_sources", "changed": False, "indexed": 0}

    fingerprint = _content_fingerprint(sources)
    try:
        client = _client()
        collection_exists = client.collection_exists(_COLLECTION)
        corpus_has_entries = bool(_collect_entries(sources))
        collection_usable = collection_exists and not (
            corpus_has_entries and _collection_is_empty(client)
        )
        if not force and _fingerprint_matches(sources, fingerprint) and collection_usable:
            return {
                "status": "unchanged",
                "changed": False,
                "indexed": 0,
                "fingerprint": fingerprint,
            }

        from qdrant_client.models import Distance, PointStruct, VectorParams

        entries = _collect_entries(sources)
        if collection_exists:
            client.delete_collection(_COLLECTION)
        client.create_collection(
            collection_name=_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )
        if entries:
            passages = [f"{entry['title']}\n\n{entry['body']}".strip() for entry in entries]
            vectors = _embed(passages)
            points = [
                PointStruct(
                    id=_point_id(entry["project"], entry["link"]),
                    vector=vector,
                    payload=entry,
                )
                for entry, vector in zip(entries, vectors, strict=True)
            ]
            client.upsert(collection_name=_COLLECTION, points=points)
        _write_fingerprints(sources, fingerprint)
        return {
            "status": "rebuilt",
            "changed": True,
            "indexed": len(entries),
            "fingerprint": fingerprint,
        }
    except Exception as exc:
        log.warning("memory retrieval: rebuild failed: %s", exc)
        return {
            "status": "error",
            "changed": False,
            "indexed": 0,
            "error": str(exc),
        }


def refresh_memories(
    *,
    sources: list[MemorySource],
    projects: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    force_reindex: bool = False,
) -> dict[str, Any]:
    """Prune+reindex explicit sources; never silently resolve live paths."""
    selected = (
        {canonical for project in projects if (canonical := canonical_project(project))}
        if projects
        else {source.project for source in sources}
    )
    prune_results: dict[str, dict[str, Any]] = {}
    for source in sources:
        if source.project not in selected:
            continue
        if not source.index_path.is_file():
            prune_results[source.project] = {"status": "missing"}
            continue
        try:
            result = prune(
                index_path=source.index_path,
                archive_path=source.archive_path,
                max_bytes=max_bytes,
                sort="mtime",
                pins=list(source.pins),
            )
            prune_results[source.project] = {
                "status": (
                    "concurrent_modification"
                    if result.aborted_concurrent_modification
                    else ("pruned" if result.changed else "unchanged")
                ),
                "kept": result.kept_count,
                "archived": result.archived_count,
            }
        except Exception as exc:
            log.warning("memory retrieval: prune failed for %s: %s", source.project, exc)
            prune_results[source.project] = {"status": "error", "error": str(exc)}

    return {
        "projects": prune_results,
        "reindex": rebuild_memory_index(sources=sources, force=force_reindex),
    }


def refresh_configured_memories(
    *,
    projects: list[str] | None = None,
    max_bytes: int = DEFAULT_MAX_BYTES,
    force_reindex: bool = False,
) -> dict[str, Any]:
    """Explicit production wrapper resolving the configured live sources.

    The manual CLI calls this directly because invoking that command is itself
    explicit authorization. Automatic callers must additionally check
    :func:`auto_refresh_enabled` before reaching this wrapper.
    """
    return refresh_memories(
        sources=configured_sources(),
        projects=projects,
        max_bytes=max_bytes,
        force_reindex=force_reindex,
    )


def search_memory(
    query: str,
    *,
    project: str = "",
    include_archived: bool = True,
    top_k: int = DEFAULT_TOP_K,
    sources: list[MemorySource] | None = None,
) -> dict[str, Any]:
    """Search live/archive memory with server-side payload filters."""
    if not _RAG_ENABLED:
        return {"hits": [], "error": "memory RAG is disabled (KHIMAIRA_MEMORY_RAG=0)"}
    query = query.strip()
    if not query:
        return {"hits": [], "error": "query must be non-empty"}
    canonical = canonical_project(project) if project else None
    if project and canonical is None:
        return {"hits": [], "error": f"unknown project: {project!r}"}
    top_k = max(1, min(int(top_k), 50))
    sources = configured_sources() if sources is None else sources

    try:
        client = _client()
        collection_exists = client.collection_exists(_COLLECTION)
        collection_empty = collection_exists and _collection_is_empty(client)
        if not collection_exists or (collection_empty and _collect_entries(sources)):
            rebuilt = rebuild_memory_index(sources=sources, force=True)
            if rebuilt.get("status") == "error":
                return {"hits": [], "error": rebuilt.get("error", "rebuild failed")}
            client = _client()
        if not client.collection_exists(_COLLECTION):
            return {"hits": [], "error": None}

        from qdrant_client.models import FieldCondition, Filter, MatchValue

        must = []
        if canonical:
            must.append(FieldCondition(key="project", match=MatchValue(value=canonical)))
        if not include_archived:
            must.append(FieldCondition(key="source_file", match=MatchValue(value="MEMORY.md")))
        query_filter = Filter(must=must) if must else None
        response = client.query_points(
            collection_name=_COLLECTION,
            query=_embed_query(query),
            query_filter=query_filter,
            limit=top_k,
            with_payload=True,
        )
        hits = []
        for point in response.points:
            payload = dict(point.payload or {})
            payload["score"] = round(float(point.score or 0.0), 4)
            hits.append(payload)
        return {"hits": hits, "error": None}
    except Exception as exc:
        log.warning("memory retrieval: search failed for %r: %s", query, exc)
        return {"hits": [], "error": str(exc)}


_MEMORY_REFRESH_JOB_ID: str | None = None
_MEMORY_REFRESH_TASK: asyncio.Task | None = None
_MEMORY_REFRESH_STATE: dict[str, Any] | None = None


async def _run_memory_refresh_job(job_id: str) -> None:
    global _MEMORY_REFRESH_STATE
    try:
        result = await asyncio.to_thread(refresh_configured_memories)
        _MEMORY_REFRESH_STATE = {"job_id": job_id, "status": "done", "result": result}
    except Exception as exc:
        log.exception("memory retrieval: refresh job %s crashed", job_id)
        _MEMORY_REFRESH_STATE = {"job_id": job_id, "status": "error", "error": str(exc)}


def schedule_memory_refresh() -> str:
    """Schedule one refresh, coalescing concurrent timer/manual requests."""
    global _MEMORY_REFRESH_JOB_ID, _MEMORY_REFRESH_STATE, _MEMORY_REFRESH_TASK
    if not auto_refresh_enabled():
        return ""
    if _MEMORY_REFRESH_TASK is not None and not _MEMORY_REFRESH_TASK.done():
        return _MEMORY_REFRESH_JOB_ID or ""

    job_id = "memory-refresh-" + uuid.uuid4().hex[:12]
    _MEMORY_REFRESH_JOB_ID = job_id
    _MEMORY_REFRESH_STATE = {"job_id": job_id, "status": "pending"}
    _MEMORY_REFRESH_TASK = asyncio.create_task(_run_memory_refresh_job(job_id))
    return job_id


def get_memory_refresh_status() -> dict[str, Any]:
    return {
        "in_progress": bool(_MEMORY_REFRESH_TASK is not None and not _MEMORY_REFRESH_TASK.done()),
        "job": _MEMORY_REFRESH_STATE,
    }
