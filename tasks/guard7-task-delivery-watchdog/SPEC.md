# Guard-7 — task-delivery watchdog (master-side idle/owed + cogitate-then-drop)

> Status: SPEC / scoped · 2026-06-23 · filed by khimaira-0 (master)
> Source: jeevy master roster-infra bug reports (2026-06-22/23). Task #32.
> Scoping: Explore agent map of the master-side dispatch/liveness landscape (this session).

## Why

The daemon dispatches tasks fire-and-forget. Existing guards miss the case where an
agent was *dispatched a task* and then either went idle owing it, OR took a turn but
never delivered the expected artifact. The reactive blind-injection wake (roster_recovery)
is fragile (over-wake, busy false-positives, backfill floods — all hit this session). The
durable fix is a **task-contract-aware** watchdog: key on the task's own state vs the
assignee's activity, and SURFACE to master to decide (don't blind-inject).

Two failure modes from the jeevy master, both currently undetected:
- **Idle-while-owed:** assignee went idle holding a `pending`/`in_progress` task.
- **Cogitate-then-drop (DEFECT A):** assignee TOOK a turn (advanced `last_active`) but the
  task never advanced + no artifact posted. Worse than not-waking — looks like it acted.

## The signal the existing guards lack

Compare two independent clocks per task:
- `task.last_state_change_ts` = latest of TASK_UPDATE / TASK_SIGNAL(start) / TASK_VERDICT
  for that task (chats.py — Guard-5 already computes this in `_scan_blocking_gates`).
- assignee `last_active_age_s` = session-dir mtime age (sessions.py:~1800; advances on any
  tool call, NOT on SSE delivery).

Guard-4 (per-obligation), Guard-5 (roster-level stall: ≥2 idle + open gate >8min), Guard-6
(dark heartbeat >45min) none of them key on "this specific assignee vs this specific task's
advancement." That's the Guard-7 gap.

## What Guard-7 detects (3 signals)

Per task with status in {pending, in_progress, done}, NOT in roster wind-down:

1. **assigned-but-dark** — `pending|in_progress`, `last_state_change_ts` stale > T_STALL,
   assignee `last_active_age_s` > T_INACTIVE (assignee went dark). → escalate (assignee
   likely dead/stuck; reassign or restart).
2. **cogitate-then-drop (DEFECT A)** — `pending|in_progress`, `last_state_change_ts` stale
   > T_STALL, assignee `last_active_age_s` < T_INACTIVE (assignee IS turning but the task
   isn't advancing). → nudge the assignee with task-specific text ("you hold task T; it
   hasn't advanced — run it and post the artifact, or report why you can't").
3. **verdict-owed-unposted** — task `done`, `last_verdict_ts` stale > T_VERDICT_STALL, a
   member whose role is critic/verifier has an empty verdict slot. → escalate that reviewer.
   (Overlaps the direct-verdict obligation in `_get_session_obligations`; reuse, don't dup.)

## Where it lives + how it surfaces

- **New file** `packages/khimaira/src/khimaira/monitor/guard7.py` (do NOT extend the
  manual-formatted guard5/guard6/auto_dispatch — new file = clean diff, no reformat risk).
- Invoked from the proven-firing `roster_recovery.watcher_loop` (the 60–90s sweep that
  Guard-5/6 already ride — avoids the #18 auto_dispatch-loop fragility; do NOT add an
  independent `asyncio.sleep` loop).
- **Surface to MASTER via NOTICE/chat first, not blind kitty injection.** The whole point
  is task-aware *deliberate* escalation. Reuse Guard-5's target-resolution
  (`_escalate_to_target`: same-role peer → master → coordinator) + `sessions.post_notice` /
  `chats._post_synthetic_message`. A kitty wake of an idle master via
  `auto_dispatch._maybe_wake_idle_master(wake_text=...)` is the LAST resort, only when master
  itself is idle — NOT the default (blind injection is what caused this session's floods).
  NOTE: `_maybe_wake_idle_master` is a separate path, NOT gated by `KHIMAIRA_ROSTER_WAKE_INJECT`.

## Tunables (env, fail-open)

- `KHIMAIRA_GUARD7_WATCH_S` (default reuse the watcher interval)
- `KHIMAIRA_GUARD7_TASK_STALL_S` (default 600s — task not advancing)
- `KHIMAIRA_GUARD7_INACTIVE_S` (default 900s — assignee-dark vs still-turning split)
- `KHIMAIRA_GUARD7_VERDICT_STALL_S` (default 900s)
- `KHIMAIRA_GUARD7` enable flag (default on) + wind-down suppression like Guard-5/6.
- Debounce per (task, signal) so it doesn't re-escalate every sweep (mirror Guard-5).

## Acceptance / tests

- **cogitate-then-drop:** task in_progress, last_state_change 12min ago, assignee
  last_active 1min ago → fires signal-2 (the key new detection). Same task with assignee
  last_active 20min ago → fires signal-1 (dark), not signal-2.
- **healthy:** task in_progress advancing (last_state_change recent) → no fire. Task `done`
  with both verdicts in → no fire (committable, master owns it).
- **wind-down suppression:** roster winding down → no fire (mirror Guard-5/6 tests).
- **debounce:** repeated sweeps on the same stalled task escalate once per cooldown.
- **surfacing:** asserts a NOTICE/chat escalation to the resolved target, NOT a kitty inject
  (unless master-idle last-resort path).

## Cross-references
- Explore scoping report (this session) — full file:line map of the dispatch/task model,
  Guard-4/5/6 coverage table, the delivered-vs-took-a-turn gap.
- `guard5.py` `_scan_blocking_gates` / `_gate_is_stale` / `_escalate_to_target` — reuse.
- `auto_dispatch.py` `_maybe_wake_idle_master`, `committable_gate_tasks` — reuse/avoid-dup.
- `api/chats.py` `_get_session_obligations` direct-verdict path — signal-3 overlaps; reuse.
- #14 (auto-BEGIN: pending→in_progress) + #18 (auto_dispatch freeze, RESOLVED) — no conflict;
  Guard-7 watches in_progress→done delivery, daemon-side sweep, loop-unmodified.
- DEFECT B (busy-check false-positive) already fixed this session.
