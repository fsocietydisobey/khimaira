"""Freeze-class invariant (2026-07-11) — the kitty-subprocess-in-async-loop fix.

Every other test touching `_kitty`/`_discover_roster_windows`/`_get_screen`/etc.
mocks those functions away entirely, so it proves correct RETURN VALUES but says
nothing about whether the real call chain still blocks the daemon's event loop —
exactly the class of bug that shipped (a synchronous `subprocess.run(["kitty",
...])` called from `async def` code, freezing every other coroutine on the loop
for the duration of the shell-out).

These tests exercise the REAL `roster_recovery._kitty` chokepoint (only
`subprocess.run` itself is faked, as a controlled blocking call) through two of
the confirmed-broken call chains from the chat-102d8b5fd82f audit:
  - registry_gc's reap chain (`_live_window_identities` → `_kitty_ls_data` →
    `_kitty`) — the dominant confirmed instance, called every GC sweep.
  - api/chats `_classify_unresponsive` — the dominant confirmed instance on the
    overdue-watcher path, called up to twice per overdue candidate per tick.

Synchronization is via `threading.Event`, not wall-clock sleeps + timing
assertions, so the test is deterministic: it can't flake on scheduler jitter,
and if the chokepoint regresses (a caller starts running the subprocess call
inline on the event loop again) it fails fast and reliably rather than racing a
clock. See ~/.claude/rules/personal/bug-class-enumeration.md — "Test
verification of CLASS, not path": either call chain regressing trips this file.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading

import pytest


def _make_blocking_kitty_run(*, release: threading.Event, entered: threading.Event):
    """A `subprocess.run` stand-in that proves it's running off-loop: it signals
    `entered` the instant it starts (so the test can wait for the real thread to
    actually be inside the call) and then blocks on `release` until the test lets
    it go. Returns a well-formed empty `kitty @ ls` response so callers complete
    normally once released.
    """

    def _run(cmd, **kwargs):
        entered.set()
        if not release.wait(timeout=5.0):
            raise AssertionError("test never released the blocking kitty call")
        return subprocess.CompletedProcess(cmd, returncode=0, stdout="[]", stderr="")

    return _run


async def _assert_call_does_not_block_loop(coro_factory, *, rr_module) -> None:
    """Drive `coro_factory()` while `roster_recovery.subprocess.run` is blocked on
    a controlled gate, and prove the event loop stayed live the whole time:
    1. start the target coroutine as a task
    2. wait until the (real, off-loop) thread has entered the blocking call
    3. while STILL blocked, prove the loop can still schedule other work AND that
       the target task has not (and could not have) completed yet
    4. release the gate and let the target task finish
    """
    release = threading.Event()
    entered = threading.Event()

    import khimaira.monitor.roster_recovery as rr

    orig_run = rr.subprocess.run
    rr.subprocess.run = _make_blocking_kitty_run(release=release, entered=entered)
    try:
        task = asyncio.create_task(coro_factory())

        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "target never reached the kitty subprocess call"

        # PROOF: the background thread is still parked inside subprocess.run
        # (release not yet set), yet the event loop keeps running other work.
        ticked = False
        for _ in range(5):
            await asyncio.sleep(0)
            ticked = True
        assert ticked
        assert not task.done(), (
            "target completed while the kitty subprocess call should still be "
            "blocked — this means the call ran SYNCHRONOUSLY on the event loop "
            "instead of being offloaded, i.e. the freeze-class bug is back"
        )

        release.set()
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        rr.subprocess.run = orig_run
        release.set()  # in case of an early failure, don't leave the thread parked


@pytest.mark.asyncio
async def test_registry_gc_reap_chain_offloads_kitty_call(monkeypatch):
    """registry_gc's `_live_window_identities` (the reap chain's kitty call) must
    not block the event loop — the dominant confirmed freeze-class instance."""
    from khimaira.monitor import registry_gc as gc
    import khimaira.monitor.roster_recovery as rr

    await _assert_call_does_not_block_loop(gc._live_window_identities, rr_module=rr)


@pytest.mark.asyncio
async def test_classify_unresponsive_offloads_kitty_call(isolated_state, monkeypatch):
    """api/chats `_classify_unresponsive` must not block the event loop — the
    dominant confirmed freeze-class instance on the overdue-watcher path."""
    from khimaira.monitor.api import chats as api_chats
    from khimaira.monitor import sessions as sess_mod
    import khimaira.monitor.roster_recovery as rr

    monkeypatch.setattr(api_chats, "_session_active_within", lambda sid, w: False)
    monkeypatch.setattr(sess_mod, "state", lambda sid, recent=0: {"name": "freeze-class-probe"})

    await _assert_call_does_not_block_loop(
        lambda: api_chats._classify_unresponsive("freeze-class-probe-sid"),
        rr_module=rr,
    )
