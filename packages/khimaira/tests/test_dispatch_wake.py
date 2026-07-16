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


# ---------------------------------------------------------------------------
# Burst-429 chokepoint (2026-07-16) — class-level coverage. Any two wake
# dispatches close together in time (regardless of which caller triggered
# them: a targeted send_message, a create_task assignment, the gate-complete
# master wake, or any future caller that loops send_message/create_task over
# multiple targets) must come out staggered by ~_DISPATCH_STAGGER_S, because
# the gate lives at the one place all dispatches converge
# (`_dispatch_wake_worker_async`'s call to `_reserve_dispatch_slot`), not at
# any individual caller.
# ---------------------------------------------------------------------------


def _wire_multi(c, monkeypatch, *, idle_s, windows):
    """Like `_wire`, but the injected callback records a monotonic timestamp
    per call (thread-safe) instead of just the call args — needed to assert
    actual spacing between concurrent dispatches."""
    import threading
    import time as time_mod

    import khimaira.monitor.sessions as sess
    monkeypatch.setattr(sess, "summary", lambda sid: {"last_active_age_s": idle_s})
    import khimaira.monitor.roster_recovery as rr

    async def _discover():
        return windows

    async def _screen(wid):
        return ""

    monkeypatch.setattr(rr, "_discover_roster_windows", _discover)
    monkeypatch.setattr(rr, "_get_screen", _screen)
    monkeypatch.setattr(rr, "_is_busy", lambda s: False)

    injected: list[tuple[int, str, float]] = []
    lock = threading.Lock()

    async def _inject(wid, text, title=""):
        with lock:
            injected.append((wid, title, time_mod.monotonic()))
        return True

    monkeypatch.setattr(rr, "_inject_text_and_submit", _inject)
    return injected


def _await_count(injected, n, *, timeout_s=5.0):
    """Busy-poll (short sleep, generous timeout) until `injected` reaches `n`
    entries — the dispatch threads are real daemon threads, so the test has
    to wait for them without relying on wall-clock scheduling assumptions
    beyond 'they'll finish well inside the timeout'."""
    import time as time_mod

    deadline = time_mod.monotonic() + timeout_s
    while len(injected) < n and time_mod.monotonic() < deadline:
        time_mod.sleep(0.01)


def test_stagger_via_auto_wake_targeted_idle(c, monkeypatch):
    """3 different idle targets, one `_auto_wake_targeted_idle` call (the
    function `send_message` and `create_task` both funnel through) — the
    actual injection timestamps must come out spaced by >= the stagger
    interval, even though the test itself adds zero artificial delay between
    targets."""
    stagger = 0.15
    monkeypatch.setattr(c, "_DISPATCH_STAGGER_S", stagger)
    wins = [
        {"window_id": 1, "raw_name": "agent-1"},
        {"window_id": 2, "raw_name": "agent-2"},
        {"window_id": 3, "raw_name": "agent-3"},
    ]
    injected = _wire_multi(c, monkeypatch, idle_s=120, windows=wins)

    c._auto_wake_targeted_idle(
        [("uuid-1", "agent-1"), ("uuid-2", "agent-2"), ("uuid-3", "agent-3")]
    )
    _await_count(injected, 3)

    assert len(injected) == 3, f"expected 3 dispatches, got {injected}"
    ts = sorted(t for _, _, t in injected)
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    for gap in gaps:
        assert gap >= stagger - 0.05, f"dispatches not staggered: gaps={gaps}"


def test_stagger_across_independent_dispatch_worker_calls(c, monkeypatch):
    """Same assertion, but exercised directly against `_dispatch_wake_worker`
    (the thread entry point EVERY call site uses — `_auto_wake_targeted_idle`
    for send_message/create_task, and the direct
    `threading.Thread(target=_dispatch_wake_worker, ...)` spawn in
    `_maybe_wake_master_on_gate_complete`) rather than through
    `_auto_wake_targeted_idle`. Proves the gate is in the shared worker, not
    something `_auto_wake_targeted_idle` alone provides — so a caller that
    bypasses `_auto_wake_targeted_idle` (as the gate-complete wake does) is
    still covered."""
    stagger = 0.15
    monkeypatch.setattr(c, "_DISPATCH_STAGGER_S", stagger)
    wins = [
        {"window_id": 1, "raw_name": "agent-1"},
        {"window_id": 2, "raw_name": "agent-2"},
        {"window_id": 3, "raw_name": "agent-3"},
    ]
    injected = _wire_multi(c, monkeypatch, idle_s=120, windows=wins)

    import threading

    threads = [
        threading.Thread(target=c._dispatch_wake_worker, args=(f"uuid-{i}", f"agent-{i}"))
        for i in (1, 2, 3)
    ]
    for t in threads:
        t.start()
    _await_count(injected, 3)

    assert len(injected) == 3, f"expected 3 dispatches, got {injected}"
    ts = sorted(t for _, _, t in injected)
    gaps = [ts[i + 1] - ts[i] for i in range(len(ts) - 1)]
    for gap in gaps:
        assert gap >= stagger - 0.05, f"dispatches not staggered: gaps={gaps}"


def test_lone_wake_gets_no_added_delay(c, monkeypatch):
    """A single wake with nothing else in flight must not be spuriously
    delayed by the stagger gate — the gate only matters for 2+ concurrent
    dispatches."""
    import time as time_mod

    stagger = 2.5  # even at the real default, a lone wake should be instant
    monkeypatch.setattr(c, "_DISPATCH_STAGGER_S", stagger)
    injected = _wire_multi(c, monkeypatch, idle_s=120, windows=WIN)

    t0 = time_mod.monotonic()
    c._dispatch_wake_worker("uuid-x", "agent-3")
    elapsed = time_mod.monotonic() - t0

    assert len(injected) == 1
    assert elapsed < 0.5, f"lone wake was delayed {elapsed:.2f}s by the stagger gate"


def test_busy_target_does_not_consume_a_stagger_slot(c, monkeypatch):
    """A target correctly judged busy must return before reserving a slot, so
    it can't spuriously delay a DIFFERENT target's dispatch that races it."""
    stagger = 0.5
    monkeypatch.setattr(c, "_DISPATCH_STAGGER_S", stagger)
    busy_win = {"window_id": 1, "raw_name": "agent-busy"}
    idle_win = {"window_id": 2, "raw_name": "agent-idle"}

    import khimaira.monitor.sessions as sess
    monkeypatch.setattr(sess, "summary", lambda sid: {"last_active_age_s": 120})
    import khimaira.monitor.roster_recovery as rr

    async def _discover():
        return [busy_win, idle_win]

    async def _screen(wid):
        return "esc to interrupt" if wid == 1 else ""

    monkeypatch.setattr(rr, "_discover_roster_windows", _discover)
    monkeypatch.setattr(rr, "_get_screen", _screen)
    monkeypatch.setattr(rr, "_is_busy", lambda s: "esc to interrupt" in (s or "").lower())

    injected = []

    async def _inject(wid, text, title=""):
        injected.append((wid, title))
        return True

    monkeypatch.setattr(rr, "_inject_text_and_submit", _inject)

    import time as time_mod

    t0 = time_mod.monotonic()
    c._dispatch_wake_worker("uuid-busy", "agent-busy")  # busy — skipped, no slot reserved
    c._dispatch_wake_worker("uuid-idle", "agent-idle")  # idle — should fire immediately
    elapsed = time_mod.monotonic() - t0

    assert injected == [(2, "agent-idle")], "only the idle target should have been injected"
    assert elapsed < 0.3, (
        f"idle target was delayed {elapsed:.2f}s — a busy target that never "
        "reserved a slot must not push the next dispatch's timing out"
    )
