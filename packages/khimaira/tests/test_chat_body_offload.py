"""Chat-body fan-out cap (2026-06-07 money-printer fix).

A chat message fans out over SSE to EVERY accepted member; an uncapped body
means one large post is ingested by all N members and re-bills each member's
full context window. Over the cap, the daemon offloads the full body to a
per-chat artifact file and stores a preview + pointer. Content is never lost.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_chats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    from khimaira.monitor import sessions as sessions_mod
    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod
    importlib.reload(chats_mod)
    yield chats_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)


MASTER = "11111111-0000-0000-0000-000000000001"
WORKER = "22222222-0000-0000-0000-000000000002"


def _room(c):
    room = c.create_room(MASTER, [WORKER], title="t", topology="hierarchical")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, WORKER)
    return chat_id


def test_small_body_passes_through_unchanged(isolated_chats):
    c = isolated_chats
    chat_id = _room(c)
    rec = c.send_message(chat_id, MASTER, "short message")
    assert rec["body"] == "short message"


def test_large_body_offloaded_to_artifact_with_pointer(isolated_chats):
    c = isolated_chats
    chat_id = _room(c)
    big = "DESIGN DOC line.\n" * 2000  # ~34KB
    rec = c.send_message(chat_id, MASTER, big)
    # stored body is the bounded preview + pointer, NOT the full 34KB
    assert len(rec["body"]) < c._CHAT_BODY_CAP_CHARS + 600
    assert "truncated for chat fan-out" in rec["body"]
    assert rec["id"] in rec["body"]  # pointer references the artifact filename
    # full content preserved on disk
    artifact = c._artifacts_dir(chat_id) / f"{rec['id']}.md"
    assert artifact.exists()
    assert artifact.read_text() == c._sanitize_message_body(big)


def test_large_task_body_offloaded(isolated_chats):
    c = isolated_chats
    chat_id = _room(c)
    big = "x" * 50000
    rec = c.create_task(chat_id, MASTER, big, assignee_session_id=WORKER)
    assert len(rec["body"]) < c._CHAT_BODY_CAP_CHARS + 600
    assert "truncated for chat fan-out" in rec["body"]
    artifact = c._artifacts_dir(chat_id) / f"{rec['id']}.md"
    assert artifact.exists()


def test_offload_preserves_preview_head(isolated_chats):
    c = isolated_chats
    chat_id = _room(c)
    big = "IMPORTANT FIRST LINE so members see the gist.\n" + ("filler\n" * 3000)
    rec = c.send_message(chat_id, MASTER, big)
    assert "IMPORTANT FIRST LINE" in rec["body"]


def test_exact_cap_boundary_not_offloaded(isolated_chats):
    c = isolated_chats
    chat_id = _room(c)
    body = "a" * c._CHAT_BODY_CAP_CHARS  # exactly at cap → unchanged
    rec = c.send_message(chat_id, MASTER, body)
    assert rec["body"] == body
    assert "truncated" not in rec["body"]
