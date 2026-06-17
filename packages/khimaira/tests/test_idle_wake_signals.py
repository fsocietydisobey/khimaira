"""Tests for the Path-2 idle-wake signals: unread-inbox + unconsumed-chat.

CONTEXT: roster_recovery._process_window wakes an idle session only if its OR-gate
trips. Path 1 added owed-verdict obligations; Path 2 adds two more OR-gate signals so
an idle session (incl. the master) is woken when a peer pinged it:

  A) _session_has_unread_inbox  — notices / handoffs piling up unread (session_post_notice)
  B) _session_has_unconsumed_chat — an inbound chat message newer than the session's
     last observable action (the peer-reply case). NOT the chat cursor: the cursor
     advances on SSE DELIVERY, which continues while an idle session is turn-gated, so
     cursor-lag is ~0 exactly when we need to wake. We compare message ts against
     last_active (session-dir mtime), which delivery does not pollute.

STORM-SAFETY ACCEPTANCE (the master's hard constraint): both signals must read ZERO on a
quiet/healthy roster. The fire tests prove they trip when they should; the quiet tests
prove they stay silent. The clock-skew guard (_TS_SKEW_EPSILON_S) is exercised so a
borderline-equal ts/mtime can't false-fire.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from khimaira.monitor import chats
from khimaira.monitor import roster_recovery as rr
from khimaira.monitor import sessions as sessions_mod

MEMBER_SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # session under test
PEER_SID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"  # the one who pinged
CHAT_ID = "chat-cafebabe0001"


def _pin_last_active(session_id: str, epoch: float) -> None:
    """Force the session's last_active (max mtime of its dir files) to `epoch`."""
    sd = sessions_mod._session_dir(session_id)
    for p in sd.iterdir():
        if p.is_file():
            os.utime(p, (epoch, epoch))


def _write_chat(messages: list[dict], member_state: str = "accepted") -> None:
    """Write a chat with MEMBER_SID as a member + the given message events."""
    chat_dir = chats._chat_dir()
    chat_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "kind": chats.MEMBER,
            "session_id": MEMBER_SID,
            "session_name": "void",
            "state": member_state,
            "ts": "2026-06-17T00:00:00+00:00",
            "event_id": "evt-member-0001",
        }
    ] + messages
    (chat_dir / f"{CHAT_ID}.jsonl").write_text(
        "".join(json.dumps(line) + "\n" for line in lines)
    )


def _msg(sender_id: str, ts_epoch: float, event_id: str) -> dict:
    from datetime import datetime, timezone

    return {
        "kind": chats.MSG,
        "chat_id": CHAT_ID,
        "sender_id": sender_id,
        "body": "ping",
        "ts": datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat(),
        "event_id": event_id,
    }


# --- Signal A: unread inbox ------------------------------------------------


def test_unread_inbox_fires(isolated_state):
    sessions_mod.set_status(MEMBER_SID, "idle")  # register the session
    sessions_mod.post_notice(
        MEMBER_SID, "peer left you a note", fire_desktop_notify=False
    )

    assert rr._session_has_unread_inbox(MEMBER_SID) is True


def test_drained_inbox_stays_quiet(isolated_state):
    sessions_mod.set_status(MEMBER_SID, "idle")  # register the session
    sessions_mod.post_notice(MEMBER_SID, "note", fire_desktop_notify=False)
    sessions_mod.pending_notes(
        MEMBER_SID, mark_read=True
    )  # drain it (the active-turn path)

    assert rr._session_has_unread_inbox(MEMBER_SID) is False


def test_no_inbox_stays_quiet(isolated_state):
    sessions_mod.set_status(MEMBER_SID, "idle")  # session exists, empty inbox

    assert rr._session_has_unread_inbox(MEMBER_SID) is False


# --- Signal B: unconsumed chat (ts > last_active) --------------------------


def test_unconsumed_chat_fires_for_newer_peer_message(isolated_state):
    """A peer message AFTER the session's last action → wake."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _pin_last_active(MEMBER_SID, now - 3600)  # last acted an hour ago
    _write_chat([_msg(PEER_SID, now, "evt-msg-0001")])  # peer pinged just now

    assert rr._session_has_unconsumed_chat(MEMBER_SID) is True


def test_old_message_before_last_action_stays_quiet(isolated_state):
    """A peer message the session already acted past → no wake (self-clearing)."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat([_msg(PEER_SID, now - 3600, "evt-msg-0001")])  # message an hour ago
    _pin_last_active(MEMBER_SID, now)  # session acted just now → past msg consumed

    assert rr._session_has_unconsumed_chat(MEMBER_SID) is False


def test_self_sent_message_stays_quiet(isolated_state):
    """The session's own message must not wake it."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _pin_last_active(MEMBER_SID, now - 3600)
    _write_chat([_msg(MEMBER_SID, now, "evt-msg-0001")])  # self-sent

    assert rr._session_has_unconsumed_chat(MEMBER_SID) is False


def test_system_message_stays_quiet(isolated_state):
    """A SYSTEM-emitted message (role directives etc.) must not wake."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _pin_last_active(MEMBER_SID, now - 3600)
    _write_chat([_msg(chats.SYSTEM_SENDER_ID, now, "evt-msg-0001")])

    assert rr._session_has_unconsumed_chat(MEMBER_SID) is False


def test_non_member_stays_quiet(isolated_state):
    """A newer peer message in a chat the session isn't an accepted member of → no wake."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _pin_last_active(MEMBER_SID, now - 3600)
    _write_chat([_msg(PEER_SID, now, "evt-msg-0001")], member_state="pending")

    assert rr._session_has_unconsumed_chat(MEMBER_SID) is False


def test_clock_skew_epsilon_blocks_borderline(isolated_state):
    """A message within _TS_SKEW_EPSILON_S of last_active must NOT fire (skew guard)."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _pin_last_active(MEMBER_SID, now)
    # message 1s newer than last_active, inside the 2s epsilon → no fire
    _write_chat([_msg(PEER_SID, now + 1.0, "evt-msg-0001")])

    assert rr._session_has_unconsumed_chat(MEMBER_SID) is False
