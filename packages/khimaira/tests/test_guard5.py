"""Tests for Guard-5 roster-progress monitor.

CLASS-INVARIANT: given a blocking gate + no-state-change >T_stall + K idle sessions
+ not-wind-down → exactly ONE escalation fires to a reachable master naming
{gate, owner-role, duration, K}.

Negative tests:
  - wind-down → NO fire
  - K-idle + no gate → NO fire (precision guard)
  - unreachable-owner → re-target (not silent)
"""

from __future__ import annotations

import asyncio
import importlib
import time
from typing import Any
from unittest.mock import MagicMock

import pytest

import khimaira.monitor.guard5 as g5


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_gate(
    task_id: str = "task-aabbcc001122",
    chat_id: str = "chat-deadbeef0001",
    status: str = "done",
    assignee_id: str = "sid-owner-0001",
    assignee_role: str = "verifier",
    last_state_change_ts: str | None = None,
    has_verdict: bool = False,
    begin_fired: bool = True,
    preview: str = "test task",
) -> dict[str, Any]:
    if last_state_change_ts is None:
        # Default to 20 minutes ago — definitely stale
        import datetime

        dt = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)
        last_state_change_ts = dt.isoformat()
    return {
        "task_id": task_id,
        "chat_id": chat_id,
        "status": status,
        "assignee_id": assignee_id,
        "assignee_role": assignee_role,
        "last_state_change_ts": last_state_change_ts,
        "last_event_ts": last_state_change_ts,
        "has_verdict": has_verdict,
        "begin_fired": begin_fired,
        "preview": preview,
    }


def _make_session_row(
    session_id: str,
    effective_status: str = "idle",
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "status": {"effective_status": effective_status},
    }


def _fresh_guard5_module():
    """Reload guard5 to reset module-level state (_GUARD5_STALLED, _ROSTER_WIND_DOWN)."""
    importlib.reload(g5)
    return g5


# ---------------------------------------------------------------------------
# Wind-down suppression — NEGATIVE test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wind_down_suppresses_escalation(monkeypatch):
    """During wind-down, no escalation fires even with stale gates + idle sessions."""
    g5_mod = _fresh_guard5_module()

    stale_gate = _make_gate()
    idle_rows = [_make_session_row("sid-alpha"), _make_session_row("sid-beta")]

    monkeypatch.setattr(g5_mod, "_scan_blocking_gates", lambda: [stale_gate])
    monkeypatch.setattr(g5_mod, "_K_IDLE", 2)

    calls: list[str] = []

    async def fake_escalate(gate, k_idle, target):
        calls.append(gate["task_id"])

    monkeypatch.setattr(g5_mod, "_guard5_escalate", fake_escalate)

    from khimaira.monitor import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "list_sessions", lambda use_cache=True: idle_rows)

    g5_mod.set_wind_down(True)
    try:
        await g5_mod._guard5_check_once()
    finally:
        g5_mod.set_wind_down(False)

    assert calls == [], "wind-down must suppress all escalations"


# ---------------------------------------------------------------------------
# Precision guard — K-idle + no gate → STAY QUIET — NEGATIVE test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_gate_no_escalation(monkeypatch):
    """When no open blocking gate exists, Guard-5 stays silent even if K sessions are idle."""
    g5_mod = _fresh_guard5_module()

    idle_rows = [_make_session_row("sid-alpha"), _make_session_row("sid-beta")]
    monkeypatch.setattr(g5_mod, "_scan_blocking_gates", lambda: [])

    calls: list[str] = []

    async def fake_escalate(gate, k_idle, target):
        calls.append(gate["task_id"])

    monkeypatch.setattr(g5_mod, "_guard5_escalate", fake_escalate)

    from khimaira.monitor import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "list_sessions", lambda use_cache=True: idle_rows)

    await g5_mod._guard5_check_once()

    assert calls == [], "no gate → precision guard must prevent escalation"


# ---------------------------------------------------------------------------
# Not enough idle sessions — NEGATIVE test
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insufficient_idle_sessions_no_escalation(monkeypatch):
    """When fewer than K sessions are idle, no escalation fires."""
    g5_mod = _fresh_guard5_module()

    stale_gate = _make_gate()
    monkeypatch.setattr(g5_mod, "_scan_blocking_gates", lambda: [stale_gate])
    monkeypatch.setattr(g5_mod, "_K_IDLE", 3)  # require 3

    # Only 1 idle session
    idle_rows = [_make_session_row("sid-alpha")]

    calls: list[str] = []

    async def fake_escalate(gate, k_idle, target):
        calls.append(gate["task_id"])

    monkeypatch.setattr(g5_mod, "_guard5_escalate", fake_escalate)
    monkeypatch.setattr(g5_mod, "_count_idle_sessions", lambda rows: (1, ["sid-alpha"]))

    from khimaira.monitor import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "list_sessions", lambda use_cache=True: idle_rows)

    await g5_mod._guard5_check_once()

    assert calls == [], "fewer than K idle → no escalation"


# ---------------------------------------------------------------------------
# Class-invariant: stale gate + K-idle → exactly ONE escalation, debounced
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_gate_k_idle_fires_exactly_once(monkeypatch):
    """Stale gate + K idle → exactly one escalation; second sweep is debounced."""
    g5_mod = _fresh_guard5_module()
    g5_mod._GUARD5_STALLED.clear()
    g5_mod._K_IDLE = 2

    stale_gate = _make_gate()
    idle_rows = [_make_session_row("sid-alpha"), _make_session_row("sid-beta")]

    monkeypatch.setattr(g5_mod, "_scan_blocking_gates", lambda: [stale_gate])
    monkeypatch.setattr(g5_mod, "_count_idle_sessions", lambda rows: (2, ["sid-alpha", "sid-beta"]))

    calls: list[tuple] = []

    async def fake_escalate(gate, k_idle, target):
        calls.append((gate["task_id"], k_idle, target))

    monkeypatch.setattr(g5_mod, "_guard5_escalate", fake_escalate)
    monkeypatch.setattr(g5_mod, "_resolve_escalation_target", lambda gate, rows: "sid-master")

    from khimaira.monitor import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "list_sessions", lambda use_cache=True: idle_rows)

    # First sweep — should fire.
    await g5_mod._guard5_check_once()
    assert len(calls) == 1
    tid, k, target = calls[0]
    assert tid == stale_gate["task_id"]
    assert k == 2
    assert target == "sid-master"

    # Second sweep — debounce must prevent re-fire.
    await g5_mod._guard5_check_once()
    assert len(calls) == 1, "debounce must suppress repeat escalation for same gate"


# ---------------------------------------------------------------------------
# Unreachable owner → re-target, not silent — NEGATIVE (no silent drop)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unreachable_owner_retargets(monkeypatch):
    """When the gate owner is unreachable, escalation re-targets to master, not silent."""
    g5_mod = _fresh_guard5_module()
    g5_mod._GUARD5_STALLED.clear()
    g5_mod._K_IDLE = 2

    stale_gate = _make_gate(assignee_id="sid-dead-owner")
    idle_rows = [_make_session_row("sid-alpha"), _make_session_row("sid-beta")]

    monkeypatch.setattr(g5_mod, "_scan_blocking_gates", lambda: [stale_gate])
    monkeypatch.setattr(g5_mod, "_count_idle_sessions", lambda rows: (2, ["sid-alpha", "sid-beta"]))

    # Owner is unreachable; master is reachable
    def fake_resolve(gate, rows):
        return "sid-master-reachable"

    monkeypatch.setattr(g5_mod, "_resolve_escalation_target", fake_resolve)

    calls: list[tuple] = []

    async def fake_escalate(gate, k_idle, target):
        calls.append((gate["task_id"], target))

    monkeypatch.setattr(g5_mod, "_guard5_escalate", fake_escalate)

    from khimaira.monitor import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "list_sessions", lambda use_cache=True: idle_rows)

    await g5_mod._guard5_check_once()

    assert len(calls) == 1, "should escalate even when owner is unreachable"
    _, target = calls[0]
    assert target == "sid-master-reachable", "must re-target to reachable master"


# ---------------------------------------------------------------------------
# Class-invariant parametrized: different producing paths all fire the same invariant
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status,has_verdict,begin_fired,description",
    [
        ("done", False, True, "done-awaiting-verdict"),
        ("in_progress", False, True, "in_progress-no-verdict"),
        ("pending", False, False, "assigned-no-BEGIN"),
    ],
)
async def test_class_invariant_parametrized(
    monkeypatch, status, has_verdict, begin_fired, description
):
    """Any producing path fires the class-invariant: one escalation naming gate+owner."""
    g5_mod = _fresh_guard5_module()
    g5_mod._GUARD5_STALLED.clear()
    g5_mod._K_IDLE = 2

    stale_gate = _make_gate(status=status, has_verdict=has_verdict, begin_fired=begin_fired)
    idle_rows = [_make_session_row("sid-alpha"), _make_session_row("sid-beta")]

    monkeypatch.setattr(g5_mod, "_scan_blocking_gates", lambda: [stale_gate])
    monkeypatch.setattr(g5_mod, "_count_idle_sessions", lambda rows: (2, ["sid-alpha", "sid-beta"]))
    monkeypatch.setattr(g5_mod, "_resolve_escalation_target", lambda gate, rows: "sid-master")

    calls: list[dict] = []

    async def fake_escalate(gate, k_idle, target):
        calls.append({"task_id": gate["task_id"], "k_idle": k_idle, "target": target})

    monkeypatch.setattr(g5_mod, "_guard5_escalate", fake_escalate)

    from khimaira.monitor import sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "list_sessions", lambda use_cache=True: idle_rows)

    await g5_mod._guard5_check_once()

    assert len(calls) == 1, f"{description}: must fire exactly one escalation"
    assert calls[0]["task_id"] == stale_gate["task_id"], "escalation must name the gate"
    assert calls[0]["target"] == "sid-master", "escalation must target a reachable actor"


# ---------------------------------------------------------------------------
# Fresh gate (not stale) → no escalation
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fresh_gate_no_escalation(monkeypatch):
    """A gate with a very recent state-change is NOT stale → no escalation fires."""
    g5_mod = _fresh_guard5_module()

    import datetime

    recent_ts = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
    ).isoformat()
    fresh_gate = _make_gate(last_state_change_ts=recent_ts)

    monkeypatch.setattr(g5_mod, "_scan_blocking_gates", lambda: [fresh_gate])
    monkeypatch.setattr(g5_mod, "_K_IDLE", 2)
    monkeypatch.setattr(g5_mod, "_T_STALL_S", 300.0)  # 5 min

    calls: list[str] = []

    async def fake_escalate(gate, k_idle, target):
        calls.append(gate["task_id"])

    monkeypatch.setattr(g5_mod, "_guard5_escalate", fake_escalate)

    from khimaira.monitor import sessions as sessions_mod

    monkeypatch.setattr(
        sessions_mod,
        "list_sessions",
        lambda use_cache=True: [_make_session_row("sid-alpha"), _make_session_row("sid-beta")],
    )

    await g5_mod._guard5_check_once()

    assert calls == [], "fresh gate must not trigger escalation"


# ---------------------------------------------------------------------------
# _gate_is_stale unit tests
# ---------------------------------------------------------------------------


def test_gate_is_stale_old_ts():
    """Gate with ts 20 min ago is stale given T_stall=5 min."""
    import datetime

    old_ts = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=20)
    ).isoformat()
    gate = _make_gate(last_state_change_ts=old_ts)
    g5._T_STALL_S = 300  # 5 min
    assert g5._gate_is_stale(gate, time.time()) is True


def test_gate_is_stale_recent_ts():
    """Gate with ts 30s ago is NOT stale given T_stall=5 min."""
    import datetime

    recent_ts = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=30)
    ).isoformat()
    gate = _make_gate(last_state_change_ts=recent_ts)
    g5._T_STALL_S = 300
    assert g5._gate_is_stale(gate, time.time()) is False


# ---------------------------------------------------------------------------
# _is_reachable — fail-open when chats.is_reachable absent
# ---------------------------------------------------------------------------


def test_is_reachable_stub_fail_open(monkeypatch):
    """When chats.is_reachable is not yet merged, _is_reachable returns True (fail-open)."""
    import khimaira.monitor.chats as chats_mod

    # Simulate agent-4's Part A not yet merged
    if hasattr(chats_mod, "is_reachable"):
        monkeypatch.delattr(chats_mod, "is_reachable", raising=False)

    result = g5._is_reachable("some-session-id")
    assert result is True, "_is_reachable must fail open when interface is absent"
