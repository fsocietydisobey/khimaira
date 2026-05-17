# khimaira v1.9 Orchestration — Usage Guide

This document is the definitive operator guide for the 7-role orchestration system
as of v1.9.7. For what was built and why, see STATE.md in this directory.

---

## Role Topology

```
Joseph
  └─ [intake-1]         ← you talk here (sonnet/medium)
       │  🎯 INTAKE HANDOFF (private DM)
       ▼
    [master]            ← orchestrator (sonnet/medium)
       │  /khimaira-assign    /khimaira-consult
       ├─── [agent-1]         ← executor (sonnet/medium)
       ├─── [agent-2]         ← executor (sonnet/medium)
       ├─── [observer-1]      ← auditor (haiku/default)
       ├─── [architect-1]     ← design sidecar (opus/max, on-demand)
       └─── [critic-1]        ← reviewer (orchestrator picks budget)
```

---

## Morning Startup Sequence

This is the standard procedure for starting a fresh multi-agent session.
Follow it in order — the later steps depend on earlier ones.

### Step 1 — Open your windows

Open 7 Claude Code windows (tabs, splits, or terminal panes — doesn't matter).
Name them before anything else. In each window, type:

```
/rename intake-1
```
```
/rename khimaira-0
```
```
/rename agent-1
```
```
/rename agent-2
```
```
/rename observer-1
```
```
/rename architect-1
```
```
/rename critic-1
```

Window names are used by `/khimaira-assign` and `/khimaira-consult` to route
tasks to the right session. Misspelled names cause silent routing failures.

### Step 2 — Bootstrap the roster from the master window

In the `khimaira-0` window, run:

```
/khimaira-bootstrap-roster
```

This fires one call to the daemon that:
1. Creates a single hierarchical chat room with all 7 sessions as members
2. Assigns explicit roles in `member_roles` (master, agent, intake, observer, architect, critic)
3. Fires role-directive SSE events to each session so they see the `🎚️` budget prompt
4. Logs the session into the khimaira state system

After bootstrap completes, every window will display:
```
🎚️ khimaira chat roles + recommended budgets (1 chat(s)):
  chat-<id> "roster — YYYY-MM-DD" — <role> → /model <X>, /effort <Y>
```

### Step 3 — Set model and effort in each window

In each window, type the two commands shown by the `🎚️` block:

| Window | Commands |
|---|---|
| `intake-1` | `/model sonnet` + `/effort medium` |
| `khimaira-0` | `/model sonnet` + `/effort medium` |
| `agent-1` | `/model sonnet` + `/effort medium` |
| `agent-2` | `/model sonnet` + `/effort medium` |
| `observer-1` | `/model haiku` + `/effort default` |
| `architect-1` | `/model opus` + `/effort max` |
| `critic-1` | (orchestrator's discretion — default sonnet/medium) |

These are not automatically enforced — the hook makes recommendations, you apply
them. Budget compliance is checked at task-assignment time via `/agent-ready`.

### Step 4 — Verify boot context

On each session's first turn, SessionStart injects context blocks. Verify you see:

- `🆔 khimaira session_id: ...` — confirms daemon is reachable
- `📖 ROLE FILE — <role>` — confirms role.md auto-loaded (v1.9.6+)
- `🎚️ chat roles + recommended budgets` — confirms chat membership

If `📖 ROLE FILE` is absent: the session isn't a member of any accepted chat yet.
Run `/khimaira-bootstrap-roster` first.

### Step 5 — Route requests through intake-1

Once all windows are up and budgets are set, your workflow starts at `intake-1`.
Type your task there; intake parses intent and hands off to master.

---

## Task Assignment Flow (Full)

This is the end-to-end lifecycle of a task from master to agent:

```
1. Master creates task  ─────────────────────────────────────────────┐
   /khimaira-assign agent-1 "implement rate limiting" \              │
       --model sonnet --effort medium                                 │
   → daemon creates chat_task record, fires SSE to agent-1           │
                                                                      │
2. Agent sees enforcement gate ──────────────────────────────────────┤
   ⏳ KHIMAIRA PENDING ASSIGNMENT(S)                                  │
     [task-abc] implement rate limiting middleware                    │
     Required: /model sonnet, /effort medium                         │
     DO NOT START WORK YET                                            │
                                                                      │
3. Agent types /agent-ready (after setting /model + /effort) ────────┤
   → skill reads settings.json, verifies compliance                  │
   → sends ✅ ready [task-id: task-abc] | model=sonnet effort=medium  │
   → master receives ack                                              │
                                                                      │
4. Master fires begin signal ────────────────────────────────────────┤
   (automatically via assign-batch coordinator, or manually)          │
   🟢 ALL AGENTS CONFIRMED — BEGIN                                    │
                                                                      │
5. Agent works, then reports done ──────────────────────────────────┤
   chat_task_update(task_id, "done", note="...")                     │
                                                                      │
6. Master reviews ───────────────────────────────────────────────────┤
   📋 channel event — master review required                         │
   → Master reads note, inspects files                               │
   → chat_task_update(task_id, "approved") or "changes_requested"   │
                                                                      │
7. If changes_requested → agent gets feedback, fixes, re-submits ────┘
   (TASK_CHANGES_REQUESTED → TASK_IN_PROGRESS is allowed for assignee)
```

**Master can cancel at any point** (v1.9.7):
- `pending → cancelled`: task was superseded before agent started
- `in_progress → cancelled`: agent went silent mid-task
- Neither assignees nor observers can cancel — master-only.

---

## Role-by-Role Operating Guide

### intake

**Budget:** sonnet/medium  
**Triggered by:** Joseph typing to the intake window  
**Primary output:** `🎯 INTAKE HANDOFF` spec delivered to master via private DM

**What intake does:**
1. Reads the user's message carefully
2. Parses intent (what do they want?), scope (what's in/out?), and success criterion
3. Asks ONE clarifying question if intent is ambiguous — no more
4. Formats the handoff spec and sends it privately to master
5. Waits for master's `🛬 INTAKE RECEIVED` ack
6. When master signals `🏁 INTAKE COMPLETE`, formats the result for Joseph

**Handoff format:**
```
🎯 INTAKE HANDOFF [intake-id: <8-char-hex>]
User: <name>
Intent (one-line): <what they want>
Scope: <what's in-scope; what's excluded>
Success criterion: <how master knows it's done>
Constraints: <must-not-break, time, technical>
Raw user message (for context): "<verbatim>"
```

**What NOT to do:**
- Don't decompose the task into subtasks — that's master's job
- Don't fire multiple messages to master before receiving ack
- Don't expose master's internal coordination to Joseph
- Don't do implementation work

---

### master

**Budget:** sonnet/medium  
**Triggered by:** intake handoff, direct Joseph request, or own reasoning  
**Primary output:** task assignments, consult questions, integration decisions

**What master does:**
1. Receives intake handoff
2. Decomposes into parallelizable subtasks
3. Assigns via `/khimaira-assign` (one call per agent or batch)
4. Monitors via `📊 ASSIGNMENTS AWAITING ACK` and `📋 review required` banners
5. Reviews completed tasks: approves or sends back with specific feedback
6. Integrates agent results into a coherent whole
7. Signals `🏁 INTAKE COMPLETE` to intake when done

**Consult pattern:** When facing an architectural trade-off:
```
/khimaira-consult architect-1 "Should we use Redis or Postgres for
token buckets? 500k req/min peak, existing PG infra, no Redis today."
```

**What NOT to do:**
- Don't do implementation work that agents can do
- Don't approve tasks without actually reading the note and inspecting work
- Don't fire multiple `/khimaira-assign` to the same agent for the same task
- Don't use `chat_task_update` for status pings — only at lifecycle boundaries

---

### agent

**Budget:** sonnet/medium  
**Triggered by:** `⏳ PENDING ASSIGNMENT` banner or `🔔 TASK ASSIGNMENT` SSE block  
**Primary output:** completed work + `chat_task_update(done, note=...)`

**Protocol:**
1. See pending assignment banner → set `/model sonnet` + `/effort medium`
2. Type `/agent-ready` → skill verifies settings.json + acks master
3. Wait for `🟢 BEGIN` signal before reading task
4. Read task, do work, report done with a brief note (what changed, test results)
5. Wait for master approval before starting next task

**Note format (done):** One sentence on what was implemented + key file/line +
test results. Example: "Added `_ROLES_DIR` constant and role.md injection block
at session_start.py:842-850; 520 passed, 0 failures."

**What NOT to do:**
- Don't start work before receiving `🟢 BEGIN` signal
- Don't pre-read or pre-plan during the hold gate
- Don't run multiple tasks in parallel without explicit master approval
- Don't send status updates mid-task via `chat_task_update` — use `session_log_decision`

---

### observer

**Budget:** haiku/default  
**Triggered by:** chat activity (passive)  
**Primary output:** none unless explicitly asked

**What observer does:**
- Reads all chat history passively
- Surface anomalies if spotted: budget violations, scope creep, stalled tasks
- Responds only when directly addressed by master or via `chat_send_to`

**What NOT to do:**
- Don't send unsolicited opinions
- Don't approve or reject tasks (observer has no master rights)
- Don't fire MCP tool calls without master asking

---

### architect

**Budget:** opus/max  
**Triggered by:** `/khimaira-consult architect-1 "<question>"` from master  
**Primary output:** one structured synthesis reply

**Reply format:**
```
## Context
<what I understand the situation to be>

## Options
### Option A: <name>
<mechanics, pros, cons>

### Option B: <name>
<mechanics, pros, cons>

## Recommendation
<which option and why; what conditions would change this>

## Risks
<what to watch for if recommendation is followed>
```

**What NOT to do:**
- Don't send multiple replies to one consult — one structured answer only
- Don't do implementation work
- Don't initiate conversation with master unsolicited

---

### critic

**Budget:** orchestrator's discretion (typically sonnet/medium; opus/max for deep review)  
**Triggered by:** master assignment via `/khimaira-assign critic-1 "review ..."`  
**Primary output:** structured review with must-fix items first

**Review format:**
```
## Must-fix
- <specific issue + file:line + why it's a problem>
...

## Worth noting (not blocking)
- <observations, style, future risks>
...

## Summary
<overall verdict: looks good / has issues / needs rework>
```

**What NOT to do:**
- Don't approve or reject tasks directly (critic has no master rights)
- Don't rewrite code in the review — describe what should change
- Don't conflate must-fix with worth-noting

---

## Primitive Reference

### Chat task primitives

**`chat_task_create`** — master creates a task in a chat:
```python
mcp__khimaira-chat__chat_task_create(
    session_id=my_id,
    chat_id=chat_id,
    body="implement rate limiting middleware",
    assignee_id=agent_session_id,
    private=True  # only master + assignee see it
)
```

**`chat_task_update`** — move a task through its lifecycle:
```python
mcp__khimaira-chat__chat_task_update(
    session_id=my_id,
    chat_id=chat_id,
    task_id=task_id,
    new_status="done",    # or: in_progress, approved, changes_requested, cancelled
    note="Done. Rate limiter in auth/middleware.py:42. 520 passed."
)
```
Valid transitions:
- `pending → in_progress`: assignee or any accepted member
- `in_progress → done`: assignee or any accepted member
- `done → approved | changes_requested`: master only
- `changes_requested → in_progress`: assignee or any accepted member
- `pending → cancelled`: master only
- `in_progress → cancelled`: master only

**`chat_task_signal_start`** — master fires the "you can start" signal to a pending task:
```python
mcp__khimaira-chat__chat_task_signal_start(
    session_id=my_id,
    chat_id=chat_id,
    task_id=task_id,
    note="all blockers resolved"
)
```
This does NOT change task status — the agent still drives `pending → in_progress`.
Used for the explicit "begin" gate in the enforcement-gate flow.

**`chat_task_status`** — list all tasks in a chat with current status:
```python
mcp__khimaira-chat__chat_task_status(session_id=my_id, chat_id=chat_id)
```

---

### Messaging primitives

**`chat_send`** — broadcast to all members of a chat:
```python
mcp__khimaira-chat__chat_send(session_id=my_id, chat_id=chat_id, body="...")
```

**`chat_send_to`** — private message to a subset of members:
```python
mcp__khimaira-chat__chat_send_to(
    session_id=my_id,
    chat_id=chat_id,
    recipients=[target_session_id],
    body="...",
    private=True
)
```

---

### Session coordination primitives

**`session_log_decision`** — bank an architectural commitment:
```python
mcp__khimaira__session_log_decision(
    session_id=my_id,
    text="Use cursor-based pagination for the task list endpoint",
    why="Result set can grow unbounded; offset pagination breaks under concurrent writes"
)
```
Use for real choices (architecture, approach, trade-off). Don't log trivial acts
like "renamed variable X to Y".

**`session_log_question`** — post a question for another session to answer:
```python
mcp__khimaira__session_log_question(
    session_id=my_id,
    text="Is the assign-batch endpoint idempotent on retry?",
    target_session_id=other_session_id  # optional; surfaces in their inbox
)
```

**`session_post_notice`** — send a durable FYI to another session's inbox:
```python
mcp__khimaira__session_post_notice(
    from_session_id=my_id,
    target_session_id=recipient_id,
    text="I found a related bug in auth/middleware.py:88 — flagging for your review",
    scope_cwd="/home/_3ntropy/dev/khimaira"  # scopes to this project only
)
```
Notices surface in the recipient's inbox on their next SessionStart or
`session_pending_notes` check. Auto-expire after 3 surfaces.

**`session_post_handoff`** — leave a work directive for the NEXT session in this project:
```python
mcp__khimaira__session_post_handoff(
    from_session_id=my_id,
    text="HANDOFF: The rate-limiter is merged. Next: wire it into the API gateway.",
    scope_cwd="/home/_3ntropy/dev/khimaira"
)
```
Handoffs auto-surface on SessionStart for any future session in this cwd.
They are **directives** — the receiving agent should start on them without
waiting for explicit user confirmation.

---

### Skills

| Skill | Who uses it | What it does |
|---|---|---|
| `/khimaira-assign <agent(s)> <task> [--model X] [--effort Y]` | master | Assign task with enforcement gate via daemon coordinator |
| `/agent-ready` | agent | Read pending assignment, verify settings.json budget, ack master |
| `/khimaira-consult <name> "<question>"` | master | Fire synthesis question to architect sidecar |
| `/khimaira-spawn-architect [name]` | master | Request Joseph to open architect window (opus/max) |
| `/khimaira-spawn-intake [name]` | master | Request Joseph to open intake window (sonnet/medium) |
| `/khimaira-bootstrap-roster [<map>]` | master | Onboard a fresh 7-role roster in one call |
| `/khimaira-deputize <vice>` | master | Pause-and-handoff master role to a fresh window |
| `/khimaira-resume` | vice | Reclaim master role across deputized chats |
| `/khimaira-orchestrate <peers...> <scope>` | master | Bootstrap a multi-turn collab chat manually |

---

## Reading the Hook Context Blocks

Each turn surfaces layered context injected by the hooks. Here is what each block
means and what to do when you see it:

### SessionStart blocks (on boot)

| Block | Meaning | Action |
|---|---|---|
| `🆔 khimaira session_id: ...` | Your session ID for passing to tools | Copy it if you need to pass it manually |
| `💬 To enable real-time chat delivery...` | SSE subscriber not yet registered | Call `chat_my_chats(session_id=...)` once |
| `📬 khimaira inbox — N unread` | Other sessions answered your questions | Read the answers; they're relevant to your current work |
| `📦 khimaira handoffs` | Prior session left work for you | Treat as your task list; start on the highest-priority item |
| `📖 ROLE FILE — <role>` | Your role's operating guide was injected | Read it if unfamiliar with the role |
| `🎚️ khimaira chat roles + recommended budgets` | You're a member of chat(s) with a role | Set `/model` + `/effort` to match if you haven't |

### UserPromptSubmit blocks (every turn)

| Block | Meaning | Action |
|---|---|---|
| `💬 MISSED CHAT EVENTS — <chat> (N new)` | Messages arrived while you were idle | Read them; decide if any require action |
| `📊 ASSIGNMENTS AWAITING ACK` | You created task(s) not yet acked by assignee | Wait for ack or check if agent is up |
| `⏳ KHIMAIRA PENDING ASSIGNMENT(S)` | You have an unacked task assignment | Set budget + run `/agent-ready` |
| `⚠️ STALE TASK ACK(S)` | Your budget drifted since you acked | Correct budget + re-run `/agent-ready` |
| `🎚️ chat roles + recommended budgets` | Budget reminder (repeats per-turn) | Set budget if you haven't already |
| `🔇 channel-only event — respond minimally` | Turn triggered by SSE (status update) | One-sentence ack; don't synthesize |
| `📋 channel event — master review required` | Agent's task reached done/changes_requested | Master must review and approve or request changes |

---

## Flows

### Flow 1 — Joseph asks intake a question

1. **You type to intake-1:**
   > "Can we add rate limiting to the auth endpoints?"

2. **Intake parses intent.** If clear → formats the handoff spec and sends it
   privately to master:
   ```
   🎯 INTAKE HANDOFF [intake-id: 3f9a1b2c]
   User: Joseph
   Intent (one-line): Add rate limiting to auth endpoints
   Scope: auth/ directory; exclude public read endpoints
   Success criterion: 429 responses after N requests/min, configurable
   Constraints: don't break existing tests
   Raw user message: "Can we add rate limiting to the auth endpoints?"
   ```

3. **Master acks** privately:
   ```
   🛬 INTAKE RECEIVED [intake-id: 3f9a1b2c] — decomposing now
   ```

4. **Master decomposes + delegates** to agents. Work proceeds.

5. **Master signals completion:**
   ```
   🏁 INTAKE COMPLETE [intake-id: 3f9a1b2c]
   Rate limiting added via token-bucket middleware. 3 files changed, 520 tests pass.
   ```

6. **Intake formats for Joseph** in natural language.

---

### Flow 2 — Master delegates work to agents

```
/khimaira-assign agent-1,agent-2 "implement rate limiting middleware" \
    --model sonnet --effort medium
```

The daemon coordinator handles everything server-side — task creation, SSE
fan-out, ack collection, begin signal — in one round-trip. Master gets back:

```json
{
  "task_ids": {"agent-1-id": "task-abc", "agent-2-id": "task-def"},
  "acks": {"agent-1-id": {"model": "sonnet", "effort": "medium"}, ...},
  "begin_fired": true,
  "elapsed_ms": 4312
}
```

If an agent times out (`missing_acks` non-empty): re-run `/khimaira-assign` for
the unresponsive agent or fire begin manually for the acked subset.

---

### Flow 3 — Agent receives an assignment

The `⏳ KHIMAIRA PENDING ASSIGNMENT(S)` banner appears each turn until acked:

```
⏳ KHIMAIRA PENDING ASSIGNMENT(S) — 1 task(s) waiting for your ack:

  [task-abc] implement rate limiting middleware
  Required budget: /model sonnet, /effort medium
  From: master (chat-<id>)

  DO NOT START WORK YET. Set the required budget first, then type:
    /agent-ready
```

After `/agent-ready` acks master and the `🟢 BEGIN` signal arrives, the agent
reads the task body and begins work.

---

### Flow 4 — Master consults architect

```
/khimaira-consult architect-1 "Should we use Redis or Postgres for the
rate-limit token bucket? 500k req/min peak, existing PG infra, no Redis today."
```

Architect replies with one structured synthesis. Master integrates the
recommendation into agent assignments.

---

### Flow 5 — Stale-ack recovery after restart

If a session restarted and `/model` reverted, the `⚠️ STALE TASK ACK(S)` banner
appears:

```
⚠️ STALE TASK ACK(S) — 1 assignment(s) with budget drift post-restart:
  [task-abc] implement rate limiting middleware
  Acked: model=sonnet effort=medium  |  Now: model=opus effort=max
  From: master (chat-<id>)
  Run /agent-ready when budget is corrected.
```

Fix: `/model sonnet` + `/effort medium` → `/agent-ready`. The ack re-fires.

---

### Flow 6 — Master cancels a stale task

If an agent went silent or a task is superseded:

```python
mcp__khimaira-chat__chat_task_update(
    session_id=master_id,
    chat_id=chat_id,
    task_id=stale_task_id,
    new_status="cancelled",
    note="Superseded by new approach — agent-2 will handle this instead"
)
```

The task moves to `cancelled` (terminal). The ⏳ banner in the original agent's
window will stop firing on the next turn.

---

## Post-Restart Checklist

1. **Rename all windows.** `/rename <role>-1` in each window before anything else.

2. **Bootstrap or verify chat membership.** Run `/khimaira-bootstrap-roster` in
   master's window if starting fresh, or verify each session shows `🎚️` on boot.

3. **Set budgets.** Each window's `🎚️` block shows required `/model` + `/effort`.
   Apply them.

4. **Check for missed events.** The `💬 MISSED CHAT EVENTS` banner surfaces messages
   that arrived while the session was restarting. Read them before proceeding.

5. **Check for pending assignments.** The `⏳ PENDING ASSIGNMENT(S)` banner fires
   automatically. If shown: set budget + `/agent-ready`.

6. **Check for stale acks.** The `⚠️ STALE TASK ACK(S)` banner fires if budget
   drifted. Correct + re-ack.

7. **Wake architect and intake if needed.** Send any message in those windows to
   surface their inbox (pending notices from pre-restart).

8. **Verify test suite.**
   ```
   pytest packages/khimaira/tests/ -q
   ```
   Should show 522/0 passed/failed. Any failure after restart is a regression —
   don't proceed until clean.

9. **Send a smoke-test assignment.**
   ```
   /khimaira-assign agent-1 "hello world" --model sonnet --effort medium
   ```
   Verify the ack flow, begin signal, and approval all work end-to-end.

---

## Troubleshooting

**"I don't see the ⏳ enforcement gate / pending assignment banner."**

The `_discover_pending_assignments` scanner walks `~/.local/state/khimaira/chats/*.jsonl`
for `🔔 TASK ASSIGNMENT` messages targeted at this session. Check:
1. Is the assignment message in the chat history? (`chat_history(session_id=..., chat_id=...)`)
2. Is this session an accepted member of that chat? (`chat_my_chats(session_id=...)`)
3. Was the assignment delivered via `chat_send_to` with the correct session ID?
   Session name resolution uses `session_list()` — if the agent hasn't set a name,
   resolution fails silently.

**"Banner shows ? as sender for a task event."**

Private task records (`kind=task`, `kind=task_update`) have `sender_id` redacted
for non-recipients. The missed-chat banner filters to `kind=msg` only (v1.9.7 fix),
so this should no longer appear. If it does: the banner is reading a pre-v1.9.7
chat JSONL — it's cosmetic, not a bug.

**"Agent reset model after restart — stale-ack banner didn't fire."**

The stale-ack scanner compares `settings.json.model` at ack time vs. now. If the
session restarted but hasn't had a turn yet, the scanner hasn't run. Type anything
in the window to trigger a turn — the banner will fire on the next prompt.

**"Missed-chat event didn't arrive during idle."**

The `💬 MISSED CHAT EVENTS` banner polls `/api/chats/{id}/messages?since=<watermark>`
on every UserPromptSubmit. If the session was completely idle (no turns fired),
the poll didn't run. The event will surface on the next turn automatically.

**"Agent never received the 🔔 TASK ASSIGNMENT block."**

Check:
1. Is the session an accepted member of the chat? (chat_my_chats)
2. Was the SSE subscriber registered? (chat_my_chats nudge on SessionStart)
3. Did the assignment specify the correct session ID or name?
4. Was the daemon running when the assignment was fired?

**"Consult question was sent but architect never answered."**

Architect's inbox surfaces on their NEXT turn. If the architect window hasn't
received a user prompt since the consult was sent, the notice is sitting in their
inbox. Type anything in the architect window to surface it.

**"Private DMs not showing in chat_history."**

Private messages are filtered by recipient. If you're calling `chat_history` with
the wrong `session_id`, private records won't appear. The chat creator (master)
always sees all messages for audit — verify you're querying as the correct session.

**"Can't approve a task — getting 403."**

Only the chat master (creator) can approve. Verify you're calling `chat_task_update`
with the master's `session_id`, not an agent's. If the master role transferred
(via `/khimaira-deputize`), the vice-master is the new approver.

**"Task is stuck in pending — agent is unresponsive."**

Cancel it from the master window:
```python
mcp__khimaira-chat__chat_task_update(
    session_id=master_id, chat_id=chat_id, task_id=...,
    new_status="cancelled", note="agent unresponsive — reassigning"
)
```
Then re-assign to a different agent.
