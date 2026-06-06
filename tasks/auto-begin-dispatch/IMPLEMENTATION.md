# #14 — Auto-BEGIN dispatch (remove the master from the per-task BEGIN hot loop)

**Alpha-close item.** Status: ⬜ not started. Priority: P1 (root fix for recurring
roster-idle). Owner: dispatch to a roster (architect designs the state machine → agent
implements → verifier gates the class-invariant).

## Problem

Current task-start flow (see `_format_assignment_block` in `chats.py` + the enforcement-gate
block agents receive):

1. Master `chat_task_create` → assigns a task to N agents with a required model/effort.
2. Each agent: on the user's "ready", verifies `~/.claude/settings.json` model+effort →
   `chat_send` ack `"ready [task-id] | model=X effort=Y"`.
3. **Master watches for all N acks, then MANUALLY fires** `chat_task_signal_start` → the
   `🟢 ALL AGENTS CONFIRMED — BEGIN` signal. Agents start only on 🟢.

**The hot loop:** step 3 couples the master into *every* task's start. If the master is busy,
context-saturated, mid-other-work, idle, or away, the task sits **pending-with-all-agents-ready**
— agents idle, waiting on a 🟢 that never comes. This is the recurring **roster-idle** failure:
work is ready to start but stalls on the master's manual confirm. (Guard-4's 2D pending-gate at
`api/chats.py:405` already distinguishes "compliant-waiting" from "begun-not-started" — auto-BEGIN
removes the wait entirely.)

## Approach — daemon auto-fires BEGIN when the gate is satisfied

Move "fire BEGIN once all agents ack" from the **master (manual)** to the **daemon (automatic)**,
preserving the *synchronization* semantics (all agents start together) and the *budget gate*
(each ack carries verified model+effort).

1. **Track per-task readiness** (extend the task record written by `chat_task_create`):
   `required_agents: [session_id...]`, `ready_acks: {session_id: {model, effort, ts}}`,
   `auto_begin: bool = true`, `begun: bool`.
2. **On each ready-ack** (the path that processes `"ready [task-id] | model=X effort=Y"` —
   structured via `chat_task_update` or parsed from the ack `chat_send`): the daemon
   - validates the ack's model/effort against the task's required tier (reject/ignore a
     non-compliant ack, exactly as the master does today),
   - records it in `ready_acks`,
   - checks the **gate**: `auto_begin == true` AND `ready_acks` covers **all** `required_agents`
     AND any master-set verdict/budget gate is satisfied (reuse `gate_verdict_satisfied` from
     `api/chats.py:439`).
   - If the gate is satisfied → **auto-fire `chat_task_signal_start`** (the same TASK_SIGNAL
     start event the master fires today) + broadcast the `🟢 BEGIN` to the agents.
3. **Master override preserved:** `auto_begin=false` on `chat_task_create` (or a
   `chat_task_hold` toggle) lets the master keep manual control for a task that must wait on an
   external dependency. Master can also still fire BEGIN manually (idempotent — `_is_task_begun`
   guards against double-fire).

Net: tasks begin **the instant the last required agent is ready**, with no master action — the
master sets up the task and walks away; the daemon synchronizes the start.

## Acceptance criteria

- A task with `auto_begin=true` and all required agents acked (compliant model/effort) →
  daemon fires BEGIN **without the master acting**; agents receive 🟢.
- A non-compliant ack (wrong model/effort) does NOT count toward the gate (no premature BEGIN).
- Partial acks (k of N) → no BEGIN; task stays compliant-waiting.
- `auto_begin=false` → daemon does NOT auto-fire; master's manual `chat_task_signal_start` still works.
- Double-fire safe: auto + manual BEGIN on the same task fires the signal exactly once.
- Master-set verdict/budget gate unsatisfied → no auto-BEGIN even with all acks (gate honored).

## Tests (`packages/khimaira/tests/test_chats.py` + `test_chats_v2_e2e.py`)

- `test_auto_begin_fires_on_last_ack` — N agents, acks 1..N-1 no BEGIN, ack N → BEGIN fired once.
- `test_auto_begin_ignores_noncompliant_ack` — wrong model/effort ack doesn't satisfy the gate.
- `test_auto_begin_respects_hold` — `auto_begin=false` → no auto-fire; manual still works.
- `test_auto_begin_idempotent` — auto-fire then manual fire → single TASK_SIGNAL start.
- `test_auto_begin_honors_verdict_gate` — verdict-gated task with all acks but unsatisfied gate → no BEGIN.
- **Class-invariant:** `test_no_ready_task_idles_without_master` — for any task whose required
  agents are all compliant-ready and whose gate is satisfied, `_is_task_begun` is true within the
  ack-processing turn — i.e. a fully-ready task can never sit un-begun waiting on the master.

## Risks / notes
- Keep the budget-gate validation IDENTICAL to the master's current check — auto-BEGIN must not
  weaken the model/effort enforcement, only automate the trigger.
- Synchronization is preserved: BEGIN still fires once, to all agents, when *all* are ready — not
  per-agent. This is the same multi-agent-start guarantee, minus the manual step.
