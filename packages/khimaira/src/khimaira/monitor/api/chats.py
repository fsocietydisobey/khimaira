"""`/api/chats` — REST + SSE for cross-session chat.

Endpoints:
  POST   /api/chats                              — create_room
  POST   /api/chats/{chat_id}/invite             — invite member
  POST   /api/chats/{chat_id}/accept             — accept invite
  POST   /api/chats/{chat_id}/messages           — send a message
  GET    /api/chats/{chat_id}/messages           — paginated history
  GET    /api/chats/{chat_id}                    — room metadata + members + history
  POST   /api/chats/{chat_id}/leave              — leave
  DELETE /api/chats/{chat_id}                    — archive (creator only)
  GET    /api/chats?session_id=…                 — my chats
  GET    /api/chats/events?session_id=…          — SSE event stream
                                                   (Last-Event-ID header → backfill from JSONL)
"""

from __future__ import annotations

import json

from fastapi import Request
from pydantic import BaseModel

from khimaira.monitor import chats

from .._optional import require


class CreateRoomReq(BaseModel):
    creator_session_id: str
    member_session_ids: list[str]
    title: str | None = None
    fresh: bool = False


class InviteReq(BaseModel):
    by_session_id: str
    invitee_session_id: str


class AcceptReq(BaseModel):
    session_id: str


class SendReq(BaseModel):
    sender_session_id: str
    body: str


class LeaveReq(BaseModel):
    session_id: str


class RejectReq(BaseModel):
    session_id: str


class RegisterPpidReq(BaseModel):
    ppid: int
    session_id: str


def build_router():
    fastapi = require("fastapi")
    sse_starlette = require("sse_starlette.sse")

    router = fastapi.APIRouter()

    @router.post("/chats")
    async def create_room(req: CreateRoomReq) -> dict:
        try:
            return chats.create_room(
                req.creator_session_id,
                req.member_session_ids,
                title=req.title,
                fresh=req.fresh,
            )
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.get("/chats")
    async def list_my_chats(session_id: str) -> dict:
        try:
            return {"chats": chats.my_chats(session_id)}
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    # Specific routes BEFORE the {chat_id} catch-all, or FastAPI matches
    # the wildcard first (treats "session-by-ppid" as a chat_id).
    @router.post("/chats/register-pending-session")
    async def register_pending_session(req: RegisterPpidReq) -> dict:
        """SessionStart hook posts {ppid, session_id} so the chat MCP
        subprocess (same parent ppid) can self-register at startup
        without waiting for the agent's first chat tool call."""
        chats.register_session_by_ppid(req.ppid, req.session_id)
        return {"ok": True, "ppid": req.ppid, "session_id": req.session_id}

    @router.get("/chats/session-by-ppid")
    async def session_by_ppid(ppid: int) -> dict:
        """Chat MCP subprocess at startup queries by its own getppid().
        Returns the session_id the SessionStart hook registered, or null."""
        return {"session_id": chats.lookup_session_by_ppid(ppid)}

    @router.get("/chats/pending/latest")
    async def latest_pending(session_id: str) -> dict:
        """Return the most-recent pending chat_id for this session, or null.

        Used by /khimaira-chat-accept and /khimaira-chat-reject so the
        slash commands work without the user knowing the chat_id.
        """
        try:
            chat_id = chats.latest_pending_chat_id(session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc
        return {"chat_id": chat_id}

    @router.get("/chats/events")
    async def chat_events(session_id: str, request: Request):
        try:
            chats._resolve_or_uuid(session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

        last_event_id = request.headers.get("last-event-id")

        async def event_generator():
            async for record in chats.subscribe(session_id, since_event_id=last_event_id):
                if await request.is_disconnected():
                    break
                yield {
                    "id": record.get("event_id", ""),
                    "event": record.get("kind", "message"),
                    "data": json.dumps(record, separators=(",", ":")),
                }

        return sse_starlette.EventSourceResponse(event_generator())

    @router.get("/chats/{chat_id}")
    async def get_room(chat_id: str, session_id: str) -> dict:
        try:
            room = chats.load_room(chat_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc
        member = room["members"].get(chats._resolve_or_uuid(session_id))
        if not member or member["state"] not in (chats.PENDING, chats.ACCEPTED):
            raise fastapi.HTTPException(
                403, f"Session {session_id!r} is not a member of {chat_id!r}"
            )
        return room

    @router.post("/chats/{chat_id}/invite")
    async def invite_member(chat_id: str, req: InviteReq) -> dict:
        try:
            return chats.invite(chat_id, req.by_session_id, req.invitee_session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.post("/chats/{chat_id}/accept")
    async def accept_invite(chat_id: str, req: AcceptReq) -> dict:
        try:
            return chats.accept(chat_id, req.session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.post("/chats/{chat_id}/reject")
    async def reject_invite(chat_id: str, req: RejectReq) -> dict:
        try:
            return chats.reject(chat_id, req.session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.post("/chats/{chat_id}/messages")
    async def send_message(chat_id: str, req: SendReq) -> dict:
        try:
            return chats.send_message(chat_id, req.sender_session_id, req.body)
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    @router.get("/chats/{chat_id}/messages")
    async def get_history(
        chat_id: str,
        session_id: str,
        limit: int = 50,
        since: str | None = None,
    ) -> dict:
        try:
            return {
                "messages": chats.history(chat_id, session_id, limit=limit, since_event_id=since)
            }
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    @router.post("/chats/{chat_id}/leave")
    async def leave_chat(chat_id: str, req: LeaveReq) -> dict:
        try:
            return chats.leave(chat_id, req.session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.delete("/chats/{chat_id}")
    async def delete_chat(chat_id: str, by_session_id: str) -> dict:
        try:
            return chats.delete(chat_id, by_session_id)
        except ValueError as exc:
            msg = str(exc)
            code = 403 if "creator" in msg else 404
            raise fastapi.HTTPException(code, msg) from exc

    return router
