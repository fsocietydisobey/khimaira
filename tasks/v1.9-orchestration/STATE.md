# khimaira — v1.9 Orchestration Session State

> State-of-the-build for Phase B, covering v1.6.1.2 through v1.9.3.
> Reference chat: `chat-62166102f561` (Round 12 through Round 14+).
> 507/507 tests pass. ~15+ commits worth of changes uncommitted on `main`.

---

## What this session built

A complete multi-agent orchestration stack on top of the khimaira-chat primitive.
Started with a working but rough chat system (v1.6) and ended with:
a 6-role taxonomy with budget bindings, enforcement-gate task assignment,
persistent assignment banners, private DMs, burst-cost reduction, an assign-batch
coordinator daemon endpoint, and full role + spawn skill coverage.

---

## Topology

```
Joseph → [intake-1] ← user-facing (sonnet/medium)
             ↓ 🎯 INTAKE HANDOFF (private DM)
      [master/khimaira-0] ← coordinator (sonnet/medium)
             ↓ /khimaira-assign + /khimaira-consult
             ┌──────────┼──────────────┬───────────────┐
             ↓          ↓              ↓               ↓
       [agents × N] [observers × M] [architect-1] [critic ad-hoc]
        (sonnet)     (haiku)         (opus, on-demand)
```

---

## Final role taxonomy

| Role | Model | Effort | Job |
|---|---|---|---|
| `intake` | sonnet | medium | User-facing front-end; parses intent → handoff to master |
| `master` | sonnet | medium | Orchestrator; decomposes, delegates, integrates |
| `agent` | sonnet | medium | Executor; runs assigned tasks |
| `observer` | haiku | default | Read-only auditor |
| `critic` | (none) | (none) | Constructive challenger |
| `architect` | opus | max | Synthesis sidecar (consulted on demand) |

**Note:** master was changed from opus/max to sonnet/medium for routine
coordination. Routine orchestration is mechanical; architect at opus/max is
consulted on-demand for synthesis questions. This cuts cost concentration
from the coordination layer.

---

## Versions shipped

### v1.6.1.2 — `_discover_chat_roles` silent-skip fix

3-LOC fallback to "agent" for accepted-but-not-in-`member_roles` sessions.
Verified across 4 seats — all chats surfacing budget recommendations where
previously 0 were.

**File:** `packages/khimaira/src/khimaira/hooks/session_start.py`

---

### v1.7.x — `/khimaira-assign` + `/agent-ready` enforcement-gate

`/khimaira-assign` assignment block contains:
- "DO NOT START WORK YET" gate + explicit "suppress research reflex during gate" directive
- Numbered agent protocol (hold → user sets budget → user types `ready [task-id: ...]` →
  verify settings.json → ack master → wait for 🟢 begin)

Tested e2e. Agents transparently decline budget directives that conflict with
user's explicit `/model` settings (correct per untrusted-external-data protocol).

**Design decisions banked:**
- Chat directives are recommendation-shape, not commands — agent's `settings.json`
  at the moment of "ready" is authoritative; chat directive is advisory.
- Enforcement-gate suppresses "research before implementing" reflex — load-bearing
  default inverts to failure mode during a hold gate; gate spec must explicitly
  name which defaults it overrides.
- Disclosure-as-remediation — transparent disclosure of a pre-read violation IS the
  remediation; no re-run needed.

**Files:** `~/.claude/commands/khimaira-assign.md`, `~/.claude/commands/agent-ready.md`

---

### v1.8 — Persistent assignment banner

**Problem:** In-window prompt for a `/khimaira-assign` task scrolled past unnoticed.

**Solution:** UserPromptSubmit hook re-surfaces pending assignments every turn:

| Function | Purpose |
|---|---|
| `_discover_pending_assignments(session_id)` | Walk chats JSONL; find unacked `🔔 TASK ASSIGNMENT` blocks targeted at this session |
| `_format_pending_assignments(assignments)` | `⏳ KHIMAIRA PENDING ASSIGNMENT(S)` banner |
| `_check_stale_acks(session_id)` | Detect acks whose budget has drifted post-restart |
| `_format_stale_acks(stale)` | `⚠️ STALE TASK ACK(S)` banner |

**v1.8.1 bug fixes:**
- Task status folding: first pass iterates `kind=task`/`task_update` records;
  `done`/`approved` tasks excluded.
- Required budget section scoping: `/model` + `/effort` regex scoped to lines
  after "Required budget" header (breaks on blank line). Prevented false positive
  where "model/effort settings from ~/.claude/settings.json" in prose matched
  `required_effort="settings"`.

**Index-based ack filtering:** uses record-index comparison within JSONL (not
timestamps). Re-acks after restart have higher indices; handles re-ack-after-restart
correctly.

**File:** `packages/khimaira/src/khimaira/hooks/user_prompt_submit.py`

---

### v1.8.1 — Cross-project notice scope (`scope_cwd`) — P0 fix

**Bug:** `session_post_notice` leaked across projects. khimaira-0's deputize
broadcast surfaced in a `/home/_3ntropy/dev/jeevy_portal` session.

**Fix:** `scope_cwd: str | None = None` on `session_post_notice`. When set, notice
surfaces only to sessions whose `cwd == scope OR cwd.startswith(scope)`.
Mismatched-cwd notices skip WITHOUT incrementing `surface_count` (preserved for
the correct session). Backward-compat: `scope_cwd=None` → broadcast-all unchanged.

**Files touched (8):**
- `monitor/sessions.py` + `monitor/api/sessions.py` — storage + route
- `server/monitor_tools.py` — MCP tool
- `hooks/user_prompt_submit.py` + `hooks/session_start.py` — hook pass-through
- `scripts/watchers/khimaira-bottleneck-watch.sh` — watcher scoping
- `tests/test_sessions_unit.py` — +4 scope tests

---

### v1.9 — Burst-cost reduction

**Mitigation 2 — `_channel_event_response_level`**

When the prompt is purely a `<channel source="khimaira-chat">` block (no user
typing), classify and inject:

| Event | Level | Block |
|---|---|---|
| `kind=task_update, status=in_progress/pending` or `kind=msg` | `"minimal"` | `🔇 channel-only event — respond minimally` |
| `kind=task_update, status=done/approved/changes_requested` or `kind=task` | `"review"` | `📋 channel event — master review required` |
| User text also present | `""` | (no injection) |

Replaces boolean `_is_channel_only_prompt`. The `status=done` silent-suppression
bug (master was failing to review completed tasks) was caught and fixed in this
same session.

**Mitigation 3 — Assign-batch coordinator (`POST /api/chats/{chat_id}/assign-batch`)**

Collapses master's 3N+K+2 call loop → 1 daemon call.

State machine: `CREATE_TASKS → NOTIFY → AWAIT_ACKS → FIRE_BEGIN`

`/khimaira-assign` skill rewired as thin wrapper; `--no-batch` flag for manual fallback.

Design note: `pending→in_progress` NOT driven by coordinator — requires assignee
context. Defers to agent self-transition (consistent with `signal_task_start`).

**Files:** `monitor/chats.py:1748`, `monitor/api/chats.py:304`

---

### v1.9.1 — Architect role

New 5th role: **architect** — synthesis + design thinker, consult sidecar.
Budget: opus/max. Idle until consulted; one structured reply per consult.

- `ROLE_ARCHITECT` in `chats.py` + `session_start.py`
- `packages/khimaira/src/khimaira/roles/architect.md` (~115 lines)
- `/khimaira-spawn-architect [name]` skill (default: `architect-1`)
  - Cross-references `/khimaira-architect` chain primitive (writes IMPLEMENTATION.md docs;
    different shape from live-session consult)
- `/khimaira-spawn-deputy` deleted; `/khimaira-architect` chain skill unchanged

---

### v1.9.2 — Private DMs (`private=True`)

`chat_send` / `chat_send_to` / `chat_task_create` / `chat_task_update` all gained
`private: bool = False`. When True, filtered from `history()` for non-recipients.

**Filter contract:**
- Sender always sees own message
- Explicit `to` recipients see it
- Chat master always sees all private messages (audit)
- Non-recipients: silently excluded

**Validation:** private=True without recipients → ValueError. Private task without
assignee → ValueError.

**Non-obvious fix: `load_room` history gap**
`load_room` only included `kind=MSG` in `room["messages"]`. TASK / TASK_UPDATE /
TASK_SIGNAL records were absent from `history()` for ALL callers. Extended to
`kind in (MSG, TASK, TASK_UPDATE, TASK_SIGNAL)`. This was necessary for the private
task filter AND made `chat_history` a complete transcript for the first time.

**Role discipline pass:** Low-volume-events constraint bullet added to all 5 existing
role .md files (one bullet per Constraints section):
> Fire `chat_task_update` ONLY at major lifecycle transitions. Use `session_log_decision`
> for intermediate progress.

**Files touched (5):**
- `monitor/chats.py` — 3 function sigs + records + `history()` filter + `load_room`
- `monitor/api/chats.py` — 3 Pydantic models + 3 route handlers
- `packages/khimaira-chat/src/khimaira_chat/server.py` — 4 schemas + dispatch
- `packages/khimaira-chat/src/khimaira_chat/daemon_client.py` — 3 wrappers
- `tests/test_chats.py` — +4 private DM tests

---

### v1.9.3 — Intake role

New 6th role: **intake** — user-facing front-end.
Budget: sonnet/medium.

- `ROLE_INTAKE` in `chats.py` + `session_start.py`
- `packages/khimaira/src/khimaira/roles/intake.md` (~165 lines)
  - Full intake↔master handoff protocol with `🎯 INTAKE HANDOFF` spec format:
    `intake-id`, `intent`, `scope`, `success-criterion`, `constraints`, `raw-message`
  - Master ack pattern
  - `private=True` noted as default for intake↔master channel (first concrete user of v1.9.2)
- `/khimaira-spawn-intake [name]` skill (default: `intake-1`)
  - Master-must-exist pre-check with helpful error
  - Comparison table vs `/khimaira-spawn-architect`

---

## All new skills (this session)

| Skill | Description |
|---|---|
| `/khimaira-assign <agent> <task> [--model X] [--effort Y]` | Assign task with enforcement gate; thin wrapper over `assign-batch` |
| `/agent-ready` | Agent verifies settings.json + sends ack to master for pending assignment |
| `/khimaira-consult <deputy> "<question>"` | Fire opus-grade synthesis question to a named sidecar |
| `/khimaira-spawn-architect [name]` | Spawn opus/max consult sidecar (default: `architect-1`) |
| `/khimaira-spawn-intake [name]` | Spawn sonnet/medium user-facing front-end (default: `intake-1`) |

Deleted: `/khimaira-spawn-deputy`

---

## Role files

| File | Status |
|---|---|
| `roles/master.md` | ✅ |
| `roles/agent.md` | ✅ |
| `roles/observer.md` | ✅ |
| `roles/critic.md` | ✅ |
| `roles/architect.md` | ✅ (v1.9.1) |
| `roles/intake.md` | ✅ (v1.9.3) |

All 6 have low-volume-events constraint in Constraints section.

---

## Test suite

507/507 pass. No regressions.

Key additions this session: +4 scope_cwd tests, +5 assign-batch tests, +4 private DM tests.

---

## File map (rough)

| File | Changes |
|---|---|
| `monitor/chats.py` | `ROLE_ARCHITECT`, `ROLE_INTAKE`, `ROLE_BUDGET`; `private` param on 3 functions; `history()` filter; `load_room` fix; `assign_batch` coordinator |
| `monitor/api/chats.py` | `private` fields on 3 models; `AssignBatchReq`; route handlers |
| `monitor/sessions.py` | `scope_cwd` on `post_notice`; inbox filter |
| `monitor/api/sessions.py` | `NoticeReq.scope_cwd`; `?cwd=` query params |
| `server/monitor_tools.py` | `session_post_notice` MCP `scope_cwd` param |
| `hooks/user_prompt_submit.py` | `_check_bottleneck`, persistent banner functions, `_channel_event_response_level`; `scope_cwd` pass-through |
| `hooks/session_start.py` | `_ROLE_BUDGET` (architect, intake); implicit-agent fallback; `_consume_inbox(cwd=)` |
| `packages/khimaira-chat/src/khimaira_chat/server.py` | 4 tool schemas + dispatch with `private` |
| `packages/khimaira-chat/src/khimaira_chat/daemon_client.py` | 3 wrappers with `private` |
| `packages/khimaira/src/khimaira/roles/` | 6 role docs |
| `~/.claude/commands/` | 5 new skills; `khimaira-spawn-deputy.md` deleted |
| `tests/test_chats.py` | +4 private DM tests; +5 assign-batch tests |
| `tests/test_sessions_unit.py` | +4 scope_cwd tests |
| `scripts/watchers/khimaira-bottleneck-watch.sh` | `scope_cwd` pass-through |
| `tasks/v1.9-assign-batch/IMPLEMENTATION.md` | Assign-batch coordinator design spec |

---

## Pending / not done

| Item | Notes |
|---|---|
| intake-1 session not yet spawned | Skill exists; needs Joseph to `/rename` a fresh window + `/model sonnet` + `/effort medium` |
| Role.md auto-loading | Files exist but NOT injected into hook context yet — agents must read them manually |
| v1.7.3 SSE replay-on-resume | Known gap; not fixed this session |
| `task_status` private leak | `task_status()` reads all records via `_read()` directly; may expose private task metadata |
| ~15+ commits uncommitted on `main` | |
| Anthropic GitHub issues #59499–#59502 outreach | Pending |
| Intake-master private bootstrap | `intake.md` spec says `private=True` default; no enforcement yet |
