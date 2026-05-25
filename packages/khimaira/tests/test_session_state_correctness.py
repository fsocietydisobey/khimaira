"""Session-state correctness invariant tests — Phase 1.

Asserts that bug classes A (resolution lies) and B (reachability lies)
are correctly closed. Tests verify:
  1. Live sessions maintain usable status
  2. Dead subscribers are demoted to unreachable
  3. Name resolution prefers live over stub
  4. Stubs resolve only when no live exists
  5. post_notice returns target reachability
  6. post_notice loud-fails on unreachable targets
  7. send_message includes targets_reachability in return value
"""

from __future__ import annotations

import os
import time

import pytest


@pytest.fixture
def fast_thresholds(monkeypatch: pytest.MonkeyPatch):
    """Set aggressive demote threshold for testing.

    Reads KHIMAIRA_DEMOTE_THRESHOLD_S at fixture call time (not module-level)
    so monkeypatch works without needing importlib.reload.
    """
    monkeypatch.setenv("KHIMAIRA_DEMOTE_THRESHOLD_S", "2")
    yield


def test_alive_session_status_remains_usable(isolated_state, fast_thresholds):
    """A session with recent SSE heartbeat remains in its declared status."""
    from khimaira.monitor.sessions import set_status, set_name, state, write_sse_heartbeat

    sid = "test-session-1"
    isolated_state._session_dir(sid).mkdir(exist_ok=True, parents=True)

    # Set status and name
    set_status(sid, "implementing")
    set_name(sid, "demo")

    # Emit a heartbeat (as the PostToolUse hook would)
    write_sse_heartbeat(sid)

    # Read state — effective_status should match declared status
    s = state(sid)
    status = s.get("status") or {}
    assert status.get("status") == "implementing"
    assert status.get("effective_status") == "implementing"
    assert status.get("last_sse_heartbeat") is not None


def test_dead_subscriber_demotes_to_unreachable(isolated_state, fast_thresholds):
    """A session with no heartbeat + no tool activity for > threshold is demoted."""
    from khimaira.monitor.sessions import set_status, state

    sid = "test-session-2"
    isolated_state._session_dir(sid).mkdir(exist_ok=True, parents=True)

    # Set status but no heartbeat, no tool calls
    set_status(sid, "implementing")

    # Sleep past the threshold (2 seconds)
    time.sleep(2.5)

    # Read state — effective_status should be unreachable
    s = state(sid)
    status = s.get("status") or {}
    assert status.get("effective_status") == "unreachable"
    assert status.get("demoted_reason") is not None
    assert "no SSE heartbeat or tool activity" in status.get("demoted_reason", "")


def test_name_resolution_prefers_live_over_stub(isolated_state, fast_thresholds):
    """When multiple sessions share a name, live (heartbeat + decisions) wins."""
    from khimaira.monitor.sessions import (
        set_name,
        log_decision,
        write_sse_heartbeat,
        resolve_session_id,
    )

    # Session 1: stub (name only, no activity)
    sid1 = "test-session-stub"
    isolated_state._session_dir(sid1).mkdir(exist_ok=True, parents=True)
    set_name(sid1, "shared-name")

    # Session 2: live (name + heartbeat + decision)
    sid2 = "test-session-live"
    isolated_state._session_dir(sid2).mkdir(exist_ok=True, parents=True)
    set_name(sid2, "shared-name")
    write_sse_heartbeat(sid2)
    log_decision(sid2, "I chose option A")

    # Resolve by name — should get the live one
    resolved = resolve_session_id("shared-name")
    assert resolved == sid2


def test_stub_resolves_only_when_no_live_exists(isolated_state, fast_thresholds):
    """When only a stub exists with the name, it still resolves (log warning)."""
    from khimaira.monitor.sessions import set_name, resolve_session_id

    sid = "test-session-stub-only"
    isolated_state._session_dir(sid).mkdir(exist_ok=True, parents=True)
    set_name(sid, "stub-only-name")

    # Should resolve without error (logs warning internally)
    resolved = resolve_session_id("stub-only-name")
    assert resolved == sid


def test_post_notice_returns_target_reachable(isolated_state, fast_thresholds):
    """post_notice() return includes target_reachable + target_status fields."""
    from khimaira.monitor.sessions import set_status, set_name, write_sse_heartbeat, post_notice

    target_sid = "test-target"
    isolated_state._session_dir(target_sid).mkdir(exist_ok=True, parents=True)
    set_status(target_sid, "idle")
    set_name(target_sid, "target-live")
    write_sse_heartbeat(target_sid)

    # Post a notice — should include reachability
    note = post_notice(target_sid, "test message", fire_desktop_notify=False)
    assert note.get("target_reachable") is True
    assert note.get("target_status") in ("idle", "working", "listening")
    assert note.get("target_last_active_iso") is not None


def test_post_notice_loud_fails_on_unreachable_target(isolated_state, fast_thresholds):
    """post_notice() loudly marks unreachable targets in return value."""
    from khimaira.monitor.sessions import set_status, post_notice

    target_sid = "test-unreachable"
    isolated_state._session_dir(target_sid).mkdir(exist_ok=True, parents=True)
    set_status(target_sid, "implementing")

    # Let it become unreachable (no heartbeat, past threshold)
    time.sleep(2.5)

    # Post a notice — should mark as unreachable
    note = post_notice(target_sid, "msg to unreachable", fire_desktop_notify=False)
    assert note.get("target_reachable") is False
    assert note.get("target_status") == "unreachable"
    assert note.get("reason_if_not_ok") is not None


def test_send_message_includes_targets_reachability(isolated_state, fast_thresholds):
    """send_message() return includes targets_reachability when `to` is set."""
    from khimaira.monitor.chats import create_room, accept, send_message
    from khimaira.monitor.sessions import set_status, set_name, write_sse_heartbeat

    master = "master-session"
    member = "member-session"

    isolated_state._session_dir(master).mkdir(exist_ok=True, parents=True)
    isolated_state._session_dir(member).mkdir(exist_ok=True, parents=True)

    set_status(master, "idle")
    set_name(master, "master")
    set_status(member, "idle")
    set_name(member, "member")
    write_sse_heartbeat(master)
    write_sse_heartbeat(member)

    # create_room: creator_session_id + member_session_ids (list)
    room = create_room(master, [member])
    chat_id = room["meta"]["chat_id"]

    # Accept invite so member becomes accepted member
    accept(chat_id, member)

    # Send a targeted message from master to member
    msg = send_message(chat_id, master, "hello", to=[member])

    assert "targets_reachability" in msg
    assert isinstance(msg["targets_reachability"], list)
    assert len(msg["targets_reachability"]) == 1
    target_info = msg["targets_reachability"][0]
    assert target_info["session_id"] == member
    assert "target_reachable" in target_info
    assert "target_status" in target_info

    # Broadcast (no `to`) should NOT have targets_reachability
    broadcast = send_message(chat_id, master, "hello all")
    assert "targets_reachability" not in broadcast
