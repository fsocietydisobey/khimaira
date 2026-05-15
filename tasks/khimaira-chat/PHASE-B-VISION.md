# khimaira-chat Phase B vision — master/agent orchestration

**Status**: discussion captured 2026-05-14 by `khimaira-21`. Joseph wants to review after Phase A.1 smoke succeeds. Not committed-to-implement yet.

## Joseph's vision

> "I create a session called master (names could be anything). In the session I tell it to create a session with sessions (called agent-a, agent-b, agent-c, agent-d). And those sessions are already up and running. Then I give master a task and I tell it to delegate assignments to those agents. And when they're done they need to report back their work/changes and then master either rejects, approves or requests changes."

## What works today (Phase A.1)

The conversational shape is fully supported:

1. User launches 5 windows via `claude-chat` (one master, four agents). Each gets a friendly name via `/rename`.
2. From master: `/khimaira-chat agent-a agent-b agent-c agent-d --title "build feature X"`.
3. Each agent receives a `<channel kind="invite" ...>` block. They `/khimaira-chat-accept` (no chat_id needed — Phase A.1 default-to-latest-pending).
4. Master sends free-form: *"@agent-a: implement auth route. @agent-b: write tests. @agent-c: docs. @agent-d: review."*
5. Agents see the message in their next prompt context, do the work, `chat_send` back: *"agent-a: done, diff in abc123"*.
6. Master sees each report as a `<channel sender="agent-a" ...>` block. Sends back: *"agent-a: approved. agent-b: missing edge case Y, please add."*

The chat infrastructure handles all delivery. The "intelligence" of master/agent coordination lives in agent prompting (telling each Claude Code instance "you are agent-a, your job is to take tasks from master and report results"). No new chat features required for the basic flow.

## Where Phase A falls short for this vision

| Gap | What the user has to do today | Phase B fix |
|---|---|---|
| Master @-tags agents in free-form text | All members see every message | **Per-recipient addressing** — `chat_send(to=["agent-a"], body=...)` so others don't see |
| "Approved" / "rejected" is just text | No structural audit trail | **Task messages** — `kind=task` with `status` field (pending/in-progress/done/approved/changes-requested), tracked separately from chat lines |
| Master tracks who's done what manually | Reads chat history line by line | **Status view** — `chat_task_status(chat_id)` returns task list with each agent's state |
| No "this is a master/agent room" semantic | Free-for-all chat | **Roles per chat** — creator is implicit master; messages can be tagged as "task assignment" / "task reply" / "review verdict" |

## Existing alternative: `/delegate`

Donor's earlier work (`/listen`, `/delegate`, `/accept`, `/reject`) already implements a master/agent pattern — but **single-shot**. `/delegate <targets> <task>` sends a question, agents respond, master accepts/rejects. No multi-turn revision.

Joseph's vision is multi-turn (request → work → report → review → revise → re-report). That's genuinely chat-shaped. `/delegate` is the wrong primitive for it; chat is the right primitive but needs Phase B to be ergonomic.

## Three paths forward (re-surface after smoke)

- **(a) Try the flow as-is.** Practice with today's chat. Identify which Phase B gaps actually hurt vs. which were imagined. Then build Phase B targeting real friction.
- **(b) Build Phase B now.** Task messages + per-recipient addressing + status tracking + roles. ~3-4h. Lands a real master/agent product on top of chat.
- **(c) Polish the manual flow.** Add `/khimaira-orchestrate <agents...> <task>` slash command that wraps the multi-step setup (create chat + invite + send first task) into one command. ~30min. Doesn't add structural features but makes the existing flow much easier to invoke.

## Recommendation

**(a) for one round, then (b) informed by what hurt.** (c) is cheap polish either way. Final call sits with Joseph.

## Open design questions (deferred)

If Phase B happens:

- **Per-recipient addressing semantics**: when `to=["agent-a"]` is used, do other members see the message in `chat_history`, or is it truly private? (Privacy = stronger semantic but adds an access-control layer to history reads.)
- **Task lifecycle states**: pending → in-progress (set by agent on first reply) → done (agent claims completion) → approved | changes-requested | rejected (master verdict). Reasonable? Missing states?
- **Status reporting cadence**: do agents auto-emit "in-progress" when they read a task message, or does master poll `chat_task_status`?
- **Multi-master vs single-master**: chat creator implicit master. Can master delegate master-rights? (Probably not for v1; only creator approves.)
- **Cross-chat task references**: can a task in one chat reference work happening in another? (Probably not for v1.)
- **Future-session invites**: today, `chat_create_room(members=["test-agent"])` requires `test-agent` to already exist as a known session. Master/agent flow would benefit from pre-registering an invite for a name not yet alive — when a session later registers under that name, the queued invite auto-fires. Joseph hit this on first try (invited "test-agent" before any session named test-agent existed).
