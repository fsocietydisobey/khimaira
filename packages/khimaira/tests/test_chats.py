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


def test_reject_pending_invite(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]

    rec = c.reject(chat_id, "bob")
    assert rec["state"] == c.REJECTED

    # Rejected member cannot then accept.
    with pytest.raises(ValueError):
        c.accept(chat_id, "bob")

    # Rejected member cannot send.
    with pytest.raises(ValueError):
        c.send_message(chat_id, "bob", "should fail")


def test_reject_non_pending_raises(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")
    # bob is now accepted, not pending — can't reject.
    with pytest.raises(ValueError):
        c.reject(chat_id, "bob")


def test_latest_pending_returns_chat_id(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]

    assert c.latest_pending_chat_id("bob") == chat_id


def test_latest_pending_returns_none_when_no_invites(isolated_chats):
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    assert c.latest_pending_chat_id("alice") is None


def test_sanitize_message_body_strips_thinking_tags(isolated_chats):
    c = isolated_chats
    assert (
        c._sanitize_message_body("hello <thinking>internal</thinking> world")
        == "hello internal world"
    )
    assert c._sanitize_message_body("clean message") == "clean message"
    assert (
        c._sanitize_message_body("oops</thinking>") == "oops"
    )  # opening tag missing — still strip
    assert c._sanitize_message_body("body <scratchpad>...</scratchpad>") == "body ..."
    assert c._sanitize_message_body("<reasoning>X</reasoning>actual reply") == "Xactual reply"


def test_send_message_strips_thinking_tags(isolated_chats):
    """End-to-end: a message body with leaked tags lands in JSONL clean."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    msg = c.send_message(chat_id, "alice", "Reply: <thinking>x</thinking>actual content")
    assert "<thinking>" not in msg["body"]
    assert "actual content" in msg["body"]


def test_register_and_lookup_session_by_ppid(isolated_chats):
    c = isolated_chats
    assert c.lookup_session_by_ppid(99999) is None
    c.register_session_by_ppid(99999, "uuid-aaaa-bbbb")
    assert c.lookup_session_by_ppid(99999) == "uuid-aaaa-bbbb"


def test_lookup_session_by_ppid_expires(isolated_chats, monkeypatch):
    c = isolated_chats
    import time

    real_time = time.time
    fake_t = [real_time()]
    monkeypatch.setattr(time, "time", lambda: fake_t[0])

    c.register_session_by_ppid(12345, "uuid-fresh")
    assert c.lookup_session_by_ppid(12345) == "uuid-fresh"

    # Advance past TTL.
    fake_t[0] += c._PPID_TTL_SECONDS + 1
    assert c.lookup_session_by_ppid(12345) is None


def test_subscribe_replays_pending_invite_when_late(isolated_chats):
    """Real bug: invitee subscribes AFTER an invite was broadcast. The
    live broadcast missed an empty queue, so the catch-up on subscribe()
    must yield the pending-invite record from the JSONL."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    # Invite happens BEFORE bob subscribes. The live broadcast goes to
    # an empty subscriber set (silently dropped).
    c.create_room("alice", ["bob"])

    async def run():
        # Now bob subscribes — should immediately receive the pending
        # invite from JSONL replay, even though no live broadcast was
        # captured.
        gen = c.subscribe("bob")
        received: list[dict] = []

        async def collect():
            async for record in gen:
                received.append(record)
                if len(received) >= 1:
                    return

        task = asyncio.create_task(collect())
        await asyncio.wait_for(task, timeout=2.0)
        return received

    received = asyncio.run(run())
    assert any(
        r.get("kind") == "member" and r.get("state") == "pending" and r.get("session_id") == "bob"
        for r in received
    )


def test_invite_broadcast_routes_to_invitee(isolated_chats):
    """When a member is added in pending state, the broadcast must go
    ONLY to the invitee — even though they're not yet `accepted`. This
    is what surfaces "you've been invited" to the receiver as a channel
    notification."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

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
        c.create_room("alice", ["bob"])  # bob gets invited as pending
        await asyncio.wait_for(task, timeout=2.0)
        return received

    received = asyncio.run(run())
    # Should receive bob's own member-pending record.
    assert any(
        r.get("kind") == "member" and r.get("state") == "pending" and r.get("session_id") == "bob"
        for r in received
    )


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


def test_subscribe_skips_messages_for_pending_members(isolated_chats):
    """Pending member's subscribe yields the pending-invite catch-up record
    (so they can see they've been invited), but does NOT receive chat
    messages until they accept. Verify the message broadcast is skipped."""
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
                # Stop after we get at least the catch-up invite + a chance
                # for any errant message to arrive.
                if len(received) >= 5:
                    return

        task = asyncio.create_task(collect())
        await asyncio.sleep(0.05)  # let subscribe yield catch-up
        c.send_message(chat_id, "alice", "ping pending")
        try:
            await asyncio.wait_for(task, timeout=0.5)
        except TimeoutError:
            task.cancel()
        return received

    received = asyncio.run(run())
    # The pending-invite catch-up record should be present.
    assert any(r.get("kind") == "member" and r.get("state") == "pending" for r in received)
    # But NO chat message should have been delivered.
    assert not any(r.get("kind") == "msg" for r in received)


# ---------------------------------------------------------------------------
# Phase B: per-recipient addressing
# ---------------------------------------------------------------------------


def test_send_message_with_to_records_recipients(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    _make_session(sessions_mod, "carol-uuid", "carol")
    c.create_room("alice-uuid", ["bob-uuid", "carol-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.accept(chat_id, "carol-uuid")
    msg = c.send_message(chat_id, "alice-uuid", "for bob only", to=["bob-uuid"])
    assert msg["to"] == ["bob-uuid"]


def test_send_message_to_rejects_non_accepted_recipient(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    with pytest.raises(ValueError, match="pending"):
        c.send_message(chat_id, "alice-uuid", "hi", to=["bob-uuid"])


def test_send_message_no_to_preserves_broadcast(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    msg = c.send_message(chat_id, "alice-uuid", "for everyone")
    assert msg["to"] is None


# ---------------------------------------------------------------------------
# Phase B: tasks
# ---------------------------------------------------------------------------


def _setup_two_member_chat(c, sessions_mod):
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    return chat_id


def test_create_task_records_pending_status(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid")
    assert task["status"] == c.TASK_PENDING
    assert task["assignee_id"] == "bob-uuid"
    assert task["id"].startswith("task-")


def test_create_task_requires_accepted_member(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    with pytest.raises(ValueError, match="pending"):
        c.create_task(chat_id, "bob-uuid", "do thing")


def test_task_status_lifecycle_happy_path(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid")
    tid = task["id"]
    c.update_task_status(chat_id, tid, "bob-uuid", c.TASK_IN_PROGRESS)
    c.update_task_status(chat_id, tid, "bob-uuid", c.TASK_DONE)
    c.update_task_status(chat_id, tid, "alice-uuid", c.TASK_APPROVED)
    status = c.task_status(chat_id, "alice-uuid")
    assert len(status) == 1
    assert status[0]["status"] == c.TASK_APPROVED


def test_task_non_assignee_cannot_progress(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    _make_session(sessions_mod, "carol-uuid", "carol")
    c.create_room("alice-uuid", ["bob-uuid", "carol-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.accept(chat_id, "carol-uuid")
    task = c.create_task(chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid")
    with pytest.raises(ValueError, match="not authorized"):
        c.update_task_status(chat_id, task["id"], "carol-uuid", c.TASK_IN_PROGRESS)


def test_task_non_master_cannot_approve(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid")
    c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_IN_PROGRESS)
    c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_DONE)
    with pytest.raises(ValueError, match="not authorized"):
        c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_APPROVED)


def test_task_changes_requested_can_resume(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid")
    tid = task["id"]
    c.update_task_status(chat_id, tid, "bob-uuid", c.TASK_IN_PROGRESS)
    c.update_task_status(chat_id, tid, "bob-uuid", c.TASK_DONE)
    c.update_task_status(chat_id, tid, "alice-uuid", c.TASK_CHANGES_REQUESTED, note="redo X")
    c.update_task_status(chat_id, tid, "bob-uuid", c.TASK_IN_PROGRESS)
    assert c.task_status(chat_id, "alice-uuid")[0]["status"] == c.TASK_IN_PROGRESS


def test_task_invalid_transition_raises(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid")
    with pytest.raises(ValueError, match="Invalid transition"):
        c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_DONE)


# ---------------------------------------------------------------------------
# Phase B: auto-accept allowlist
# ---------------------------------------------------------------------------


def test_set_and_get_auto_accept_round_trip(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    c.set_auto_accept("alice-uuid", ["trusted-peer", "another-uuid"])
    payload = c.get_auto_accept("alice-uuid")
    assert payload["allow"] == ["trusted-peer", "another-uuid"]


def test_should_auto_accept_matches_uuid(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    c.set_auto_accept("alice-uuid", ["bob-uuid"])
    assert c.should_auto_accept("alice-uuid", "bob-uuid") is True


def test_should_auto_accept_matches_friendly_name(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.set_auto_accept("alice-uuid", ["bob"])
    assert c.should_auto_accept("alice-uuid", "bob-uuid") is True


def test_should_auto_accept_returns_false_for_unknown_peer(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    c.set_auto_accept("alice-uuid", ["bob"])
    assert c.should_auto_accept("alice-uuid", "carol-uuid") is False


def test_create_room_auto_accepts_allowlisted_invitee(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.set_auto_accept("bob-uuid", ["alice"])
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    room = c.load_room(chat_id)
    assert room["members"]["bob-uuid"]["state"] == c.ACCEPTED


def test_create_room_keeps_pending_for_non_allowlisted(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    room = c.load_room(chat_id)
    assert room["members"]["bob-uuid"]["state"] == c.PENDING


def test_set_auto_accept_unknown_session_raises(isolated_chats):
    """Per CLAUDE.md: every session-resolving primitive needs unknown-name coverage.
    set_auto_accept calls _resolve_or_uuid which raises ValueError on unknown
    names — the API endpoint must catch and 404.
    """
    c = isolated_chats
    with pytest.raises(ValueError, match="No session"):
        c.set_auto_accept("nope-not-real", ["whoever"])


# ---------------------------------------------------------------------------
# Phase B v1.1: per-friendly-name auto-accept persistence
# ---------------------------------------------------------------------------


def test_set_auto_accept_writes_by_name_when_session_has_name(isolated_chats):
    """When a session has a friendly name, set_auto_accept persists under
    that name (durable across UUID churn), NOT under the UUID."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid-v1", "alice")
    c.set_auto_accept("alice-uuid-v1", ["bob"])
    # By-name file exists, by-UUID file does NOT.
    assert c._auto_accept_by_name_path("alice").is_file()
    assert not c._auto_accept_path("alice-uuid-v1").is_file()


def test_get_auto_accept_prefers_by_name_for_named_session(isolated_chats):
    """Once a session has a name, get_auto_accept reads the by-name file —
    even if a stale by-UUID file exists from a prior session that wrote
    UUID-only state. This is the load-bearing assertion: if the same name
    boots with a NEW UUID, it inherits the by-name allowlist."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid-v2", "alice")
    # Manually write a by-name file (simulating a prior session having set it).
    c._ensure_dir()
    c._auto_accept_by_name_path("alice").write_text(
        '{"allow": ["bob", "carol"], "updated_at": "2026-05-15T00:00:00+00:00"}',
        encoding="utf-8",
    )
    payload = c.get_auto_accept("alice-uuid-v2")
    assert payload["allow"] == ["bob", "carol"]


def test_apply_auto_accept_by_name_returns_applied_true_when_file_exists(isolated_chats):
    """apply_auto_accept_by_name (called at chat MCP subprocess boot)
    surfaces the by-name allowlist for a freshly-named session."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid-v3", "alice")
    c._ensure_dir()
    c._auto_accept_by_name_path("alice").write_text(
        '{"allow": ["master-bot"], "updated_at": "2026-05-15T00:00:00+00:00"}',
        encoding="utf-8",
    )
    result = c.apply_auto_accept_by_name("alice-uuid-v3", "alice")
    assert result["applied"] is True
    assert result["allow"] == ["master-bot"]
    # And: a fresh call with NO file present returns applied=False.
    result_missing = c.apply_auto_accept_by_name("alice-uuid-v3", "no-such-name")
    assert result_missing == {"applied": False, "allow": []}
