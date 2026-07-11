"""Path-3 tests: Guard-5 stall → kitty-wake the master's window.

Guard-5 detects a stalled gate-pipeline and posts a notice to the escalation target.
A notice does NOT wake a turn-gated master (the muther stall). Path 3 bridges the
escalation into auto_dispatch._maybe_wake_idle_master so a confirmed stall wakes the
master's WINDOW once — reusing that actuator's per-master cooldown + idle/busy guards,
NOT a parallel actuator. The bridge fires ONLY when the escalation target is the
master (same-role-peer targets are handled by the Path-1 owed-verdict obligation).

Acceptance: wakes the master once on a stall (with a stall-specific message), is
cooldown-bounded (no second wake within the window), and stays silent when the target
isn't the master.
"""

from __future__ import annotations

import asyncio

from khimaira.monitor import auto_dispatch as ad
from khimaira.monitor import guard5

MASTER_UUID = "11111111-1111-1111-1111-111111111111"
VERIFIER_UUID = "22222222-2222-2222-2222-222222222222"
CHAT_ID = "chat-abcdef000001"
TASK_ID = "task-0123456789ab"


def _run(coro):
    # asyncio.run() creates a FRESH loop each call — robust to test ordering.
    # get_event_loop().run_until_complete broke when an earlier async test in
    # the same session closed the shared loop (see test_process_window_wake_
    # integration.py's _drive_process_window for the same fix).
    return asyncio.run(coro)


def _gate() -> dict:
    return {
        "task_id": TASK_ID,
        "chat_id": CHAT_ID,
        "status": "done",
        "assignee_role": "verifier",
        "assignee_id": VERIFIER_UUID,
        "preview": "implement X",
        "last_state_change_ts": "2026-06-17T00:00:00+00:00",
    }


def _room(target_role: str) -> dict:
    return {
        "meta": {"member_roles": {MASTER_UUID: target_role}},
        "members": {MASTER_UUID: {"session_name": "khimaira-0"}},
        "messages": [],
    }


# --- _maybe_wake_idle_master: wake_text override + cooldown ------------------


def test_wake_text_override_and_cooldown(monkeypatch):
    """wake_text overrides the inject message; the per-master cooldown bounds it to once."""
    injected: list[str] = []

    monkeypatch.setattr(
        "khimaira.monitor.sessions.summary",
        lambda sid: {"last_active_age_s": 9999.0},  # idle long enough
    )
    async def _discover():
        return [{"role": "master", "window_id": 7, "raw_name": "khimaira-0"}]

    async def _screen(wid):
        return ""

    async def _inject(wid, text, name=""):
        injected.append(text)
        return True

    monkeypatch.setattr(
        "khimaira.monitor.roster_recovery._discover_roster_windows", _discover
    )
    monkeypatch.setattr("khimaira.monitor.roster_recovery._get_screen", _screen)
    monkeypatch.setattr(
        "khimaira.monitor.roster_recovery._is_busy", lambda screen: False
    )
    monkeypatch.setattr(
        "khimaira.monitor.roster_recovery._inject_text_and_submit", _inject
    )

    ad._last_master_wake.pop(MASTER_UUID, None)  # clear cooldown state

    stall_msg = "⏰ pipeline stalled: task-01234567 awaiting verifier's verdict"
    _run(ad._maybe_wake_idle_master(MASTER_UUID, owed_count=1, wake_text=stall_msg))

    assert injected == [stall_msg]  # used the override, not the default text

    # Second call immediately → cooldown blocks it (wakes once, not a storm).
    _run(ad._maybe_wake_idle_master(MASTER_UUID, owed_count=1, wake_text=stall_msg))

    assert injected == [stall_msg]  # still exactly one inject
    ad._last_master_wake.pop(MASTER_UUID, None)


def test_active_master_not_woken(monkeypatch):
    """If the master isn't idle long enough, the stall wake does not fire."""
    injected: list[str] = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.summary",
        lambda sid: {"last_active_age_s": 5.0},  # active
    )
    async def _inject(wid, text, name=""):
        injected.append(text)
        return True

    monkeypatch.setattr(
        "khimaira.monitor.roster_recovery._inject_text_and_submit", _inject
    )
    ad._last_master_wake.pop(MASTER_UUID, None)

    _run(ad._maybe_wake_idle_master(MASTER_UUID, owed_count=1, wake_text="x"))

    assert injected == []
    ad._last_master_wake.pop(MASTER_UUID, None)


# --- the bridge: routes only when target is the master ----------------------


def test_bridge_wakes_when_target_is_master(monkeypatch):
    """Target resolves to the chat's master → bridge calls the wake actuator once."""
    calls: list[dict] = []

    monkeypatch.setattr(
        "khimaira.monitor.chats.load_room", lambda chat_id: _room("master")
    )

    async def _fake_wake(master_id, owed_count, **kw):
        calls.append({"master_id": master_id, **kw})

    monkeypatch.setattr(ad, "_maybe_wake_idle_master", _fake_wake)

    _run(
        guard5._wake_master_window_on_stall(
            _gate(), k_idle=2, target_session_id=MASTER_UUID
        )
    )

    assert len(calls) == 1
    assert calls[0]["master_id"] == MASTER_UUID
    assert calls[0]["chat_id"] == CHAT_ID
    assert calls[0]["master_name"] == "khimaira-0"
    assert TASK_ID[:8] in calls[0]["wake_text"]
    assert "verdict" in calls[0]["wake_text"]


def test_bridge_silent_when_target_not_master(monkeypatch):
    """Target is a same-role peer (not master) → bridge does NOT wake a window."""
    calls: list[dict] = []

    monkeypatch.setattr(
        "khimaira.monitor.chats.load_room", lambda chat_id: _room("verifier")
    )

    async def _fake_wake(master_id, owed_count, **kw):
        calls.append(master_id)

    monkeypatch.setattr(ad, "_maybe_wake_idle_master", _fake_wake)

    _run(
        guard5._wake_master_window_on_stall(
            _gate(), k_idle=2, target_session_id=MASTER_UUID
        )
    )

    assert calls == []
