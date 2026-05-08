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


class StatusReq(BaseModel):
    status: str
    detail: str = ""


class AnswerReq(BaseModel):
    question_id: str
    answer: str
    from_session_id: str = "external"


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
        return sessions.log_question(session_id, req.text)

    @router.post("/sessions/{session_id}/status")
    async def post_status(session_id: str, req: StatusReq) -> dict:
        return sessions.set_status(session_id, req.status, req.detail)

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

    return router
