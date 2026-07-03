"""Tests for khimaira.monitor.notebook_retrieval (Phase 2b).

Integration-style: uses a REAL qdrant instance + the REAL fastembed embedder
(no network needed — the bge-small ONNX weights are already cached locally,
shared with mnemosyne's own use of the same model). Skipped entirely if
qdrant isn't reachable at :6343, mirroring this repo's "integration: ...
skipped when the required runner isn't installed" convention.

Each test uses a disposable per-test collection (never the real
`khimaira_notes`), cleaned up after.
"""

from __future__ import annotations

import uuid

import pytest

pytestmark = pytest.mark.integration


def _qdrant_reachable() -> bool:
    try:
        import httpx

        r = httpx.get("http://localhost:6343/collections", timeout=2.0)
        return r.status_code == 200
    except Exception:
        return False


if not _qdrant_reachable():
    pytest.skip(
        "qdrant not reachable at :6343 — skipping notebook_retrieval integration tests",
        allow_module_level=True,
    )


@pytest.fixture
def retrieval(monkeypatch):
    from khimaira.monitor import notebook_retrieval as r

    test_collection = f"khimaira_notes_test_{uuid.uuid4().hex[:8]}"
    monkeypatch.setattr(r, "_COLLECTION", test_collection)
    yield r
    try:
        client = r._client()
        if client.collection_exists(test_collection):
            client.delete_collection(test_collection)
    except Exception:
        pass


def _note(note_id: str, *, raw_text: str = "raw", pipeline: dict | None = None) -> dict:
    return {"id": note_id, "raw_text": raw_text, "pipeline": pipeline}


def test_upsert_and_search_finds_note(retrieval):
    note = _note(
        "note-1",
        pipeline={
            "summary": "Fixing a race condition in the session reaper",
            "organized_md": "## Fix\nAdded a lock to serialize access.",
        },
    )
    retrieval.upsert_note(note)
    hits = retrieval.search_notes("race condition session reaper", top_k=5, threshold=0.3)
    assert any(h["note_id"] == "note-1" for h in hits)


def test_upsert_falls_back_to_raw_text_when_no_pipeline(retrieval):
    note = _note("note-2", raw_text="Stripe payment integration notes and webhook handling")
    retrieval.upsert_note(note)
    hits = retrieval.search_notes("stripe payments webhooks", top_k=5, threshold=0.3)
    assert any(h["note_id"] == "note-2" for h in hits)


def test_search_respects_threshold(retrieval):
    note = _note("note-3", raw_text="quantum chromodynamics lattice gauge theory computation")
    retrieval.upsert_note(note)
    hits = retrieval.search_notes("what's for dinner tonight", top_k=5, threshold=0.9)
    assert hits == []


def test_delete_note_removes_point(retrieval):
    note = _note("note-4", raw_text="deleteme testing content unique phrase xyzzy")
    retrieval.upsert_note(note)
    assert any(
        h["note_id"] == "note-4"
        for h in retrieval.search_notes("xyzzy testing content", threshold=0.3)
    )
    retrieval.delete_note("note-4")
    assert not any(
        h["note_id"] == "note-4"
        for h in retrieval.search_notes("xyzzy testing content", threshold=0.3)
    )


def test_upsert_empty_text_is_noop(retrieval):
    # Nothing to embed — must not raise, must not create a point.
    retrieval.upsert_note(_note("note-5", raw_text="", pipeline=None))


def test_upsert_note_missing_id_key_does_not_raise(retrieval):
    retrieval.upsert_note({"raw_text": "no id key here"})


async def test_search_notes_async_wraps_sync_call(retrieval):
    note = _note("note-6", raw_text="async wrapper test unique phrase blorp")
    retrieval.upsert_note(note)
    hits = await retrieval.search_notes_async("blorp async wrapper", threshold=0.3)
    assert any(h["note_id"] == "note-6" for h in hits)


def test_rag_disabled_short_circuits_everything(monkeypatch):
    from khimaira.monitor import notebook_retrieval as r

    monkeypatch.setattr(r, "_RAG_ENABLED", False)
    r.upsert_note({"id": "x", "raw_text": "hello"})
    assert r.search_notes("hello") == []
    r.delete_note("x")


def test_search_notes_fail_open_on_qdrant_error(monkeypatch):
    from khimaira.monitor import notebook_retrieval as r

    def broken_client():
        raise RuntimeError("qdrant is down")

    monkeypatch.setattr(r, "_client", broken_client)
    assert r.search_notes("anything") == []
    r.upsert_note({"id": "y", "raw_text": "hi"})
    r.delete_note("y")
