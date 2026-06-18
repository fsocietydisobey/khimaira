# auto_begin assignee-binding — muther ISSUE 3

> Status: SPEC / ready-to-build · audit-grade (khimaira-research agent, 2026-06-18)
> Source: muther 2026-06-18 platform bug report, ISSUE 3
> Related: tasks/sse-deaf-idle-wake/SPEC.md (ISSUE 1/2 — compounds path 4 below)

## The report

"`chat_task_create(assignee, auto_begin=true)` doesn't bind/start the assignee;
AWAITING-ACK shows assignees that already did the work never acked. Issues 1+3
compound — neither a message nor a task wakes an idle target."

## Audit findings (audit-grade where tested; cite chats.py)

Three distinct defects, not one:

### Defect 1 — `auto_begin=True` with no `required_agents` is a silent no-op [BROKEN]
`_try_auto_begin` (`chats.py:3533`) is the ONLY path that fires the BEGIN, and it
early-exits at `chats.py:3577-3578`:
```python
required_agents = task_record.get("required_agents") or []
if not required_agents:
    return False   # auto_begin never fires
```
So `chat_task_create(assignee_session_id=X, auto_begin=True)` with no
`required_agents` NEVER auto-begins — the task sits `pending` forever server-side.
The only signal the assignee gets is the create-time kitty dispatch-wake
(`_auto_wake_targeted_idle`, `chats.py:1456`), which is cooldown/idle/window-gated.

### Defect 2 — `signal_task_start` does not flip status; AWAITING-ACK reads only `task_update` [BROKEN]
`signal_task_start` (`chats.py:1598`) appends a `task_signal` record but
deliberately does NOT write a `task_update` / flip to `in_progress` — "the assignee
still drives pending → in_progress." The AWAITING-ACK banner (`_discover_unfired_acks`,
`user_prompt_submit.py:517`) classifies a task as still-pending purely on the
absence of a `task_update` with status≠pending. So an assignee who receives BEGIN,
does the work, but never calls `chat_task_update(in_progress)` shows as
"unacked" forever — exactly muther's "assignees who already did the work never acked."

### Defect 3 — BEGIN signal is SSE-only; compounds ISSUE 1 [BROKEN]
When the gate DOES fire, the BEGIN goes out `send_message(..., to=[agent_id])`
(`chats.py:3640`) — SSE-only. An idle SSE-dead assignee won't take a turn on it
(documented at `chats.py:1671-1677`). The create-time kitty wake fired EARLIER
(before acks), so its cooldown can suppress a second wake at BEGIN time. There is
no kitty re-wake after the gate fires.

## Coverage decision (proposed — needs Joseph's ruling on Defect 2 semantics)

| Defect | Fix | Risk |
|---|---|---|
| 1 | When `auto_begin=True` AND an `assignee_session_id` is set AND `required_agents` is empty, treat the assignee as the implicit single required agent (or directly fire `signal_task_start` for the assignee). A solo-assignee task should auto-begin on the assignee's ack, not require a separately-populated `required_agents`. | low — additive; preserves the multi-agent gate path |
| 2 | TWO options — Joseph picks: (a) **leave the contract** (assignee must call `chat_task_update(in_progress)`) but make the role-doc + BEGIN message explicit that the ack is required, OR (b) **auto-advance** PENDING→in_progress when `signal_task_start` fires for a solo assignee (the signal IS the start). (b) changes a deliberate state-machine contract — needs a ruling. | (b) is medium — touches the task state machine + AWAITING-ACK semantics |
| 3 | After the auto_begin gate fires, re-fire the kitty dispatch-wake for the assignee (reuse `_auto_wake_targeted_idle`) so the BEGIN reaches an SSE-dead seat. Bypass/parameterize the create-time cooldown for the BEGIN-time wake. Pairs with ISSUE 1 Fix B/C (already shipped) + Path A (prefixed-roster discovery, shipped). | low — reuses existing wake actuator |

## Test contract
- D1: `test_auto_begin_solo_assignee_no_required_agents` — create task with assignee +
  auto_begin=true, no required_agents; assignee acks → BEGIN fires (currently: never).
- D2: depends on ruling. If (b): `test_signal_start_advances_solo_assignee_to_in_progress`
  + assert AWAITING-ACK banner clears. If (a): `test_begin_message_states_ack_required`.
- D3: `test_begin_refires_kitty_wake_for_sse_dead_assignee` — gate fires, assignee
  SSE-dead → a kitty wake is injected at BEGIN time (not just the create-time one).

## Open question for Joseph
Defect 2 is a contract decision: should `signal_task_start` for a SOLO assignee
auto-advance the task to `in_progress` (the signal = the start), or keep the
assignee-drives-the-transition contract and just make the ack requirement explicit?
The first is more robust against "did the work, forgot to ack"; the second keeps the
state machine honest (status reflects the assignee's own confirmation). Lean: (b) for
solo-assignee tasks only — multi-agent gated tasks keep the explicit-ack contract.
