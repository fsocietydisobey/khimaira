"""Phase 2b — vector search substrate for the self-healing notebook.

Mirrors mnemosyne's hippocampus retrieval pattern
(~/dev/ai-lab/mnemosyne/src/mnemosyne/retrieval.py): a local fastembed
bge-small-en-v1.5 (384d) embedder + a qdrant collection, same :6343 instance
mnemosyne already uses. NOT a cross-repo import — mnemosyne is a standalone
service; this is a separate implementation of the same pattern, in its own
collection (`khimaira_notes`) so the two never collide.

Fail-open everywhere (per this module's whole reason to exist): qdrant
unreachable, fastembed not loaded, or any embed/upsert/search error must
never break the notebook. upsert_note/delete_note become silent no-ops;
search_notes returns []. Toggle with KHIMAIRA_NOTEBOOK_RAG=0 to disable
outright (mirrors mnemosyne's own live-upsert flag convention).
"""

from __future__ import annotations

import asyncio
import os
import uuid
from functools import lru_cache
from typing import Any

from khimaira.log import get_logger

log = get_logger("monitor.notebook_retrieval")

# Strong references to background embed/delete tasks — asyncio.create_task()
# only holds a weak ref, so a fire-and-forget task can be silently
# garbage-collected mid-flight (same pattern as notebook_pipeline.py's
# _BACKGROUND_TASKS and server.py's _spawn).
_BACKGROUND_TASKS: set[asyncio.Task] = set()

_RAG_ENABLED = os.environ.get("KHIMAIRA_NOTEBOOK_RAG", "1") != "0"

EMBED_MODEL = os.environ.get("KHIMAIRA_NOTEBOOK_EMBED_MODEL", "BAAI/bge-small-en-v1.5")
EMBED_DIM = int(os.environ.get("KHIMAIRA_NOTEBOOK_EMBED_DIM", "384"))
# bge retrieval convention: prefix the QUERY (not passages) with this instruction.
_BGE_QUERY_PREFIX = "Represent this sentence for searching relevant passages: "

_QDRANT_URL = os.environ.get("KHIMAIRA_NOTEBOOK_QDRANT_URL", "http://localhost:6343")
_COLLECTION = "khimaira_notes"

DEFAULT_TOP_K = 5
# Looser than mnemosyne's fact-precision gate (0.65) — this is note-recall
# ("which notes might be relevant"), not fact-precision; a downstream
# staleness-gated revalidate pass re-grounds whatever comes back, so a
# slightly-off hit costs a cheap gate check, not a wrong fact injected raw.
DEFAULT_THRESHOLD = 0.5


@lru_cache(maxsize=1)
def _embedder():
    """Lazy singleton fastembed model (first call downloads the ONNX weights)."""
    from fastembed import TextEmbedding

    return TextEmbedding(model_name=EMBED_MODEL)


@lru_cache(maxsize=1)
def _client():
    from qdrant_client import QdrantClient

    return QdrantClient(url=_QDRANT_URL)


def _embed(texts: list[str]) -> list[list[float]]:
    return [v.tolist() for v in _embedder().embed(texts)]


def _embed_query(query: str) -> list[float]:
    vec = next(iter(_embedder().embed([_BGE_QUERY_PREFIX + query])))
    return vec.tolist()


def _ensure_collection() -> None:
    from qdrant_client.models import Distance, VectorParams

    client = _client()
    if not client.collection_exists(_COLLECTION):
        client.create_collection(
            collection_name=_COLLECTION,
            vectors_config=VectorParams(size=EMBED_DIM, distance=Distance.COSINE),
        )


def _point_id(note_id: str) -> str:
    """Stable Qdrant point id from a note id (notes use 12-char hex, not a
    UUID Qdrant accepts directly, so always derive via uuid5)."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, note_id))


def _passage_text(record: dict[str, Any]) -> str:
    """What we embed: summary + organized_md once processed (best retrieval
    signal); raw_text for a draft that hasn't been structured yet."""
    pipeline = record.get("pipeline")
    if pipeline:
        return f"{pipeline.get('summary', '')}\n\n{pipeline.get('organized_md', '')}".strip()
    return (record.get("raw_text") or "").strip()


def upsert_note(record: dict[str, Any]) -> None:
    """Embed + upsert a note. Call on create and again whenever its embedded
    content changes (structuring completes, or a heal lands a new pipeline).
    Fail-open — never raises."""
    if not _RAG_ENABLED:
        return
    text = _passage_text(record)
    if not text:
        return
    try:
        from qdrant_client.models import PointStruct

        _ensure_collection()
        vec = _embed([text])[0]
        _client().upsert(
            collection_name=_COLLECTION,
            points=[
                PointStruct(
                    id=_point_id(record["id"]),
                    vector=vec,
                    payload={"note_id": record["id"]},
                )
            ],
        )
    except Exception as exc:
        log.warning("notebook_retrieval: upsert_note(%s) failed: %s", record.get("id"), exc)


def delete_note(note_id: str) -> None:
    """Remove a note's point on note deletion. Fail-open — never raises."""
    if not _RAG_ENABLED:
        return
    try:
        client = _client()
        if not client.collection_exists(_COLLECTION):
            return
        client.delete(collection_name=_COLLECTION, points_selector=[_point_id(note_id)])
    except Exception as exc:
        log.warning("notebook_retrieval: delete_note(%s) failed: %s", note_id, exc)


def search_notes(
    query: str, *, top_k: int = DEFAULT_TOP_K, threshold: float = DEFAULT_THRESHOLD
) -> list[dict[str, Any]]:
    """Top-k note ids above the similarity threshold, best-first.

    Returns [{note_id, score}]. Empty on no hits, missing collection, RAG
    disabled, or any error (fail-open) — never raises.
    """
    if not _RAG_ENABLED:
        return []
    try:
        client = _client()
        if not client.collection_exists(_COLLECTION):
            return []
        qvec = _embed_query(query)
        res = client.query_points(
            collection_name=_COLLECTION, query=qvec, limit=top_k, with_payload=True
        )
        hits = []
        for p in res.points:
            if p.score is None or p.score < threshold:
                continue
            note_id = (p.payload or {}).get("note_id", "")
            if note_id:
                hits.append({"note_id": note_id, "score": round(float(p.score), 4)})
        return hits
    except Exception as exc:
        log.warning("notebook_retrieval: search_notes(%r) failed: %s", query, exc)
        return []


async def search_notes_async(
    query: str, *, top_k: int = DEFAULT_TOP_K, threshold: float = DEFAULT_THRESHOLD
) -> list[dict[str, Any]]:
    """Async-safe wrapper — embed+qdrant I/O is synchronous, so callers in an
    async route/handler must not call search_notes() directly (blocks the
    event loop). Use this instead."""
    return await asyncio.to_thread(search_notes, query, top_k=top_k, threshold=threshold)


def schedule_upsert(record: dict[str, Any]) -> None:
    """Fire-and-forget upsert off the event loop. Use from a sync call site
    (e.g. an API route) right after a note is created — embed+qdrant I/O is
    synchronous and must never block the response."""
    task = asyncio.create_task(asyncio.to_thread(upsert_note, record))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def schedule_delete(note_id: str) -> None:
    """Fire-and-forget point removal off the event loop."""
    task = asyncio.create_task(asyncio.to_thread(delete_note, note_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
