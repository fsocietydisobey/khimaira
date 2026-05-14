"""`/api/scheduled-tasks` — REST endpoints for the daemon-side scheduler.

Endpoints:
  POST   /api/scheduled-tasks            — create
  GET    /api/scheduled-tasks            — list (filterable by status, target)
  GET    /api/scheduled-tasks/{id}       — single record
  DELETE /api/scheduled-tasks/{id}       — cancel
"""

from __future__ import annotations

from pydantic import BaseModel

from khimaira.monitor import scheduler

from .._optional import require


class CreateTaskReq(BaseModel):
    target_session: str
    fire_at_utc: str
    prompt: str
    retry_policy: dict | None = None
    expires_in_hours: float = 168.0


def build_router():
    fastapi = require("fastapi")

    router = fastapi.APIRouter()

    @router.post("/scheduled-tasks")
    async def create_task(req: CreateTaskReq) -> dict:
        try:
            return scheduler.create(
                target_session=req.target_session,
                fire_at_utc=req.fire_at_utc,
                prompt=req.prompt,
                retry_policy=req.retry_policy,
                expires_in_hours=req.expires_in_hours,
            )
        except ValueError as exc:
            # Unknown target session, malformed fire_at_utc, etc.
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.get("/scheduled-tasks")
    async def list_tasks(status: str | None = None, target: str | None = None) -> dict:
        status_filter = status.split(",") if status else None
        return {"tasks": scheduler.list_tasks(status_filter=status_filter, target_filter=target)}

    @router.get("/scheduled-tasks/{task_id}")
    async def get_task(task_id: str) -> dict:
        rec = scheduler.get(task_id)
        if rec is None:
            raise fastapi.HTTPException(404, f"No scheduled task with id={task_id!r}")
        return rec

    @router.delete("/scheduled-tasks/{task_id}")
    async def cancel_task(task_id: str) -> dict:
        try:
            return scheduler.cancel(task_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc
        except RuntimeError as exc:
            raise fastapi.HTTPException(409, str(exc)) from exc

    return router
