"""Dispatch-wake (2026-06-10) — _dispatch_wake_worker / _auto_wake_targeted_idle.

When master routes work to an idle agent (targeted send / task assignment), the
turn-gated SSE push won't surface until the agent turns. The daemon fires the
kitty wake itself, closing the loop. Conservative: idle-only, busy-checked,
cooldowned.
"""

from __future__ import annotations

import importlib
import pytest


@pytest.fixture
def c(monkeypatch):
    from khimaira.monitor import sessions as sess
    importlib.reload(sess)
    from khimaira.monitor import chats as mod
    importlib.reload(mod)
    mod._last_dispatch_wake.clear()
    return mod


def _wire(c, monkeypatch, *, idle_s, windows, screen=""):
    import khimaira.monitor.sessions as sess
    monkeypatch.setattr(sess, "summary", lambda sid: {"last_active_age_s": idle_s})
    import khimaira.monitor.roster_recovery as rr

    async def _discover():
        return windows

    async def _screen(wid):
        return screen

    monkeypatch.setattr(rr, "_discover_roster_windows", _discover)
    monkeypatch.setattr(rr, "_get_screen", _screen)
    monkeypatch.setattr(rr, "_is_busy", lambda s: "esc to interrupt" in (s or "").lower())
    injected = []

    async def _inject(wid, text, title=""):
        injected.append((wid, text, title))
        return True

    monkeypatch.setattr(rr, "_inject_text_and_submit", _inject)
    return injected


WIN = [{"window_id": 7, "role": "agent", "raw_name": "agent-3"}]


def test_wakes_idle_target(c, monkeypatch):
    injected = _wire(c, monkeypatch, idle_s=120, windows=WIN)
    c._dispatch_wake_worker("uuid-x", "agent-3")
    assert len(injected) == 1
    wid, text, title = injected[0]
    assert wid == 7 and title == "agent-3"
    assert "dispatch from master" in text


def test_no_wake_for_active_target(c, monkeypatch):
    injected = _wire(c, monkeypatch, idle_s=3, windows=WIN)  # below 20s threshold
    c._dispatch_wake_worker("uuid-x", "agent-3")
    assert injected == [], "active agent will see the push itself — no wake"


def test_no_wake_when_busy(c, monkeypatch):
    injected = _wire(c, monkeypatch, idle_s=120, windows=WIN, screen="working…\n esc to interrupt")
    c._dispatch_wake_worker("uuid-x", "agent-3")
    assert injected == [], "busy window must not be injected over"


def test_no_wake_when_no_window(c, monkeypatch):
    injected = _wire(c, monkeypatch, idle_s=120, windows=[{"window_id": 9, "raw_name": "someone-else"}])
    c._dispatch_wake_worker("uuid-x", "agent-3")
    assert injected == []


def test_cooldown_dedups(c, monkeypatch):
    injected = _wire(c, monkeypatch, idle_s=120, windows=WIN)
    c._dispatch_wake_worker("uuid-x", "agent-3")
    c._dispatch_wake_worker("uuid-x", "agent-3")
    assert len(injected) == 1, "per-target cooldown prevents re-wake on burst"


def test_disabled_via_env(c, monkeypatch):
    import threading
    spawned = []
    monkeypatch.setattr(threading, "Thread", lambda *a, **k: spawned.append(1) or type("T", (), {"start": lambda self: None})())
    monkeypatch.setattr(c, "_DISPATCH_WAKE_ENABLED", False)
    c._auto_wake_targeted_idle([("uuid-x", "agent-3")])
    assert spawned == [], "KHIMAIRA_DISPATCH_WAKE=0 → no wake threads"
