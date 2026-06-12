# #18 — auto_dispatch loop freeze: characterization (PARKED 2026-06-12)

> Status: **root cause PARKED** after exhaustive offline elimination.
> User-facing symptom is **covered by a deployed workaround** (piggyback, `c7dda69`).
> This document is the durable record for a future live-daemon deep-dive — the chat
> thread (`chat-3e677725c16e`, khimaira-0 + void) is not discoverable long-term.

## Symptom

On the **production** monitor daemon, `auto_dispatch_loop` logs its
`auto-dispatch: loop started (interval=…)` line, then parks at the **first**
`await asyncio.sleep(_AUTO_DISPATCH_INTERVAL_S)` and **never sweeps**. A TASKDUMP
(`KHIMAIRA_DEBUG_TASKDUMP=1`, committed gated in `49efedb`) shows the task's frame
at that `sleep`, its `_fut_waiter` future **PENDING**, on the **same uvloop loop-id**
as sibling tasks that are firing normally. `reconcile`-count stays 0. The whole
auto_dispatch feature is inert with only "loop started" in the log.

## What is PROVEN

**The freeze is coroutine-identity-specific.** On the live daemon, byte-identical
control loops spawned at the same startup position all tick every interval — only
`auto_dispatch_loop` freezes:

| Canary | Structure | Live-daemon result |
|---|---|---|
| A | bare `while True: await asyncio.sleep(i)` | **ticks** |
| B | sleep wrapped in auto_dispatch_loop's exact `try / except CancelledError: raise / except BaseException` + no-op inner sweep | **ticks** |
| D | `loop.call_later` self-rescheduler (loop-level timer, not task-suspended) | **ticks** |
| auto_dispatch_loop | the real loop | **FROZEN** |

B ticking rules out the try/except wrapper (consistent with git history: the freeze
predates the `0fcef3c` wrapper commit). A ticking rules out the sleep mechanism.
D ticking rules out the loop-level timer path.

## What is RULED OUT (each independently tested)

- **load / SSE volume / data** — a 261-task in-process lab and a 22-verified-SSE lab both swept fine.
- **startup position** — canaries at the same and later startup positions all tick; roster_recovery (spawned *earlier* than auto_dispatch) also ticks. Spawn order is non-monotonic w.r.t. the freeze.
- **structure / try-except** — canary B ticks.
- **interval mutation** — `_AUTO_DISPATCH_INTERVAL_S` is never reassigned after import.
- **external name-canceller** — repo-wide grep is clean: nothing references `auto_dispatch_loop` / `auto_dispatch_sweep` by name except the gated TASKDUMP. `auto_dispatch.py` has zero module-level side effects. `khimaira_observer` (venv-injected) does not patch `asyncio.sleep` and matches only on `asyncio.iscoroutinefunction`, not by name.
- **fork alone** — a real `daemonize_and_serve` (double-fork + setsid + no-TTY) daemon with **empty** isolated state **wakes** normally.
- **fork + real state** — same daemon with **copied** real chats (43) + sessions (28) **wakes** and actively reconciles.
- **fork + SSE-at-startup** — same daemon + 22 verified SSE clients (HTTP 200, first connect +0.8s after fork, racing boot) **wakes**.

## Reproduction matrix

```
in-process lab (uvicorn.Server)        → wakes
forked-empty daemon                    → wakes
forked + real-state daemon (+reconciles)→ wakes
forked + 22-verified-SSE-at-startup    → wakes
PRODUCTION monitor daemon              → FROZEN   ← only env that reproduces
```

**Nothing constructible offline reproduces it.** The trigger requires something the
production daemon has that none of the labs replicate — a deep uvloop/libuv timer
anomaly specific to that one coroutine in that one running process.

## Reliable signal (IMPORTANT)

The **only** trustworthy freeze signal is the gated **AD-WOKE** WARNING in
`auto_dispatch_loop` (fires immediately after the sleep). Do **not** rely on the
`reconcile` / `no global master` / `no backlog` lines — those are `_log.debug`,
invisible at INFO level, and caused a false-positive "REPRODUCED" mid-investigation
(2026-06-12). Always read AD-WOKE, never the debug counts.

## Workaround (deployed, covers the user-facing need)

**Piggyback** (`c7dda69` + cross-roster fix `5bb1834`): `_reconcile_commit_ready`
is driven from `roster_recovery`'s **proven-firing** 60s `watcher_loop` instead of
auto_dispatch's frozen loop. Enabled via `KHIMAIRA_RECONCILE_VIA_ROSTER_RECOVERY=1`.
Commit-ready tasks are reconciled every 60s regardless of the freeze.

## For a future deep-dive (live prod daemon only)

This is crackable **only** by introspecting the live production daemon on a bounce —
the offline rig cannot reproduce it. On the next prod bounce, with
`KHIMAIRA_DEBUG_CANARY=1 KHIMAIRA_AUTO_DISPATCH_S=5`:

1. Confirm the freeze via AD-WOKE silence (canaries A/B/D ticking).
2. From an in-process introspection point (or a signal handler), dump:
   - the uvloop/libuv **timer heap** (`loop._ready`, scheduled timer handles),
   - the auto_dispatch task's **`_fut_waiter`** and the loop's **`_scheduled`** TimerHandle list,
   to see why that one timer's callback is never serviced while siblings' are.
3. Compare the auto_dispatch task's TimerHandle against a ticking canary's — look for a cancelled/orphaned handle or a handle scheduled against a clock the loop isn't draining.

## Instrumentation (committed, gated/inert)

All gated behind `KHIMAIRA_DEBUG_CANARY=1` (zero prod impact otherwise):

- `monitor/auto_dispatch.py` — AD-WOKE per-tick WARNING after the first sleep (the reliable signal).
- `monitor/server.py` — `_start_debug_canary` startup handler spawning the A/B/D control loops.
- `monitor/server.py` — `_taskdump` (`KHIMAIRA_DEBUG_TASKDUMP=1`, `49efedb`) dumping `all_tasks()` at +150s.

## Harness (vendored)

Rebuilt-from-`/tmp` offline rig (these wake — they are the *elimination* rig, not a repro):

- `harness/launch_empty.py` — forked isolated daemon, empty state, port 8799.
- `harness/launch_real.py` — forked isolated daemon, copied real state, port 8798 (kitty socket stripped for inject-safety).
- `harness/orchestrate_sse.py` — fork + 22 SSE clients storming from boot.

Each isolates via temp `XDG_STATE_HOME`/`XDG_DATA_HOME` so it runs alongside prod
without touching the production daemon. Run with the khimaira venv python.
