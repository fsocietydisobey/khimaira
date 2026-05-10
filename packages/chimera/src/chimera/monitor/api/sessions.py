"""`/api/sessions` — REST endpoints for cross-session shared state.

Endpoints:
  GET  /api/sessions                          — list all sessions
  GET  /api/sessions/{sid}                    — full state digest
  GET  /api/sessions/{sid}/pending            — A's inbox read (mark_read default)
  GET  /api/sessions/recent_decisions         — recent across all sessions
  POST /api/sessions/{sid}/decision           — A logs a decision
  POST /api/sessions/{sid}/touch              — A logs a file touch
  POST /api/sessions/{sid}/question           — A opens a question
  POST /api/sessions/{sid}/status             — A updates status
  POST /api/sessions/{sid}/answer             — B answers A's question
"""

from __future__ import annotations

from pydantic import BaseModel

from chimera.monitor import sessions

from .._optional import require


# Module-level — FastAPI body-detection requires the Pydantic class to be
# defined at module scope, not in a closure.
class DecisionReq(BaseModel):
    text: str
    why: str = ""


class TouchReq(BaseModel):
    file: str
    summary: str = ""
    line_start: int | None = None
    line_end: int | None = None


class QuestionReq(BaseModel):
    text: str
    target_session_id: str | None = None


class StatusReq(BaseModel):
    status: str
    detail: str = ""


class NameReq(BaseModel):
    name: str


class AnswerReq(BaseModel):
    question_id: str
    answer: str
    from_session_id: str = "external"


class NoticeReq(BaseModel):
    text: str
    from_session_id: str = "external"


class AckNotesReq(BaseModel):
    note_ids: list[str] | None = None  # None = ack all unread


class HandoffReq(BaseModel):
    text: str
    from_session_id: str
    scope_cwd: str | None = None
    expires_in_hours: float = 168.0


def build_router():
    fastapi = require("fastapi")

    router = fastapi.APIRouter()

    @router.get("/sessions")
    async def list_all() -> dict:
        return {"sessions": sessions.list_sessions()}

    @router.get("/sessions/recent_decisions")
    async def recent_decisions(recent_per_session: int = 5) -> dict:
        return {"decisions": sessions.recent_decisions(recent_per_session=recent_per_session)}

    @router.get("/sessions/{session_id}")
    async def get_state(session_id: str, recent: int = 10) -> dict:
        return sessions.state(session_id, recent=recent)

    @router.get("/sessions/{session_id}/pending")
    async def get_pending(session_id: str, mark_read: bool = True) -> dict:
        return {"notes": sessions.pending_notes(session_id, mark_read=mark_read)}

    @router.post("/sessions/{session_id}/decision")
    async def post_decision(session_id: str, req: DecisionReq) -> dict:
        return sessions.log_decision(session_id, req.text, req.why)

    @router.post("/sessions/{session_id}/touch")
    async def post_touch(session_id: str, req: TouchReq) -> dict:
        line_range = (
            (req.line_start, req.line_end)
            if req.line_start is not None and req.line_end is not None
            else None
        )
        return sessions.log_touch(session_id, req.file, req.summary, line_range)

    @router.post("/sessions/{session_id}/question")
    async def post_question(session_id: str, req: QuestionReq) -> dict:
        return sessions.log_question(
            session_id,
            req.text,
            target_session_id=req.target_session_id,
        )

    @router.get("/sessions/{session_id}/incoming")
    async def get_incoming(session_id: str) -> dict:
        """Open questions from OTHER sessions targeted at this one.

        Symmetric counterpart to /pending — pending shows answers to
        questions THIS session asked; /incoming shows questions OTHER
        sessions asked targeting THIS session.
        """
        return {"questions": sessions.incoming_questions(session_id)}

    @router.get("/sessions/{session_id}/questions/{question_id}/wait")
    async def wait_for_answer(
        session_id: str, question_id: str, timeout: float = 300.0
    ) -> dict:
        """Long-poll: block until question_id is answered on session_id.

        session_id is the OWNER of the question (the asking session).
        Returns the answered question record on success. Returns 408
        (Request Timeout) if no answer in `timeout` seconds. Returns 410
        (Gone) if the question was withdrawn.

        Caller's HTTP timeout MUST be greater than `timeout` parameter.
        """
        try:
            answered = await sessions.wait_for_answer(
                session_id, question_id, timeout=timeout
            )
            return {"answered": True, "question": answered}
        except TimeoutError:
            raise fastapi.HTTPException(
                408, f"No answer to {question_id} within {timeout:.0f}s"
            )
        except ValueError as e:
            raise fastapi.HTTPException(410, str(e))

    @router.post("/sessions/{session_id}/status")
    async def post_status(session_id: str, req: StatusReq) -> dict:
        return sessions.set_status(session_id, req.status, req.detail)

    @router.post("/sessions/{session_id}/name")
    async def post_name(session_id: str, req: NameReq) -> dict:
        return sessions.set_name(session_id, req.name)

    @router.get("/sessions/resolve/{query}")
    async def resolve(query: str) -> dict:
        try:
            return {"query": query, "session_id": sessions.resolve_session_id(query)}
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))

    @router.post("/sessions/{session_id}/answer")
    async def post_answer(session_id: str, req: AnswerReq) -> dict:
        try:
            return sessions.post_answer(
                session_id,
                req.question_id,
                req.answer,
                from_session_id=req.from_session_id,
            )
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))

    @router.post("/sessions/{session_id}/notice")
    async def post_notice(session_id: str, req: NoticeReq) -> dict:
        """Drop a FYI/ack note in target session's inbox. No question/answer
        coupling — for "you don't need to reply, just want you to know" info."""
        return sessions.post_notice(
            session_id,
            req.text,
            from_session_id=req.from_session_id,
        )

    @router.get("/sessions/{session_id}/inbox/surface")
    async def surface_inbox(session_id: str) -> dict:
        """Hook-only fetch path. Returns unread notes + increments
        surface_count. Notes auto-mark read after 3 surfaces (safety net).

        Distinct from /pending: doesn't drain on first fetch — caller is
        expected to surface the content to the user, then call /ack to
        explicitly clear.
        """
        return {"notes": sessions.surface_inbox_for_hook(session_id)}

    @router.post("/sessions/{session_id}/inbox/ack")
    async def ack_inbox_notes(session_id: str, req: AckNotesReq) -> dict:
        """Mark inbox notes as read. note_ids=None acks all unread."""
        count = sessions.ack_notes(session_id, req.note_ids)
        return {"acked": count}

    @router.post("/handoffs")
    async def post_handoff(req: HandoffReq) -> dict:
        """Drop a handoff note any future session in matching cwd will read.

        Closes the gap that post_notice left: notices need a target_session_id
        but cross-session handoffs to future sessions had no target. Handoffs
        are scoped by cwd and surface on the next matching SessionStart.
        """
        return sessions.post_handoff(
            req.from_session_id,
            req.text,
            scope_cwd=req.scope_cwd,
            expires_in_hours=req.expires_in_hours,
        )

    @router.get("/handoffs/consume")
    async def consume_handoffs(session_id: str, cwd: str) -> dict:
        """Return handoffs matching cwd; mark this session_id as having read."""
        return {"handoffs": sessions.consume_handoffs(session_id, cwd)}

    @router.get("/sessions/{session_id}/inbox/archive")
    async def search_inbox_archive(
        session_id: str,
        q: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Search archived (read) inbox notes by substring.

        Read notes get moved from inbox.jsonl → archive.jsonl on ack /
        drain / auto-expire. This endpoint exposes archive history so
        you can query "what did session X say about topic Y" without
        losing past cross-session context.
        """
        return {
            "query": q,
            "results": sessions.search_archive(session_id, q, limit),
        }

    return router
