# khimaira v1.9 Orchestration — Usage Guide

This document covers the day-to-day operation of the 6-role orchestration
system. For what was built and why, see STATE.md in this directory.

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
       └─── [critic ad-hoc]   ← challenger (orchestrator picks budget)
```

---

## Setup

### Starting a session from scratch

**Master** is whatever Claude Code window you open for the coordinator.
No special spawn command needed — just set the budget:

```
/rename master          (or khimaira-0, or whatever you prefer)
/model sonnet
/effort medium
```

**Intake** — spawn via skill:

```
/khimaira-spawn-intake intake-1 "front-end for today's session"
```

This fires a push notification + notice to your other windows asking you
to open a new Claude Code window. In that window:

```
/rename intake-1
/model sonnet
/effort medium
```

Intake will receive an intro notice and stand by for your first message.

**Architect** — spawn via skill when you need synthesis:

```
/khimaira-spawn-architect architect-1 "design sidecar"
```

In the new window: `/rename architect-1` + `/model opus` + `/effort max`.
Architect stays idle until consulted.

**Agents** — open a new window per agent:

```
/rename agent-1
/model sonnet
/effort medium
```

No spawn command; agents are just Claude Code windows at sonnet/medium.
Master will assign tasks to them by session name.

**Observers** — same pattern as agents, but haiku:

```
/rename observer-1
/model haiku
/effort default
```

---

## Day-to-Day Flows

### Flow 1 — Joseph asks intake a question

1. **You type to intake-1** (in the intake window):
   > "Can we add rate limiting to the auth endpoints?"

2. **Intake parses intent.** If clear → formats the handoff spec and sends
   it to master via private DM:
   ```
   🎯 INTAKE HANDOFF [intake-id: 3f9a1b2c]
   User: Joseph
   Intent (one-line): Add rate limiting to auth endpoints
   Scope: auth/ directory; exclude public read endpoints
   Success criterion: 429 responses after N requests/min, configurable
   Constraints: don't break existing tests
   Raw user message (for context): "Can we add rate limiting to the auth endpoints?"
   ```

3. **Master acks** via private reply:
   ```
   🛬 INTAKE RECEIVED [intake-id: 3f9a1b2c] — decomposing now
   ```

4. **Master decomposes + delegates** to agents. Work proceeds.

5. **Master signals completion**:
   ```
   🏁 INTAKE COMPLETE [intake-id: 3f9a1b2c]
   Rate limiting added via token-bucket middleware. 3 files changed.
   All tests pass.
   ```

6. **Intake formats for you** in natural language and delivers the result.

If intake needs clarification, it asks **one question** before routing.

---

### Flow 2 — Master delegates work to agents

From the master window:

```
/khimaira-assign agent-1,agent-2 "implement rate limiting middleware" \
    --model sonnet --effort medium
```

The daemon coordinator (`POST /api/chats/{chat_id}/assign-batch`) handles
everything server-side — task creation, SSE fan-out, ack collection, begin
signal — in **one round-trip**. Master gets back:

```
{
  "task_ids": {"agent-1-id": "task-abc", "agent-2-id": "task-def"},
  "acks": {"agent-1-id": {model: "sonnet", effort: "medium"}, ...},
  "begin_fired": true,
  "elapsed_ms": 4312
}
```

If an agent times out (never acked), `missing_acks` is non-empty. Master
can re-run `/khimaira-assign` for the unresponsive agent or fire begin
manually for the acked subset.

---

### Flow 3 — Agent receives an assignment

When the `🔔 TASK ASSIGNMENT` block arrives in an agent window, the agent
outputs a prompt to **you**:

```
📋 Task waiting on you (task-id: task-abc):
   implement rate limiting middleware

To proceed:
  1. Type `/model sonnet` in this window
  2. Type `/effort medium` in this window
  3. Type `ready [task-id: task-abc]`

I'll verify settings.json at that moment and ack master. Holding until you confirm.
```

**What you do:** follow the numbered steps in the agent window. The
`/agent-ready` skill automates step 3 if you prefer:

```
/agent-ready
```

This reads your pending assignment, reads `~/.claude/settings.json`,
verifies compliance, and sends the ack automatically.

After all agents ack, master fires `🟢 ALL AGENTS CONFIRMED — BEGIN`.
Agents start work and report `done` via `chat_task_update`.

---

### Flow 4 — Master consults architect

When facing an architectural question:

```
/khimaira-consult architect-1 "Should we use Redis or Postgres for the
rate-limit token bucket? We have 500k req/min peak, existing PG infra,
no Redis in the stack today."
```

Architect reads the question, thinks at opus/max, and replies with one
structured synthesis: context → options → recommendation → risks.
Master integrates the recommendation into the agent assignments.

Architect stays idle and costs nothing between consults.

---

### Flow 5 — Critic review

Ad-hoc, no spawn command needed. Open a window:

```
/rename critic-1
/model sonnet       (or opus for deep review)
/effort medium
```

Master assigns via `/khimaira-assign critic-1 "review rate-limit PR" --model sonnet`.
Critic reads the artifact completely, then delivers **one structured report**:
must-fix items first, worth-noting items second. Master integrates.

---

### Flow 6 — Stale-ack recovery after restart

If a session was assigned a task but settings.json changed since the ack
(e.g. you restarted and `/model` reverted), the `⚠️ STALE TASK ACK(S)`
banner appears at the top of the agent's next turn:

```
⚠️ STALE TASK ACK(S) — 1 assignment(s) with budget drift post-restart:
  [task-abc] implement rate limiting middleware
  Acked: model=sonnet effort=medium  |  Now: model=opus effort=max
  From: master (chat-62166102f561)
  Run /agent-ready when budget is corrected.
```

Fix: set the correct budget (`/model sonnet` + `/effort medium`) in that
window, then `/agent-ready`. The ack re-fires and master can proceed.

---

## Reading the System Context Blocks

Each turn surfaces context blocks at the top of the model's prompt. Here
is what each one means:

| Block | When it appears | What to do |
|---|---|---|
| `🆔 khimaira session_id: ...` | Always (on boot) | Copy the ID when you need to pass it to tools manually |
| `💬 To enable real-time chat delivery...` | On boot | Run `chat_my_chats(session_id=...)` once to activate SSE |
| `🎚️ khimaira chat roles + recommended budgets` | Every turn | Set `/model` + `/effort` to match if you haven't |
| `⏳ KHIMAIRA PENDING ASSIGNMENT(S)` | When you have an unacked task | Read the task, set budget, run `/agent-ready` |
| `⚠️ STALE TASK ACK(S)` | When your budget drifted since acking | Correct budget + re-ack via `/agent-ready` |
| `🔇 channel-only event — respond minimally` | When turn was triggered by a chat block (in_progress etc.) | Acknowledge in one sentence; don't synthesize |
| `📋 channel event — master review required` | When an agent's task changed to `done` or `changes_requested` | **Master must review.** Approve or send back with feedback. |

The `📋 review required` block fires for all accepted chat members when a
task reaches `done` or `changes_requested`. The hook fires on task status,
not on recipient role — every member sees it and is expected to engage if
they're the reviewer.

---

## Common Scenarios

| Situation | What to do |
|---|---|
| "I have a fuzzy question" | Type it to intake-1; intake will clarify + route |
| "Implement feature X" | Tell intake → intake hands to master → master decomposes + assigns |
| "Should we use approach A or B?" | Tell master → master runs `/khimaira-consult architect-1 "<question>"` |
| "Review this PR" | Master assigns critic-1 → `/khimaira-assign critic-1 "review ..."` |
| "Master is overloaded / idle" | Option 1: `/khimaira-consult architect-1 "should I drop tier or deputize?"` Option 2: `/khimaira-deputize vice-name` to transfer master role |
| "I want to run all the work myself" | Skip intake, skip architect; direct master window — that's fine for simple sessions |

---

## Troubleshooting

**⏳ banner shows a task that IS already done.**
The `_discover_pending_assignments` scanner looks for a `✅ ready [task-id: ...]`
ack in the chat history. If the task was acked but the ack message used a
different format or the task was never formally acked (you started without
going through the gate), the banner fires incorrectly. Fix: find the chat,
verify the task's actual status, then send a correctly-formatted ack message
to clear the scanner's state. File a bug if the format is right but it still
fires.

**Agent window never received the `🔔 TASK ASSIGNMENT` block.**
Check:
1. Is the agent an accepted member of the chat (`chat_my_chats(session_id=...)`)?
2. Did the assign call specify the correct session ID? Names resolve via
   `session_list()` — if the agent hasn't set a name or used a different name, resolution fails silently.
3. Was the assignment sent with `scope_cwd` set to a path that doesn't match
   the agent's cwd? Check `session_state(agent)` for their cwd.

**Consult question was sent but architect never answered.**
Architect's inbox surfaces on their NEXT turn. If the architect window hasn't
received a user prompt since the consult was sent, the notice is sitting in
their inbox unsurfaced. Type anything in the architect window (even a space)
to trigger a turn and surface the inbox.

**Private DMs not showing in `chat_history`.**
Private messages are filtered by recipient. If you're calling `chat_history`
with the wrong `session_id`, private records from other sessions won't appear.
The chat creator (master) always sees all messages for audit — verify you're
querying as the correct session.

---

## Post-Restart Checklist

After restarting all Claude Code windows:

1. **Verify roles are set.** Check `🎚️ chat roles` block on each window's
   first turn. Set `/model` + `/effort` to match the recommended budget.

2. **Check for pending assignments.** The `⏳ PENDING ASSIGNMENT(S)` banner
   fires automatically. If it shows, run `/agent-ready` after setting budget.

3. **Check for stale acks.** The `⚠️ STALE TASK ACK(S)` banner fires if
   budget drifted. Correct budget + re-ack.

4. **Wake architect / intake if needed.** Send any message in those windows
   to surface their inbox (pending notices from pre-restart).

5. **Verify test suite.** `pytest packages/khimaira/tests/ -q` should show
   507/0 passed/failed. Any failure after restart is a regression — don't
   proceed until clean.

6. **Send a test assignment.** Run a minimal `/khimaira-assign agent-1 "hello world"
   --model sonnet --effort medium` end-to-end to confirm the coordinator,
   SSE delivery, ack flow, and begin signal all work.

---

## Skill Quick Reference

| Skill | Who uses it | What it does |
|---|---|---|
| `/khimaira-assign <agent(s)> <task>` | master | Assign task(s) with enforcement gate; daemon handles fan-out |
| `/agent-ready` | agent | Read pending assignment, verify settings.json, ack master |
| `/khimaira-consult <name> "<question>"` | master | Fire synthesis question to architect sidecar |
| `/khimaira-spawn-architect [name]` | master | Request Joseph to open architect window (opus/max) |
| `/khimaira-spawn-intake [name]` | master | Request Joseph to open intake window (sonnet/medium) |
| `/khimaira-deputize <vice>` | master | Pause-and-handoff master role to a fresh window |
| `/khimaira-resume` | vice | Reclaim master role across deputized chats |
