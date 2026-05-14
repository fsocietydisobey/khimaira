# persistent scheduler — daemon-side ScheduleWakeup

**Status**: spec landed 2026-05-14 in session `khimaira-21` (carried over from `khimaira-6` session transfer). Phase A implementation pending.

**Origin**: Today's session transfer (`khimaira-6` → `khimaira-21`) required hand-rolling a re-schedule of a `ScheduleWakeup` because the wakeup is bound to the donor agent process — when the donor closes, pending wakeups die. The transfer worked but the dance is fragile. Daemon-side persistent scheduling is the foundational primitive that removes the dance: schedule once, fires regardless of which (or any) Claude Code window is open.

Beyond session transfer the same primitive unlocks: reliable retries with backoff (PyPI cascades become one call, not a hand-cascading chain), cron-like maintenance ("every night at 3am: refresh seance indexes"), cross-machine work (when daemon-sync lands), reminders, long-running workflow continuations, polling-with-timeout, cross-session deadlines.

---

## Current state (pre-task)

`ScheduleWakeup` is the only scheduling primitive. It lives in the harness (Claude Code), not in khimaira:

- Schedule is **process-local** to the calling agent. Window close = wakeup lost.
- Max single delay is 3600s; longer delays cascade by re-scheduling on each fire.
- No introspection: no list-pending, no cancel-by-id, no observability beyond the agent's own memory.
- No retry policy, no failure handling, no targeting another session.

The khimaira monitor daemon (`khimaira-monitor`) already runs as a long-lived systemd-user service with persistent state at `~/.local/state/khimaira/`. It owns sessions.jsonl, handoffs.jsonl, the inbox/answer JSONLs. Adding a `scheduled_tasks.jsonl` + worker coroutine is a natural extension.

## What this adds

A daemon-side scheduler with the following surface:

1. **Storage** — append-only JSONL at `~/.local/state/khimaira/scheduled_tasks.jsonl`. Replay-on-boot rebuilds in-memory schedule. Compaction is opt-in (>1MB threshold OR explicit `khimaira monitor compact-scheduler-state`) — daemon boot does NOT compact (avoids thrash).

2. **Worker coroutine** — daemon-internal `asyncio` task polling `fire_at_utc` against `now()` every 5s (configurable). On match: write `status=firing`, invoke, write `status=fired` + attempt entry. On daemon SIGKILL mid-fire: replay-on-boot detects `status=firing` older than 60s and re-fires (at-least-once semantics).

3. **Invoke mechanism (Phase A)** — append a special inbox note to the target session's `inbox.jsonl` with kind=`scheduled-task`. The session's `UserPromptSubmit` hook surfaces it on next user prompt; agent runs the prompt verbatim. Target session **must be alive** at fire time. If target session is dead (no inbox dir, or last activity > 7d), task is marked `failed` with `detail: target_session_id no longer alive` and retries per policy.

4. **HTTP API** (added to monitor's FastAPI):
   - `POST /api/scheduled-tasks` — create; body `{target_session_name, fire_at_utc, prompt, retry_policy?, expires_in_hours?}`; returns `{id, ...full record}`
   - `GET /api/scheduled-tasks` — list; query params `?status=scheduled,firing` and `?target=<name>`
   - `GET /api/scheduled-tasks/{id}` — single record
   - `DELETE /api/scheduled-tasks/{id}` — cancel; valid only when status ∈ {scheduled, pending_retry}; 409 if firing; 404-equivalent if terminal

5. **MCP tool** `mcp__khimaira__schedule_task(target_session, fire_at_utc, prompt, retry_policy=None, expires_in_hours=168)` — thin wrapper over the POST endpoint.

6. **Slash command** `/schedule-task <target> <when> <prompt>` — wraps the MCP tool. `<when>` accepts both ISO 8601 UTC and relative ("+30m", "+2h", "tomorrow 09:00").

## Decisions (all locked)

| Q | Decision | Why |
|---|---|---|
| **Storage format** | JSONL append-only at `~/.local/state/khimaira/scheduled_tasks.jsonl`. Compact on >1MB OR explicit maintenance call. Replay-on-boot rebuilds schedule. | Matches existing handoffs/sessions JSONL pattern. Daemon boot must be fast — no compaction thrash. |
| **Race on SIGKILL mid-fire** | At-least-once. `firing` >60s on restart → re-fire. **Tasks must be idempotent — scheduler contract.** | Exactly-once requires receipt-ack from target, which is complex. Real use cases (PyPI re-publish, refresh-index, reminder) are naturally idempotent. Document the contract loudly. |
| **Invoke mechanism** | Phase A: inbox-note-as-prompt only. Target session must be alive at fire. TTL 7d default. Headless (spawn Claude Code at fire time) = Phase B. | Phase A needs zero new infrastructure — reuses the existing inbox + UserPromptSubmit hook. Headless is genuinely different work (TTY, MCP wiring, transport). |
| **Retry policy** | Linear backoff only. `max_attempts: 1, retry_after_seconds: 300` default. Per-outcome routing + exponential = Phase B. | Phase A retry exists mainly for "target window briefly closed during fire" — linear + small N covers it. |
| **Resolution timing** | Phase A: schedule-time (look up name → store both name + id). Stale id at fire time = `error: target_session_id no longer alive`. Phase C: re-resolve name at fire time if stored id is dead. | Schedule-time is one less moving part. Rename + reopen between schedule and fire is a known v1 wart, documented. |
| **Cancellation** | Valid in `{scheduled, pending_retry}`. 409 if `firing`. Terminal → 404-equivalent (idempotent no-op). | Mid-fire cancellation is racy by nature — give the user an explicit signal (409) rather than silent-discard. |

## Schema

`~/.local/state/khimaira/scheduled_tasks.jsonl` — one JSON object per line, append-only. Each append represents a state transition. Replay folds the stream into the current in-memory map keyed by `id`.

```json
{
  "id": "task-<12-char-hex>",
  "target_session_name": "khimaira-21",
  "target_session_id": "1b41b45c-15fb-459d-9df0-8c34e57febfc",
  "fire_at_utc": "2026-05-14T22:00:00Z",
  "prompt": "<verbatim text the agent should run>",
  "retry_policy": {"max_attempts": 1, "retry_after_seconds": 300},
  "status": "scheduled",
  "created_at": "2026-05-14T19:00:00Z",
  "expires_at": "2026-05-21T19:00:00Z",
  "attempts": [
    {"ts": "2026-05-14T22:00:01Z", "outcome": "fired", "detail": "inbox note delivered"}
  ]
}
```

Status enum: `scheduled | firing | fired | failed | pending_retry | cancelled | expired`.

## Phase A scope

Ship the load-bearing pieces:

1. **Storage primitives** (read/write JSONL, replay-on-boot, compaction)
2. **Worker coroutine** (5s tick, fire-time match, status transitions)
3. **Invoke = inbox-note-as-prompt** (uses existing `_session_dir(target) / "inbox.jsonl"`)
4. **HTTP API** (POST/GET/DELETE on `/api/scheduled-tasks`)
5. **MCP tool** `schedule_task` + thin slash command
6. **Hook surfacing** — extend the UserPromptSubmit auto-injector to recognize kind=`scheduled-task` notes and render them as "🕒 scheduled task fired: <prompt>"
7. **Tests** — round-trip the JSONL primitive, test worker fire path with a mocked clock, test cancel semantics in each status state, test inbox delivery

Phase B (deferred): exponential backoff, per-outcome retry, headless `target_mode`, observability dashboard.

Phase C (deferred): fire-time name resolution, cross-machine targeting (after daemon-sync lands).

## File map

```
packages/khimaira/src/khimaira/monitor/scheduler.py (new)
  Storage:
    + append_task(record)
    + replay() → dict[task_id, record]   # called once on daemon boot
    + compact_if_needed()                 # >1MB threshold
  Worker:
    + scheduler_loop(stop_event)         # asyncio coroutine, 5s tick
    + _fire(record)                      # mark firing → invoke → mark fired/failed
    + _invoke_inbox(record)              # append to target's inbox.jsonl
  Public API (called by HTTP layer):
    + create(target, fire_at_utc, prompt, retry_policy, expires_in_hours) → record
    + list(status_filter=None, target_filter=None) → list[record]
    + get(task_id) → record | None
    + cancel(task_id) → record (raises on bad state)

packages/khimaira/src/khimaira/monitor/api/scheduled_tasks.py (new)
  POST   /api/scheduled-tasks
  GET    /api/scheduled-tasks
  GET    /api/scheduled-tasks/{id}
  DELETE /api/scheduled-tasks/{id}
  All resolve ValueError → 404, KeyError → 409 per project convention.

packages/khimaira/src/khimaira/monitor/api/__init__.py
  Register the new router.

packages/khimaira/src/khimaira/monitor/daemon.py
  In daemon startup: asyncio.create_task(scheduler.scheduler_loop(stop_event))
  In shutdown: signal stop_event so worker exits cleanly.

packages/khimaira/src/khimaira/server.py (or wherever MCP tools live)
  + schedule_task MCP tool — POST wrapper.
  + list_scheduled_tasks MCP tool — GET wrapper.
  + cancel_scheduled_task MCP tool — DELETE wrapper.

packages/khimaira/src/khimaira/cli/monitor.py (or similar)
  + khimaira monitor compact-scheduler-state — explicit compaction command.

scripts/hooks/user_prompt_submit_hook.* (existing — extend the inbox surfacer)
  Recognize kind=scheduled-task notes; render with the 🕒 prefix.

~/.claude/commands/schedule-task.md (new — symlinked from dotfiles)
  Slash command wrapper.

packages/khimaira/tests/test_scheduler.py (new)
  - JSONL round-trip (write task → replay → assert in-memory map matches)
  - Worker fire path with frozen clock (advance to fire_at_utc, assert status=fired + attempt entry)
  - SIGKILL recovery (write firing-status entry with old ts, replay, assert re-fire scheduled)
  - Cancel in {scheduled, pending_retry} → 200; cancel in {firing} → 409; cancel in {fired/cancelled/expired} → no-op
  - Inbox delivery (mock target session dir, fire, assert inbox.jsonl contains scheduled-task note)
  - TTL expiration (task with expires_at in past, worker tick, assert status=expired)

packages/khimaira/tests/test_scheduled_tasks_api.py (new)
  Standard 4-endpoint test pattern (happy + unhappy paths per route per CLAUDE.md rule).
```

## Anti-patterns

- **Don't schedule non-idempotent work.** The scheduler is at-least-once. If your prompt does `INSERT` without an upsert, a re-fire will double-insert. Make prompts idempotent (check-then-act, upsert, conditional run) — this is the scheduler's documented contract.
- **Don't poll for task status in tight loops.** The worker is the source of truth. Agents should call `list_scheduled_tasks(status=scheduled)` once per turn at most, not in a wait loop. For "wait for this specific task to fire" use a notice-on-fire pattern (Phase B).
- **Don't compact on every daemon boot.** Boot must be fast — compaction is a maintenance op, not a startup op.
- **Don't replicate the schedule in agent memory.** Agents that schedule should let go: the daemon owns the lifecycle, and observability comes through the API. Caching the task list in agent memory drifts.
- **Don't shell out from the worker.** The worker is in-process with the daemon — invoke via the existing `_append_jsonl` primitive, not via subprocess to a CLI.
- **Don't use `ScheduleWakeup` once `schedule_task` ships** for anything that matters past the current window. ScheduleWakeup remains valid for "remind me in 5 min within this conversation"; longer or cross-session work should use `schedule_task`.

## Done when

- `mcp__khimaira__schedule_task(target_session_name="khimaira-21", fire_at_utc="2026-05-14T22:00:00Z", prompt="echo hello")` returns a task record. At 22:00:00Z, an inbox note appears in khimaira-21's inbox with the prompt body.
- Daemon restart between schedule and fire does not lose the task.
- Daemon SIGKILL'd mid-fire and restarted re-fires (idempotent, contract documented).
- `GET /api/scheduled-tasks?status=scheduled` returns pending tasks; `DELETE /api/scheduled-tasks/{id}` cancels.
- `khimaira monitor compact-scheduler-state` rewrites the JSONL with terminal entries dropped.
- Tests cover all decision points (status transitions, race recovery, cancellation states, TTL expiry, inbox delivery).
- Today's PyPI cascade pattern (currently a hand-cascading `ScheduleWakeup` chain) can be re-expressed as one `schedule_task` call with `retry_policy: {max_attempts: 5, retry_after_seconds: 7200}`.

## Migration path for the PyPI cascade

Once Phase A ships, the inherited PyPI cascade (currently re-schedules itself hourly until past 00:10Z) can be replaced by a single call:

```python
schedule_task(
    target_session_name="khimaira-21",
    fire_at_utc="2026-05-15T00:15:00Z",
    prompt="<the publish + verify + tag + notice prompt>",
    retry_policy={"max_attempts": 3, "retry_after_seconds": 7200},
)
```

The cascade self-extends-until-after-reset pattern becomes obsolete — `fire_at_utc` already encodes "wait until past reset", and retry handles the 429-still-throttled case with linear 2h backoff.

This migration is the natural Phase A acceptance test against a real workload.

## Open follow-ups (NOT v1 scope)

- Phase B: exponential + jittered backoff, per-outcome retry routing, headless `target_mode`, dashboard for pending tasks
- Phase C: fire-time name resolution (survives session renames), cross-machine targeting (post daemon-sync)
- Phase D: exactly-once via target-ack handshake (only if a real use case demands it)
- Cron-syntax sugar for recurring tasks (separate primitive: `recurring_tasks.jsonl`?)
- Scheduler-event observability (notice-on-fire, notice-on-failure to the original scheduler)
- `khimaira monitor list-scheduled` CLI mirror of the GET endpoint

## References

- Inbox mechanism: `packages/khimaira/src/khimaira/monitor/sessions.py:418-430` (`_append_jsonl` to `_session_dir(target) / "inbox.jsonl"`)
- UserPromptSubmit surfacer: `scripts/hooks/user_prompt_submit_hook.*` (renders inbox notes inline)
- XDG state convention: `packages/khimaira/src/khimaira/monitor/paths.py`
- API + tests pattern: `packages/khimaira/src/khimaira/monitor/api/sessions.py` + `packages/khimaira/tests/test_sessions_api.py`
- Model spec for structure: `tasks/khimaira-sync/IMPLEMENTATION.md`
- Originating session transfer: `khimaira-6` → `khimaira-21` handoff `ffad7e40` (2026-05-14T20:25:52Z)
