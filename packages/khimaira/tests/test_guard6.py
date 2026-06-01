"""Tests for Guard-6 — heartbeat-liveness detector."""

from __future__ import annotations

import asyncio
import importlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_guard6(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fresh guard6 module with isolated state dir and cleared debounce."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    monkeypatch.setenv("KHIMAIRA_DARK_THRESHOLD_S", "600")  # 10 min for tests

    from khimaira.monitor import sessions as sessions_mod
    importlib.reload(sessions_mod)

    from khimaira.monitor import guard6 as guard6_mod
    importlib.reload(guard6_mod)

    guard6_mod._GUARD6_DARK.clear()
    yield guard6_mod, sessions_mod, state_root

    guard6_mod._GUARD6_DARK.clear()
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("KHIMAIRA_DARK_THRESHOLD_S", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(guard6_mod)


def _make_session_row(session_id: str, last_active_age_s: float, name: str = "") -> dict:
    return {
        "session_id": session_id,
        "name": name or session_id[:8],
        "status": "idle",
        "last_active_age_s": last_active_age_s,
    }


# ---------------------------------------------------------------------------
# Unit tests: _guard6_check_once logic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dark_session_fires(isolated_guard6):
    """Session dark >T_DARK → escalation fires and debounce is set."""
    guard6_mod, sessions_mod, state_root = isolated_guard6

    dark_sid = "aaaaaaaa-0000-0000-0000-000000000001"
    sessions_data = [_make_session_row(dark_sid, last_active_age_s=700)]  # > 600s threshold

    escalated = []

    async def _fake_escalate(sid, age, name, role):
        escalated.append((sid, age))

    with (
        patch.object(guard6_mod, "_get_roster_session_ids", return_value={dark_sid}),
        patch.object(
            sessions_mod, "list_sessions", return_value=sessions_data
        ),
        patch.object(guard6_mod, "_guard6_escalate", side_effect=_fake_escalate),
    ):
        await guard6_mod._guard6_check_once()

    assert len(escalated) == 1
    assert escalated[0][0] == dark_sid
    assert dark_sid in guard6_mod._GUARD6_DARK


@pytest.mark.asyncio
async def test_recently_active_session_does_not_fire(isolated_guard6):
    """Session active within T_DARK → no escalation."""
    guard6_mod, sessions_mod, _ = isolated_guard6

    active_sid = "bbbbbbbb-0000-0000-0000-000000000001"
    sessions_data = [_make_session_row(active_sid, last_active_age_s=60)]  # well under threshold

    escalated = []

    with (
        patch.object(guard6_mod, "_get_roster_session_ids", return_value={active_sid}),
        patch.object(sessions_mod, "list_sessions", return_value=sessions_data),
        patch.object(guard6_mod, "_guard6_escalate", side_effect=AsyncMock(side_effect=lambda *a, **kw: escalated.append(a))),
    ):
        await guard6_mod._guard6_check_once()

    assert len(escalated) == 0
    assert active_sid not in guard6_mod._GUARD6_DARK


@pytest.mark.asyncio
async def test_wind_down_suppresses(isolated_guard6):
    """Wind-down active → no escalation even if session is dark."""
    guard6_mod, sessions_mod, state_root = isolated_guard6

    # Create the wind-down sentinel
    sentinel = state_root / "khimaira" / "roster_wind_down"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.touch()

    dark_sid = "cccccccc-0000-0000-0000-000000000001"
    sessions_data = [_make_session_row(dark_sid, last_active_age_s=9999)]

    escalated = []

    with (
        patch.object(guard6_mod, "_get_roster_session_ids", return_value={dark_sid}),
        patch.object(sessions_mod, "list_sessions", return_value=sessions_data),
        patch.object(guard6_mod, "_guard6_escalate", side_effect=AsyncMock(side_effect=lambda *a, **kw: escalated.append(a))),
    ):
        await guard6_mod._guard6_check_once()

    assert len(escalated) == 0


@pytest.mark.asyncio
async def test_debounce_prevents_repeat_alert(isolated_guard6):
    """Dark session already debounced → no second escalation."""
    guard6_mod, sessions_mod, _ = isolated_guard6

    import time
    dark_sid = "dddddddd-0000-0000-0000-000000000001"
    guard6_mod._GUARD6_DARK[dark_sid] = time.time()  # recently debounced

    sessions_data = [_make_session_row(dark_sid, last_active_age_s=9999)]
    escalated = []

    with (
        patch.object(guard6_mod, "_get_roster_session_ids", return_value={dark_sid}),
        patch.object(sessions_mod, "list_sessions", return_value=sessions_data),
        patch.object(guard6_mod, "_guard6_escalate", side_effect=AsyncMock(side_effect=lambda *a, **kw: escalated.append(a))),
    ):
        await guard6_mod._guard6_check_once()

    assert len(escalated) == 0


@pytest.mark.asyncio
async def test_revival_re_arms_debounce(isolated_guard6):
    """Session revives (age < T_DARK) → debounce entry cleared, next dark fires again."""
    guard6_mod, sessions_mod, _ = isolated_guard6

    sid = "eeeeeeee-0000-0000-0000-000000000001"
    import time as _time
    guard6_mod._GUARD6_DARK[sid] = _time.time()  # was dark (recently)

    # Now revived — age below threshold
    sessions_data = [_make_session_row(sid, last_active_age_s=30)]

    with (
        patch.object(guard6_mod, "_get_roster_session_ids", return_value={sid}),
        patch.object(sessions_mod, "list_sessions", return_value=sessions_data),
    ):
        await guard6_mod._guard6_check_once()

    # Debounce should be cleared
    assert sid not in guard6_mod._GUARD6_DARK


@pytest.mark.asyncio
async def test_idle_but_reachable_session_not_dark(isolated_guard6):
    """Session is idle (last_active > T_DARK) but SSE-alive (reachable) → NOT dark.

    Arch catch: dark = inactive AND unreachable. A holding/awaiting-gate session
    with an open SSE stream is ALIVE, not dark. Without this check Guard-6 would
    false-alarm on every idle-but-connected session after 45min.
    """
    guard6_mod, sessions_mod, _ = isolated_guard6

    idle_sid = "11111111-0000-0000-0000-000000000001"
    # Session is inactive (700s > T_DARK=600s) but SSE-reachable
    sessions_data = [_make_session_row(idle_sid, last_active_age_s=700)]
    escalated = []

    with (
        patch.object(guard6_mod, "_get_roster_session_ids", return_value={idle_sid}),
        patch.object(sessions_mod, "list_sessions", return_value=sessions_data),
        patch.object(guard6_mod, "_is_reachable", return_value=True),  # SSE alive
        patch.object(guard6_mod, "_guard6_escalate", side_effect=AsyncMock(side_effect=lambda *a, **kw: escalated.append(a))),
    ):
        await guard6_mod._guard6_check_once()

    assert len(escalated) == 0, "Idle-but-reachable session must NOT be flagged dark"
    assert idle_sid not in guard6_mod._GUARD6_DARK


@pytest.mark.asyncio
async def test_non_roster_session_ignored(isolated_guard6):
    """Session not in roster → ignored even if dark."""
    guard6_mod, sessions_mod, _ = isolated_guard6

    foreign_sid = "ffffffff-0000-0000-0000-000000000001"
    sessions_data = [_make_session_row(foreign_sid, last_active_age_s=9999)]
    escalated = []

    with (
        patch.object(guard6_mod, "_get_roster_session_ids", return_value=set()),  # empty roster
        patch.object(sessions_mod, "list_sessions", return_value=sessions_data),
        patch.object(guard6_mod, "_guard6_escalate", side_effect=AsyncMock(side_effect=lambda *a, **kw: escalated.append(a))),
    ):
        await guard6_mod._guard6_check_once()

    assert len(escalated) == 0
