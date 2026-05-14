# khimaira-chat — real-time cross-session chat via Claude Code channels

**Status**: spec landed 2026-05-14 in session `khimaira-21`. Phase A implementation pending. Predecessor research at `RESEARCH-daemon-push.md`.

**Origin**: Joseph wants "phone-feel realtime" chat between Claude Code sessions — multiple peers in different terminals, messages land in seconds, full transcript per chat, groups + handshake. Donor's first proposal was scheduler-as-transport (async, leave-message style); the "phone-feel" requirement and a deeper spike found Claude Code's `claude/channel` MCP capability (v2.1.80+, research preview) which delivers true daemon-push at <1s latency. Chat ships on channels.

---

## Architecture

```
┌──────────────────┐  stdio   ┌─────────────────────┐  HTTP/SSE  ┌───────────────────┐
│ Claude Code A    │◄────────►│ khimaira-chat MCP   │◄─────────► │ khimaira-monitor  │
│ (session 1b41…)  │          │ subprocess (per-    │            │ daemon            │
│                  │          │  session, stdio)    │            │ /api/chats/*      │
└──────────────────┘          └─────────────────────┘            │ chats JSONL state │
                                                                  │ event fan-out     │
┌──────────────────┐  stdio   ┌─────────────────────┐  HTTP/SSE  │                   │
│ Claude Code B    │◄────────►│ khimaira-chat MCP   │◄─────────► │                   │
│ (session 5644…)  │          │ subprocess          │            └───────────────────┘
└──────────────────┘          └─────────────────────┘
```

- **`packages/khimaira-chat/`** — workspace package (peer to seance/scarlet/sibyl). Ships a stdio MCP subprocess that Claude Code spawns per session.
- **Subprocess role** — declares `claude/channel` capability. On first agent tool call, learns its session_id (lazy registration; SessionStart hook calls `chat_my_chats()` to force this on boot). Subscribes to daemon's `/api/chats/events?session_id=...` SSE stream. Tracks last-seen event id per chat in memory; on reconnect (e.g. after daemon restart) passes `Last-Event-ID` header so daemon backfills from JSONL. New events → emits `notifications/claude/channel` over stdio → agent sees `<channel>` block.
- **Daemon role (khimaira-monitor extension)** — owns chat state (rooms, members, transcripts), exposes `/api/chats/*` REST + an SSE event stream. Validates sender membership before fan-out (prompt injection gate per channels best practice).
- **Per-peer install** — `khimaira sync` registers the chat MCP in each peer's `claude mcp` config. One-time setup per machine.

## Decisions (locked from prior conversation)

| # | Decision | Rationale |
|---|---|---|
| 1. **chat_id model** | Stable per-pair-or-group: `chat_id = "chat-" + sha256(sorted(member_ids) + (fresh_suffix or "")).hexdigest()[:12]`. `--new` generates a `fresh_suffix` (UTC ISO timestamp at create) that's hashed in, so the same members can start a fresh transcript with a distinct id. | Resume conversation across days by default; opt out for fresh starts. The suffix MUST be in the hash or `--new` collides with resume. |
| 2. **Cardinality** | Groups from v1 (N-session rooms). Members can be added/removed after creation. Each member goes through handshake. | Joseph picked it; no extra cost vs pairs given the JSONL schema. |
| 3. **Membership model** | True handshake — invitee must run `chat_accept(chat_id)` (or via `/khimaira-chat-accept`) before they receive messages. Sends to a member in `pending` state queue server-side until they accept. | Prevents surprise-routed messages; matches "phone call" mental model where both parties must answer. |
| 4. **Lifecycle** | `chat_create_room`, `chat_invite`, `chat_accept`, `chat_send`, `chat_history`, `chat_leave`, `chat_delete`. Delete hard-archives the JSONL (move to `chats/archive/`); doesn't permanently shred. **Permission rule: only the creator (`created_by`) can `chat_delete`.** Non-creators wanting out call `chat_leave` (sets their member-state to `left`; doesn't nuke history for everyone else). | Matches typical chat affordances. Archive over delete to allow recovery. Restricting delete to creator prevents hostile-actor "join 5-member group, nuke chat" attack. |
| 5. **Transport** | `claude/channel` only (no polling fallback). | Matches the phone-feel requirement; the channels primitive is the right abstraction. Polling complexity (cursors, dedup against push) deferred to a v1.1 if real users hit channel breakage. |
| 6. **MCP server location** | Workspace package `packages/khimaira-chat/`, separate process per session, separate `claude mcp` config entry. | Chat is structurally separate from monitor (different lifecycle, different scaling needs); split now is cheaper than at 50x scale. Pattern matches seance/scarlet/sibyl. |
| 7. **Subprocess identity** | Lazy registration — agent passes its `session_id` on the first chat tool call. Subprocess stores it for its lifetime, opens SSE subscription. | Claude Code does NOT set `CLAUDE_SESSION_ID` env var for MCP subprocesses (only `CLAUDE_PROJECT_DIR`). Lazy-init is the cleanest workable pattern. |
| 8. **Sender gating** | Daemon enforces: only members of `chat_id` can `chat_send` to it; only `accepted` members receive notifications. Cross-session writes by non-members → 403. | Channels reference is emphatic: ungated channel = prompt injection. Must gate at the daemon. |

## Schema

`~/.local/state/khimaira/chats/<chat_id>.jsonl` — append-only. First line is room metadata; subsequent lines are member-state changes + messages, ordered by ts.

```json
// Line 1: room metadata (immutable except via explicit chat_meta_update)
{"kind":"meta","chat_id":"chat-abc123def456","created_at":"...","created_by":"khimaira-21","title":"khimaira-21 + khimaira-6","fresh_suffix":null}

// Member transitions (append on invite/accept/leave)
{"kind":"member","ts":"...","session_id":"5644…","session_name":"khimaira-6","state":"pending","invited_by":"khimaira-21"}
{"kind":"member","ts":"...","session_id":"5644…","state":"accepted"}
{"kind":"member","ts":"...","session_id":"5644…","state":"left"}

// Messages
{"kind":"msg","id":"msg-abc123","ts":"...","sender_id":"1b41…","sender_name":"khimaira-21","body":"hey"}
```

Member state machine: `pending` (invited, not yet accepted) → `accepted` (active) → `left` (removed self) | `removed` (kicked by another member). Only `accepted` members receive channel notifications.

## Phase A scope

Ship the load-bearing pieces:

1. **Workspace package** — `packages/khimaira-chat/` with `pyproject.toml`, FastMCP server in `src/khimaira_chat/server.py`
2. **Daemon endpoints** — `/api/chats` CRUD + `/api/chats/{chat_id}/events` SSE
3. **State module** — `khimaira/monitor/chats.py` (write side, replay, sender gating, member transitions)
4. **Subprocess MCP** — declares `claude/channel`, registers session lazily, subscribes to SSE, emits notifications, exposes `chat_*` tools
5. **Slash commands** — `/khimaira-chat <peers...>` create+invite combo, `/khimaira-chat-accept <id>`, `/khimaira-chat-send <id> <body>`, `/khimaira-chat-history <id>`, `/khimaira-chat-leave <id>`, `/khimaira-chat-delete <id>`, `/khimaira-chat-list` (my chats), `/khimaira-chat-poll <id>` (manual escape hatch — see "Channels failure mode" below)
6. **Bootstrap integration** — `khimaira sync` registers the chat MCP per peer using `install_mode.detect_upgrade_tool()` so uvx users get `uv tool` and pip users get `pip`. `claude mcp add --transport stdio khimaira-chat -- <detected-runner> khimaira-chat` (or workspace path during dev). SessionStart hook addition: fire a one-shot `chat_my_chats(session_id)` to force subprocess registration on session boot — without this, "joined a chat yesterday, restarted today, didn't get a message until I happened to type" is a real failure mode.
7. **Tests** — daemon-side: round-trip JSONL, member-state machine, gating (non-member send rejected, pending member doesn't receive). Subprocess-side: notification emit on SSE event. Smoke test: two real Claude Code sessions exchange messages live.

Phase B (deferred):
- Read receipts (per-message per-recipient ack)
- Typing indicators (low-frequency presence channel notifications)
- File attachments (file path in meta; channel content references it)
- Message editing / deletion (mutable transcript via tombstones)
- Permission relay (`claude/channel/permission` capability — per-message remote tool-call approvals)
- Chat search / paging (chats over a few thousand messages)

Phase C (deferred):
- Group admin roles (who can add/remove members)
- Cross-machine chat (when daemon-sync lands)
- Chat plugins (package as a Claude Code marketplace plugin to drop the `--dangerously-load-development-channels` flag)

## File map

```
packages/khimaira-chat/  (new workspace package)
  pyproject.toml                                         — depends on mcp[python] + httpx for daemon SSE
  src/khimaira_chat/__init__.py
  src/khimaira_chat/server.py                            — FastMCP entry; declares claude/channel; spawns SSE subscriber on first registration
  src/khimaira_chat/daemon_client.py                     — thin httpx wrapper over /api/chats/* + SSE subscriber
  src/khimaira_chat/tools.py                             — MCP tool implementations (chat_create_room, chat_invite, ...)
  tests/test_subprocess.py                               — local subprocess tests (mock daemon)
  tests/test_daemon_client.py                            — httpx mock tests

packages/khimaira/src/khimaira/monitor/chats.py          (new)
  Storage:
    + create_room(creator_session_id, member_session_ids, title, fresh: bool) → record
    + invite(chat_id, by_session, invitee_session) → record (raises if not member)
    + accept(chat_id, session_id) → record (raises if not pending)
    + leave(chat_id, session_id) → record
    + delete(chat_id, by_session) → archives to chats/archive/
    + send_message(chat_id, sender_id, body) → record (raises if not accepted member)
    + history(chat_id, requester_id, limit) → list[record] (raises if not member)
    + my_chats(session_id) → list[chat_meta]
  Replay:
    + load_room(chat_id) → dict (members, messages, etc.)
  Event fan-out:
    + subscribe(session_id) → AsyncIterator[event]  (yields events for chats this session is accepted in)

packages/khimaira/src/khimaira/monitor/api/chats.py      (new)
  POST   /api/chats                                       — create_room
  POST   /api/chats/{chat_id}/invite                      — invite member
  POST   /api/chats/{chat_id}/accept                      — accept invite (caller is invitee)
  POST   /api/chats/{chat_id}/messages                    — send a message
  GET    /api/chats/{chat_id}/messages?since=…&limit=…    — paginated history
  GET    /api/chats/{chat_id}                             — room metadata + member list
  POST   /api/chats/{chat_id}/leave                       — leave
  DELETE /api/chats/{chat_id}                             — archive
  GET    /api/chats?session_id=…                          — my chats
  GET    /api/chats/events?session_id=…                   — SSE stream of events for this session's chats
                                                          — honors Last-Event-ID header for reconnect backfill (replays
                                                            events from JSONL since the given event_id)

packages/khimaira/src/khimaira/monitor/server.py         (extend)
  + register chats router

packages/khimaira/tests/test_chats.py                    (new)
  - Round-trip JSONL (create + accept + send + history matches)
  - Sender gating (non-member send → ValueError; pending member doesn't get history)
  - Member state machine (invite → accept → leave; can't accept twice)
  - Group cardinality (3+ members)
  - Fresh-vs-resume chat_id
  - Delete archives (file moved to chats/archive/)

packages/khimaira/tests/test_chats_api.py                (new)
  - 4-endpoint pattern: happy + unhappy per route per CLAUDE.md rule
  - SSE stream: subscribe, send via another client, assert event arrives

~/dotfiles/claude/commands/khimaira-chat.md              (new — symlinked)
~/dotfiles/claude/commands/khimaira-chat-accept.md       (new)
~/dotfiles/claude/commands/khimaira-chat-send.md         (new)
~/dotfiles/claude/commands/khimaira-chat-history.md      (new)
~/dotfiles/claude/commands/khimaira-chat-leave.md        (new)
~/dotfiles/claude/commands/khimaira-chat-delete.md       (new)
~/dotfiles/claude/commands/khimaira-chat-list.md         (new)
~/dotfiles/claude/commands/khimaira-chat-poll.md         (new — manual escape hatch; pure pull via chat_history(since=last_seen))

packages/khimaira/src/khimaira/bootstrap/runner.py       (extend)
  + register khimaira-chat MCP entry on sync
```

## Subprocess registration flow

```
1. Claude Code starts session 1b41…
2. Per .mcp.json, spawns `khimaira-chat` stdio subprocess
3. Subprocess starts; declares claude/channel + chat_* tools; awaits requests
4. Agent (later, on first chat operation): chat_my_chats(session_id="1b41…")
5. Subprocess sees session_id for the first time:
   - stores it for its lifetime
   - opens HTTP/SSE: GET /api/chats/events?session_id=1b41…
   - returns the chat list to agent
6. Daemon's SSE pushes events for any chat 1b41… is accepted in
7. Subprocess receives event → emits notifications/claude/channel
8. Agent sees <channel source="khimaira-chat" chat_id="…" sender="…"> block
```

If the subprocess sees a tool call with a different `session_id` after registration, it raises (one subprocess = one session for its lifetime; agents can't impersonate).

## Anti-patterns

- **Don't gate on chat_id, gate on sender.** Membership in a chat doesn't mean every message in it is from a trusted source — only daemon-validated `accepted` members can write.
- **Don't share subprocess across sessions.** One subprocess per Claude Code session (matches the stdio model). Sharing breaks the per-client routing channels gives us for free.
- **Don't poll the daemon's history endpoint as a heartbeat.** The SSE stream IS the source of truth for new messages. Polling is for explicit "show me history" requests.
- **Don't push channel notifications for the sender's own message.** Daemon's fan-out skips the sender's session_id. Otherwise the sender's agent would see its own send echo back as a `<channel>` block.
- **Don't queue messages for `pending` members forever.** Until they accept, sends to them go to a per-chat pending buffer. Cap at last 50 messages or 7 days; older ones drop with a `[N earlier messages dropped before you joined]` marker on accept.
- **Don't depend on `CLAUDE_SESSION_ID`.** It's not set; lazy-register via tool call.
- **Don't put chat tools in khimaira-monitor's MCP.** The split is intentional — chat lifetime, scaling, and failure mode differ from monitor. Mixing them couples lifecycles.
- **Don't conflate chat with the other inbox primitives.** Three coexisting communication channels, three different use cases:
  - `chat_send` — real-time interactive conversation (push via channels, sub-2s latency, requires both ends running)
  - `session_post_notice` — async durable FYI to a specific session (lands in inbox, peer sees on next prompt; ack-or-expire after 3 surfaces)
  - `session_log_question` — async with a reply contract (peer must answer; surfaces as incoming-question block)
  Don't use chat for "leave a note for them to see when they wake" — that's `session_post_notice`. Don't use notice for "I need their input now" — that's `session_log_question` or chat.

## Done when

- Two Claude Code sessions can `khimaira sync` to install chat, run `/khimaira-chat <peer-name>` to invite, both accept, and exchange messages with <2s end-to-end latency
- A 3-member group works: A invites B and C, both accept, A sends → B and C receive within 2s; C sends → A and B receive
- A non-member's `chat_send` returns 403; a pending member's session does NOT receive notifications until accept
- `khimaira-monitor` daemon restart preserves chat state (replay-on-boot from JSONL)
- Subprocess restart re-registers cleanly (agent's first chat call after restart re-establishes SSE)
- `chat_history` returns the full transcript for accepted members; rejected for non-members
- Tests cover the gating matrix + state machine transitions + SSE delivery
- Smoke test: real two-window exchange documented in PR description with screenshots / transcript
- Perf budget: daemon handles N=20 concurrent SSE connections with p99 fan-out latency < 500ms (verifiable via a tight-loop test fixture; useful for catching regressions)
- Channels failure mode covered: `/khimaira-chat-poll <chat_id>` works as a manual pull when channels misbehave (research-preview protocol drift, MCP transport hiccup) — calls `chat_history(since=<last_seen>)`, prints new messages inline. Not auto-invoked; user runs explicitly.

## Open follow-ups (NOT v1 scope)

- Read receipts + typing indicators (Phase B)
- File attachments in messages (Phase B — needs storage strategy)
- Message editing / deletion via tombstones (Phase B)
- Permission relay (`claude/channel/permission`) — chat could double as remote tool-approval surface (Phase B; note: requires the channel to authenticate the sender, which our handshake handles)
- Chat search across history (Phase B — needs FTS index)
- Group admin roles + role-based add/remove (Phase C)
- Cross-machine chat once daemon-sync lands (Phase C)
- Package as Claude Code marketplace plugin to drop `--dangerously-load-development-channels` (Phase C — requires Anthropic security review)
- TUI / web UI for chat history browsing outside Claude Code (potential separate effort)

## References

- Channels research-preview docs: https://code.claude.com/docs/en/channels.md
- Channels reference walkthrough (incl. two-way + permission relay): https://code.claude.com/docs/en/channels-reference.md
- MCP Python SDK: https://github.com/modelcontextprotocol/python-sdk (FastMCP supports custom notification methods + experimental capabilities)
- Predecessor research memo: `tasks/khimaira-chat/RESEARCH-daemon-push.md`
- Channel architecture (per-session stdio subprocess): `RESEARCH-daemon-push.md` § "Deeper spike"
- Sender-gating best practice: channels-reference.md § "Gate inbound messages"
- Persistent scheduler spec (sibling pattern, decision-table format): `tasks/persistent-scheduler/IMPLEMENTATION.md`
- Workspace package convention (matches seance/scarlet/sibyl): `packages/seance/pyproject.toml`
