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


def _drive_process_window(
    monkeypatch, *, win_extra: dict | None = None, inject_result: bool = True
) -> list[tuple[int, str]]:
    """Run the real _process_window with kitty I/O boundaries mocked; return injects.

    win_extra: extra keys merged into the window dict (e.g. is_focused=True).
    inject_result: what the mocked _inject_text_and_submit returns (False simulates
    a TOCTOU abort, for the cooldown-on-failure guard).
    """
    injected: list[tuple[int, str]] = []

    monkeypatch.setattr(rr, "_resolve_session_by_name", lambda name: SEAT_SID)
    # A benign idle screen → not busy, no HITL prompt, no rate-limit, no context %.
    monkeypatch.setattr(rr, "_get_screen", lambda wid: "> \n(idle prompt)\n")
    monkeypatch.setattr(rr, "_session_has_recent_wip", lambda *a, **k: False)
    monkeypatch.setattr(
        rr,
        "_inject_text_and_submit",
        lambda wid, text, name="": (injected.append((wid, text)), inject_result)[1],
    )

    win = {
        "window_id": WINDOW_ID,
        "role": chats.ROLE_VERIFIER,
        "raw_name": SEAT_NAME,
        "cmdline": "claude",
    }
    if win_extra:
        win.update(win_extra)
    rr._DEBOUNCE.clear()  # fresh cooldown state
    # asyncio.run() creates a FRESH loop each call — robust to test ordering. The old
    # get_event_loop().run_until_complete broke when an earlier async test closed the
    # shared loop (DeprecationWarning + RuntimeError under full-suite ordering).
    asyncio.run(rr._process_window(win))
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


# ---------------------------------------------------------------------------
# Guards added after the muther-intake injection storm (2026-06-18)
# ---------------------------------------------------------------------------


def test_user_focused_window_is_never_injected(isolated_state, monkeypatch):
    """HUMAN-PRESENCE guard: a window the user is focused on (is_focused=True) is
    skipped even when it has a recent owing obligation + is idle — injecting would
    type under the user's cursor (the muther-intake incident)."""
    _register_idle_seat(idle_s=600.0)
    _write_owed_chat(done_epoch=time.time() - 120)  # would wake if not focused

    injected = _drive_process_window(monkeypatch, win_extra={"is_focused": True})

    assert injected == []


def test_focus_override_env_allows_injection(isolated_state, monkeypatch):
    """The override lets headless/test rosters inject into a focused window."""
    monkeypatch.setenv("KHIMAIRA_ROSTER_INJECT_FOCUSED", "1")
    _register_idle_seat(idle_s=600.0)
    _write_owed_chat(done_epoch=time.time() - 120)

    injected = _drive_process_window(monkeypatch, win_extra={"is_focused": True})

    assert len(injected) == 1  # override → focused window CAN be injected


def test_human_interface_role_is_never_actuated(isolated_state, monkeypatch):
    """HUMAN-INTERFACE guard: an intake/master seat is never auto-actuated even when
    unfocused + owing + idle — the user drives their own interface window (the
    muther-intake-compaction incident: it got /compact'd whenever the user tabbed
    away). Agents stay auto-managed."""
    _register_idle_seat(idle_s=600.0)
    _write_owed_chat(done_epoch=time.time() - 120)

    # role=intake (human interface), is_focused=False (user is elsewhere) → still skipped
    injected = _drive_process_window(
        monkeypatch, win_extra={"role": chats.ROLE_INTAKE, "is_focused": False}
    )

    assert injected == []


def test_failed_wake_actuation_sets_cooldown(isolated_state, monkeypatch):
    """STORM guard: a wake that TOCTOU-aborts (inject returns False) must still set
    the debounce, so it does NOT retry every sweep (the muther-intake retry storm)."""
    _register_idle_seat(idle_s=600.0)
    _write_owed_chat(done_epoch=time.time() - 120)

    injected = _drive_process_window(monkeypatch, inject_result=False)

    assert len(injected) == 1  # it attempted once
    # ...but the cooldown is set despite the abort, so the next sweep is suppressed.
    assert (WINDOW_ID, "wake") in rr._DEBOUNCE
