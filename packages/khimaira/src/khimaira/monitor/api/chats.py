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
    to: list[str] | None = None  # Phase B: optional per-recipient addressing


class CreateTaskReq(BaseModel):
    sender_session_id: str
    body: str
    assignee_session_id: str | None = None


class UpdateTaskStatusReq(BaseModel):
    by_session_id: str
    new_status: str
    note: str | None = None


class SignalTaskStartReq(BaseModel):
    by_session_id: str
    note: str | None = None


class AutoAcceptReq(BaseModel):
    session_id: str
    allowlist: list[str]


class LeaveReq(BaseModel):
    session_id: str


class TransferMembershipReq(BaseModel):
    from_session_id: str
    to_session_id: str
    # Phase B v1.6: when True, atomically writes
    # meta.deputized_original_master = from_session_id AND skips the donor's
    # TRANSFERRED_OUT MEMBER write so the donor stays ACCEPTED throughout
    # the deputize→resume cycle. Default False = terminal-handoff behavior.
    as_deputize: bool = False


class ResumeMasterReq(BaseModel):
    # Phase B v1.6: caller (must equal recorded meta.deputized_original_master).
    by_session_id: str
    # Role the vice gets demoted to on resume. Defaults to "agent"; cannot be
    # "master" (closes quorum loophole, mirrors chat_grant_role).
    demote_to: str = "agent"


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

        # ping=15 makes sse_starlette emit a `: keepalive` comment line
        # every 15 seconds. SSE comments are valid keep-alives that don't
        # show up as events but DO refresh TCP buffers and reset the
        # client-side read timeout. Without this, an SSE connection that
        # survives a laptop suspend (or any long network silence) becomes
        # silently dead — the daemon's view is gone but the client's
        # aiter_lines() blocks forever waiting for events that never come.
        return sse_starlette.EventSourceResponse(event_generator(), ping=15)

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
            return chats.send_message(chat_id, req.sender_session_id, req.body, to=req.to)
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    # ---- Phase B: tasks ----

    @router.post("/chats/{chat_id}/tasks")
    async def create_task(chat_id: str, req: CreateTaskReq) -> dict:
        try:
            return chats.create_task(
                chat_id,
                req.sender_session_id,
                req.body,
                assignee_session_id=req.assignee_session_id,
            )
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    @router.post("/chats/{chat_id}/tasks/{task_id}/status")
    async def update_task_status(chat_id: str, task_id: str, req: UpdateTaskStatusReq) -> dict:
        try:
            return chats.update_task_status(
                chat_id, task_id, req.by_session_id, req.new_status, note=req.note
            )
        except ValueError as exc:
            # 403 for permission errors (master-only transitions); 404 for unknown task
            msg = str(exc)
            code = 403 if any(w in msg for w in ("creator", "assignee", "transition")) else 404
            raise fastapi.HTTPException(code, msg) from exc

    @router.post("/chats/{chat_id}/tasks/{task_id}/signal-start")
    async def signal_task_start(chat_id: str, task_id: str, req: SignalTaskStartReq) -> dict:
        try:
            return chats.signal_task_start(chat_id, task_id, req.by_session_id, note=req.note)
        except ValueError as exc:
            msg = str(exc)
            if "No task" in msg:
                code = 404
            elif "not 'pending'" in msg:
                code = 409
            else:
                # master-only / non-accepted member → 403
                code = 403
            raise fastapi.HTTPException(code, msg) from exc

    @router.get("/chats/{chat_id}/tasks")
    async def list_tasks(chat_id: str, session_id: str) -> dict:
        try:
            return {"tasks": chats.task_status(chat_id, session_id)}
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    # ---- Phase B: auto-accept ----

    @router.post("/sessions/{session_id}/auto-accept")
    async def set_auto_accept(session_id: str, req: AutoAcceptReq) -> dict:
        # session_id from path is the source of truth; req.session_id should match
        # but we accept the path version
        try:
            chats.set_auto_accept(session_id, req.allowlist)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc
        return {"ok": True, "session_id": session_id, "allowlist": req.allowlist}

    @router.post("/sessions/{session_id}/auto-accept/apply-by-name")
    async def apply_auto_accept_by_name(session_id: str, name: str) -> dict:
        """Surface the by-name allowlist file for a freshly-named session.
        Called by the chat MCP subprocess at boot, after the dual-name
        auto-bridge detects `-n NAME` and registers it. No-op if no
        by-name file exists for `name`."""
        try:
            return chats.apply_auto_accept_by_name(session_id, name)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

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

    @router.post("/chats/{chat_id}/transfer-membership")
    async def transfer_membership(chat_id: str, req: TransferMembershipReq) -> dict:
        try:
            return chats.transfer_membership(
                chat_id,
                req.from_session_id,
                req.to_session_id,
                as_deputize=req.as_deputize,
            )
        except ValueError as exc:
            msg = str(exc)
            if "already accepted" in msg:
                code = 409
            elif "only accepted members" in msg:
                code = 403
            else:
                code = 404
            raise fastapi.HTTPException(code, msg) from exc

    @router.post("/chats/{chat_id}/resume-master")
    async def resume_master(chat_id: str, req: ResumeMasterReq) -> dict:
        """Phase B v1.6: caller (original master per meta marker) reclaims
        master role from the vice that's currently holding it. Pairs with
        /khimaira-resume on the skill side. See chats.chat_resume_master for
        semantics + invariants."""
        try:
            return chats.chat_resume_master(
                chat_id,
                req.by_session_id,
                demote_to=req.demote_to,
            )
        except ValueError as exc:
            msg = str(exc)
            if "not currently deputized" in msg or "no deputized_original_master" in msg:
                code = 409
            elif "not the recorded original master" in msg or "demote_to" in msg:
                code = 403
            else:
                code = 404
            raise fastapi.HTTPException(code, msg) from exc

    @router.delete("/chats/{chat_id}")
    async def delete_chat(chat_id: str, by_session_id: str) -> dict:
        try:
            return chats.delete(chat_id, by_session_id)
        except ValueError as exc:
            msg = str(exc)
            code = 403 if "creator" in msg else 404
            raise fastapi.HTTPException(code, msg) from exc

    return router
