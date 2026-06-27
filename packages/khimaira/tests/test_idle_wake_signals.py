"""Tests for the idle-wake OR-gate signals: unread-inbox + directed-unanswered.

CONTEXT: roster_recovery._process_window wakes an idle session only if its OR-gate
trips. Path 1 added owed-verdict obligations; the OR-gate also includes:

  A) _session_has_unread_inbox       — notices / handoffs piling up unread.
  B) _session_has_directed_unanswered — a DIRECTED chat message (to=[me] or
     @<my-name>) newer than the session's OWN last post in that chat. This is the
     #23 directed-wake signal: it REPLACED the prior _session_has_unconsumed_chat,
     which fired on ANY peer message newer than last_active and so (1) over-woke
     idle-consult seats on undirected chatter and (2) false-cleared the moment the
     agent took any unrelated action (last_active is global). Directedness + last
     OWN POST fixes both.

STORM-SAFETY ACCEPTANCE (the master's hard constraint): the signal MUST read ZERO on
undirected peer chatter (the over-wake fix) and on a directed ask the session has
already replied to. The fire tests prove it trips on a real directed ask; the quiet
tests prove it stays silent otherwise.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from khimaira.monitor import chats
from khimaira.monitor import roster_recovery as rr
from khimaira.monitor import sessions as sessions_mod

MEMBER_SID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"  # session under test (name "void")
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


def _msg(
    sender_id: str,
    ts_epoch: float,
    event_id: str,
    to: list[str] | None = None,
    body: str = "ping",
) -> dict:
    from datetime import datetime, timezone

    return {
        "kind": chats.MSG,
        "chat_id": CHAT_ID,
        "sender_id": sender_id,
        "body": body,
        "to": to,
        "ts": datetime.fromtimestamp(ts_epoch, tz=timezone.utc).isoformat(),
        "event_id": event_id,
    }


# --- Signal A: unread inbox ------------------------------------------------


def test_unread_inbox_fires(isolated_state):
    sessions_mod.set_status(MEMBER_SID, "idle")
    sessions_mod.post_notice(MEMBER_SID, "peer left you a note", fire_desktop_notify=False)
    assert rr._session_has_unread_inbox(MEMBER_SID) is True


def test_drained_inbox_stays_quiet(isolated_state):
    sessions_mod.set_status(MEMBER_SID, "idle")
    sessions_mod.post_notice(MEMBER_SID, "note", fire_desktop_notify=False)
    sessions_mod.pending_notes(MEMBER_SID, mark_read=True)
    assert rr._session_has_unread_inbox(MEMBER_SID) is False


def test_no_inbox_stays_quiet(isolated_state):
    sessions_mod.set_status(MEMBER_SID, "idle")
    assert rr._session_has_unread_inbox(MEMBER_SID) is False


# --- Signal B: directed-unanswered (the #23 directed wake) -----------------


def test_directed_to_me_fires(isolated_state):
    """A to=[me] message newer than my last post → wake."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat([_msg(PEER_SID, now, "evt-1", to=[MEMBER_SID])])
    assert rr._session_has_directed_unanswered(MEMBER_SID) is True


def test_at_mention_fires(isolated_state):
    """An @<my-name> mention newer than my last post → wake."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat([_msg(PEER_SID, now, "evt-1", body="hey @void can you look")])
    assert rr._session_has_directed_unanswered(MEMBER_SID) is True


def test_undirected_peer_chatter_stays_quiet(isolated_state):
    """THE over-wake fix: an undirected peer message (no to, no @mention) must NOT
    wake an idle-consult seat, even though it's newer than the session's last post."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat([_msg(PEER_SID, now, "evt-1", body="status update for the room")])
    assert rr._session_has_directed_unanswered(MEMBER_SID) is False


def test_directed_but_answered_since_stays_quiet(isolated_state):
    """A directed ask the session has POSTED since → answered → no wake."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat(
        [
            _msg(PEER_SID, now - 100, "evt-1", to=[MEMBER_SID]),  # directed ask
            _msg(MEMBER_SID, now - 50, "evt-2", body="on it"),  # my reply after
        ]
    )
    assert rr._session_has_directed_unanswered(MEMBER_SID) is False


def test_directed_unanswered_survives_unrelated_action(isolated_state):
    """The false-negative fix: a directed ask stays owed even after the session
    takes an UNRELATED action (last_active advances) as long as it hasn't replied
    in chat. The old signal cleared on any action; this keys on last own POST."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat([_msg(PEER_SID, now - 100, "evt-1", to=[MEMBER_SID])])  # directed, no reply
    _pin_last_active(MEMBER_SID, now)  # did something unrelated just now
    assert rr._session_has_directed_unanswered(MEMBER_SID) is True


def test_self_directed_stays_quiet(isolated_state):
    """The session's own message must not wake it (even if to=[self])."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat([_msg(MEMBER_SID, now, "evt-1", to=[MEMBER_SID])])
    assert rr._session_has_directed_unanswered(MEMBER_SID) is False


def test_system_directed_stays_quiet(isolated_state):
    """A SYSTEM-emitted directed message must not wake."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat([_msg(chats.SYSTEM_SENDER_ID, now, "evt-1", to=[MEMBER_SID])])
    assert rr._session_has_directed_unanswered(MEMBER_SID) is False


def test_non_member_stays_quiet(isolated_state):
    """A directed message in a chat the session isn't an accepted member of → no wake."""
    sessions_mod.set_status(MEMBER_SID, "idle")
    now = time.time()
    _write_chat([_msg(PEER_SID, now, "evt-1", to=[MEMBER_SID])], member_state="pending")
    assert rr._session_has_directed_unanswered(MEMBER_SID) is False
