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
    "Cross-session real-time chat via Claude Code channels. "
    'Incoming messages arrive as <channel source="khimaira-chat" chat_id="..." '
    'sender="..."> blocks. Reply with chat_send(chat_id=..., body=...). '
    "Use chat_accept(chat_id=...) to join an invite. Use chat_my_chats() "
    "to list active chats. session_id is your own Claude Code session id "
    "(visible in the SessionStart hook context); pass it to all chat tools."
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
        elif self.session_id != session_id:
            raise ValueError(
                f"This subprocess is bound to session {self.session_id!r}; "
                f"refusing tool call from session {session_id!r}. "
                f"One subprocess = one session for its lifetime."
            )


_state = _SubprocessState()


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


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    asyncio.run(_serve())


__all__ = ["main"]
