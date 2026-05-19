# khimaira — v1.9 Orchestration Session State

> State-of-the-build for Phase B, covering v1.6.1.2 through v1.9.7.
> Reference chat: `chat-62166102f561` (Round 12 through Round 14+).
> 522/522 tests pass. HEAD: `179f729`.

---

## What this session built

A complete multi-agent orchestration stack on top of the khimaira-chat primitive.
Started with a working but rough chat system (v1.6) and ended with:
a 7-role taxonomy with budget bindings, enforcement-gate task assignment,
persistent assignment banners, private DMs, burst-cost reduction, an assign-batch
coordinator daemon endpoint, role.md auto-loading at boot, task cancellation,
SSE replay-on-resume, and full role + spawn skill coverage.

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

The standard 7-session roster is: `intake-1`, `khimaira-0` (master), `agent-1`,
`agent-2`, `observer-1`, `architect-1`, `critic-1`. Bootstrap via
`/khimaira-bootstrap-roster`.

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

### v1.9.4 — Intake + private DM default for hierarchical topology

Hierarchical topology chats default `private=True` for intake↔master DMs.
Session spawn skills updated to set topology field on room creation.

---

### v1.9.5 — Chat topology field + `task_status` private leak fix

**Topology field:** `create_room` now accepts `topology: str` parameter
(`"flat"` / `"hierarchical"` / `"custom"`). Stored in room meta. Surfaces in
`my_chats` list so clients can visually distinguish chat types.

**task_status private leak:** `task_status()` previously read all JSONL records
directly via `_read()`, bypassing the private-filter path. Private task metadata
was visible to non-recipients. Fixed: `task_status()` now routes through
`history()` (which applies the filter), then folds `kind=task` + `kind=task_update`
records to compute the final status.

---

### v1.9.6 — Role.md auto-loading, `member_roles` in `create_room`, `infer_role_from_name`, 📊 inverse banner

**Role.md auto-loading at boot:**
`session_start.py` now reads `_ROLES_DIR/<role>.md` and injects its contents
into the session's boot context as:
```
📖 ROLE FILE — <role>
<contents of roles/<role>.md>
```
Requires a non-empty `chat_roles` from `_discover_chat_roles`. The file must
exist at `packages/khimaira/src/khimaira/roles/<role>.md`. OSError guard: missing
file skips silently. Block appears before the `🎚️ chat roles` budget reminder.

**`member_roles` in `create_room`:**
`create_room(creator_id, invitees, ..., member_roles: dict | None = None)` stores
the role map in the room's META record. `_discover_chat_roles` can then resolve
each member's role from the explicit map (vs. inferring from created_by/accepted
records). `/khimaira-bootstrap-roster` uses this to wire roles at room creation
time.

**`infer_role_from_name`:**
`infer_role_from_name(name: str) -> str | None` — checks the session's name
prefix against the ROLE_BUDGET allowlist keys. `"agent-1"` → `"agent"`,
`"architect-2"` → `"architect"`. Returns None for names that don't match any
known role prefix (e.g. `"khimaira-0"`).

**📊 ASSIGNMENTS AWAITING ACK banner (inverse of ⏳):**
UserPromptSubmit hook now surfaces tasks that the current session created
(as master) but that still have `status=pending`. Closes the gap where master
lost track of pending assignments they fired but haven't received acks for yet.
Opt-out: `KHIMAIRA_UNFIRED_ACK_BANNER=0`.

---

### v1.9.7 — SSE replay-on-resume, task TASK_CANCELLED, kind==msg filter

**SSE replay-on-resume (💬 MISSED CHAT EVENTS banner):**
UserPromptSubmit hook now polls `/api/chats/{id}/messages?since=<watermark>` for
each accepted chat. Messages from other senders that arrived while the session was
idle (not within the last 10 minutes, to avoid double-reporting live events) are
surfaced as:
```
💬 MISSED CHAT EVENTS — <chat-title> (N new)
  [sender → HH:MM]: <first 80 chars of body>
  ...
```
Watermarks persist in `_WATERMARKS_PATH` so replay is idempotent across turns.
Opt-out: `KHIMAIRA_CHAT_POLL_BANNER=0`.

**TASK_CANCELLED terminal state:**
`TASK_CANCELLED = "cancelled"` added to `monitor/chats.py`. Two new transitions
in `_TASK_TRANSITIONS`:
- `(TASK_PENDING, TASK_CANCELLED): {"master"}` — cancel stale/superseded pending task
- `(TASK_IN_PROGRESS, TASK_CANCELLED): {"master"}` — cancel task whose agent went silent

Assignees cannot cancel (`assignee_or_any` is NOT in the allowed set). `done →
cancelled` is intentionally absent — use approve or changes_requested instead.

`"cancelled"` added to `new_status` enum in `khimaira-chat/server.py` MCP tool schema.

**kind==msg filter on missed-chat banner:**
Private task records (`kind=task`, `kind=task_update`) have `sender_id` redacted
for non-recipients. These leaked through the sender filter as blank entries in the
missed-chat banner. Fixed: only `kind=msg` records are included.

---

## Hook context blocks injected per turn

Each session receives a layered context injection from the SessionStart and
UserPromptSubmit hooks. Here is the complete catalog:

### SessionStart blocks (once per boot)

| Block | Hook | Since | Notes |
|---|---|---|---|
| `🆔 khimaira session_id: ...` | SessionStart | v1.0 | Always emitted |
| `💬 To enable real-time chat delivery...` | SessionStart | v1.3 | Nudges agent to call `chat_my_chats` |
| `📬 khimaira inbox — N unread answer(s)` | SessionStart | v1.0 | Only when inbox non-empty |
| `📦 khimaira handoffs` | SessionStart | v1.0 | Only when cwd-scoped handoffs exist |
| `📋 khimaira tasks — N open assignment(s)` | SessionStart | v1.0 | Only when task sources configured |
| `📖 ROLE FILE — <role>` | SessionStart | v1.9.6 | Only when session is in a chat with a resolved role |
| `🎚️ khimaira chat roles + recommended budgets` | SessionStart | v1.6.1 | Only when session has accepted chat memberships |
| `📋 khimaira — N other session(s) active` | SessionStart | v1.0 | Only when other sessions active in last 30min |

### UserPromptSubmit blocks (every turn)

| Block | Since | Condition |
|---|---|---|
| `💬 MISSED CHAT EVENTS — <chat>` | v1.9.7 | Messages from others arrived while idle; replayed from watermark |
| `📊 ASSIGNMENTS AWAITING ACK` | v1.9.6 | Master has pending tasks not yet acked by assignee |
| `⏳ KHIMAIRA PENDING ASSIGNMENT(S)` | v1.8 | Session has unacked task assignment |
| `⚠️ STALE TASK ACK(S)` | v1.8 | Previously-acked task's budget drifted post-restart |
| `🎚️ khimaira chat roles + recommended budgets` | v1.7 | Session is in at least one chat |
| `🔇 channel-only event — respond minimally` | v1.9 | Turn triggered by SSE channel block (non-review event) |
| `📋 channel event — master review required` | v1.9 | Turn triggered by SSE channel block (done/task event) |

---

## All new skills (this session)

| Skill | Description |
|---|---|
| `/khimaira-assign <agent> <task> [--model X] [--effort Y]` | Assign task with enforcement gate; thin wrapper over `assign-batch` |
| `/agent-ready` | Agent verifies settings.json + sends ack to master for pending assignment |
| `/khimaira-consult <deputy> "<question>"` | Fire opus-grade synthesis question to a named sidecar |
| `/khimaira-spawn-architect [name]` | Spawn opus/max consult sidecar (default: `architect-1`) |
| `/khimaira-spawn-intake [name]` | Spawn sonnet/medium user-facing front-end (default: `intake-1`) |
| `/khimaira-bootstrap-roster [<map>]` | Onboard a fresh 7-role roster in one call |

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
Role files are auto-injected at boot via `📖 ROLE FILE` block (v1.9.6).

---

## Test suite

522/522 pass. No regressions.

Key additions this session: +4 scope_cwd tests, +5 assign-batch tests, +4 private DM
tests, +3 task-cancel tests, +2 missed-chat banner tests.

---

## File map (rough)

| File | Changes |
|---|---|
| `monitor/chats.py` | `ROLE_ARCHITECT`, `ROLE_INTAKE`, `ROLE_BUDGET`; `private` param on 3 functions; `history()` filter; `load_room` fix; `assign_batch` coordinator; `topology` field; `member_roles` param; `infer_role_from_name`; `TASK_CANCELLED` + 2 transitions |
| `monitor/api/chats.py` | `private` fields on 3 models; `AssignBatchReq`; route handlers; `topology` field |
| `monitor/sessions.py` | `scope_cwd` on `post_notice`; inbox filter |
| `monitor/api/sessions.py` | `NoticeReq.scope_cwd`; `?cwd=` query params |
| `server/monitor_tools.py` | `session_post_notice` MCP `scope_cwd` param |
| `hooks/user_prompt_submit.py` | `_check_bottleneck`, persistent banner functions, `_channel_event_response_level`; `scope_cwd` pass-through; `_discover_unfired_acks`; `_poll_missed_chat_events` |
| `hooks/session_start.py` | `_ROLE_BUDGET` (architect, intake); implicit-agent fallback; `_consume_inbox(cwd=)`; `_ROLES_DIR`; role.md injection block |
| `packages/khimaira-chat/src/khimaira_chat/server.py` | 4 tool schemas + dispatch with `private`; `"cancelled"` in `new_status` enum |
| `packages/khimaira-chat/src/khimaira_chat/daemon_client.py` | 3 wrappers with `private` |
| `packages/khimaira/src/khimaira/roles/` | 6 role docs |
| `~/.claude/commands/` | 6 new skills; `khimaira-spawn-deputy.md` deleted |
| `tests/test_chats.py` | +4 private DM tests; +5 assign-batch tests; +3 cancel tests |
| `tests/test_sessions_unit.py` | +4 scope_cwd tests |
| `tests/test_user_prompt_submit.py` | +2 missed-chat banner tests |
| `scripts/watchers/khimaira-bottleneck-watch.sh` | `scope_cwd` pass-through |
| `tasks/v1.9-assign-batch/IMPLEMENTATION.md` | Assign-batch coordinator design spec |

---

## Known gaps

| Gap | Notes |
|---|---|
| SSE delivery during restart window | Events that arrive while Claude Code is restarting are not delivered. Missed-chat banner (v1.9.7) surfaces them on the next turn via polling — but the gap still exists; it's mitigated, not fixed. |
| `-n` flag not syncing to `session_list` | `session_set_name` updates the daemon's in-memory state; if the daemon restarts, names are lost until the session sets them again on boot. |
| Programmatic `/model` switching requires user action | Chat role directives are advisory. If a session is reassigned to a different role requiring a different model, the user must type the `/model` command manually in that window. No automated enforcement path exists. |
| ~~Intake-master `private=True`~~ | ✅ API-enforced: hierarchical chats auto-default `private=True` for targeted messages since v1.9.5 (`chats.py:597-603`). Was mistakenly marked as convention-only. |
| Anthropic GitHub issues #59499–#59502 outreach | Pending |
