"""MCP stdio server — declares `claude/channel` capability + chat tools.

Per Claude Code's channels model, this process is spawned as a stdio
subprocess by Claude Code itself (one per session). The agent calls
the chat_* tools; the subprocess holds an HTTP/SSE connection to
khimaira-monitor and forwards inbound chat events into the agent's
context as `notifications/claude/channel` events.

Lazy session_id registration: Claude Code does NOT set CLAUDE_SESSION_ID
in the subprocess env. The agent passes its session_id on the first
chat tool call; we store it for the subprocess lifetime and start the
SSE subscriber. The SessionStart hook fires `chat_my_chats(session_id)`
on session boot to force this registration before any chat events
need to be delivered.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from khimaira_chat import daemon_client

log = logging.getLogger("khimaira_chat.server")

SERVER_NAME = "khimaira-chat"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "Cross-session real-time chat via Claude Code channels.\n"
    "\n"
    "WHEN TO USE WHICH PRIMITIVE (read this carefully — agents often pick wrong):\n"
    "- The user says 'reply to <session>', 'send <session> a message', 'chat with "
    "<session>', or 'tell <session>': call `chat_my_chats(session_id=<my_id>)` FIRST. "
    "If you have an active chat with that session, use `chat_send(chat_id=..., body=...)`. "
    "Only fall back to `mcp__khimaira__session_post_notice` if NO shared chat exists.\n"
    '- Incoming `<channel kind="invite" ...>` block: use `chat_accept` '
    "(no chat_id arg defaults to the latest pending invite).\n"
    '- Incoming `<channel sender="..." ...>` block (no kind=invite): that\'s a chat '
    "message. Reply with `chat_send` to the same chat_id from the channel meta.\n"
    "- The user wants to leave a note for someone who's not actively chatting: "
    "use `mcp__khimaira__session_post_notice` (durable, lands in their inbox).\n"
    "- The user wants a synchronous answer from one peer: use `mcp__khimaira__"
    "session_log_question` (formal Q→A contract, blocking).\n"
    "\n"
    "session_id is your own Claude Code session id (visible in the SessionStart hook "
    "context block titled '🆔 khimaira session_id'); pass it to all chat_* tools.\n"
    "\n"
    "DON'T leak `<thinking>`, `<scratchpad>`, etc. tags into chat message bodies — "
    "the daemon strips them defensively but it's noise. Send only the message body."
)


# ---------------------------------------------------------------------------
# Per-subprocess state — bound on first tool call
# ---------------------------------------------------------------------------


class _SubprocessState:
    """Holds the session_id (lazy-registered) + SSE subscriber task.

    **One subprocess = one session, for its lifetime.** This is load-bearing:
    Claude Code's channel-notification routing is per-stdio-pipe (the
    notification only reaches the agent that spawned this subprocess).
    Sharing a subprocess across sessions would break that routing — the
    daemon would push events for session B's chats to a subprocess that
    actually serves session A, and the agent never sees them.

    `register()` enforces this by raising if a tool call arrives bearing a
    different session_id after the first one is bound. The raise is
    visible in the agent's tool-call result, so a misconfigured caller
    fails loudly rather than silently rewriting our identity.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.last_event_id: str | None = None
        self.subscriber_task: asyncio.Task | None = None

    def register(self, session_id: str) -> None:
        if self.session_id is None:
            self.session_id = session_id
            log.info("khimaira-chat: registered session_id=%s", session_id)
            # Bridge Claude Code's `-n <name>` flag → khimaira friendly name.
            _maybe_register_display_name(session_id)
        elif self.session_id != session_id:
            raise ValueError(
                f"This subprocess is bound to session {self.session_id!r}; "
                f"refusing tool call from session {session_id!r}. "
                f"One subprocess = one session for its lifetime."
            )


_state = _SubprocessState()


def _detect_claude_display_name() -> str | None:
    """Read parent's argv to find a `-n <name>` / `--name <name>` flag.

    Claude Code's `-n NAME` sets a display name in its own session
    metadata, but doesn't propagate that name into khimaira's session
    registry — meaning `claude-chat -n test-agent` makes the session
    findable via `claude -r test-agent` BUT chat_create_room(members=
    ["test-agent"]) still 404s because the daemon doesn't know about
    the name. Bridging that gap manually requires `/rename` after
    launch, which is friction.

    By reading our parent process's argv (Claude Code is our parent
    when spawned as a stdio MCP subprocess), we can detect the user's
    intended name and propagate it to the daemon automatically.

    Linux-only via /proc; returns None on other platforms or if the
    flag isn't present.
    """
    try:
        ppid = os.getppid()
        with open(f"/proc/{ppid}/cmdline", "rb") as f:
            argv = f.read().decode("utf-8", errors="replace").split("\x00")
        for i, arg in enumerate(argv):
            if arg in ("-n", "--name") and i + 1 < len(argv):
                name = argv[i + 1].strip()
                if name:
                    return name
    except (OSError, IndexError, UnicodeDecodeError):
        pass
    return None


def _maybe_register_display_name(session_id: str) -> None:
    """If Claude Code launched with `-n <name>`, propagate that to the
    daemon as a friendly session name. Best-effort: silent on failure
    (the chat tools still work without the name; user just has to
    `/rename` manually if they want it)."""
    name = _detect_claude_display_name()
    if not name:
        return
    try:
        daemon_client.set_session_name(session_id, name)
        log.info("khimaira-chat: auto-registered name=%s for session %s", name, session_id)
    except Exception as exc:
        # Likely: name already taken, or daemon unreachable. Either way,
        # don't fail the chat tool call — fallback is /rename.
        log.warning("khimaira-chat: name auto-register failed for %r — %s", name, exc)


# ---------------------------------------------------------------------------
# Channel notification — bypass typed API to send the custom method
# ---------------------------------------------------------------------------


async def _emit_channel_notification(session: Any, content: str, meta: dict[str, str]) -> None:
    """Send a notifications/claude/channel event over the MCP transport.

    The Python MCP SDK's typed send_notification() rejects unknown methods,
    so we go around it via send_message() with a raw JSONRPCNotification.
    Channels is a research-preview Claude Code extension; this is the
    standard escape hatch documented in the SDK.
    """
    notif = types.JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta},
    )
    msg = SessionMessage(message=types.JSONRPCMessage(root=notif))
    await session.send_message(msg)


# ---------------------------------------------------------------------------
# SSE subscriber — runs in background, emits channel notifications
# ---------------------------------------------------------------------------


async def _sse_loop(session: Any) -> None:
    """Pump events from the daemon's SSE stream into channel notifications.

    Filters out the subprocess's own session_id as the sender — otherwise
    the agent's own send echoes back as a channel block.
    """
    assert _state.session_id is not None
    async for record in daemon_client.subscribe_events(
        _state.session_id, last_event_id=_state.last_event_id
    ):
        evt_id = record.get("event_id")
        if evt_id:
            _state.last_event_id = evt_id

        kind = record.get("kind")

        # Pending-invite record routed to invitee → render as a "you've
        # been invited" channel notification so the receiver doesn't
        # have to poll chat_my_chats.
        if (
            kind == "member"
            and record.get("state") == "pending"
            and record.get("session_id") == _state.session_id
        ):
            chat_id = record.get("chat_id", "")
            inviter = record.get("invited_by", "someone")
            content = (
                f"{inviter} invited you to chat {chat_id}. "
                f"Accept with `/khimaira-chat-accept` or decline with "
                f"`/khimaira-chat-reject` (no chat_id needed — defaults "
                f"to this invite)."
            )
            meta = {
                "chat_id": str(chat_id),
                "kind": "invite",
                "from": str(inviter),
            }
            try:
                await _emit_channel_notification(session, content, meta)
            except Exception as exc:
                log.warning("khimaira-chat: failed to emit invite notification — %s", exc)
            continue

        if kind != "msg":
            # Other member transitions / meta updates aren't surfaced as
            # chat messages — they're still observable via chat_history.
            continue

        sender_id = record.get("sender_id")
        if sender_id == _state.session_id:
            # don't echo own messages back
            continue

        content = record.get("body", "")
        meta = {
            "chat_id": str(record.get("chat_id", "")),
            "sender": str(record.get("sender_name") or sender_id or ""),
            "msg_id": str(record.get("id", "")),
        }
        try:
            await _emit_channel_notification(session, content, meta)
        except Exception as exc:
            log.warning("khimaira-chat: failed to emit channel notification — %s", exc)


def _ensure_subscriber(session: Any) -> None:
    """Start the SSE subscriber if it isn't running yet."""
    if _state.subscriber_task is not None and not _state.subscriber_task.done():
        return
    _state.subscriber_task = asyncio.create_task(_sse_loop(session))


# ---------------------------------------------------------------------------
# MCP Server + tool handlers
# ---------------------------------------------------------------------------


def _build_server() -> Server:
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="chat_create_room",
                description=(
                    "Create a new chat room with the given members. Creator is "
                    "auto-accepted; invitees go through handshake (chat_accept). "
                    "If the same members already have a chat, returns the existing "
                    "one — pass fresh=True for a new transcript."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Your session id (creator)",
                        },
                        "members": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Other session ids/names to invite",
                        },
                        "title": {"type": "string", "description": "Optional room title"},
                        "fresh": {"type": "boolean", "default": False},
                    },
                    "required": ["session_id", "members"],
                },
            ),
            types.Tool(
                name="chat_invite",
                description="Invite another session into an existing chat. Caller must be an accepted member.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "invitee": {"type": "string", "description": "Session id or name"},
                    },
                    "required": ["session_id", "chat_id", "invitee"],
                },
            ),
            types.Tool(
                name="chat_accept",
                description=(
                    "Accept an invite into a chat. If chat_id is omitted, "
                    "accepts the most recent pending invite (the common case — "
                    "you don't need to know the chat_id)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {
                            "type": "string",
                            "description": "Optional; defaults to latest pending",
                        },
                    },
                    "required": ["session_id"],
                },
            ),
            types.Tool(
                name="chat_reject",
                description=(
                    "Decline an invite. If chat_id is omitted, rejects the most "
                    "recent pending invite. Creator can re-invite later."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {
                            "type": "string",
                            "description": "Optional; defaults to latest pending",
                        },
                    },
                    "required": ["session_id"],
                },
            ),
            types.Tool(
                name="chat_send",
                description="Send a message to a chat. You must be an accepted member.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "body": {"type": "string"},
                    },
                    "required": ["session_id", "chat_id", "body"],
                },
            ),
            types.Tool(
                name="chat_history",
                description="Read recent messages from a chat. Caller must be an accepted member.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 50},
                        "since": {
                            "type": "string",
                            "description": "Optional event_id to start after (for /khimaira-chat-poll)",
                        },
                    },
                    "required": ["session_id", "chat_id"],
                },
            ),
            types.Tool(
                name="chat_my_chats",
                description=(
                    "List chats you're a member of (pending or accepted). "
                    "Also serves as the registration ping for this subprocess — "
                    "the SessionStart hook calls this on boot to force lazy "
                    "session_id registration before any messages need to be delivered."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"session_id": {"type": "string"}},
                    "required": ["session_id"],
                },
            ),
            types.Tool(
                name="chat_leave",
                description="Leave a chat. You stop receiving messages but the chat continues for others.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                    },
                    "required": ["session_id", "chat_id"],
                },
            ),
            types.Tool(
                name="chat_delete",
                description=(
                    "Archive a chat (move JSONL to chats/archive/). "
                    "Only the creator can call this — non-creators should use chat_leave."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                    },
                    "required": ["session_id", "chat_id"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, args: dict[str, Any]) -> list[types.ContentBlock]:
        session_id = args.get("session_id")
        if not session_id:
            return [types.TextContent(type="text", text="Error: session_id is required")]
        try:
            _state.register(session_id)
        except ValueError as exc:
            return [types.TextContent(type="text", text=f"Error: {exc}")]

        # Lazy-start the SSE subscriber. Needs the live MCP session,
        # which is on the request_context.
        ctx = server.request_context
        _ensure_subscriber(ctx.session)

        try:
            result = await _dispatch_tool(name, args)
        except daemon_client.DaemonError as exc:
            return [
                types.TextContent(
                    type="text",
                    text=f"Daemon error (HTTP {exc.status_code}): {exc.detail}",
                )
            ]
        except Exception as exc:
            log.exception("khimaira-chat: tool %s failed", name)
            return [types.TextContent(type="text", text=f"Tool error: {exc}")]

        return [
            types.TextContent(
                type="text", text=json.dumps(result, separators=(",", ":"), default=str)
            )
        ]

    return server


async def _dispatch_tool(name: str, args: dict[str, Any]) -> Any:
    sid = args["session_id"]
    if name == "chat_create_room":
        return daemon_client.create_room(
            sid,
            args["members"],
            title=args.get("title"),
            fresh=bool(args.get("fresh", False)),
        )
    if name == "chat_invite":
        return daemon_client.invite(args["chat_id"], sid, args["invitee"])
    if name == "chat_accept":
        chat_id = args.get("chat_id")
        if not chat_id:
            chat_id = daemon_client.latest_pending(sid)
            if not chat_id:
                return {"error": "no pending invites to accept"}
        return daemon_client.accept(chat_id, sid)
    if name == "chat_reject":
        chat_id = args.get("chat_id")
        if not chat_id:
            chat_id = daemon_client.latest_pending(sid)
            if not chat_id:
                return {"error": "no pending invites to reject"}
        return daemon_client.reject(chat_id, sid)
    if name == "chat_send":
        return daemon_client.send_message(args["chat_id"], sid, args["body"])
    if name == "chat_history":
        return daemon_client.history(
            args["chat_id"], sid, limit=args.get("limit", 50), since=args.get("since")
        )
    if name == "chat_my_chats":
        return daemon_client.my_chats(sid)
    if name == "chat_leave":
        return daemon_client.leave(args["chat_id"], sid)
    if name == "chat_delete":
        return daemon_client.delete_chat(args["chat_id"], sid)
    raise ValueError(f"Unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _serve() -> None:
    server = _build_server()
    init_opts = InitializationOptions(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        capabilities=types.ServerCapabilities(
            experimental={"claude/channel": {}},
            tools=types.ToolsCapability(listChanged=False),
        ),
        instructions=INSTRUCTIONS,
    )
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, init_opts)


def _ancestor_pids(max_depth: int = 5) -> list[int]:
    """Walk the parent chain via /proc/<pid>/status, return ancestor PIDs.

    Claude Code spawns chat MCP via `bash -lc 'uv run khimaira-chat ...'`,
    so the actual chain is Claude Code → bash → uv → khimaira-chat.
    The SessionStart hook (also spawned by Claude Code) posts its OWN
    ppid (= Claude Code's PID). This subprocess's getppid() returns uv's
    PID, not Claude Code's — so a single ppid lookup misses. Walking
    up to grandparents (and beyond) until we find the registered ppid
    bridges that gap.

    Linux-only via /proc; returns [] on other platforms.
    """
    import os as _os

    out: list[int] = []
    cur = _os.getppid()
    for _ in range(max_depth):
        if cur <= 1:
            break
        out.append(cur)
        try:
            with open(f"/proc/{cur}/status", "r", encoding="utf-8") as f:
                next_pid = None
                for line in f:
                    if line.startswith("PPid:"):
                        next_pid = int(line.split()[1])
                        break
            if next_pid is None or next_pid == cur:
                break
            cur = next_pid
        except (OSError, ValueError):
            break
    return out


def _try_auto_register_from_ppid() -> None:
    """At subprocess startup, query the daemon for our session_id by
    parent (or grandparent / great-grandparent) PID. The SessionStart
    hook posted {ppid: getppid(), session_id} at boot; this lookup
    retrieves it. If found, we skip lazy registration.

    **Walks the ancestor chain** because `bash -lc 'uv run khimaira-chat'`
    means our direct parent is `uv`, not Claude Code. The hook's ppid
    is Claude Code's PID (a few levels up). Try each ancestor.

    **Race-tolerant**: the hook and this subprocess are spawned roughly
    concurrently. If subprocess wins, lookup returns null at each
    ancestor. Retry the whole walk with backoff over ~3s.

    Best-effort: silent on persistent failure; lazy registration takes
    over on the agent's first chat tool call.
    """
    import time

    ancestors = _ancestor_pids(max_depth=6)
    if not ancestors:
        log.info("khimaira-chat: no ancestor PIDs found, skipping bridge")
        return

    session_id = None
    matched_ppid = None
    for attempt in range(6):  # ~3s total: 0.1+0.2+0.4+0.8+1.5 = 3s
        for ppid in ancestors:
            try:
                session_id = daemon_client.lookup_session_by_ppid(ppid)
            except Exception:
                session_id = None
            if session_id:
                matched_ppid = ppid
                break
        if session_id:
            break
        time.sleep(0.1 * (2**attempt))

    if not session_id:
        log.info(
            "khimaira-chat: no session_id found for ancestors %s, falling back to lazy",
            ancestors,
        )
        return
    _state.session_id = session_id
    log.info(
        "khimaira-chat: auto-registered session_id=%s via ancestor ppid=%s (no agent tool call needed)",
        session_id,
        matched_ppid,
    )
    _maybe_register_display_name(session_id)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    _try_auto_register_from_ppid()
    asyncio.run(_serve())


__all__ = ["main"]
