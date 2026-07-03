"""Tests for khimaira.monitor.notebook_pipeline (Phase 1c).

The `claude -p` subprocess is mocked throughout — these tests exercise the
deterministic parse/retry/tollgate logic, not the real CLI. Canned envelopes
mirror `claude -p --output-format json`'s shape: {"result": "<json string>", ...}.
"""

from __future__ import annotations

import asyncio
import importlib
import json

import pytest


@pytest.fixture
def notes_store(isolated_state, monkeypatch):
    from khimaira.monitor import notes as notes_mod

    importlib.reload(notes_mod)
    yield notes_mod
    importlib.reload(notes_mod)


@pytest.fixture
def pipeline(notes_store, monkeypatch):
    from khimaira.monitor import notebook_pipeline as pipeline_mod

    importlib.reload(pipeline_mod)
    yield pipeline_mod


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input=None):
        return self._stdout, self._stderr


def _queue_responses(pipeline, monkeypatch, responses: list[_FakeProc]):
    """Patch asyncio.create_subprocess_exec to pop canned responses in order."""
    queue = list(responses)

    async def fake_exec(*args, **kwargs):
        return queue.pop(0)

    monkeypatch.setattr(pipeline.asyncio, "create_subprocess_exec", fake_exec)


_VALID_PAYLOAD = {
    "summary": "a summary",
    "technical": "tech details",
    "plain": "plain english",
    "organized_md": "# organized",
    "tags": ["fix", "backend"],
    "entities": ["notes.py"],
}


def _envelope(result_str: str) -> bytes:
    return json.dumps({"result": result_str, "session_id": "irrelevant"}).encode("utf-8")


async def test_transform_note_success(pipeline, monkeypatch):
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))])
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_transform_note_strips_markdown_fence(pipeline, monkeypatch):
    fenced = "```json\n" + json.dumps(_VALID_PAYLOAD) + "\n```"
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(fenced))])
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_transform_note_retries_once_then_succeeds(pipeline, monkeypatch):
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("not json at all")),
            _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD))),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_transform_note_fails_after_two_bad_attempts(pipeline, monkeypatch):
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("not json at all")),
            _FakeProc(_envelope("still not json")),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result is None


async def test_transform_note_validation_failure_retries(pipeline, monkeypatch):
    """A well-formed JSON object missing required keys must also retry, not crash."""
    incomplete = {"summary": "only this key"}
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope(json.dumps(incomplete))),
            _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD))),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_transform_note_nonzero_exit_retries(pipeline, monkeypatch):
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(b"", stderr=b"boom", returncode=1),
            _FakeProc(_envelope(json.dumps(_VALID_PAYLOAD))),
        ],
    )
    result = await pipeline.transform_note("raw text")
    assert result == _VALID_PAYLOAD


async def test_trigger_pipeline_success_sets_processed(notes_store, pipeline, monkeypatch):
    note = notes_store.add_note("some raw content")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))])

    await pipeline.trigger_pipeline(note["id"])

    updated = notes_store.get_note(note["id"])
    assert updated["status"] == "processed"
    assert updated["pipeline"] == _VALID_PAYLOAD
    assert updated["raw_text"] == "some raw content"


async def test_trigger_pipeline_failure_marks_failed_and_preserves_raw_text(
    notes_store, pipeline, monkeypatch
):
    note = notes_store.add_note("irreplaceable raw content")
    _queue_responses(
        pipeline,
        monkeypatch,
        [
            _FakeProc(_envelope("garbage")),
            _FakeProc(_envelope("still garbage")),
        ],
    )

    await pipeline.trigger_pipeline(note["id"])

    updated = notes_store.get_note(note["id"])
    assert updated["status"] == "failed"
    assert updated["pipeline"] is None
    assert updated["raw_text"] == "irreplaceable raw content"


async def test_trigger_pipeline_note_deleted_before_transform_is_a_noop(
    notes_store, pipeline, monkeypatch
):
    # No note created — note_id resolves to nothing. Must not raise.
    await pipeline.trigger_pipeline("no-such-note")


async def test_schedule_pipeline_creates_background_task(notes_store, pipeline, monkeypatch):
    note = notes_store.add_note("scheduled content")
    _queue_responses(pipeline, monkeypatch, [_FakeProc(_envelope(json.dumps(_VALID_PAYLOAD)))])

    pipeline.schedule_pipeline(note["id"])
    # Let the scheduled task run to completion.
    await asyncio.sleep(0.05)

    updated = notes_store.get_note(note["id"])
    assert updated["status"] == "processed"
