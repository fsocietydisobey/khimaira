"""Unit tests for khimaira.monitor.chats.

Covers:
  - JSONL round-trip: create + accept + send + history matches
  - Sender gating: non-member send → ValueError; pending member can't read history
  - Member state machine: invite → accept → leave; can't accept twice; can't accept non-existent
  - Group cardinality (3+ members)
  - chat_id derivation: fresh-vs-resume produces different ids; same args produce same id
  - Delete: only creator; archives the JSONL file
  - my_chats: lists chats where session is pending or accepted
"""

from __future__ import annotations

import asyncio
import importlib
import json
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


def _make_session(sessions_mod, session_id: str, name: str | None = None) -> None:
    sd = sessions_mod._session_dir(session_id)
    payload = {"status": "implementing", "detail": ""}
    if name:
        payload["name"] = name
    (sd / "status.json").write_text(json.dumps(payload), encoding="utf-8")


# ---------------------------------------------------------------------------
# chat_id derivation
# ---------------------------------------------------------------------------


def test_derive_chat_id_stable_for_same_members(isolated_chats):
    c = isolated_chats
    a = c.derive_chat_id(["alice", "bob"])
    b = c.derive_chat_id(["bob", "alice"])
    assert a == b  # order-invariant


def test_my_chats_accepts_unresolved_uuid(isolated_chats):
    """Lazy registration: a fresh session whose state dir doesn't exist
    yet (Claude Code just spawned the chat MCP subprocess; no
    session_log_* call has hit the daemon) must still be able to call
    chat_my_chats with its session_id from the SessionStart hook.

    Regression test for the original lazy-registration 404 bug — `chat_my_chats`
    used `sessions_mod.resolve_session_id` which required the dir to
    exist. Now it accepts UUIDs verbatim via `_resolve_or_uuid`.
    """
    c = isolated_chats
    fresh_uuid = "280bcb97-3c9f-4a2b-9813-5d3c76169967"
    # No _make_session call — this UUID has no state dir.
    result = c.my_chats(fresh_uuid)
    assert result == []  # no chats yet, but the call succeeds


def test_resolve_or_uuid_passes_uuid_through(isolated_chats):
    c = isolated_chats
    uuid_in = "deadbeef-1234-5678-9abc-def012345678"
    assert c._resolve_or_uuid(uuid_in) == uuid_in


def test_resolve_or_uuid_rejects_unknown_name(isolated_chats):
    c = isolated_chats
    with pytest.raises(ValueError):
        c._resolve_or_uuid("not-a-uuid-and-not-a-session")


def test_derive_chat_id_changes_with_fresh_suffix(isolated_chats):
    c = isolated_chats
    base = c.derive_chat_id(["alice", "bob"])
    fresh = c.derive_chat_id(["alice", "bob"], fresh_suffix="2026-05-14T22:00:00Z")
    assert base != fresh
    # Same members + same suffix → same id (deterministic).
    again = c.derive_chat_id(["alice", "bob"], fresh_suffix="2026-05-14T22:00:00Z")
    assert fresh == again


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


def test_create_accept_send_round_trip(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    assert room["members"]["alice"]["state"] == c.ACCEPTED
    assert room["members"]["bob"]["state"] == c.PENDING

    c.accept(chat_id, "bob")
    room = c.load_room(chat_id)
    assert room["members"]["bob"]["state"] == c.ACCEPTED

    msg = c.send_message(chat_id, "alice", "hello bob")
    assert msg["body"] == "hello bob"
    assert msg["sender_id"] == "alice"

    history = c.history(chat_id, "bob")
    assert len(history) == 1
    assert history[0]["body"] == "hello bob"


# ---------------------------------------------------------------------------
# Sender gating
# ---------------------------------------------------------------------------


def test_non_member_cannot_send(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")
    _make_session(sessions_mod, "eve")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    with pytest.raises(ValueError):
        c.send_message(chat_id, "eve", "I'm hostile and shouldn't be here")


def test_pending_member_cannot_send(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    # bob never accepts
    with pytest.raises(ValueError):
        c.send_message(chat_id, "bob", "premature")


def test_pending_member_cannot_read_history(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.send_message(chat_id, "alice", "msg from alice")

    # bob is pending — can't read.
    with pytest.raises(ValueError):
        c.history(chat_id, "bob")


# ---------------------------------------------------------------------------
# Member state machine
# ---------------------------------------------------------------------------


def test_cannot_accept_twice(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")
    with pytest.raises(ValueError):
        c.accept(chat_id, "bob")


def test_cannot_accept_non_member(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")
    _make_session(sessions_mod, "eve")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    with pytest.raises(ValueError):
        c.accept(chat_id, "eve")


def test_invite_then_accept(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")
    _make_session(sessions_mod, "carol")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    # Now bob (an accepted member) invites carol.
    c.invite(chat_id, "bob", "carol")
    room = c.load_room(chat_id)
    assert room["members"]["carol"]["state"] == c.PENDING

    c.accept(chat_id, "carol")
    room = c.load_room(chat_id)
    assert room["members"]["carol"]["state"] == c.ACCEPTED


def test_pending_member_cannot_invite(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")
    _make_session(sessions_mod, "carol")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    # bob is still pending; can't invite carol.
    with pytest.raises(ValueError):
        c.invite(chat_id, "bob", "carol")


def test_leave_then_cannot_send(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")
    c.leave(chat_id, "bob")

    with pytest.raises(ValueError):
        c.send_message(chat_id, "bob", "I left, this should fail")


# ---------------------------------------------------------------------------
# Group cardinality
# ---------------------------------------------------------------------------


def test_three_member_group_round_trip(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    for sid in ("alice", "bob", "carol"):
        _make_session(sessions_mod, sid, sid)

    room = c.create_room("alice", ["bob", "carol"])
    chat_id = room["meta"]["chat_id"]
    assert len(room["members"]) == 3

    c.accept(chat_id, "bob")
    c.accept(chat_id, "carol")

    c.send_message(chat_id, "alice", "hello group")
    c.send_message(chat_id, "bob", "hi alice")

    history = c.history(chat_id, "carol")
    assert len(history) == 2
    assert history[0]["sender_id"] == "alice"
    assert history[1]["sender_id"] == "bob"


# ---------------------------------------------------------------------------
# Delete + archive
# ---------------------------------------------------------------------------


def test_only_creator_can_delete(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    with pytest.raises(ValueError):
        c.delete(chat_id, "bob")  # bob is not creator

    result = c.delete(chat_id, "alice")
    assert "archived_to" in result


def test_delete_archives_jsonl(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]

    src = c._chat_path(chat_id)
    assert src.exists()

    c.delete(chat_id, "alice")
    assert not src.exists()
    # File should be in archive/.
    archive_files = list(c._archive_dir().glob(f"{chat_id}*"))
    assert len(archive_files) == 1


# ---------------------------------------------------------------------------
# my_chats
# ---------------------------------------------------------------------------


def test_my_chats_lists_pending_and_accepted(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")
    _make_session(sessions_mod, "carol")

    c.create_room("alice", ["bob"])  # bob pending
    room2 = c.create_room("alice", ["carol"])  # carol pending
    c.accept(room2["meta"]["chat_id"], "carol")

    bob_chats = c.my_chats("bob")
    assert len(bob_chats) == 1
    assert bob_chats[0]["my_state"] == c.PENDING

    carol_chats = c.my_chats("carol")
    assert len(carol_chats) == 1
    assert carol_chats[0]["my_state"] == c.ACCEPTED


def test_my_chats_excludes_left(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")
    c.leave(chat_id, "bob")

    assert c.my_chats("bob") == []


# ---------------------------------------------------------------------------
# Pub/sub for SSE
# ---------------------------------------------------------------------------


def test_subscribe_receives_new_messages(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    async def run():
        gen = c.subscribe("bob")
        # Pull from the generator in a task; send in parallel.
        received: list[dict] = []

        async def collect():
            async for record in gen:
                received.append(record)
                if len(received) >= 1:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)  # let subscribe register
        c.send_message(chat_id, "alice", "ping")
        await asyncio.wait_for(task, timeout=2.0)
        return received

    received = asyncio.run(run())
    assert len(received) == 1
    assert received[0]["body"] == "ping"
    assert received[0]["kind"] == c.MSG


def test_subscribe_skips_pending_members(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    # bob never accepts

    async def run():
        gen = c.subscribe("bob")
        received: list[dict] = []

        async def collect():
            async for record in gen:
                received.append(record)
                if len(received) >= 1:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)
        c.send_message(chat_id, "alice", "ping pending")
        # Wait briefly; bob shouldn't receive (pending).
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except TimeoutError:
            task.cancel()
        return received

    received = asyncio.run(run())
    assert received == []
