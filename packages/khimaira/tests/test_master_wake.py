"""Master-drive wake (2026-06-10) — _maybe_wake_idle_master.

Closes IDLE-ROSTER BLINDNESS: when owed work is undispatched AND the master is
idle past the threshold, nudge its window to drive. Conservative — never wakes
without owed work, never interrupts an active/busy master, deduped by cooldown.
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def ad():
    from khimaira.monitor import auto_dispatch as mod
    importlib.reload(mod)
    mod._last_master_wake.clear()
    return mod


def _wire(mod, monkeypatch, *, idle_s, role_windows, screen=""):
    """Stub sessions.summary (master idle_s) + roster_recovery window/inject."""
    import khimaira.monitor.sessions as sess
    monkeypatch.setattr(sess, "summary", lambda sid: {"last_active_age_s": idle_s})
    import khimaira.monitor.roster_recovery as rr
    monkeypatch.setattr(rr, "_discover_roster_windows", lambda: role_windows)
    monkeypatch.setattr(rr, "_get_screen", lambda wid: screen)
    monkeypatch.setattr(rr, "_is_busy", lambda s: "esc to interrupt" in (s or "").lower())
    injected = []
    monkeypatch.setattr(rr, "_inject_text_and_submit",
                        lambda wid, text, title="": injected.append((wid, text)) or True)
    return injected


MASTER = "mmmm0000-0000-0000-0000-000000000001"
WIN = [{"window_id": 5, "role": "master", "raw_name": "muther"}]


@pytest.mark.asyncio
async def test_no_wake_when_no_owed_work(ad, monkeypatch):
    injected = _wire(ad, monkeypatch, idle_s=9999, role_windows=WIN)
    await ad._maybe_wake_idle_master(MASTER, owed_count=0)
    assert injected == [], "owed_count=0 must never wake (don't nag)"


@pytest.mark.asyncio
async def test_wakes_idle_master_with_owed_work(ad, monkeypatch):
    injected = _wire(ad, monkeypatch, idle_s=9999, role_windows=WIN)
    await ad._maybe_wake_idle_master(MASTER, owed_count=2)
    assert len(injected) == 1
    wid, text = injected[0]
    assert wid == 5
    assert "DRIVE" in text and "2 owed" in text


@pytest.mark.asyncio
async def test_no_wake_when_master_active(ad, monkeypatch):
    injected = _wire(ad, monkeypatch, idle_s=10, role_windows=WIN)  # below 180s
    await ad._maybe_wake_idle_master(MASTER, owed_count=3)
    assert injected == [], "master mid-work (idle < threshold) must not be interrupted"


@pytest.mark.asyncio
async def test_no_wake_when_window_busy(ad, monkeypatch):
    injected = _wire(ad, monkeypatch, idle_s=9999, role_windows=WIN,
                     screen="thinking…\n  esc to interrupt")
    await ad._maybe_wake_idle_master(MASTER, owed_count=1)
    assert injected == [], "busy master window must not be injected over"


@pytest.mark.asyncio
async def test_no_wake_when_no_master_window(ad, monkeypatch):
    injected = _wire(ad, monkeypatch, idle_s=9999, role_windows=[{"window_id": 9, "role": "agent"}])
    await ad._maybe_wake_idle_master(MASTER, owed_count=1)
    assert injected == []


@pytest.mark.asyncio
async def test_cooldown_dedups_repeat_wakes(ad, monkeypatch):
    injected = _wire(ad, monkeypatch, idle_s=9999, role_windows=WIN)
    await ad._maybe_wake_idle_master(MASTER, owed_count=1)
    await ad._maybe_wake_idle_master(MASTER, owed_count=1)
    assert len(injected) == 1, "cooldown must prevent re-waking every sweep"
