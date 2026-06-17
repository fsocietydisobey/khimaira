"""Integration test for the idle-owing WAKE PATH end-to-end through _process_window.

The unit tests cover the obligation detector + the signal helpers in isolation. This
test exercises the REAL roster_recovery._process_window decision path — obligation
detection → recency gate → idle gate → OR-gate → actuator — against isolated state,
mocking only the kitty I/O boundary (screen read + text inject). It is the integration
the suite was missing, and the offline analog of the live "throwaway-seat wake-fires"
proof: a RECENT owed-verdict seat that is idle gets a wake inject; a STALE one stays
silent.

Boundaries mocked (kitty side only): _resolve_session_by_name, _get_screen,
_inject_text_and_submit, _session_has_recent_wip. Everything else is real:
_get_session_obligations (isolated chat JSONL), list_sessions (isolated session dir +
mtime), the idle threshold, the busy check, and the per-window cooldown.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone

from khimaira.monitor import chats
from khimaira.monitor import roster_recovery as rr
from khimaira.monitor import sessions as sessions_mod

SEAT_SID = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
CRITIC_SID = "11111111-2222-3333-4444-555555555555"
SEAT_NAME = "void-test-seat"
CHAT_ID = "chat-0ffee0000001"
WORK_TASK = "task-aceace000001"
WINDOW_ID = 4242


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


def _write_owed_chat(done_epoch: float) -> None:
    """Isolated chat: SEAT is an accepted verifier; a done task with critic-approve but
    no verifier-ship → SEAT owes `ship`. `done_epoch` sets the done-transition ts.
    """
    chat_dir = chats._chat_dir()
    chat_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        {
            "kind": chats.MEMBER,
            "session_id": SEAT_SID,
            "session_name": SEAT_NAME,
            "state": chats.ACCEPTED,
            "ts": _iso(done_epoch - 100),
            "event_id": "evt-m1",
        },
        {
            "kind": chats.META,
            "member_roles": {
                SEAT_SID: chats.ROLE_VERIFIER,
                CRITIC_SID: chats.ROLE_CRITIC,
            },
        },
        {
            "kind": chats.TASK,
            "id": WORK_TASK,
            "status": chats.TASK_DONE,
            "ts": _iso(done_epoch),
        },
        {
            "kind": chats.TASK_VERDICT,
            "task_id": WORK_TASK,
            "verdict": "approve",
            "by_session_id": CRITIC_SID,
        },
    ]
    (chat_dir / f"{CHAT_ID}.jsonl").write_text(
        "".join(json.dumps(x) + "\n" for x in lines)
    )


def _register_idle_seat(idle_s: float = 600.0) -> None:
    """Create the session dir + force last_active to idle_s seconds ago."""
    sessions_mod.set_status(SEAT_SID, "idle")
    past = time.time() - idle_s
    sd = sessions_mod._session_dir(SEAT_SID)
    for p in sd.iterdir():
        if p.is_file():
            os.utime(p, (past, past))


def _drive_process_window(monkeypatch) -> list[tuple[int, str]]:
    """Run the real _process_window with kitty I/O boundaries mocked; return injects."""
    injected: list[tuple[int, str]] = []

    monkeypatch.setattr(rr, "_resolve_session_by_name", lambda name: SEAT_SID)
    # A benign idle screen → not busy, no HITL prompt, no rate-limit, no context %.
    monkeypatch.setattr(rr, "_get_screen", lambda wid: "> \n(idle prompt)\n")
    monkeypatch.setattr(rr, "_session_has_recent_wip", lambda *a, **k: False)
    monkeypatch.setattr(
        rr,
        "_inject_text_and_submit",
        lambda wid, text, name="": injected.append((wid, text)) or True,
    )

    win = {
        "window_id": WINDOW_ID,
        "role": chats.ROLE_VERIFIER,
        "raw_name": SEAT_NAME,
        "cmdline": "claude",
    }
    rr._DEBOUNCE.clear()  # fresh cooldown state
    asyncio.get_event_loop().run_until_complete(rr._process_window(win))
    return injected


def test_recent_owing_idle_seat_gets_woken(isolated_state, monkeypatch):
    """RECENT owed-verdict + idle >5min → _process_window injects a wake."""
    _register_idle_seat(idle_s=600.0)
    _write_owed_chat(done_epoch=time.time() - 120)  # done 2 min ago — within window

    injected = _drive_process_window(monkeypatch)

    assert len(injected) == 1, injected
    assert injected[0][0] == WINDOW_ID
    assert "resume" in injected[0][1].lower()


def test_stale_owing_seat_stays_silent(isolated_state, monkeypatch):
    """STALE owed-verdict (done days ago) → no obligation → no wake (the muther storm)."""
    _register_idle_seat(idle_s=600.0)
    _write_owed_chat(done_epoch=time.time() - 5 * 24 * 3600)  # done 5 days ago

    injected = _drive_process_window(monkeypatch)

    assert injected == []


def test_recent_owing_but_active_seat_stays_silent(isolated_state, monkeypatch):
    """RECENT owed-verdict but seat NOT idle (<5min) → no wake (don't interrupt work)."""
    _register_idle_seat(idle_s=30.0)  # active
    _write_owed_chat(done_epoch=time.time() - 120)

    injected = _drive_process_window(monkeypatch)

    assert injected == []
