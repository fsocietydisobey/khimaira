"""Tests for `khimaira monitor watch` supervisor logic.

The supervisor's job: spawn the daemon, restart on non-zero exit with
exponential backoff, exit cleanly on SIGINT, reset backoff after a
healthy uptime.

These tests don't actually launch the real daemon (too heavyweight +
needs a port). Instead we mock subprocess.Popen with a fake child
that exits with controlled rcs and uptimes, and verify the watcher's
loop behavior.
"""

from __future__ import annotations

import argparse
import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture
def mock_args() -> argparse.Namespace:
    """Empty Namespace — _cmd_watch doesn't read any args."""
    return argparse.Namespace()


def _make_popen_mock(
    *,
    rcs: list[int],
    uptimes: list[float] | None = None,
):
    """Returns a Popen-like mock that returns each rc in sequence.

    `uptimes`: optional per-call sleep before wait() returns. Defaults to 0.
    """
    uptimes = uptimes or [0.0] * len(rcs)
    call_count = {"n": 0}

    class FakeChild:
        def __init__(self, rc: int, uptime: float):
            self._rc = rc
            self._uptime = uptime
            self.terminated = False

        def wait(self) -> int:
            if self._uptime > 0:
                time.sleep(self._uptime)
            return self._rc

        def poll(self) -> int | None:
            return self._rc

        def send_signal(self, signum):  # noqa: ARG002
            self.terminated = True

    def factory(*args, **kwargs):  # noqa: ARG001
        idx = call_count["n"]
        call_count["n"] += 1
        if idx >= len(rcs):
            # Loop finished spawning — any further calls would mean a bug
            raise RuntimeError(f"Popen called too many times (idx={idx})")
        return FakeChild(rcs[idx], uptimes[idx])

    factory.call_count = call_count  # type: ignore[attr-defined]
    return factory


def test_watch_exits_clean_on_rc_zero(mock_args, monkeypatch):
    """Daemon clean exit (rc=0) → watcher exits with rc=0, no restart."""
    from khimaira.monitor import cli

    popen_mock = _make_popen_mock(rcs=[0])
    monkeypatch.setattr("subprocess.Popen", popen_mock)
    # Make sleep a no-op so backoff doesn't actually delay tests
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    rc = cli._cmd_watch(mock_args)
    assert rc == 0
    assert popen_mock.call_count["n"] == 1  # spawned once, daemon clean-exited


def test_watch_restarts_on_non_zero_then_succeeds(mock_args, monkeypatch):
    """Daemon dies (rc=1) → watcher restarts → next run rc=0 → exit clean."""
    from khimaira.monitor import cli

    popen_mock = _make_popen_mock(rcs=[1, 0])
    monkeypatch.setattr("subprocess.Popen", popen_mock)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    rc = cli._cmd_watch(mock_args)
    assert rc == 0
    assert popen_mock.call_count["n"] == 2  # spawned, died, respawned, clean


def test_watch_backoff_increases(mock_args, monkeypatch):
    """Successive non-zero exits → backoff doubles each time."""
    from khimaira.monitor import cli

    sleep_durations: list[float] = []

    def fake_sleep(seconds):
        sleep_durations.append(seconds)

    popen_mock = _make_popen_mock(rcs=[1, 1, 1, 0])
    monkeypatch.setattr("subprocess.Popen", popen_mock)
    monkeypatch.setattr("time.sleep", fake_sleep)

    rc = cli._cmd_watch(mock_args)
    assert rc == 0
    # 3 non-zero exits → 3 backoff sleeps. Sequence: 1, 2, 4
    assert sleep_durations == [1.0, 2.0, 4.0]


def test_watch_backoff_caps_at_60s(mock_args, monkeypatch):
    """Backoff doesn't exceed 60s even after many failures."""
    from khimaira.monitor import cli

    sleep_durations: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_durations.append(s))
    # 10 non-zero exits then clean — should cap at 60s
    popen_mock = _make_popen_mock(rcs=[1] * 10 + [0])
    monkeypatch.setattr("subprocess.Popen", popen_mock)

    cli._cmd_watch(mock_args)

    # All sleep durations should be ≤ 60
    assert all(s <= 60.0 for s in sleep_durations)
    # And eventually we hit the cap (some sleep == 60.0)
    assert 60.0 in sleep_durations


def test_watch_resets_backoff_after_healthy_uptime(mock_args, monkeypatch):
    """5+ minute uptime → backoff resets to 1s for the next failure.

    Simulated via mock_time that jumps the clock forward by >300s during
    the child's wait() so the watcher computes uptime as healthy.
    """
    from khimaira.monitor import cli

    sleep_durations: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: sleep_durations.append(s))

    # Simulated time: each tick advances by 350s when wait() is called
    fake_time = {"now": 1000.0}

    real_time_module = time
    monkeypatch.setattr(cli.time if hasattr(cli, "time") else real_time_module,
                        "time", lambda: fake_time["now"])
    # Fall through: time used in cli's _cmd_watch is `import time` at function scope
    monkeypatch.setattr("time.time", lambda: fake_time["now"])

    rc_sequence = [1, 1, 0]  # die, healthy run, then clean
    call_count = {"n": 0}

    class FakeChild:
        def __init__(self, idx):
            self.idx = idx

        def wait(self):
            # First spawn: short uptime (no backoff reset)
            # Second spawn: long uptime (>300s — should reset)
            if self.idx == 0:
                fake_time["now"] += 1.0
            elif self.idx == 1:
                fake_time["now"] += 350.0  # healthy run
            else:
                fake_time["now"] += 1.0
            return rc_sequence[self.idx]

        def poll(self): return rc_sequence[self.idx]
        def send_signal(self, s): pass

    def fake_popen(*a, **kw):
        idx = call_count["n"]
        call_count["n"] += 1
        return FakeChild(idx)

    monkeypatch.setattr("subprocess.Popen", fake_popen)

    cli._cmd_watch(mock_args)

    # First failure → sleep 1s (initial backoff)
    # After healthy 350s run, second failure → sleep 1s again (reset)
    assert sleep_durations == [1.0, 1.0]


def test_watch_propagates_sigint_to_child(mock_args, monkeypatch):
    """SIGINT to watcher → SIGTERM to child → watcher exits cleanly."""
    from khimaira.monitor import cli
    import signal as signal_mod

    sigint_handler = {"fn": None}

    def fake_signal(sig, handler):
        if sig == signal_mod.SIGINT:
            sigint_handler["fn"] = handler

    monkeypatch.setattr("signal.signal", fake_signal)
    monkeypatch.setattr("time.sleep", lambda *_a, **_kw: None)

    child_terminated = {"flag": False}

    class FakeChild:
        def wait(self):
            # Simulate the SIGINT firing while we're "waiting"
            if sigint_handler["fn"]:
                sigint_handler["fn"](signal_mod.SIGINT, None)
            return -15  # SIGTERM rc

        def poll(self): return None  # Pretend it's still alive when SIGINT fires

        def send_signal(self, signum):
            child_terminated["flag"] = True

    monkeypatch.setattr("subprocess.Popen", lambda *a, **k: FakeChild())

    rc = cli._cmd_watch(mock_args)
    assert rc == 0  # clean exit on interrupt
    assert child_terminated["flag"] is True  # SIGTERM was sent to child
