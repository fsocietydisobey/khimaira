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

import asyncio
import json
import time

from fastapi import Request
from pydantic import BaseModel

_PENDING_POLL_INTERVAL = 5.0  # seconds between polls when a `to` recipient is pending
_PENDING_WAIT_DEADLINE = 30.0  # total seconds to wait for recipient to accept

# Expected-reply registry — tracks targeted sends that expect a reply back.
# Key: (to_id, from_id) meaning "to_id owes a reply to from_id".
# Value: {"ts": wall_clock_time, "from": from_id, "to": to_id,
#          "chat_id": str, "threshold_s": float}
_EXPECTED_REPLIES: dict[tuple[str, str], dict] = {}
_REPLY_OVERDUE_DEFAULT_S = 90.0  # default threshold before a missing reply is diagnosed
_REPLY_WATCH_INTERVAL = 30.0  # seconds between watcher scans
_REGISTRY_LOCK = asyncio.Lock()

# Per-role thresholds — known-long-work roles get more time before presumed-dead.
_REPLY_OVERDUE_BY_ROLE: dict[str, float] = {
    "architect": 180.0,
    "analyst": 180.0,
    "verifier": 300.0,
    "critic": 120.0,
    # Domain leads do substantive research; 90s default is too tight.
    "backend-lead": 180.0,
    "data-lead": 180.0,
    "jp-backend-lead": 180.0,
    "jp-data-lead": 180.0,
    "jp-frontend-lead": 180.0,
}
_LIVENESS_WINDOW_S = 60.0  # activity within this window = session is alive

_RECENTLY_PRESUMED_DEAD: dict[tuple[str, str], dict] = {}
_PRESUMED_DEAD_TTL_S = 300.0  # 5 minutes for late-reply supersede


def _threshold_for_session(to_id: str, chat_id: str) -> float:
    """Per-role threshold lookup.

    Lookup order:
    1. chat.meta.member_roles[to_id] — canonical (set by chat_create_room v1.9.6+)
    2. infer_role_from_name(session_name) — fallback for chats created before
       member_roles was a chat param, or sessions added without role binding
    3. _REPLY_OVERDUE_DEFAULT_S (90s) — last resort
    """
    try:
        room = chats.load_room(chat_id)
        role = (room.get("meta", {}).get("member_roles") or {}).get(to_id)
        if role and role in _REPLY_OVERDUE_BY_ROLE:
            return _REPLY_OVERDUE_BY_ROLE[role]

        # Fallback: infer role from session name. Scan each dash-segment left-to-right
        # so both "architect-1" and prefixed forms like "jp-architect-1" match.
        from khimaira.monitor import sessions as sessions_mod

        session_state = sessions_mod.state(to_id)
        name = (session_state.get("status") or {}).get("name") or ""
        for segment in name.split("-"):
            if segment in _REPLY_OVERDUE_BY_ROLE:
                return _REPLY_OVERDUE_BY_ROLE[segment]
    except Exception:
        pass
    return _REPLY_OVERDUE_DEFAULT_S


def _session_active_within(session_id: str, window_s: float) -> bool:
    """Liveness check: True if session logged any tool_call or file_touch
    within the last window_s seconds.
    Fail-open: any error returns False (treat as silent — conservative).
    """
    try:
        from datetime import datetime
        from khimaira.monitor import sessions as sessions_mod

        def _ts_float(iso_str: str) -> float:
            return datetime.fromisoformat(iso_str).timestamp()

        now = time.time()
        recent = sessions_mod.recent_tool_calls(session_id, limit=1)
        if (
            recent
            and (now - _ts_float(recent[0].get("ts", "1970-01-01T00:00:00+00:00"))) < window_s
        ):
            return True
        touches = (
            sessions_mod.recent_touches(session_id, limit=1)
            if hasattr(sessions_mod, "recent_touches")
            else []
        )
        if (
            touches
            and (now - _ts_float(touches[0].get("ts", "1970-01-01T00:00:00+00:00"))) < window_s
        ):
            return True
    except Exception:
        return False
    return False


def _resolve_master_session_id(chat_id: str) -> str | None:
    """Find the session_id with role=master in this chat. None if not found."""
    try:
        room = chats.load_room(chat_id)
        member_roles = room.get("meta", {}).get("member_roles") or {}
        for sid, role in member_roles.items():
            if role == chats.ROLE_MASTER:
                return sid
    except Exception:
        pass
    return None


async def _send_diagnostic_probe(
    chat_id: str,
    to_id: str,
    from_id: str,
    elapsed_s: float,
) -> bool:
    """Send a synthetic ping via daemon-internal primitive.

    Returns True on success; False on error (fail-open — caller proceeds
    to presumed-dead path on next tick regardless).
    """
    body = (
        f"🩺 ping from diagnostic — `{from_id}` has been waiting since "
        f"T+{elapsed_s:.0f}s. Please broadcast any message to confirm you're alive."
    )
    try:
        from khimaira.monitor.chats import _post_synthetic_message

        result = await _post_synthetic_message(chat_id, body)
        return result is not None
    except Exception:
        return False


def _sweep_presumed_dead(now: float) -> None:
    """Drop _RECENTLY_PRESUMED_DEAD entries older than TTL.
    Called opportunistically from _check_overdue_once.
    """
    expired = [
        key
        for key, entry in _RECENTLY_PRESUMED_DEAD.items()
        if now - entry["notice_ts"] > _PRESUMED_DEAD_TTL_S
    ]
    for key in expired:
        _RECENTLY_PRESUMED_DEAD.pop(key, None)


async def _maybe_supersede_presumed_dead(sender_id: str) -> None:
    """If sender was recently presumed-dead, fire supersede notice to master.

    Called from _resolve_expected_reply and broadcast-resolve paths when a
    session sends ANY message. Clears matching entries from _RECENTLY_PRESUMED_DEAD.
    """
    matched_keys = [
        key
        for key in list(_RECENTLY_PRESUMED_DEAD.keys())
        if key[0] == sender_id  # to_id (the previously-silent session) == sender now
    ]
    for key in matched_keys:
        entry = _RECENTLY_PRESUMED_DEAD.pop(key, None)
        if entry is None:
            continue
        master_sid = _resolve_master_session_id(entry.get("chat_id", ""))
        if master_sid is None:
            continue
        elapsed_since_notice = time.time() - entry["notice_ts"]
        body = (
            f"♻️ SUPERSEDE: session `{sender_id}` replied {elapsed_since_notice:.0f}s "
            f"after presumed-dead notice fired. Original wait was "
            f"{entry['elapsed_s']:.0f}s on chat_send_to from `{entry['from_id']}`. "
            f"`{sender_id}` is alive after all — original notice was a false-positive. "
            f"Recommend retracting any re-dispatch decision based on the prior notice."
        )
        try:
            from khimaira.monitor import sessions as sessions_mod

            sessions_mod.post_notice(
                target_session_id=master_sid,
                text=body,
                from_session_id="khimaira-daemon",
            )
        except Exception:
            pass


async def _register_expected_reply(from_id: str, to_ids: list[str], chat_id: str = "") -> None:
    async with _REGISTRY_LOCK:
        ts = time.time()
        for to_id in to_ids:
            if to_id == from_id:
                continue
            threshold = _threshold_for_session(to_id, chat_id)
            _EXPECTED_REPLIES[(to_id, from_id)] = {
                "ts": ts,
                "from": from_id,
                "to": to_id,
                "chat_id": chat_id,
                "threshold_s": threshold,
            }


async def _resolve_expected_reply(from_id: str, to_ids: list[str], chat_id: str = "") -> None:
    async with _REGISTRY_LOCK:
        for to_id in to_ids:
            _EXPECTED_REPLIES.pop((from_id, to_id), None)
    # Supersede check: from_id just sent — if it was previously presumed-dead,
    # master gets a retraction notice.
    await _maybe_supersede_presumed_dead(from_id)


async def _diagnose_and_dispose(key: tuple, entry: dict, ts_now: float) -> None:
    """Diagnostic phase: liveness check → probe → presumed-dead.

    Phase 1 (liveness): if X has recent tool/touch activity, reschedule.
    Phase 2 (probe): if no activity AND no probe sent yet, send probe + reschedule.
    Phase 3 (presumed-dead): if probe already sent and X still silent, fire master notice.
    """
    to_id = entry["to"]
    from_id = entry["from"]
    chat_id = entry.get("chat_id", "")
    elapsed_s = ts_now - entry["ts"]
    threshold = entry.get("threshold_s", _REPLY_OVERDUE_DEFAULT_S)

    # Phase 1: liveness check
    if _session_active_within(to_id, _LIVENESS_WINDOW_S):
        async with _REGISTRY_LOCK:
            existing = _EXPECTED_REPLIES.get(key)
            if existing is not None:
                existing["ts"] = ts_now
                existing["probe_sent_at"] = None  # reset probe state on reschedule
        return

    # Phase 2: probe (only if not yet probed)
    probe_sent_at = entry.get("probe_sent_at")
    if probe_sent_at is None:
        await _send_diagnostic_probe(chat_id, to_id, from_id, elapsed_s)
        async with _REGISTRY_LOCK:
            existing = _EXPECTED_REPLIES.get(key)
            if existing is not None:
                existing["probe_sent_at"] = ts_now
        # Don't fire presumed-dead notice yet — wait one tick for probe response
        return

    # Phase 3: presumed dead (probe was sent on prior tick, X still silent)
    async with _REGISTRY_LOCK:
        _EXPECTED_REPLIES.pop(key, None)

    master_sid = _resolve_master_session_id(chat_id)
    if master_sid is None:
        return

    # Record for late-reply supersede tracking
    _RECENTLY_PRESUMED_DEAD[(to_id, from_id)] = {
        "notice_ts": ts_now,
        "chat_id": chat_id,
        "from_id": from_id,
        "to_id": to_id,
        "elapsed_s": elapsed_s,
    }

    probe_age_s = ts_now - probe_sent_at
    body = (
        f"🚨 PRESUMED-DEAD SESSION — `{to_id}` has not responded to "
        f"chat_send_to from `{from_id}` after {elapsed_s:.0f}s "
        f"(role-threshold: {threshold:.0f}s). Diagnostic probe sent "
        f"{probe_age_s:.0f}s ago also received no reply. session_state shows "
        f"no tool activity in the last {_LIVENESS_WINDOW_S:.0f}s. "
        f"Decide: re-dispatch / retry / investigate. chat_id={chat_id}"
    )
    try:
        from khimaira.monitor import sessions as sessions_mod

        sessions_mod.post_notice(
            target_session_id=master_sid,
            text=body,
            from_session_id="khimaira-daemon",
        )
    except Exception:
        pass


async def _check_overdue_once() -> None:
    now = time.time()
    _sweep_presumed_dead(now)  # opportunistic cleanup of late-reply tracking
    candidates = []
    async with _REGISTRY_LOCK:
        for key, entry in list(_EXPECTED_REPLIES.items()):
            threshold = entry.get("threshold_s", _REPLY_OVERDUE_DEFAULT_S)
            if now - entry["ts"] > threshold:
                candidates.append((key, entry))
    for key, entry in candidates:
        await _diagnose_and_dispose(key, entry, now)


async def _overdue_watcher() -> None:
    import logging

    _log = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(_REPLY_WATCH_INTERVAL)
        try:
            await _check_overdue_once()
        except Exception as exc:
            _log.warning("overdue-reply watcher error: %s", exc)


from khimaira.monitor import chats
from khimaira.monitor.chats import _resolve_sender_name  # shared with _broadcast

from .._optional import require


class CreateRoomReq(BaseModel):
    creator_session_id: str
    member_session_ids: list[str]
    title: str | None = None
    fresh: bool = False
    topology: str = "flat"  # v1.9.5: flat | hierarchical | custom
    member_roles: dict[str, str] | None = None  # session_id → role; written to meta at creation


class InviteReq(BaseModel):
    by_session_id: str
    invitee_session_id: str


class AcceptReq(BaseModel):
    session_id: str


class SendReq(BaseModel):
    sender_session_id: str
    body: str
    to: list[str] | None = None  # Phase B: optional per-recipient addressing
    private: bool | None = None  # v1.9.2: hide from non-recipients; None = topology default


class CreateTaskReq(BaseModel):
    sender_session_id: str
    body: str
    assignee_session_id: str | None = None
    private: bool = False  # v1.9.2: hide from non-assignee in chat_history


class UpdateTaskStatusReq(BaseModel):
    by_session_id: str
    new_status: str
    note: str | None = None
    private: bool = False  # v1.9.2: hide from non-assignee in chat_history


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


class AssignmentSpec(BaseModel):
    agent_session_id: str
    task_body: str
    required_model: str = "sonnet"
    required_effort: str = "medium"


class AssignBatchReq(BaseModel):
    from_session_id: str
    assignments: list[AssignmentSpec]
    timeout_s: int = 600
    wait_for_acks: bool = True
    fire_begin_on_partial: bool = False


def build_router():
    fastapi = require("fastapi")
    sse_starlette = require("sse_starlette.sse")

    router = fastapi.APIRouter()

    # Lazy import: themis role cache invalidation. Fail-open — if themis router
    # is not loaded (test environments), chat writes proceed unaffected.
    def _inval(*session_ids: str) -> None:
        try:
            from .themis import invalidate_role_cache

            for sid in session_ids:
                invalidate_role_cache(sid)
        except Exception:
            pass  # fail-open: stale cache expires in 5 min (TTL safety net)

    @router.post("/chats")
    async def create_room(req: CreateRoomReq) -> dict:
        try:
            result = chats.create_room(
                req.creator_session_id,
                req.member_session_ids,
                title=req.title,
                fresh=req.fresh,
                topology=req.topology,
                member_roles=req.member_roles,
            )
            # Invalidate creator + all initial members — they may now have roles
            _inval(req.creator_session_id, *req.member_session_ids)
            return result
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
                # Cursor advances AFTER successful yield — if yield raises
                # (ClientDisconnect, transport error), this line doesn't run,
                # so the next reconnect backfills from the prior position.
                evt_id = record.get("event_id")
                rec_chat_id = record.get("chat_id")
                if evt_id and rec_chat_id:
                    chats._advance_cursor(session_id, rec_chat_id, evt_id)

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
            result = chats.accept(chat_id, req.session_id)
            _inval(req.session_id)
            return result
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
        # Resolve: this send counts as a reply to any peer awaiting a response.
        if req.to:
            # Targeted send: resolve only named recipients.
            await _resolve_expected_reply(req.sender_session_id, req.to)
        else:
            # Broadcast: resolve for ALL accepted chat members (critic/verifier
            # conventionally reply via broadcast, not DM).
            try:
                room = chats.load_room(chat_id)
                member_ids = [
                    sid
                    for sid, m in room["members"].items()
                    if m.get("state") == chats.ACCEPTED and sid != req.sender_session_id
                ]
                if member_ids:
                    await _resolve_expected_reply(req.sender_session_id, member_ids)
            except Exception:
                pass  # fail-open: don't block the send if room lookup fails
        deadline = time.monotonic() + _PENDING_WAIT_DEADLINE
        while True:
            try:
                result = chats.send_message(
                    chat_id, req.sender_session_id, req.body, to=req.to, private=req.private
                )
                # Register only for targeted sends — broadcasts don't expect a reply.
                if req.to:
                    await _register_expected_reply(req.sender_session_id, req.to, chat_id)
                return result
            except ValueError as exc:
                msg = str(exc)
                if req.to and "pending" in msg:
                    if time.monotonic() >= deadline:
                        raise fastapi.HTTPException(
                            408,
                            f"Timed out after {_PENDING_WAIT_DEADLINE:.0f}s waiting for recipient to accept invite.",
                        ) from exc
                    await asyncio.sleep(_PENDING_POLL_INTERVAL)
                    continue
                raise fastapi.HTTPException(403, msg) from exc

    # ---- Phase B: tasks ----

    @router.post("/chats/{chat_id}/tasks")
    async def create_task(chat_id: str, req: CreateTaskReq) -> dict:
        try:
            return chats.create_task(
                chat_id,
                req.sender_session_id,
                req.body,
                assignee_session_id=req.assignee_session_id,
                private=req.private,
            )
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    @router.post("/chats/{chat_id}/tasks/{task_id}/status")
    async def update_task_status(chat_id: str, task_id: str, req: UpdateTaskStatusReq) -> dict:
        try:
            return chats.update_task_status(
                chat_id,
                task_id,
                req.by_session_id,
                req.new_status,
                note=req.note,
                private=req.private,
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

    # ---- v1.9: assign-batch coordinator ----

    @router.post("/chats/{chat_id}/assign-batch")
    async def assign_batch(chat_id: str, req: AssignBatchReq) -> dict:
        """v1.9 coordinator: fan-out assignments + collect acks + fire begin.

        Collapses the master's 3N+K+2 call loop into one daemon HTTP call.
        Long-running when wait_for_acks=True (up to timeout_s seconds).
        """
        try:
            return await chats.assign_batch(
                chat_id,
                req.from_session_id,
                [a.model_dump() for a in req.assignments],
                timeout_s=req.timeout_s,
                wait_for_acks=req.wait_for_acks,
                fire_begin_on_partial=req.fire_begin_on_partial,
            )
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
            msgs = chats.history(chat_id, session_id, limit=limit, since_event_id=since)
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc
        # Option A: resolve each sender's CURRENT name at read-time.
        # Per-request cache prevents N lookups for N messages from same sender.
        name_cache: dict[str, str] = {}
        for msg in msgs:
            sid = msg.get("sender_id")
            if not sid:
                continue
            if sid not in name_cache:
                name_cache[sid] = _resolve_sender_name(sid, msg.get("sender_name", sid[:8]))
            msg["sender_name"] = name_cache[sid]
        return {"messages": msgs}

    @router.post("/chats/{chat_id}/leave")
    async def leave_chat(chat_id: str, req: LeaveReq) -> dict:
        try:
            result = chats.leave(chat_id, req.session_id)
            _inval(req.session_id)
            return result
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.post("/chats/{chat_id}/transfer-membership")
    async def transfer_membership(chat_id: str, req: TransferMembershipReq) -> dict:
        try:
            result = chats.transfer_membership(
                chat_id,
                req.from_session_id,
                req.to_session_id,
                as_deputize=req.as_deputize,
            )
            # Both sessions swap roles — invalidate both
            _inval(req.from_session_id, req.to_session_id)
            return result
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
            result = chats.chat_resume_master(
                chat_id,
                req.by_session_id,
                demote_to=req.demote_to,
            )
            # by_session_id reclaims master; the former vice's session_id is
            # not in the request — clear entire cache so stale vice role
            # doesn't persist beyond the TTL. Nuclear but correct.
            try:
                from .themis import clear_role_cache

                clear_role_cache()
            except Exception:
                pass
            return result
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
