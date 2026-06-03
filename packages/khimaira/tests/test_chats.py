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
    # Phase B v1.5 emits a system role-directive on create_room — filter it
    # out to assert on user messages only.
    user_msgs = [m for m in history if m.get("sender_id") != c.SYSTEM_SENDER_ID]
    assert len(user_msgs) == 1
    assert user_msgs[0]["body"] == "hello bob"


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
    # Phase B v1.5: filter system role-directive out to assert on user messages.
    user_msgs = [m for m in history if m.get("sender_id") != c.SYSTEM_SENDER_ID]
    assert len(user_msgs) == 2
    assert user_msgs[0]["sender_id"] == "alice"
    assert user_msgs[1]["sender_id"] == "bob"


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
    assert (
        c._sanitize_message_body("<reasoning>X</reasoning>actual reply")
        == "Xactual reply"
    )


def test_send_message_strips_thinking_tags(isolated_chats):
    """End-to-end: a message body with leaked tags lands in JSONL clean."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "alice")
    _make_session(sessions_mod, "bob")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    msg = c.send_message(
        chat_id, "alice", "Reply: <thinking>x</thinking>actual content"
    )
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
        r.get("kind") == "member"
        and r.get("state") == "pending"
        and r.get("session_id") == "bob"
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
        r.get("kind") == "member"
        and r.get("state") == "pending"
        and r.get("session_id") == "bob"
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
    assert any(
        r.get("kind") == "member" and r.get("state") == "pending" for r in received
    )
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
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
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


def test_create_task_requires_master(isolated_chats):
    """B-M4: non-master accepted member cannot create tasks — mirrors signal_start."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    # bob is accepted but not master — should be rejected
    with pytest.raises(ValueError, match="not the master"):
        c.create_task(chat_id, "bob-uuid", "do thing")


def test_create_task_master_succeeds(isolated_chats):
    """B-M4: master can create tasks as before."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(chat_id, "alice-uuid", "do thing")
    assert task["status"] == c.TASK_PENDING


def test_task_status_lifecycle_happy_path(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
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
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
    with pytest.raises(ValueError, match="not authorized"):
        c.update_task_status(chat_id, task["id"], "carol-uuid", c.TASK_IN_PROGRESS)


def test_task_non_master_cannot_approve(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
    c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_IN_PROGRESS)
    c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_DONE)
    with pytest.raises(ValueError, match="not authorized"):
        c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_APPROVED)


def test_task_changes_requested_can_resume(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
    tid = task["id"]
    c.update_task_status(chat_id, tid, "bob-uuid", c.TASK_IN_PROGRESS)
    c.update_task_status(chat_id, tid, "bob-uuid", c.TASK_DONE)
    c.update_task_status(
        chat_id, tid, "alice-uuid", c.TASK_CHANGES_REQUESTED, note="redo X"
    )
    c.update_task_status(chat_id, tid, "bob-uuid", c.TASK_IN_PROGRESS)
    assert c.task_status(chat_id, "alice-uuid")[0]["status"] == c.TASK_IN_PROGRESS


def test_task_invalid_transition_raises(isolated_chats):
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
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


def test_apply_auto_accept_by_name_returns_applied_true_when_file_exists(
    isolated_chats,
):
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


# ---------------------------------------------------------------------------
# Phase B v1.2: transfer_membership
# ---------------------------------------------------------------------------


def test_transfer_membership_round_trip_happy_path(isolated_chats):
    """Bob transfers his chat membership to Dave; Carol (the other accepted
    member) sees the system message in her chat_history. Confirms the
    state transitions on both sides, the shared transfer_id correlation,
    and the load-bearing `invited_by` field that lets chat_my_chats
    surface the inherited chat for Dave without special-casing."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "carol", "carol")
    _make_session(sessions_mod, "dave", "dave")

    room = c.create_room("alice", ["bob", "carol"], title="ops")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")
    c.accept(chat_id, "carol")

    result = c.transfer_membership(chat_id, "bob", "dave")
    assert result["transfer_id"].startswith("xfer-")
    assert result["from"]["state"] == c.TRANSFERRED_OUT
    assert result["from"]["transferred_to"] == "dave"
    assert result["to"]["state"] == c.ACCEPTED
    assert result["to"]["transferred_from"] == "bob"
    assert result["from"]["transfer_id"] == result["to"]["transfer_id"]
    # invited_by on the new member is bob — chat_my_chats can surface the
    # inherited chat for dave without a special handler for transfers.
    assert result["to"]["invited_by"] == "bob"

    room = c.load_room(chat_id)
    assert room["members"]["bob"]["state"] == c.TRANSFERRED_OUT
    assert room["members"]["dave"]["state"] == c.ACCEPTED
    # Carol — the third party — sees the system message in chat_history.
    # Phase B v1.5 also emits a role-directive on create_room; filter by
    # meta.event_type to isolate the transfer system message specifically.
    history = c.history(chat_id, "carol")
    transfer_msg = [
        m
        for m in history
        if m.get("sender_id") == c.SYSTEM_SENDER_ID
        and (m.get("meta") or {}).get("event_type") == "transfer"
    ]
    assert len(transfer_msg) == 1
    assert "transferred this chat" in transfer_msg[0]["body"]
    assert transfer_msg[0]["meta"]["event_type"] == "transfer"
    assert transfer_msg[0]["meta"]["transfer_id"] == result["transfer_id"]


def test_transfer_membership_requires_accepted_source(isolated_chats):
    """A pending member can't transfer — they have nothing to hand off."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "dave", "dave")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    # bob is still PENDING — hasn't accepted yet.

    with pytest.raises(ValueError, match="only accepted members"):
        c.transfer_membership(chat_id, "bob", "dave")


def test_transfer_membership_readable_to_recipient_immediately(isolated_chats):
    """Dave can call chat_history right after transfer and see the FULL
    transcript (alice's earlier message + the system transfer message),
    not just messages from his accepted-at timestamp forward. The
    transferred-in state must grant the same history-read rights as a
    normally-accepted member."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "dave", "dave")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")
    c.send_message(chat_id, "alice", "early message from alice")
    c.send_message(chat_id, "bob", "early reply from bob")

    c.transfer_membership(chat_id, "bob", "dave")

    # Dave reads history — should see the two pre-transfer messages plus
    # the system transfer message.
    history = c.history(chat_id, "dave")
    bodies = [m["body"] for m in history]
    assert "early message from alice" in bodies
    assert "early reply from bob" in bodies
    assert any("transferred this chat" in b for b in bodies)


def test_transfer_membership_duplicate_target_raises(isolated_chats):
    """If the receiving session is already an accepted member, transfer
    must raise (409 case at the HTTP layer) — silently demoting an
    existing member would lose state."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "carol", "carol")

    room = c.create_room("alice", ["bob", "carol"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")
    c.accept(chat_id, "carol")

    with pytest.raises(ValueError, match="already accepted"):
        c.transfer_membership(chat_id, "bob", "carol")


# Phase B v1.3 Lane E: creator/master role propagation on transfer.
# Surfaced when khimaira-21 → khimaira-0 transfer left the successor with
# chat membership but `room.meta.created_by` still pinned to khimaira-21,
# so chat_task_update done→approved 404'd with Required roles: ['master'].


def test_transfer_membership_propagates_creator_role(isolated_chats):
    """When the chat creator transfers their membership, `room.meta.created_by`
    must also update to the recipient. Without this, the successor inherits
    membership but cannot exercise master-gated primitives (chat_task_update
    done→approved, chat_delete) — the chat is effectively orphaned of its
    master role.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "carol", "carol")

    room = c.create_room("alice", ["bob"], title="ops")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    # Pre-transfer: alice is the creator/master.
    assert room["meta"]["created_by"] == "alice"
    assert room["meta"]["created_by_name"] == "alice"

    c.transfer_membership(chat_id, "alice", "carol")

    fresh_room = c.load_room(chat_id)
    assert (
        fresh_room["meta"]["created_by"] == "carol"
    ), "master role must transfer with membership when the source is the creator"
    assert fresh_room["meta"]["created_by_name"] == "carol"
    # Sanity: member-state transitions still happen normally.
    assert fresh_room["members"]["alice"]["state"] == c.TRANSFERRED_OUT
    assert fresh_room["members"]["carol"]["state"] == c.ACCEPTED


def test_transfer_membership_non_creator_preserves_meta_created_by(isolated_chats):
    """When a non-creator transfers their membership, `room.meta.created_by`
    stays pinned to the original creator. Lane E only propagates the master
    role when the transferring session IS the creator — non-creator
    transfers must not silently steal the master role.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "dave", "dave")

    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    # bob is NOT the creator; his transfer must leave meta.created_by alone.
    c.transfer_membership(chat_id, "bob", "dave")

    fresh_room = c.load_room(chat_id)
    assert fresh_room["meta"]["created_by"] == "alice"
    assert fresh_room["meta"]["created_by_name"] == "alice"


# ---------------------------------------------------------------------------
# Phase B v1.2: master-signal-to-start primitive (task_signal records)
# ---------------------------------------------------------------------------


def test_signal_task_start_records_signal(isolated_chats):
    """Master signals a pending task; record persists with signal=start."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
    rec = c.signal_task_start(
        chat_id, task["id"], "alice-uuid", note="cleared to start"
    )
    assert rec["kind"] == c.TASK_SIGNAL
    assert rec["signal"] == "start"
    assert rec["task_id"] == task["id"]
    assert rec["by_session_id"] == "alice-uuid"
    assert rec["note"] == "cleared to start"
    # assignee_id carried on the signal so _route_record can dispatch without re-folding
    assert rec["assignee_id"] == "bob-uuid"

    # Verify the record landed in the JSONL (round-trip via _read).
    lines = c._read(chat_id)
    signal_lines = [ln for ln in lines if ln.get("kind") == c.TASK_SIGNAL]
    assert len(signal_lines) == 1
    assert signal_lines[0]["task_id"] == task["id"]


def test_signal_task_start_requires_master(isolated_chats):
    """Non-creator accepted members cannot signal start — master-only gate."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    _make_session(sessions_mod, "carol-uuid", "carol")
    c.create_room("alice-uuid", ["bob-uuid", "carol-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.accept(chat_id, "carol-uuid")
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
    # Carol is accepted but isn't the master — must be rejected.
    with pytest.raises(ValueError, match="not the master"):
        c.signal_task_start(chat_id, task["id"], "carol-uuid")


def test_signal_task_start_rejects_non_pending(isolated_chats):
    """Signal is only valid on pending tasks; once the assignee picks it up
    (in_progress), signaling is a no-op semantically and should raise."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_two_member_chat(c, sessions_mod)
    task = c.create_task(
        chat_id, "alice-uuid", "do thing", assignee_session_id="bob-uuid"
    )
    c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_IN_PROGRESS)
    with pytest.raises(ValueError, match="not 'pending'"):
        c.signal_task_start(chat_id, task["id"], "alice-uuid")


# ---------------------------------------------------------------------------
# Phase B v2 Lane V2: master-leave guard (Piece A) + chat_set_creator (Piece B)
# ---------------------------------------------------------------------------


def test_master_cannot_leave_directly(isolated_chats):
    """Piece A: chat_leave refuses for the current master. Closes the v1
    footgun where a creator's chat_leave made done→approved unreachable."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Alice (creator → implicit master via v1 fallback) tries to leave → refused.
    with pytest.raises(ValueError, match="master of"):
        c.leave(chat_id, "alice-uuid")

    # Bob (non-master) can still leave normally.
    rec = c.leave(chat_id, "bob-uuid")
    assert rec["state"] == c.LEFT


def test_set_creator_unlocks_orphaned_by_transfer(isolated_chats):
    """Piece B: chat_set_creator re-anchors master on a chat whose creator
    is TRANSFERRED_OUT. The exact dogfood failure from this session's v1.2
    miss: a transfer happened pre-v1.3, leaving no surviving master to
    approve tasks."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    _make_session(sessions_mod, "carol-uuid", "carol")
    c.create_room("alice-uuid", ["bob-uuid", "carol-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.accept(chat_id, "carol-uuid")

    # Pre-v1.3-style transfer: Alice (creator) transfers her membership to a
    # NEW session "dave-uuid" but the old transfer_membership did NOT
    # propagate created_by — simulate that gap. (Real v1.3 fixes this in
    # transfer_membership; here we manually wedge the chat into the broken
    # state to test the recovery primitive.)
    _make_session(sessions_mod, "dave-uuid", "dave")
    c.transfer_membership(chat_id, "alice-uuid", "dave-uuid")
    # After v1.3 transfer, created_by has been updated to dave. To simulate
    # the pre-v1.3 broken state, we forcibly re-emit a META record putting
    # created_by back on alice (who is now TRANSFERRED_OUT).
    room = c.load_room(chat_id)
    broken_meta = {
        **{k: v for k, v in room["meta"].items() if k != "event_id"},
        "kind": c.META,
        "event_id": c._new_event_id(),
        "ts": c._now_iso(),
        "created_by": "alice-uuid",
        "created_by_name": "alice",
    }
    c._append(chat_id, broken_meta)

    # Confirm the orphan: alice is TRANSFERRED_OUT but still created_by.
    room = c.load_room(chat_id)
    assert room["meta"]["created_by"] == "alice-uuid"
    assert room["members"]["alice-uuid"]["state"] == c.TRANSFERRED_OUT

    # Bob (accepted member) calls set_creator to claim master.
    new_meta = c.set_creator(chat_id, "bob-uuid")
    assert new_meta["created_by"] == "bob-uuid"
    assert new_meta["created_by_name"] == "bob"
    assert new_meta["member_roles"]["bob-uuid"] == "master"

    # Re-load and confirm the new state is canonical.
    room = c.load_room(chat_id)
    assert room["meta"]["created_by"] == "bob-uuid"
    assert c._is_master(room, "bob-uuid") is True
    assert c._is_master(room, "alice-uuid") is False


def test_set_creator_refuses_when_creator_still_accepted(isolated_chats):
    """Piece B: chat_set_creator is reserved for orphaned-by-transfer.
    A still-accepted creator must use chat_grant_role (V1) instead — set_creator
    raises to prevent silently overriding an active master."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Alice (creator) is still ACCEPTED — bob trying set_creator must fail.
    with pytest.raises(ValueError, match="not 'transferred-out'"):
        c.set_creator(chat_id, "bob-uuid")


def test_set_creator_rejects_non_member_target(isolated_chats):
    """Piece B: the new creator must be an accepted member of the chat."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    _make_session(sessions_mod, "carol-uuid", "carol")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Orphan the chat first.
    _make_session(sessions_mod, "dave-uuid", "dave")
    c.transfer_membership(chat_id, "alice-uuid", "dave-uuid")
    # Simulate the pre-v1.3 broken state again.
    room = c.load_room(chat_id)
    broken_meta = {
        **{k: v for k, v in room["meta"].items() if k != "event_id"},
        "kind": c.META,
        "event_id": c._new_event_id(),
        "ts": c._now_iso(),
        "created_by": "alice-uuid",
        "created_by_name": "alice",
    }
    c._append(chat_id, broken_meta)

    # Carol is not a member of this chat — set_creator must refuse her.
    with pytest.raises(ValueError, match="non-member"):
        c.set_creator(chat_id, "carol-uuid")


# ---------------------------------------------------------------------------
# Phase B v2: chat_grant_role + member_roles + _is_master
# ---------------------------------------------------------------------------


def _setup_v1_chat(c, sessions_mod, creator: str, *members: str) -> str:
    """v1-style chat: creator + accepted members, no explicit member_roles."""
    _make_session(sessions_mod, creator, creator)
    for m in members:
        _make_session(sessions_mod, m, m)
    room = c.create_room(creator, list(members), title="t")
    chat_id = room["meta"]["chat_id"]
    for m in members:
        c.accept(chat_id, m)
    return chat_id


def test_grant_role_master_only(isolated_chats):
    """Non-master accepted members cannot grant roles."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob", "carol")
    # bob is not master; can't grant any role
    with pytest.raises(ValueError, match="only the master"):
        c.chat_grant_role(chat_id, "bob", "carol", c.ROLE_OBSERVER)


def test_grant_role_promotes_and_demotes_atomically(isolated_chats):
    """Promoting B to master atomically demotes the previous master to
    `demote_to` (default agent). Single META write captures both."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob")

    result = c.chat_grant_role(chat_id, "alice", "bob", c.ROLE_MASTER)

    roles = result["member_roles"]
    assert roles["bob"] == c.ROLE_MASTER
    assert roles["alice"] == c.ROLE_AGENT
    # _is_master agrees with the dict
    room = c.load_room(chat_id)
    assert c._is_master(room, "bob") is True
    assert c._is_master(room, "alice") is False


def test_grant_role_demote_to_observer_kwarg(isolated_chats):
    """The granting master can specify what role the outgoing master
    becomes — `demote_to="observer"` means the prior master loses both
    master rights AND send rights (Lane V2 enforcement)."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob")

    result = c.chat_grant_role(
        chat_id, "alice", "bob", c.ROLE_MASTER, demote_to=c.ROLE_OBSERVER
    )

    roles = result["member_roles"]
    assert roles["bob"] == c.ROLE_MASTER
    assert roles["alice"] == c.ROLE_OBSERVER


def test_load_room_backward_compat_synthesizes_master(isolated_chats):
    """A v1-era chat (no member_roles in META) resolves master via
    `_is_master`'s fallback to `created_by`."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob")
    room = c.load_room(chat_id)

    # Pre-condition: no explicit member_roles in META.
    assert "member_roles" not in room["meta"]
    # _is_master uses the fallback path.
    assert c._is_master(room, "alice") is True
    assert c._is_master(room, "bob") is False


def test_chat_task_update_uses_member_roles_gate(isolated_chats):
    """After granting B master (demoting A), B can approve tasks and A
    cannot. Master gate now reads `member_roles`, not the static
    `created_by`."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob")

    c.chat_grant_role(chat_id, "alice", "bob", c.ROLE_MASTER)

    task = c.create_task(chat_id, "bob", "do thing", assignee_session_id="alice")
    c.update_task_status(chat_id, task["id"], "alice", c.TASK_IN_PROGRESS)
    c.update_task_status(chat_id, task["id"], "alice", c.TASK_DONE)

    # Alice (former master, now agent) cannot approve.
    with pytest.raises(ValueError, match="not authorized"):
        c.update_task_status(chat_id, task["id"], "alice", c.TASK_APPROVED)
    # Bob (new master) can approve.
    c.update_task_status(chat_id, task["id"], "bob", c.TASK_APPROVED)
    assert c.task_status(chat_id, "bob")[0]["status"] == c.TASK_APPROVED


def test_grant_role_materializes_implicit_master_on_first_call(isolated_chats):
    """First `chat_grant_role` on a v1-era chat materializes the implicit
    master into member_roles BEFORE applying the grant. Subsequent reads
    use the explicit dict as sole source of truth — no implicit/explicit
    duality."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob", "carol")
    # Alice grants Carol observer — a non-master grant.
    result = c.chat_grant_role(chat_id, "alice", "carol", c.ROLE_OBSERVER)

    # Both alice's implicit master AND carol's new observer are in the dict.
    roles = result["member_roles"]
    assert roles == {"alice": c.ROLE_MASTER, "carol": c.ROLE_OBSERVER}
    # bob is unmentioned (no explicit role assigned).
    assert "bob" not in roles


def test_transfer_membership_propagates_role_dict(isolated_chats):
    """v1.3 Lane E fix (creator-transfer propagates created_by) — when
    member_roles is already explicit, the transfer must also demote the
    old creator and promote the new one in the same META write."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob")
    _make_session(sessions_mod, "dave", "dave")

    # Materialize member_roles via an explicit grant first (any grant works).
    c.chat_grant_role(chat_id, "alice", "bob", c.ROLE_AGENT)
    # Pre-transfer: alice is master per explicit dict.
    room = c.load_room(chat_id)
    assert room["meta"]["member_roles"].get("alice") == c.ROLE_MASTER

    # Now alice (creator + master) transfers her membership to dave.
    c.transfer_membership(chat_id, "alice", "dave")
    room = c.load_room(chat_id)

    # v1.3 invariant preserved.
    assert room["meta"]["created_by"] == "dave"
    # v2 invariant: member_roles updated atomically.
    assert room["meta"]["member_roles"]["dave"] == c.ROLE_MASTER
    assert room["meta"]["member_roles"]["alice"] == c.ROLE_AGENT
    # _is_master agrees.
    assert c._is_master(room, "dave") is True
    assert c._is_master(room, "alice") is False


# ---------------------------------------------------------------------------
# Phase B v2 Lane V2: observer enforcement (Piece C) + critic surface (Piece D)
# ---------------------------------------------------------------------------


def test_observer_cannot_send_message(isolated_chats):
    """Piece C: observers can read everything but cannot send messages."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Alice (master) grants Bob the observer role.
    c.chat_grant_role(chat_id, "alice-uuid", "bob-uuid", c.ROLE_OBSERVER)

    # Bob sends → refused with the observer-specific error.
    with pytest.raises(ValueError, match="observer"):
        c.send_message(chat_id, "bob-uuid", "hi")


def test_observer_cannot_create_or_update_tasks(isolated_chats):
    """Piece C: observers cannot create_task or update_task_status. All
    write paths in the task lifecycle are closed to observers."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.chat_grant_role(chat_id, "alice-uuid", "bob-uuid", c.ROLE_OBSERVER)

    # Observer cannot create tasks.
    with pytest.raises(ValueError, match="observer"):
        c.create_task(chat_id, "bob-uuid", "some work")

    # Observer cannot update task status — even for tasks they're notionally
    # assigned to (the grant pre-dates the role demotion in this contrived
    # scenario, but the observer gate is the load-bearing check).
    task = c.create_task(
        chat_id, "alice-uuid", "work", assignee_session_id="alice-uuid"
    )
    with pytest.raises(ValueError, match="observer"):
        c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_IN_PROGRESS)


def test_observer_can_still_read_history(isolated_chats):
    """Piece C: observer's write paths are closed but read paths stay open.
    The point of the role is audit-style visibility — closing reads would
    defeat it."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    # Alice sends something before Bob becomes an observer.
    c.send_message(chat_id, "alice-uuid", "pre-observer broadcast")
    c.chat_grant_role(chat_id, "alice-uuid", "bob-uuid", c.ROLE_OBSERVER)

    # Bob (observer) reads — succeeds, sees Alice's message.
    msgs = c.history(chat_id, "bob-uuid")
    assert any(m.get("body") == "pre-observer broadcast" for m in msgs)


def test_critic_can_send_and_read_but_not_approve(isolated_chats):
    """Piece D: critic role is opinion-only — no write-path restriction
    beyond agent (can send, can create tasks, can read), but the master gate
    on done→approved still applies (critic ≠ master). The SPR-4 'critic is
    judge-not-king' precedent at chat level."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.chat_grant_role(chat_id, "alice-uuid", "bob-uuid", c.ROLE_CRITIC)

    # Critic can send free-form messages (opinion).
    msg = c.send_message(chat_id, "bob-uuid", "I think this needs more polish")
    assert msg["body"] == "I think this needs more polish"

    # Critic can read history.
    history = c.history(chat_id, "bob-uuid")
    assert any(m.get("body") == "I think this needs more polish" for m in history)

    # Critic CANNOT approve a task — master gate still applies.
    task = c.create_task(
        chat_id, "alice-uuid", "work", assignee_session_id="alice-uuid"
    )
    c.update_task_status(chat_id, task["id"], "alice-uuid", c.TASK_IN_PROGRESS)
    c.update_task_status(chat_id, task["id"], "alice-uuid", c.TASK_DONE)
    with pytest.raises(ValueError, match="not authorized"):
        c.update_task_status(chat_id, task["id"], "bob-uuid", c.TASK_APPROVED)


# ---------------------------------------------------------------------------
# Phase B v1.5: role-grant directive emit
# ---------------------------------------------------------------------------


def _directives(c, chat_id: str) -> list[dict]:
    """Return all role_directive system msg records in the chat's JSONL."""
    return [
        line
        for line in c._read(chat_id)
        if line.get("kind") == c.MSG
        and line.get("sender_id") == c.SYSTEM_SENDER_ID
        and (line.get("meta") or {}).get("event_type") == "role_directive"
    ]


def test_chat_create_room_emits_master_directive_to_creator(isolated_chats):
    """Creating a chat fires a 🎚️ directive to the creator with master-tier
    budget. v1.5 application-gap fix: tells Joseph which slash commands to
    type at the moment the role is granted (implicit-master via created_by)."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    room = c.create_room("alice-uuid", [], title="t")
    chat_id = room["meta"]["chat_id"]

    directives = _directives(c, chat_id)
    assert len(directives) == 1
    d = directives[0]
    assert d["to"] == ["alice-uuid"]
    assert d["meta"]["role"] == c.ROLE_MASTER
    assert d["meta"]["model"] == "opus"
    assert d["meta"]["effort"] == "max"
    assert "🎚️ Role updated: you are now master" in d["body"]
    assert "/model opus" in d["body"]
    assert "/effort max" in d["body"]


def test_chat_grant_role_emits_directive_to_target(isolated_chats):
    """Granting an agent role to a member fires a directive to that
    member with the agent-tier budget."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob")

    # Filter to directives emitted AFTER the create-room directive.
    pre_count = len(_directives(c, chat_id))
    c.chat_grant_role(chat_id, "alice", "bob", c.ROLE_AGENT)
    post = _directives(c, chat_id)
    assert len(post) == pre_count + 1

    d = post[-1]
    assert d["to"] == ["bob"]
    assert d["meta"]["role"] == c.ROLE_AGENT
    assert d["meta"]["model"] == "sonnet"
    assert d["meta"]["effort"] == "medium"
    assert "you are now agent" in d["body"]


def test_chat_grant_role_master_swap_emits_two_directives(isolated_chats):
    """Promoting B to master atomically demotes A (the implicit creator-
    master). Two directives fire in the same call: one to B (new master,
    opus/max), one to A (demoted, default agent tier sonnet/medium)."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob")

    pre = _directives(c, chat_id)
    c.chat_grant_role(chat_id, "alice", "bob", c.ROLE_MASTER)
    post = _directives(c, chat_id)
    new_directives = post[len(pre) :]
    assert len(new_directives) == 2

    by_target = {d["to"][0]: d for d in new_directives}
    assert by_target["bob"]["meta"]["role"] == c.ROLE_MASTER
    assert by_target["bob"]["meta"]["model"] == "opus"
    assert by_target["alice"]["meta"]["role"] == c.ROLE_AGENT
    assert by_target["alice"]["meta"]["model"] == "sonnet"


def test_chat_grant_role_critic_emits_no_directive(isolated_chats):
    """Critic role is intentionally absent from ROLE_BUDGET — no default
    slash-command recommendation exists. Helper silent-skips; the role
    grant still lands in member_roles META, but no 🎚️ directive fires.
    The orchestrator can follow up with explicit guidance if needed."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    chat_id = _setup_v1_chat(c, sessions_mod, "alice", "bob")

    pre_count = len(_directives(c, chat_id))
    result = c.chat_grant_role(chat_id, "alice", "bob", c.ROLE_CRITIC)
    # member_roles still updates — critic IS a valid role
    assert result["member_roles"]["bob"] == c.ROLE_CRITIC
    # but no directive emit
    assert len(_directives(c, chat_id)) == pre_count


# ---------------------------------------------------------------------------
# Phase B v1.5 L2: directive emits on chat_set_creator + chat_transfer_membership
# ---------------------------------------------------------------------------


def test_set_creator_emits_master_directive(isolated_chats):
    """Re-anchoring master via chat_set_creator on an orphaned chat fires a
    🎚️ directive to the new creator with master-tier budget. Closes the
    application-gap arc: the recipient knows their slash commands at the
    moment they inherit master."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    _make_session(sessions_mod, "carol-uuid", "carol")
    c.create_room("alice-uuid", ["bob-uuid", "carol-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.accept(chat_id, "carol-uuid")

    # Orphan the chat: alice transfers to dave (post-v1.3 propagates created_by),
    # then we force created_by back to alice (simulating the pre-v1.3 broken state
    # set_creator exists to repair). alice is now TRANSFERRED_OUT.
    _make_session(sessions_mod, "dave-uuid", "dave")
    c.transfer_membership(chat_id, "alice-uuid", "dave-uuid")
    room = c.load_room(chat_id)
    broken_meta = {
        **{k: v for k, v in room["meta"].items() if k != "event_id"},
        "kind": c.META,
        "event_id": c._new_event_id(),
        "ts": c._now_iso(),
        "created_by": "alice-uuid",
        "created_by_name": "alice",
    }
    c._append(chat_id, broken_meta)

    pre_count = len(_directives(c, chat_id))
    c.set_creator(chat_id, "bob-uuid")
    post = _directives(c, chat_id)
    assert (
        len(post) == pre_count + 1
    ), "set_creator should emit exactly one master directive"

    d = post[-1]
    assert d["to"] == ["bob-uuid"]
    assert d["meta"]["role"] == c.ROLE_MASTER
    assert d["meta"]["model"] == "opus"
    assert d["meta"]["effort"] == "max"
    assert "🎚️ Role updated: you are now master" in d["body"]
    assert "/model opus" in d["body"]
    assert "/effort max" in d["body"]


def test_transfer_membership_master_swap_emits_directive(isolated_chats):
    """When the chat creator transfers their membership, the receiving session
    inherits master role — emit a directive to them so they know to switch to
    master-tier slash commands. Pairs with the v1.3 META created_by swap
    (Lane E) — that test asserts on the META update; this one asserts on the
    directive that the v1.5 layer adds."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Alice (creator/master) transfers to a fresh dave.
    _make_session(sessions_mod, "dave-uuid", "dave")
    pre_count = len(_directives(c, chat_id))
    c.transfer_membership(chat_id, "alice-uuid", "dave-uuid")
    post = _directives(c, chat_id)
    assert (
        len(post) == pre_count + 1
    ), "master-transfer should emit exactly one directive"

    d = post[-1]
    assert d["to"] == ["dave-uuid"]
    assert d["meta"]["role"] == c.ROLE_MASTER
    assert "you are now master" in d["body"]
    assert "/model opus" in d["body"]


def test_transfer_membership_non_master_emits_no_directive(isolated_chats):
    """Non-creator membership transfers should NOT fire role directives —
    only the existing 📦 transfer system message. Bob is a regular member,
    not the master; transferring his seat to dave shouldn't fabricate a
    role-grant event."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = c.my_chats("alice-uuid")[0]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Bob (non-creator) transfers his seat to dave.
    _make_session(sessions_mod, "dave-uuid", "dave")
    pre_count = len(_directives(c, chat_id))
    c.transfer_membership(chat_id, "bob-uuid", "dave-uuid")
    assert (
        len(_directives(c, chat_id)) == pre_count
    ), "non-master transfer must NOT emit a role directive"


# ---------------------------------------------------------------------------
# Phase B v1.6: chat_resume_master + as_deputize kwarg + find_chats_deputized_by
# ---------------------------------------------------------------------------


def test_transfer_membership_as_deputize_sets_meta_field(isolated_chats):
    """v1.6 kwarg path: `as_deputize=True` on a creator-transfer writes
    `meta.deputized_original_master = from_session_id` atomically with
    the master-role swap."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "vice", "vice")
    room = c.create_room("alice", ["bob"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    c.transfer_membership(chat_id, "alice", "vice", as_deputize=True)

    room = c.load_room(chat_id)
    assert room["meta"]["deputized_original_master"] == "alice"
    assert room["meta"]["created_by"] == "vice"
    assert room["meta"]["member_roles"]["vice"] == c.ROLE_MASTER


def test_chat_resume_master_clears_meta_and_swaps_roles(isolated_chats):
    """Happy path: deputize via kwarg → resume primitive → field cleared,
    member_roles swapped, created_by restored to donor.

    Phase B v1.6 LOCK v3 Decision 10: also asserts donor's MEMBER state
    stays ACCEPTED throughout the deputize→resume cycle. The bug this
    caught: pre-LOCK-v3, donor was marked TRANSFERRED_OUT on deputize,
    breaking chat_send/broadcast post-resume even though member_roles
    correctly restored master. State-surfaces test coverage rule: assert
    on ALL state surfaces the primitive touches, not just the primary one.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "vice", "vice")
    room = c.create_room("alice", ["bob"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    c.transfer_membership(chat_id, "alice", "vice", as_deputize=True)
    pre = c.load_room(chat_id)
    assert pre["meta"]["deputized_original_master"] == "alice"
    # LOCK v3 D10: donor stays ACCEPTED during the pause.
    assert pre["members"]["alice"]["state"] == c.ACCEPTED
    assert pre["members"]["vice"]["state"] == c.ACCEPTED

    c.chat_resume_master(chat_id, "alice")

    post = c.load_room(chat_id)
    assert post["meta"].get("deputized_original_master") is None
    assert post["meta"]["member_roles"]["alice"] == c.ROLE_MASTER
    assert post["meta"]["member_roles"]["vice"] == c.ROLE_AGENT
    assert post["meta"]["created_by"] == "alice"
    # Post-resume: donor + vice both still ACCEPTED. Donor can chat_send.
    assert post["members"]["alice"]["state"] == c.ACCEPTED
    assert post["members"]["vice"]["state"] == c.ACCEPTED


def test_chat_resume_master_rejects_non_original_master(isolated_chats):
    """Authority check: only the recorded `deputized_original_master`
    can resume. Field-value-based gating per LOCK v2 Decision 1."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "vice", "vice")
    room = c.create_room("alice", ["bob"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")
    c.transfer_membership(chat_id, "alice", "vice", as_deputize=True)

    with pytest.raises(ValueError, match="not the original master"):
        c.chat_resume_master(chat_id, "bob")
    with pytest.raises(ValueError, match="not the original master"):
        c.chat_resume_master(chat_id, "vice")


def test_chat_resume_master_rejects_when_not_deputized(isolated_chats):
    """Chat without `deputized_original_master` → reject."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    room = c.create_room("alice", ["bob"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    with pytest.raises(ValueError, match="not in deputize mode"):
        c.chat_resume_master(chat_id, "alice")


def test_find_chats_deputized_by_returns_only_donors_chats(isolated_chats):
    """Filter precision: returns ONLY chats where caller is the recorded
    original master."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    for sid in ("alice", "bob", "carol", "vice1", "vice2"):
        _make_session(sessions_mod, sid, sid)

    r1 = c.create_room("alice", ["bob"], title="A1")
    chat_a1 = r1["meta"]["chat_id"]
    c.accept(chat_a1, "bob")
    c.transfer_membership(chat_a1, "alice", "vice1", as_deputize=True)

    r2 = c.create_room("alice", ["carol"], title="A2")
    chat_a2 = r2["meta"]["chat_id"]
    c.accept(chat_a2, "carol")
    c.transfer_membership(chat_a2, "alice", "vice2", as_deputize=True)

    r3 = c.create_room("alice", ["bob"], title="A3", fresh=True)
    chat_a3 = r3["meta"]["chat_id"]
    c.accept(chat_a3, "bob")

    r4 = c.create_room("bob", ["carol"], title="B1")
    chat_b1 = r4["meta"]["chat_id"]
    c.accept(chat_b1, "carol")
    c.transfer_membership(chat_b1, "bob", "vice1", as_deputize=True)

    alice_deputized = set(c.find_chats_deputized_by("alice"))
    assert alice_deputized == {chat_a1, chat_a2}
    assert chat_a3 not in alice_deputized
    assert chat_b1 not in alice_deputized
    assert c.find_chats_deputized_by("bob") == [chat_b1]
    assert c.find_chats_deputized_by("carol") == []


# ---------------------------------------------------------------------------
# Phase B v1.6 L4: integration / round-trip / composition tests for deputize
# ---------------------------------------------------------------------------
# These tests sit on top of L2's primitive-level coverage (test_*_as_deputize_*,
# test_chat_resume_master_*, test_find_chats_deputized_by_*). They exercise
# state surfaces that primitive-level tests have a structural blind spot for —
# per the N-state-surfaces principle banked in Round 10: when a primitive
# writes to N surfaces (META, MEMBER, sys_msg, directive emit, send rights),
# tests must assert on ALL N, not just the primary one. L4-level coverage
# catches composition gaps that show up only at integration time.


def test_transfer_membership_as_deputize_keeps_donor_accepted(isolated_chats):
    """LOCK v3 Decision 10 primary surface coverage: `as_deputize=True`
    must NOT flip donor to TRANSFERRED_OUT. Donor stays ACCEPTED throughout
    the pause so chat_send remains valid post-resume.

    Pre-LOCK-v3, the kwarg used the same out_record write as a regular
    terminal transfer — donor went to TRANSFERRED_OUT on deputize. The
    resume primitive correctly restored master in member_roles but didn't
    touch MEMBER state, so post-resume the donor held master role yet
    couldn't chat_send (send_message gates on state == ACCEPTED).

    test-agent's `test_chat_resume_master_clears_meta_and_swaps_roles`
    asserts donor.state == ACCEPTED via the round-trip; this test pins
    the same contract directly on the transfer-side primitive — focused
    coverage on the kwarg's MEMBER-state effect.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "vice", "vice")

    room = c.create_room("alice", ["bob"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    c.transfer_membership(chat_id, "alice", "vice", as_deputize=True)

    fresh = c.load_room(chat_id)
    assert (
        fresh["members"]["alice"]["state"] == c.ACCEPTED
    ), "as_deputize=True must preserve donor's ACCEPTED state (LOCK v3 D10)"
    assert fresh["members"]["vice"]["state"] == c.ACCEPTED
    # Sanity: the marker still landed (kwarg's primary META effect).
    assert fresh["meta"]["deputized_original_master"] == "alice"
    # Donor can still chat_send during the pause (the actual feature
    # concern Decision 10 protects).
    sent = c.send_message(chat_id, "alice", "still here, just paused")
    assert sent["body"] == "still here, just paused"


def test_transfer_membership_default_does_not_set_meta_field(isolated_chats):
    """Defensive: the deputize marker must NOT land on regular (non-deputize)
    transfers. Pins the kwarg's gating from the opposite direction —
    test-agent's `test_transfer_membership_as_deputize_sets_meta_field`
    asserts the marker IS written when asked; this asserts the marker is
    NOT written when not asked, preventing a future refactor from
    accidentally writing the field unconditionally and breaking the v1.2
    terminal-handoff semantic.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "dave", "dave")

    room = c.create_room("alice", ["bob"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    # Default invocation: kwarg omitted (as_deputize=False implicit).
    c.transfer_membership(chat_id, "alice", "dave")

    fresh = c.load_room(chat_id)
    assert (
        "deputized_original_master" not in fresh["meta"]
    ), "regular transfer (as_deputize=False) must not write the deputize marker"
    # Sanity: regular terminal transfer still flips donor to TRANSFERRED_OUT.
    # The kwarg's two effects (marker + skip-out) are both gated; without
    # the kwarg, neither fires.
    assert fresh["members"]["alice"]["state"] == c.TRANSFERRED_OUT


def test_deputize_resume_round_trip_single_chat_full_state(isolated_chats):
    """Integration round-trip with full state-surface assertions.

    Per the N-state-surfaces principle: a primitive that touches META,
    MEMBER, sys_msg, role_directive emit, AND send-rights needs assertions
    on ALL FIVE at each round-trip checkpoint. This test pins the v1.6
    deputize→resume cycle's full contract in one place:

    - donor + vice MEMBER state at create / mid-deputize / post-resume
    - meta.deputized_original_master round-trip (set, then cleared)
    - meta.created_by swap (donor → vice → donor)
    - meta.member_roles symmetric atomic swap
    - role_directive emit count + targets at each phase (3 phases × distinct counts)
    - sys_msg.event_type = "deputize" on the kwarg-flavored transfer
    - donor's chat_send rights restored post-resume (the actual feature concern)
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "vice", "vice")

    room = c.create_room("alice", ["bob"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    # Phase 0 — baseline. One directive (master to creator alice on create_room).
    baseline = _directives(c, chat_id)
    assert len(baseline) == 1
    assert baseline[0]["to"] == ["alice"]
    assert baseline[0]["meta"]["role"] == c.ROLE_MASTER

    # Phase 1 — deputize.
    c.transfer_membership(chat_id, "alice", "vice", as_deputize=True)
    mid = c.load_room(chat_id)

    assert mid["meta"]["deputized_original_master"] == "alice"
    assert mid["meta"]["created_by"] == "vice"
    assert mid["meta"]["member_roles"]["alice"] == c.ROLE_AGENT
    assert mid["meta"]["member_roles"]["vice"] == c.ROLE_MASTER
    assert mid["members"]["alice"]["state"] == c.ACCEPTED  # LOCK v3 D10
    assert mid["members"]["vice"]["state"] == c.ACCEPTED

    # One new directive fired: vice → master (v1.5 creator-transfer emit).
    mid_directives = _directives(c, chat_id)
    assert len(mid_directives) == 2
    assert mid_directives[-1]["to"] == ["vice"]
    assert mid_directives[-1]["meta"]["role"] == c.ROLE_MASTER

    # sys_msg for the deputize uses event_type="deputize" + the
    # pause-and-handoff body text (LOCK v3 D10 bonus refinement).
    deputize_sys = [
        m
        for m in c.history(chat_id, "alice")
        if m.get("sender_id") == c.SYSTEM_SENDER_ID
        and (m.get("meta") or {}).get("event_type") == "deputize"
    ]
    assert len(deputize_sys) == 1
    assert "deputized this chat" in deputize_sys[0]["body"]
    assert "pause-and-handoff" in deputize_sys[0]["body"]

    # Phase 2 — resume.
    c.chat_resume_master(chat_id, "alice")
    post = c.load_room(chat_id)

    assert "deputized_original_master" not in post["meta"]
    assert post["meta"]["created_by"] == "alice"
    assert post["meta"]["member_roles"]["alice"] == c.ROLE_MASTER
    assert post["meta"]["member_roles"]["vice"] == c.ROLE_AGENT
    assert post["members"]["alice"]["state"] == c.ACCEPTED
    assert post["members"]["vice"]["state"] == c.ACCEPTED

    # Two new directives fired: alice → master, vice → agent.
    post_directives = _directives(c, chat_id)
    assert len(post_directives) == 4
    swap_directives = post_directives[2:]
    by_target = {d["to"][0]: d for d in swap_directives}
    assert by_target["alice"]["meta"]["role"] == c.ROLE_MASTER
    assert by_target["alice"]["meta"]["model"] == "opus"
    assert by_target["vice"]["meta"]["role"] == c.ROLE_AGENT
    assert by_target["vice"]["meta"]["model"] == "sonnet"

    # The composition-level concern: donor can chat_send post-resume.
    sent = c.send_message(chat_id, "alice", "back in the saddle")
    assert sent["body"] == "back in the saddle"


def test_deputize_resume_round_trip_multi_chat(isolated_chats):
    """Spec test #4 explicit: donor in N chats, deputize all, resume all,
    no cross-chat state leakage.

    Also stress-tests `find_chats_deputized_by` mid-cycle: after N deputize
    transfers, the helper returns those N; after a partial resume of one,
    the helper returns the remaining N-1. The slash command iterates over
    chats per deputize+resume; this test verifies the per-chat primitive
    composes without cross-chat coupling.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    for sid in ("alice", "bob", "carol", "dave", "vice"):
        _make_session(sessions_mod, sid, sid)

    # Create 3 distinct chats alice is master in (distinct member sets
    # so derive_chat_id doesn't collide).
    chat_ids = []
    for peer, title in (("bob", "ops"), ("carol", "design"), ("dave", "review")):
        r = c.create_room("alice", [peer], title=title)
        cid = r["meta"]["chat_id"]
        c.accept(cid, peer)
        chat_ids.append(cid)

    # Deputize all 3 to the same vice.
    for cid in chat_ids:
        c.transfer_membership(cid, "alice", "vice", as_deputize=True)

    # find_chats_deputized_by returns exactly the 3 deputized chats.
    assert set(c.find_chats_deputized_by("alice")) == set(chat_ids)

    # Each chat has the marker; alice ACCEPTED + agent; vice ACCEPTED + master.
    for cid in chat_ids:
        r = c.load_room(cid)
        assert r["meta"]["deputized_original_master"] == "alice"
        assert r["meta"]["member_roles"]["alice"] == c.ROLE_AGENT
        assert r["meta"]["member_roles"]["vice"] == c.ROLE_MASTER
        assert r["members"]["alice"]["state"] == c.ACCEPTED
        assert r["members"]["vice"]["state"] == c.ACCEPTED

    # Partial resume — just the first chat. Helper reflects in-flight state.
    c.chat_resume_master(chat_ids[0], "alice")
    assert set(c.find_chats_deputized_by("alice")) == set(chat_ids[1:])

    # Resumed chat: clean; donor back to master.
    r0 = c.load_room(chat_ids[0])
    assert "deputized_original_master" not in r0["meta"]
    assert r0["meta"]["member_roles"]["alice"] == c.ROLE_MASTER
    assert r0["meta"]["member_roles"]["vice"] == c.ROLE_AGENT
    # Other chats unaffected by the partial resume — still deputized.
    for cid in chat_ids[1:]:
        r = c.load_room(cid)
        assert r["meta"]["deputized_original_master"] == "alice"
        assert r["meta"]["member_roles"]["vice"] == c.ROLE_MASTER

    # Resume the rest.
    for cid in chat_ids[1:]:
        c.chat_resume_master(cid, "alice")

    # All chats now clean; helper returns empty.
    assert c.find_chats_deputized_by("alice") == []
    for cid in chat_ids:
        r = c.load_room(cid)
        assert "deputized_original_master" not in r["meta"]
        assert r["meta"]["member_roles"]["alice"] == c.ROLE_MASTER


def test_non_creator_transfer_silently_ignores_as_deputize_marker(isolated_chats):
    """Subtle composition gap: the as_deputize=True kwarg has TWO effects
    that are gated DIFFERENTLY in `transfer_membership`:

    - **marker write** (`meta.deputized_original_master`) — gated inside
      the creator-transfer branch (only fires if `from` is the chat creator).
    - **skip donor out_record** — gated outside the creator branch
      (fires regardless of creator status whenever as_deputize=True).

    A non-creator transfer with as_deputize=True therefore: writes NO marker
    but DOES skip the donor's TRANSFERRED_OUT write. The /khimaira-deputize
    skill only ever invokes as_deputize=True on master-chats so this misuse
    is only reachable via direct Python API. Pin the asymmetric semantic
    so a future refactor that "fixes" the silence (e.g., by raising on
    non-creator misuse, or by widening the marker-write gate) doesn't
    break the slash command's reliance on the current behavior.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice", "alice")
    _make_session(sessions_mod, "bob", "bob")
    _make_session(sessions_mod, "vice", "vice")

    # alice creates; bob accepts. bob is non-creator.
    room = c.create_room("alice", ["bob"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    # Non-creator bob transfers with as_deputize=True. Should succeed but:
    # - NOT write the deputize marker (creator-branch-gated effect)
    # - DOES skip bob's TRANSFERRED_OUT MEMBER write (outside-creator-branch effect)
    c.transfer_membership(chat_id, "bob", "vice", as_deputize=True)

    fresh = c.load_room(chat_id)
    # Marker absent — kwarg silently no-ops on the marker for non-creator.
    assert "deputized_original_master" not in fresh["meta"]
    # Creator unchanged — alice still owns master role.
    assert fresh["meta"]["created_by"] == "alice"
    # Skip-out-record effect still applied — bob stays ACCEPTED.
    assert (
        fresh["members"]["bob"]["state"] == c.ACCEPTED
    ), "as_deputize=True's skip-donor-out applies regardless of creator status"
    assert fresh["members"]["vice"]["state"] == c.ACCEPTED


# ---------------------------------------------------------------------------
# v1.9 assign-batch coordinator
# ---------------------------------------------------------------------------


def _setup_batch_chat(c, sessions_mod):
    """Create a chat with master + agent_a + agent_b, all accepted."""
    for sid in ("master", "agent-a", "agent-b"):
        _make_session(sessions_mod, sid, name=sid)
    room = c.create_room("master", ["agent-a", "agent-b"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "agent-a")
    c.accept(chat_id, "agent-b")
    return chat_id


def _agent_ack(c, chat_id, agent_id, task_id, model="sonnet", effort="medium"):
    """Inject an ack message from agent_id for task_id directly into JSONL."""
    c._append(
        chat_id,
        {
            "kind": c.MSG,
            "event_id": c._new_event_id(),
            "id": "msg-ack-" + task_id[-8:],
            "ts": c._now_iso(),
            "chat_id": chat_id,
            "sender_id": agent_id,
            "sender_name": agent_id[:8],
            "body": f"✅ ready [task-id: {task_id}] | model={model} effort={effort}",
            "to": None,
        },
    )


def test_assign_batch_fire_and_forget(isolated_chats):
    """wait_for_acks=False: creates tasks + sends assignments, returns immediately
    without polling or firing begin."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id = _setup_batch_chat(c, sessions_mod)

    specs = [
        {
            "agent_session_id": "agent-a",
            "task_body": "task A",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
        {
            "agent_session_id": "agent-b",
            "task_body": "task B",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
    ]
    result = asyncio.run(c.assign_batch(chat_id, "master", specs, wait_for_acks=False))

    assert not result["begin_fired"]
    assert result["missing_acks"] == []  # not polled — unknown
    assert result["elapsed_ms"] < 2000  # returns quickly
    assert len(result["task_ids"]) == 2

    records = c._read(chat_id)
    task_records = [r for r in records if r.get("kind") == c.TASK]
    assert len(task_records) == 2
    assignment_records = [
        r
        for r in records
        if r.get("kind") == c.MSG and "🔔 TASK ASSIGNMENT" in (r.get("body") or "")
    ]
    assert len(assignment_records) == 2


def test_assign_batch_scan_acks_detects_acks(isolated_chats):
    """_scan_acks returns all agents once ack messages are in the JSONL."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id = _setup_batch_chat(c, sessions_mod)
    specs = [
        {
            "agent_session_id": "agent-a",
            "task_body": "task A",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
        {
            "agent_session_id": "agent-b",
            "task_body": "task B",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
    ]
    result = asyncio.run(c.assign_batch(chat_id, "master", specs, wait_for_acks=False))
    task_ids = result["task_ids"]

    assert c._scan_acks(chat_id, task_ids) == {}

    for agent_id, tid in task_ids.items():
        _agent_ack(c, chat_id, agent_id, tid)

    found = c._scan_acks(chat_id, task_ids)
    assert set(found.keys()) == set(task_ids.keys())
    for info in found.values():
        assert info["model"] == "sonnet"
        assert info["effort"] == "medium"


def test_assign_batch_fires_begin_when_all_acked(isolated_chats):
    """When all agents ack during the poll window, begin block is sent."""
    import unittest.mock

    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id = _setup_batch_chat(c, sessions_mod)
    specs = [
        {
            "agent_session_id": "agent-a",
            "task_body": "task A",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
        {
            "agent_session_id": "agent-b",
            "task_body": "task B",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
    ]
    acks_injected = [False]

    async def fast_sleep(delay: float) -> None:
        # On first call, inject acks; then return immediately (no recursion).
        if not acks_injected[0]:
            acks_injected[0] = True
            for r in c._read(chat_id):
                if r.get("kind") == c.TASK and r.get("assignee_id"):
                    _agent_ack(c, chat_id, r["assignee_id"], r["id"])

    async def run():
        with unittest.mock.patch("asyncio.sleep", fast_sleep):
            return await c.assign_batch(chat_id, "master", specs, timeout_s=30)

    result = asyncio.run(run())

    assert result["begin_fired"] is True
    assert result["missing_acks"] == []
    assert set(result["acks"].keys()) == {"agent-a", "agent-b"}
    begin_msgs = [
        r
        for r in c._read(chat_id)
        if (r.get("body") or "").startswith("🟢 ALL AGENTS CONFIRMED")
    ]
    # Stagger sends one individual BEGIN per confirmed agent (not one broadcast).
    assert len(begin_msgs) == 2


def test_assign_batch_partial_timeout_no_begin(isolated_chats):
    """With fire_begin_on_partial=False (default): if one agent never acks,
    begin is not fired and missing_acks reports the absent agent."""
    import unittest.mock

    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id = _setup_batch_chat(c, sessions_mod)
    specs = [
        {
            "agent_session_id": "agent-a",
            "task_body": "task A",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
        {
            "agent_session_id": "agent-b",
            "task_body": "task B",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
    ]
    acks_injected = [False]

    async def fast_sleep(delay: float) -> None:
        if not acks_injected[0]:
            acks_injected[0] = True
            for r in c._read(chat_id):
                if r.get("kind") == c.TASK and r.get("assignee_id") == "agent-a":
                    _agent_ack(c, chat_id, "agent-a", r["id"])

    async def run():
        with unittest.mock.patch("asyncio.sleep", fast_sleep):
            return await c.assign_batch(
                chat_id, "master", specs, timeout_s=1, fire_begin_on_partial=False
            )

    result = asyncio.run(run())

    assert result["begin_fired"] is False
    assert "agent-b" in result["missing_acks"]
    assert "agent-a" in result["acks"]


def test_assign_batch_unknown_agent_raises(isolated_chats):
    """Assigning to a session not in the chat raises ValueError → 403."""
    import pytest

    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id = _setup_batch_chat(c, sessions_mod)
    specs = [
        {
            "agent_session_id": "ghost-session",
            "task_body": "task",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
    ]
    with pytest.raises(ValueError):
        asyncio.run(c.assign_batch(chat_id, "master", specs, wait_for_acks=False))


def test_assign_batch_stagger_begin_fires_between_agents(isolated_chats, monkeypatch):
    """STAGGER: with N=2 agents, asyncio.sleep is called between the 2 BEGIN fires.

    The stagger prevents burst-429 (N simultaneous first-API-calls in one second)
    by staggering when each agent receives its individual BEGIN signal.
    """
    import unittest.mock

    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    monkeypatch.setattr(c, "_DISPATCH_STAGGER_S", 1.0)

    chat_id = _setup_batch_chat(c, sessions_mod)
    specs = [
        {
            "agent_session_id": "agent-a",
            "task_body": "task A",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
        {
            "agent_session_id": "agent-b",
            "task_body": "task B",
            "required_model": "sonnet",
            "required_effort": "medium",
        },
    ]
    acks_injected = [False]
    sleep_calls: list[float] = []

    async def tracked_sleep(delay: float) -> None:
        sleep_calls.append(delay)
        if not acks_injected[0]:
            acks_injected[0] = True
            for r in c._read(chat_id):
                if r.get("kind") == c.TASK and r.get("assignee_id"):
                    _agent_ack(c, chat_id, r["assignee_id"], r["id"])

    async def run():
        with unittest.mock.patch("asyncio.sleep", tracked_sleep):
            return await c.assign_batch(chat_id, "master", specs, timeout_s=30)

    result = asyncio.run(run())
    assert result["begin_fired"] is True

    # At least one sleep of the stagger delay should have fired between BEGIN signals.
    stagger_sleeps = [s for s in sleep_calls if s >= 1.0]
    assert len(stagger_sleeps) >= 1, (
        f"Expected at least one stagger sleep (≥1.0s) between BEGIN signals; "
        f"sleep_calls={sleep_calls}"
    )

    # Each agent gets an individual BEGIN (not one broadcast).
    begin_msgs = [
        r for r in c._read(chat_id)
        if (r.get("body") or "").startswith("🟢 ALL AGENTS CONFIRMED")
    ]
    assert len(begin_msgs) == 2


# ---------------------------------------------------------------------------
# v1.9.2: private=True visibility filter
# ---------------------------------------------------------------------------


def _setup_three_member_chat(c, sessions_mod):
    master_id = "priv-master"
    agent_id = "priv-agent"
    observer_id = "priv-observer"
    for sid in (master_id, agent_id, observer_id):
        _make_session(sessions_mod, sid)
    chat_id = c.create_room(master_id, [agent_id, observer_id], title="private test")[
        "meta"
    ]["chat_id"]
    c.accept(chat_id, agent_id)
    c.accept(chat_id, observer_id)
    return chat_id, master_id, agent_id, observer_id


def test_private_message_visible_to_recipient_and_sender(isolated_chats):
    """Recipient and sender both see a private=True message; non-recipient does not."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id, master_id, agent_id, observer_id = _setup_three_member_chat(
        c, sessions_mod
    )
    c.send_message(chat_id, master_id, "secret note", to=[agent_id], private=True)

    # Sender (master) sees it
    master_history = c.history(chat_id, master_id)
    assert any(m.get("private") for m in master_history)

    # Recipient (agent) sees it
    agent_history = c.history(chat_id, agent_id)
    assert any(m.get("private") for m in agent_history)

    # Non-recipient (observer) does NOT see it
    observer_history = c.history(chat_id, observer_id)
    assert not any(m.get("private") for m in observer_history)


def test_private_message_visible_to_master_for_audit(isolated_chats):
    """Chat master always sees private messages for audit even when not a recipient."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id, master_id, agent_id, observer_id = _setup_three_member_chat(
        c, sessions_mod
    )
    # agent sends a private message targeting observer (not master)
    c.send_message(
        chat_id, agent_id, "agent whisper to observer", to=[observer_id], private=True
    )

    master_history = c.history(chat_id, master_id)
    assert any(
        m.get("private") for m in master_history
    ), "master must see all private messages for audit"


def test_private_message_requires_to_field(isolated_chats):
    """private=True without `to` raises ValueError."""
    import pytest

    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id, master_id, agent_id, _ = _setup_three_member_chat(c, sessions_mod)
    with pytest.raises(ValueError, match="non-empty `to` list"):
        c.send_message(chat_id, master_id, "no recipients", private=True)


def test_private_task_hidden_from_non_assignee(isolated_chats):
    """A private task is visible to assignee and master, hidden from observer."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id, master_id, agent_id, observer_id = _setup_three_member_chat(
        c, sessions_mod
    )
    c.create_task(
        chat_id,
        master_id,
        "secret task body",
        assignee_session_id=agent_id,
        private=True,
    )

    # Assignee sees it
    agent_history = c.history(chat_id, agent_id)
    assert any(m.get("kind") == "task" and m.get("private") for m in agent_history)

    # Non-assignee observer does NOT see it
    observer_history = c.history(chat_id, observer_id)
    assert not any(
        m.get("kind") == "task" and m.get("private") for m in observer_history
    )

    # Master (sender + audit) sees it
    master_history = c.history(chat_id, master_id)
    assert any(m.get("kind") == "task" and m.get("private") for m in master_history)


# ---------------------------------------------------------------------------
# topology field — v1.9.5
# ---------------------------------------------------------------------------


def test_topology_flat_mode_send_to_visible_to_all(isolated_chats):
    """In flat-mode chat, send_to without explicit private is visible to all accepted members."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    master_id = "topo-flat-master"
    agent_id = "topo-flat-agent"
    observer_id = "topo-flat-obs"
    for sid in (master_id, agent_id, observer_id):
        _make_session(sessions_mod, sid)
    chat_id = c.create_room(
        master_id, [agent_id, observer_id], title="flat chat", topology="flat"
    )["meta"]["chat_id"]
    c.accept(chat_id, agent_id)
    c.accept(chat_id, observer_id)

    c.send_message(chat_id, master_id, "flat broadcast", to=[agent_id])

    # Non-recipient observer still sees it — flat mode has no implicit private
    observer_history = c.history(chat_id, observer_id)
    assert any(
        m.get("body") == "flat broadcast" for m in observer_history
    ), "flat topology: send_to without explicit private is NOT private — all members see it"


def test_topology_hierarchical_mode_send_to_defaults_private(isolated_chats):
    """In hierarchical-mode chat, send_to without explicit private defaults to private=True."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    master_id = "topo-hier-master"
    agent_id = "topo-hier-agent"
    observer_id = "topo-hier-obs"
    for sid in (master_id, agent_id, observer_id):
        _make_session(sessions_mod, sid)
    chat_id = c.create_room(
        master_id,
        [agent_id, observer_id],
        title="hierarchical chat",
        topology="hierarchical",
    )["meta"]["chat_id"]
    c.accept(chat_id, agent_id)
    c.accept(chat_id, observer_id)

    # No explicit private= arg — topology should default to private=True
    c.send_message(chat_id, master_id, "hierarchy msg", to=[agent_id])

    # Recipient sees it
    agent_history = c.history(chat_id, agent_id)
    assert any(m.get("body") == "hierarchy msg" for m in agent_history)

    # Non-recipient observer does NOT see it — topology defaulted private=True
    observer_history = c.history(chat_id, observer_id)
    assert not any(m.get("body") == "hierarchy msg" for m in observer_history), (
        "hierarchical topology: send_to without explicit private defaults to private — "
        "non-recipient observer must not see the message"
    )


def test_topology_missing_field_defaults_to_flat_backward_compat(isolated_chats):
    """Chats created without a topology arg default to flat — backward-compatible behavior."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    master_id = "topo-compat-master"
    agent_id = "topo-compat-agent"
    observer_id = "topo-compat-obs"
    for sid in (master_id, agent_id, observer_id):
        _make_session(sessions_mod, sid)
    # create_room called without topology — must behave as flat
    chat_id = c.create_room(master_id, [agent_id, observer_id], title="legacy chat")[
        "meta"
    ]["chat_id"]
    c.accept(chat_id, agent_id)
    c.accept(chat_id, observer_id)

    c.send_message(chat_id, master_id, "legacy msg", to=[agent_id])

    # Observer sees it — no topology field → flat default → no implicit private
    observer_history = c.history(chat_id, observer_id)
    assert any(
        m.get("body") == "legacy msg" for m in observer_history
    ), "no topology field → flat behavior (backward-compat): non-recipient observer sees the message"


def test_topology_hierarchical_explicit_private_false_overrides_default(isolated_chats):
    """explicit private=False in hierarchical mode overrides the topology's private default."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    master_id = "topo-over-master"
    agent_id = "topo-over-agent"
    observer_id = "topo-over-obs"
    for sid in (master_id, agent_id, observer_id):
        _make_session(sessions_mod, sid)
    chat_id = c.create_room(
        master_id,
        [agent_id, observer_id],
        title="override test",
        topology="hierarchical",
    )["meta"]["chat_id"]
    c.accept(chat_id, agent_id)
    c.accept(chat_id, observer_id)

    # Explicit private=False must override the hierarchical topology default
    c.send_message(
        chat_id, master_id, "public in hierarchy", to=[agent_id], private=False
    )

    # Observer sees it — explicit False wins over topology default
    observer_history = c.history(chat_id, observer_id)
    assert any(m.get("body") == "public in hierarchy" for m in observer_history), (
        "explicit private=False must override hierarchical topology default — "
        "non-recipient observer should see this message"
    )


def test_private_task_hidden_from_non_assignee_in_task_status(isolated_chats):
    """task_status() applies the same private filter as chat_history.

    Non-assignee members must not see private tasks via the task_status
    surface — closing the v1.9.2 private-leak noted in STATE.md.
    """
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    chat_id, master_id, agent_id, observer_id = _setup_three_member_chat(
        c, sessions_mod
    )
    c.create_task(
        chat_id,
        master_id,
        "private task body",
        assignee_session_id=agent_id,
        private=True,
    )

    # Assignee sees the task
    agent_tasks = c.task_status(chat_id, agent_id)
    assert any(t["body"] == "private task body" for t in agent_tasks)

    # Non-assignee observer does NOT see it
    observer_tasks = c.task_status(chat_id, observer_id)
    assert not any(t["body"] == "private task body" for t in observer_tasks)

    # Master (audit) sees it
    master_tasks = c.task_status(chat_id, master_id)
    assert any(t["body"] == "private task body" for t in master_tasks)


# ---------------------------------------------------------------------------
# infer_role_from_name + member_roles on create_room
# ---------------------------------------------------------------------------


def test_infer_role_from_name_agent(isolated_chats):
    assert isolated_chats.infer_role_from_name("agent-1") == "agent"


def test_infer_role_from_name_unknown(isolated_chats):
    assert isolated_chats.infer_role_from_name("khimaira-0") is None


def test_infer_role_from_name_prefixed_lead(isolated_chats):
    """rsplit inference resolves multi-segment lead names (S2 class test)."""
    assert (
        isolated_chats.infer_role_from_name("jp-frontend-lead-1") == "jp-frontend-lead"
    )


def test_infer_role_from_name_person_name_is_none(isolated_chats):
    """Person-named sessions (e.g. janice-0) don't infer a role — 'janice' ∉ registry."""
    assert isolated_chats.infer_role_from_name("janice-0") is None


def test_infer_role_from_name_bare_role(isolated_chats):
    """A session named exactly after a role (no numeric suffix) is recognized."""
    assert isolated_chats.infer_role_from_name("agent") == "agent"


def test_role_budget_keys_subset_of_valid_roles(isolated_chats):
    """Drift test: every key in ROLE_BUDGET is a valid role in _VALID_ROLES.

    Prevents budget entries for roles that no longer exist in the registry,
    and ensures ROLE_BUDGET stays coupled to the canonical role source.
    """
    for role in isolated_chats.ROLE_BUDGET:
        assert role in isolated_chats._VALID_ROLES, (
            f"ROLE_BUDGET key {role!r} is not in _VALID_ROLES — "
            "update the budget table or the themis rule registry"
        )


def test_chat_grant_role_accepts_all_registry_roles(isolated_chats):
    """Class test: chat_grant_role accepts every non-master role in _VALID_ROLES.

    Previously failed for lead roles (jp-frontend-lead, backend-lead, etc.)
    because _VALID_ROLES was a hardcoded frozenset that excluded them.
    Master-grant is tested separately (it mutates the master identity).
    """
    from khimaira.monitor import sessions as sessions_mod

    master_id = "aaaa1111-0000-0000-0000-000000000001"
    _make_session(sessions_mod, master_id, "master")
    target_id = "bbbb2222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, target_id, "target")

    room = isolated_chats.create_room(
        master_id,
        [target_id],
        title="registry-test",
        member_roles={master_id: "master"},
    )
    chat_id = room["meta"]["chat_id"]
    isolated_chats.accept(chat_id, target_id)

    non_master_roles = sorted(
        r for r in isolated_chats._VALID_ROLES if r != isolated_chats.ROLE_MASTER
    )
    for role in non_master_roles:
        # Grant each role without raising ValueError for unknown role
        isolated_chats.chat_grant_role(chat_id, master_id, target_id, role)


def test_create_room_member_roles_stored(isolated_chats, tmp_path, monkeypatch):
    import importlib
    from khimaira.monitor import sessions as sessions_mod

    master_id = "aaaaaaaa-0000-0000-0000-000000000001"
    agent_id = "bbbbbbbb-0000-0000-0000-000000000002"

    roles = {master_id: "master", agent_id: "agent"}
    room = isolated_chats.create_room(
        master_id,
        [agent_id],
        title="test room",
        topology="hierarchical",
        member_roles=roles,
    )
    assert room["meta"]["member_roles"] == roles

    # Verify round-trip: load_room returns the stored roles
    reloaded = isolated_chats.load_room(room["meta"]["chat_id"])
    assert reloaded["meta"]["member_roles"] == roles


# ---------------------------------------------------------------------------
# Bug #6 fix: master can cancel pending/in_progress tasks
# ---------------------------------------------------------------------------


def test_master_can_cancel_pending_task(isolated_chats):
    """Master cancels a task stuck in pending (stale/superseded)."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "master-uuid", "master")
    _make_session(sessions_mod, "agent-uuid", "agent")
    c.create_room("master-uuid", ["agent-uuid"], title="t")
    chat_id = c.my_chats("master-uuid")[0]["chat_id"]
    c.accept(chat_id, "agent-uuid")

    task = c.create_task(
        chat_id, "master-uuid", "stale work", assignee_session_id="agent-uuid"
    )
    assert task["status"] == c.TASK_PENDING

    result = c.update_task_status(chat_id, task["id"], "master-uuid", c.TASK_CANCELLED)
    assert result["status"] == c.TASK_CANCELLED


def test_master_can_cancel_in_progress_task(isolated_chats):
    """Master cancels a task whose assignee went silent mid-task."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "master-uuid", "master")
    _make_session(sessions_mod, "agent-uuid", "agent")
    c.create_room("master-uuid", ["agent-uuid"], title="t")
    chat_id = c.my_chats("master-uuid")[0]["chat_id"]
    c.accept(chat_id, "agent-uuid")

    task = c.create_task(
        chat_id, "master-uuid", "abandoned work", assignee_session_id="agent-uuid"
    )
    c.update_task_status(chat_id, task["id"], "agent-uuid", c.TASK_IN_PROGRESS)

    result = c.update_task_status(chat_id, task["id"], "master-uuid", c.TASK_CANCELLED)
    assert result["status"] == c.TASK_CANCELLED


def test_assignee_cannot_cancel_task(isolated_chats):
    """Assignee (non-master) cannot cancel a task — cancel is master-only."""
    import pytest
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "master-uuid", "master")
    _make_session(sessions_mod, "agent-uuid", "agent")
    c.create_room("master-uuid", ["agent-uuid"], title="t")
    chat_id = c.my_chats("master-uuid")[0]["chat_id"]
    c.accept(chat_id, "agent-uuid")

    task = c.create_task(
        chat_id, "master-uuid", "work", assignee_session_id="agent-uuid"
    )

    with pytest.raises(ValueError, match="not authorized"):
        c.update_task_status(chat_id, task["id"], "agent-uuid", c.TASK_CANCELLED)


# ---------------------------------------------------------------------------
# Creator role resolution — #67/#68 regression
#
# A chat created with an explicit member_roles dict that OMITS the creator
# must not leave the creator roleless. That gap made the creator unresolvable
# to the Themis role gate (Layer-1 miss → IN-UNRESOLVABLE) and fail-closed
# EVERY tool — including the ones (chat_grant_role) that would clear it. Two
# guarantees, tested at the class level so any future path into the same bug
# is caught:
#   #67 — create_room injects creator -> master into member_roles.
#   #68 — _is_master falls back to created_by when member_roles is present but
#         omits sid (covers pre-#67 chats that already exist on disk and only
#         get fixed by a daemon redeploy).
# ---------------------------------------------------------------------------


def test_create_room_includes_creator_in_member_roles(isolated_chats):
    """#67: explicit member_roles omitting the creator still records the
    creator as master, so role resolution never leaves them unresolvable."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "boss", "boss")
    _make_session(sessions_mod, "worker", "worker")

    # Creator deliberately omitted from member_roles — the exact bootstrap bug.
    room = c.create_room(
        "boss",
        ["worker"],
        member_roles={"worker": c.ROLE_AGENT},
    )

    member_roles = room["meta"]["member_roles"]
    assert member_roles["boss"] == c.ROLE_MASTER
    assert member_roles["worker"] == c.ROLE_AGENT
    # Class invariant: the creator resolves as master.
    assert c._is_master(room, "boss") is True


def test_create_room_does_not_override_explicit_creator_role(isolated_chats):
    """#67 guard: if the caller explicitly placed the creator in member_roles,
    setdefault must not clobber that choice."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "boss", "boss")
    _make_session(sessions_mod, "worker", "worker")

    room = c.create_room(
        "boss",
        ["worker"],
        member_roles={"boss": c.ROLE_MASTER, "worker": c.ROLE_AGENT},
    )
    assert room["meta"]["member_roles"]["boss"] == c.ROLE_MASTER


def test_is_master_falls_back_to_created_by_when_creator_missing(isolated_chats):
    """#68: a pre-#67 room on disk whose member_roles omits the creator must
    still resolve the creator as master via the created_by fallback."""
    c = isolated_chats
    room = {
        "meta": {
            "created_by": "creator-sid",
            "member_roles": {"agent-sid": c.ROLE_AGENT},  # creator absent
        },
        "members": {},
        "messages": [],
    }
    assert c._is_master(room, "creator-sid") is True
    assert c._is_master(room, "agent-sid") is False
    assert c._is_master(room, "stranger-sid") is False


def test_is_master_explicit_role_is_authoritative_over_created_by(isolated_chats):
    """#68 guard: an explicit member_roles entry wins over the created_by
    fallback — a creator demoted via chat_grant_role is no longer master."""
    c = isolated_chats
    room = {
        "meta": {
            "created_by": "old-master",  # was creator, since demoted
            "member_roles": {"old-master": c.ROLE_AGENT, "new-master": c.ROLE_MASTER},
        },
        "members": {},
        "messages": [],
    }
    assert c._is_master(room, "old-master") is False
    assert c._is_master(room, "new-master") is True


# ---------------------------------------------------------------------------
# Role-broadcast idempotency + GC (Lane 4 / storm cleanup)
# CLAUDE.md: role-directive emission is event-driven (role-change points), never periodic.
# ---------------------------------------------------------------------------


def test_role_directive_emission_is_event_driven(isolated_chats):
    """Regression guard: role_directives are only emitted at role-change events.
    Loading a chat (simulating daemon startup) MUST NOT emit new directives.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    room = c.create_room(
        "alice-uuid", [], title="t", member_roles={"alice-uuid": c.ROLE_MASTER}
    )
    chat_id = room["meta"]["chat_id"]

    count_before = len([r for r in c._read(chat_id) if c._is_role_directive(r)])

    # Simulated startup: load_room must not side-effect new directives
    c.load_room(chat_id)

    count_after = len([r for r in c._read(chat_id) if c._is_role_directive(r)])
    assert count_after == count_before, "load_room must not emit role_directives"


def test_role_directive_emitted_on_create_room(isolated_chats):
    """role_directive IS emitted at create_room (event-driven, role assigned)."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    room = c.create_room(
        "alice-uuid", [], title="t", member_roles={"alice-uuid": c.ROLE_MASTER}
    )
    chat_id = room["meta"]["chat_id"]

    directives = [r for r in c._read(chat_id) if c._is_role_directive(r)]
    assert len(directives) == 1
    assert directives[0]["to"] == ["alice-uuid"]
    assert directives[0]["meta"]["role"] == c.ROLE_MASTER


def test_subscribe_backfill_skips_role_directives(isolated_chats):
    """subscribe() backfill replay must NOT include role_directive records.
    They are suppressed to prevent restart replay noise.
    """
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    room = c.create_room(
        "alice-uuid",
        ["bob-uuid"],
        title="t",
        member_roles={"alice-uuid": c.ROLE_MASTER, "bob-uuid": c.ROLE_AGENT},
    )
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Confirm role_directives are in the JSONL
    directives = [r for r in c._read(chat_id) if c._is_role_directive(r)]
    assert len(directives) > 0, "must have some role_directives to test filtering"

    # The backfill filter: _is_role_directive must be True for these records
    # (subscribe() uses `if _is_role_directive(line): continue`)
    for line in c._read(chat_id):
        if c._is_role_directive(line):
            assert (line.get("meta") or {}).get("event_type") == "role_directive"
            break


def test_gc_role_directives_drops_historical_dupes(isolated_chats):
    """gc_role_directives_in_chat keeps only the latest directive per target."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    room = c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Grant bob several roles in sequence → multiple directives for bob
    c.chat_grant_role(chat_id, "alice-uuid", "bob-uuid", c.ROLE_ANALYST)
    c.chat_grant_role(chat_id, "alice-uuid", "bob-uuid", c.ROLE_AGENT)

    bob_directives_before = [
        r
        for r in c._read(chat_id)
        if c._is_role_directive(r) and (r.get("meta") or {}).get("target") == "bob-uuid"
    ]
    assert len(bob_directives_before) >= 2, "need at least 2 directives to test GC"

    dropped = c.gc_role_directives_in_chat(chat_id)
    assert dropped > 0

    bob_directives_after = [
        r
        for r in c._read(chat_id)
        if c._is_role_directive(r) and (r.get("meta") or {}).get("target") == "bob-uuid"
    ]
    assert len(bob_directives_after) == 1
    # The LAST role granted (agent) is kept
    assert bob_directives_after[0]["meta"]["role"] == c.ROLE_AGENT


def test_gc_role_directives_preserves_non_directive_records(isolated_chats):
    """gc_role_directives_in_chat must not touch any non-role_directive records."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    room = c.create_room("alice-uuid", ["bob-uuid"], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.send_message(chat_id, "alice-uuid", "hello world")

    non_directive_before = [r for r in c._read(chat_id) if not c._is_role_directive(r)]

    c.gc_role_directives_in_chat(chat_id)

    non_directive_after = [r for r in c._read(chat_id) if not c._is_role_directive(r)]
    assert len(non_directive_after) == len(non_directive_before)
    for before, after in zip(non_directive_before, non_directive_after):
        assert before["event_id"] == after["event_id"]


def test_resolve_or_uuid_chat_scoped_via_chats(isolated_chats):
    """_resolve_or_uuid with chat_id resolves within that chat's members (P2)."""
    from khimaira.monitor import sessions as sessions_mod

    c = isolated_chats
    sid_a = "aaaa0000-0000-0000-0000-000000000001"
    sid_b = "bbbb0000-0000-0000-0000-000000000002"
    for sid, name in [(sid_a, "alice"), (sid_b, "bob")]:
        d = sessions_mod._session_dir(sid)
        (d / "status.json").write_text(
            json.dumps({"name": name, "status": "idle"}), encoding="utf-8"
        )

    room = c.create_room(sid_a, [sid_b], title="t")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, sid_b)

    result = c._resolve_or_uuid("bob", chat_id=chat_id)
    assert result == sid_b


# ---------------------------------------------------------------------------
# Membership integrity (#1 phantom, #2 remove-member, #3 role-binding, #10)
# ---------------------------------------------------------------------------


def test_create_room_rejects_unregistered_name(isolated_chats):
    """#1: create_room rejects a non-UUID name that isn't in the session registry."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod
    _make_session(sessions_mod, "alice-uuid", "alice")

    # "totally-unknown-name" is not in the registry → ValueError at resolution
    with pytest.raises(ValueError):
        c.create_room("alice-uuid", ["totally-unknown-name"])


def test_invite_rejects_unregistered_name(isolated_chats):
    """#1: invite rejects a non-UUID invitee name not in the session registry."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod
    _make_session(sessions_mod, "alice-uuid", "alice")
    room = c.create_room("alice-uuid", [])
    chat_id = room["meta"]["chat_id"]
    # alice is auto-accepted as creator

    with pytest.raises(ValueError):
        # "ghost-name" doesn't resolve → rejected
        c.invite(chat_id, "alice-uuid", "ghost-name")


def test_invite_accepts_fresh_uuid_without_dir(isolated_chats):
    """#1 lazy-registration exception: UUID invitees without dirs are warned but accepted."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod
    _make_session(sessions_mod, "alice-uuid", "alice")
    room = c.create_room("alice-uuid", [])
    chat_id = room["meta"]["chat_id"]
    # alice is auto-accepted as creator — no need to call accept()

    fresh_uuid = "cccc0000-0000-0000-0000-000000000003"
    # Should NOT raise — lazy-registration for UUID-shaped IDs
    record = c.invite(chat_id, "alice-uuid", fresh_uuid)
    assert record["state"] == c.PENDING


def test_remove_member_evicts_and_unsubscribes(isolated_chats):
    """#2: remove_member transitions to REMOVED and discards from _subscribers."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    room = c.create_room("alice-uuid", ["bob-uuid"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob-uuid")

    # Simulate bob having an SSE subscriber
    import asyncio
    q = asyncio.Queue()
    c._subscribers.setdefault("bob-uuid", set()).add(q)
    assert c.is_reachable("bob-uuid")

    record = c.remove_member(chat_id, "alice-uuid", "bob-uuid")
    assert record["state"] == c.REMOVED

    # Bob should be removed from _subscribers (reachability hygiene)
    assert not c.is_reachable("bob-uuid")

    # The member state in the room should be REMOVED
    room2 = c.load_room(chat_id)
    assert room2["members"]["bob-uuid"]["state"] == c.REMOVED


def test_remove_member_non_master_rejected(isolated_chats):
    """#2: only master can remove members."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    _make_session(sessions_mod, "carol-uuid", "carol")
    room = c.create_room("alice-uuid", ["bob-uuid", "carol-uuid"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob-uuid")
    c.accept(chat_id, "carol-uuid")

    with pytest.raises(ValueError, match="not the master"):
        c.remove_member(chat_id, "bob-uuid", "carol-uuid")


def test_invite_with_role_binds_atomically(isolated_chats):
    """#3: invite with role= writes member_roles atomically."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod
    _make_session(sessions_mod, "alice-uuid", "alice")
    _make_session(sessions_mod, "bob-uuid", "bob")
    room = c.create_room("alice-uuid", [])
    chat_id = room["meta"]["chat_id"]
    # alice is auto-accepted as creator

    c.invite(chat_id, "alice-uuid", "bob-uuid", role=c.ROLE_ANALYST)

    # Role must be in member_roles immediately after invite (not after accept)
    room2 = c.load_room(chat_id)
    member_roles = room2["meta"].get("member_roles") or {}
    assert member_roles.get("bob-uuid") == c.ROLE_ANALYST


# ---------------------------------------------------------------------------
# Privileged role-assignment authority on invite (task-b7ddcbf080da)
# ---------------------------------------------------------------------------


def test_invite_with_privileged_role_requires_master(isolated_chats):
    """Non-master accepted member cannot invite with master or *-lead role."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    for sid in ("alice", "bob", "dave"):
        _make_session(sessions_mod, sid, sid)
    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    # bob is not master; inviting dave (fresh session) with master role must fail
    with pytest.raises(ValueError, match="privileged role"):
        c.invite(chat_id, "bob", "dave", role=c.ROLE_MASTER)


def test_invite_with_lead_role_requires_master(isolated_chats):
    """Non-master cannot invite with a *-lead role (e.g. backend-lead)."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    for sid in ("alice", "bob", "dave", "eve"):
        _make_session(sessions_mod, sid, sid)
    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    with pytest.raises(ValueError, match="privileged role"):
        c.invite(chat_id, "bob", "dave", role="backend-lead")

    with pytest.raises(ValueError, match="privileged role"):
        c.invite(chat_id, "bob", "eve", role="jp-frontend-lead")


def test_invite_with_nonprivileged_role_by_nonmaster_ok(isolated_chats):
    """Any accepted member may invite with a non-privileged role (agent, observer, etc.)."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    for sid in ("alice", "bob", "carol"):
        _make_session(sessions_mod, sid, sid)
    room = c.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, "bob")

    # bob (non-master) invites carol with agent role — must succeed
    c.invite(chat_id, "bob", "carol", role=c.ROLE_AGENT)
    room2 = c.load_room(chat_id)
    member_roles = room2["meta"].get("member_roles") or {}
    assert member_roles.get("carol") == c.ROLE_AGENT


def test_master_can_invite_with_privileged_role(isolated_chats):
    """Master may invite with master or *-lead role."""
    c = isolated_chats
    from khimaira.monitor import sessions as sessions_mod

    for sid in ("alice", "bob"):
        _make_session(sessions_mod, sid, sid)
    room = c.create_room("alice", [])
    chat_id = room["meta"]["chat_id"]

    # alice is master; inviting bob with backend-lead must succeed
    c.invite(chat_id, "alice", "bob", role="backend-lead")
    room2 = c.load_room(chat_id)
    member_roles = room2["meta"].get("member_roles") or {}
    assert member_roles.get("bob") == "backend-lead"


# ---------------------------------------------------------------------------
# roster_progress — observable-truth aggregator (task-96c826d8a7c0)
# ---------------------------------------------------------------------------


class TestRosterProgress:
    """roster_progress returns observable per-member work state from signals.

    Key design constraints (per analyst + architect):
    - disk-WIP is PRIMARY (hook-independent); file_touched is SECONDARY
    - No-WIP ambiguity: no-WIP + task-done → "completed", not "idle"
    - Stale-status flag fires when manual ≠ observable
    - Agent completes work, manual status unchanged → roster_progress shows completed
    """

    def _setup(self, c, sessions_mod):
        for sid in ("master", "agent-a", "agent-b"):
            _make_session(sessions_mod, sid, sid)
        room = c.create_room("master", ["agent-a", "agent-b"])
        chat_id = room["meta"]["chat_id"]
        c.accept(chat_id, "agent-a")
        c.accept(chat_id, "agent-b")
        return chat_id

    def test_no_tasks_all_idle(self, isolated_chats):
        c = isolated_chats
        from khimaira.monitor import sessions as sessions_mod

        chat_id = self._setup(c, sessions_mod)
        result = c.roster_progress(chat_id, "master")
        assert len(result) == 3  # master + 2 agents
        labels = {m["name"]: m["derived_label"] for m in result}
        assert labels["agent-a"] == "idle"
        assert labels["agent-b"] == "idle"

    def test_task_done_shows_completed_not_idle(self, isolated_chats):
        """KEY: agent completes work, manual status unchanged → 'completed'."""
        c = isolated_chats
        from khimaira.monitor import sessions as sessions_mod

        chat_id = self._setup(c, sessions_mod)
        task = c.create_task(chat_id, "master", "Implement foo", assignee_session_id="agent-a")
        c.update_task_status(chat_id, task["id"], "agent-a", c.TASK_IN_PROGRESS)
        c.update_task_status(chat_id, task["id"], "agent-a", c.TASK_DONE)
        # agent-a's manual status is still the default (not updated)

        result = c.roster_progress(chat_id, "master")
        a_entry = next(m for m in result if m["name"] == "agent-a")
        assert a_entry["derived_label"] == "completed"
        assert a_entry["owed_task"]["status"] == c.TASK_DONE

    def test_stalled_or_silent_when_in_progress_no_wip_no_done(self, isolated_chats):
        """No-WIP + in_progress task + no done-msg → stalled-or-silent (not idle)."""
        c = isolated_chats
        from khimaira.monitor import sessions as sessions_mod

        chat_id = self._setup(c, sessions_mod)
        task = c.create_task(chat_id, "master", "Implement bar", assignee_session_id="agent-b")
        c.update_task_status(chat_id, task["id"], "agent-b", c.TASK_IN_PROGRESS)
        # No WIP probe possible in isolated test (no real files); _session_has_recent_wip → False

        result = c.roster_progress(chat_id, "master")
        b_entry = next(m for m in result if m["name"] == "agent-b")
        assert b_entry["derived_label"] == "stalled-or-silent"
        assert b_entry["has_recent_wip"] is False

    def test_non_member_raises(self, isolated_chats):
        """Requester not in chat → ValueError."""
        c = isolated_chats
        from khimaira.monitor import sessions as sessions_mod

        for sid in ("master", "ghost"):
            _make_session(sessions_mod, sid, sid)
        room = c.create_room("master", [])
        chat_id = room["meta"]["chat_id"]

        import pytest
        with pytest.raises(ValueError, match="not an accepted member"):
            c.roster_progress(chat_id, "ghost")
