# Research Spike: External Daemon Push into Claude Code Agent Context

**Question:** Can an external process (daemon) push context into a running Claude Code agent's next turn WITHOUT the user typing first?

**Status:** VERDICT: **No, not currently. Daemon-push is not supported. Session is fundamentally user-input-gated.**

---

## Verdict

Claude Code's agentic loop is user-input-driven. The harness processes messages, tool calls, and context sequentially within the conversation, but **there is no documented or undocumented mechanism for an external process to inject context or trigger context injection** between user turns.

**Why this matters for khimaira-chat:**
- `/loop` (polling on a fixed or dynamic interval) is the only built-in mechanism, but it:
  - Requires a recurring task _within_ an existing session
  - Cannot be triggered externally by the daemon
  - Cannot interrupt/preempt the user (fires between turns only)
  - Has 7-day expiry; does not survive session restart
  
- Hooks (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop, etc.) are all triggered by **internal agent events**, not external signals
  - No hook fires on file changes, socket events, or IPC from another process
  - The `FileChanged` hook exists but is still internal (watches project files you configure)

**Classification: Partial Workaround Exists**

A session-scoped `/loop` can *simulate* push by polling the daemon for new messages frequently enough. This is not true push (latency is bounded by loop interval), but it's the practical v1 for khimaira-chat.

---

## Evidence

### 1. Hooks Are Internally Triggered

**Source:** [Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks.md)

All documented hooks fire on internal events only:
- `SessionStart` / `SessionEnd` — session lifecycle
- `UserPromptSubmit` / `UserPromptExpansion` — user input received
- `PreToolUse` / `PostToolUse` / `PostToolUseFailure` — tool call lifecycle
- `Stop` / `StopFailure` — turn completion
- `FileChanged` — file watcher (still user-config-driven, not external-process-driven)
- `Notification` — when Claude sends notifications (internal)
- `SubagentStart` / `SubagentStop` — subagent lifecycle

**Quote:** "Hooks fire at three cadences: once per session (SessionStart/SessionEnd), once per turn (UserPromptSubmit, Stop, StopFailure), or on every tool call (PreToolUse, PostToolUse)."

**Implication:** No hook can be triggered by an external daemon. IPC, file drops, or socket signals cannot fire hooks.

### 2. `/loop` Is Session-Scoped, Polling-Based

**Source:** [Run prompts on a schedule](https://code.claude.com/docs/en/scheduled-tasks.md)

`/loop` is the closest built-in to recurring automation:
- Runs a prompt on a fixed interval (cron-driven) or dynamic interval (Claude decides)
- **Sessions are session-scoped:** "Tasks are session-scoped: they live in the current conversation and stop when you start a new one."
- Scheduler fires "between turns" — if Claude is mid-response, the task waits
- 7-day expiry: "Recurring tasks automatically expire 7 days after creation"
- **Polling, not push:** Agent must finish current turn, then loop fires, then agent processes

**Quote:** "The scheduler checks every second for due tasks and enqueues them at low priority. A scheduled prompt fires between your turns, not while Claude is mid-response."

**Implication:** `/loop` is internal to the session and session-bound. An external daemon cannot trigger it. Cannot interrupt a user's turn. Cannot persist across session restart (unless within 7 days and on `--resume`).

### 3. No External Context Injection Mechanism Documented

**Source:** [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works.md), [Extend Claude Code](https://code.claude.com/docs/en/features-overview)

Claude loads context from:
- Conversation history (in-memory, session-specific)
- CLAUDE.md (read at session start)
- Auto memory (first 200 lines of MEMORY.md)
- MCP tools (loaded on demand)
- Skills (loaded on demand)

**No mechanism for external process to write into context mid-session.** File watching is one-directional (harness watches project files; external process cannot trigger watches). MCP is pull-only (agent calls tools; tools cannot push notifications to the agent).

### 4. MCP Is Pull-Only

**Source:** [Claude API docs (MCP section)](https://platform.claude.com/llms.txt) and [Claude Code MCP docs](https://code.claude.com/docs/en/mcp.md)

MCP (Model Context Protocol) allows Claude to call tools on a server. The server is passive; it responds to tool calls. **No server-initiated notifications exist.**

The SDK does support resource subscriptions and streaming in the `resources/messages/subscribe` paradigm, but Claude Code does not expose this as a push mechanism to agents.

**Implication:** khimaira-monitor can expose MCP tools that session agents call, but it cannot push an event that wakes the agent between turns.

### 5. Chyros Daemon (Unreleased)

**Source:** [MindStudio blog — What Is Claude Code Chyros?](https://www.mindstudio.ai/blog/what-is-claude-code-chyros-background-daemon)

A background daemon called "Chyros" appeared in Claude Code's source code (via leak). It was designed to:
- Run continuously in the background
- Handle codebase indexing and semantic indexing
- Perform memory management tasks

**Status: Not shipped.** As of May 2026, Chyros remains internal/unreleased. Even if it were enabled, it is designed for maintenance tasks, not for pushing messages into agent context.

---

## Recommended Chat Architecture v1

Given the verdict (no daemon-push), here's the practical path forward:

### **Use `/loop` with frequent polling**

1. **Session A** (initiator): Sends a message to the daemon. Daemon stores in Redis/in-memory queue, keyed by target session ID.
2. **Session B** (receiver): Starts a `/loop` that polls the daemon every 10–30 seconds for new messages on its session ID.
3. Daemon returns a newline-delimited batch of messages, each with sender metadata.
4. Session B's loop renders messages in conversation, formats as "Session A says: <msg>", and continues.

**Tradeoffs:**
- ✅ Works today, no new features needed
- ✅ Low friction (user types `/loop` once per session)
- ✅ Polling interval is user-tunable
- ❌ Not true push (latency = polling interval)
- ❌ Bounded by 7-day expiry (user must restart loop)
- ❌ Loop stops if session exits

### **v1 Demo Scope**
- Khimaira-monitor exposes `POST /api/sessions/{session_id}/message` and `GET /api/sessions/{session_id}/messages-since?timestamp=X`
- Session B runs `/loop 10s /poll-khimaira-chat` or similar custom skill
- Skill fetches messages, formats, prints, repeats

### **v2 (Future, if Feature Request Approved)**
Once Anthropic ships daemon-push (see "Upstream Ask" below), switch to:
- Hook that fires on external queue event
- Non-polling, true latency < 1 second
- Session survives hook across turns without user re-input

---

## Anthropic Upstream Ask

**Title:** Support external process → agent context injection (daemon push)

**Description:**

Claude Code agents are fundamentally user-input-gated. There is no mechanism for an external process (running outside Claude Code) to inject context or trigger context injection into an agent's next turn without user input.

Use case: Real-time cross-agent chat. Session A and Session B are parallel Claude Code agents. When A sends a message via daemon, B should see it "immediately" (< 1 second latency) without polling.

**Requested feature:**

Option A (Preferred): A new hook event `ExternalEvent` (or similar) that fires when an external process signals the session via a named socket, file drop, or HTTP endpoint. The harness passes context (event name, payload) to the hook; the hook can inject it as a message or skip.

Option B (Alternative): Extend MCP to support server-initiated messages / notifications that reach the agent mid-session (resource subscription push or similar).

Option C (Lighter): Generalize `/loop`'s scheduling mechanism so an external process can wake a sleeping scheduled task (without waiting for cron time).

**Why this matters:** Enables use cases like cross-session chat, real-time collaboration, event-driven automation, and daemon-orchestrated multi-agent workflows—all without polling or user intervention between turns.

---

## Sources

- [Claude Code Hooks Reference](https://code.claude.com/docs/en/hooks.md)
- [Run prompts on a schedule](https://code.claude.com/docs/en/scheduled-tasks.md)
- [How Claude Code works](https://code.claude.com/docs/en/how-claude-code-works.md)
- [What Is Claude Code Chyros? The Always-On Background Daemon Revealed](https://www.mindstudio.ai/blog/what-is-claude-code-chyros-background-daemon)
- [Claude Code Changelog](https://code.claude.com/docs/en/changelog.md) (May 2026)
- [Claude Code Skills](https://code.claude.com/docs/en/skills.md)

---

## Deeper spike: MCP server-initiated notifications

**Verdict: Y — daemon-push IS achievable** via Claude Code's `claude/channel` MCP capability (v2.1.80+, research preview). The earlier section's "N" is wrong; the first spike checked hooks/MCP-tools/`/loop` but missed the `channels` feature, which is exactly the missing primitive.

### Mechanism

An MCP server declares the channel capability and emits notifications:

```js
// MCP server side
{ capabilities: { experimental: { 'claude/channel': {} } } }

await mcp.notification({
  method: 'notifications/claude/channel',
  params: {
    content: 'message body',
    meta: { chat_id: '...', source: 'khimaira-chat', sender: '...' },
  },
});
```

The agent receives between turns:

```xml
<channel source="khimaira-chat" chat_id="..." sender="...">
  message body
</channel>
```

Timing: <1s if agent is idle. Queued if busy; delivered together on next turn.

### Evidence

- [Claude Code channels reference](https://code.claude.com/docs/en/channels.md) — official, "An MCP server can also act as a channel that pushes messages into your session, so Claude reacts to Telegram messages, Discord chats, or webhook events while you're away."
- [Channels reference walkthrough](https://code.claude.com/docs/en/channels-reference.md) — full two-way channel build (Telegram, Discord, iMessage, fakechat examples) with permission relay
- This machine: Claude Code 2.1.141 (verified `claude --version`); channels feature available

### Caveats

- **Research preview** — protocol may change. Not committed-stable yet.
- **Enterprise-blocked by default** — Joseph's personal install isn't enterprise, fine for our case.
- **Opt-in per MCP server** — the daemon's MCP server must declare the capability + emit notifications correctly.
- **Permission UI** — channel-initiated tool calls relay permission prompts back to the channel. For chat that's mostly a non-issue (chat doesn't itself trigger tool calls), but worth knowing.

### Architecture for khimaira-chat v1

This unblocks the design Joseph picked. Three paths:

| Path | Mechanism | Latency | Risk |
|---|---|---|---|
| **A — Channels only** | MCP `claude/channel` push | <1s | Preview-status; protocol may change |
| **B — Polling only** | `/loop 10s /khimaira-chat-poll` | 10-30s | None; works on any Claude Code version |
| **C — Hybrid** | Channels push + polling fallback | <1s when channels work, 10-30s otherwise | Higher complexity, but graceful degradation |

**Recommended for v1: A (channels-only).** Joseph picked the daemon-push path knowing the preview risk. Channels match the requirement (real "phone-feel"), the migration story if Anthropic shifts the protocol post-preview is small (the chat MCP server stays the source of truth; only the notification format would change), and the daemon (khimaira-monitor) is already an MCP server we can extend. Polling-as-fallback adds genuine complexity (cursor state, dedup against push, two delivery paths to debug) for a marginal robustness win — defer to v1.1 if real users hit channel breakage.

The chat lives as a new MCP capability on khimaira-monitor (or a sibling MCP server `khimaira-chat`) registered in each peer's `claude mcp` config. The daemon brokers state (chats JSONL, member lists, accept/invite handshake), and the chat MCP server pushes per-recipient notifications when new messages arrive. From the agent's perspective: chat messages just appear as `<channel>` blocks in context. No `/loop`, no polling, no missed messages while idle.

### Anthropic upstream ask

No new upstream feature needed — channels already exists. Only ask is "promote channels out of research-preview" once the protocol stabilizes. Worth filing as a +1 on whatever issue tracks that, but not a blocker.

