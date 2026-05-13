"""`/api/processes` — REST + SSE for tracked subprocesses.

Endpoints:
  GET  /api/processes                  — list everything in the registry
  GET  /api/processes/{label}          — detail for one process
  GET  /api/processes/{label}/stream   — SSE stream of output chunks
  POST /api/processes/{label}/kill     — send SIGTERM (then SIGKILL after grace)

The MCP tools (server/monitor_tools.py) call into the same registry —
this module is the dashboard view.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from khimaira.monitor import processes

from .._optional import require


# Module-level — FastAPI's body-vs-query auto-detection only fires reliably
# when the Pydantic class is defined at module scope. Closure-defined
# classes get treated as query params (same bug pattern as the SSE endpoint).
class SpawnRequest(BaseModel):
    cmd: list[str] = Field(min_length=1)
    label: str = Field(min_length=1, max_length=128)
    cwd: str | None = None
    env: dict[str, str] | None = None
    replace_existing: bool = False


class WaitRequest(BaseModel):
    completion_signal: str | None = None
    timeout_s: float = Field(default=300.0, ge=1.0, le=3600.0)


def build_router():
    fastapi = require("fastapi")
    from starlette.requests import Request
    from sse_starlette.sse import EventSourceResponse

    router = fastapi.APIRouter()

    @router.get("/processes")
    async def list_processes() -> dict:
        return {
            "processes": [h.to_dict() for h in processes.list_all()],
        }

    @router.post("/processes/spawn")
    async def spawn_process(req: SpawnRequest) -> dict:
        """Spawn a tracked subprocess. Returns immediately with handle metadata."""
        try:
            handle = await processes.spawn(
                req.cmd,
                label=req.label,
                cwd=req.cwd,
                env=req.env,
                replace_existing=req.replace_existing,
            )
        except processes.ProcessExists as e:
            raise fastapi.HTTPException(409, str(e))
        except FileNotFoundError as e:
            raise fastapi.HTTPException(400, f"command not found: {e}")
        return handle.to_dict()

    @router.post("/processes/{label}/wait")
    async def wait_for_process(label: str, req: WaitRequest) -> dict:
        """Block until completion_signal matches OR process exits OR timeout.

        This is the polling-replacement primitive — connect with a long
        client timeout (≥ req.timeout_s + buffer).
        """
        try:
            return await processes.wait_for_process(
                label,
                completion_signal=req.completion_signal,
                timeout_s=req.timeout_s,
            )
        except processes.ProcessNotFound as e:
            raise fastapi.HTTPException(404, str(e))

    @router.get("/processes/{label}")
    async def get_process(label: str) -> dict:
        try:
            h = processes.get(label)
        except processes.ProcessNotFound as e:
            raise fastapi.HTTPException(404, str(e))
        return {
            **h.to_dict(),
            "stdout_text": h.stdout_text(),
            "stderr_text": h.stderr_text(),
        }

    @router.post("/processes/{label}/kill")
    async def kill_process(label: str) -> dict:
        try:
            stopped = await processes.kill(label)
        except processes.ProcessNotFound as e:
            raise fastapi.HTTPException(404, str(e))
        return {"label": label, "stopped": stopped}

    @router.get("/processes/{label}/stream")
    async def stream_process(label: str, request: Request):
        """SSE stream — yields output chunks as they arrive."""
        try:
            processes.get(label)  # pre-flight existence check
        except processes.ProcessNotFound as e:
            raise fastapi.HTTPException(404, str(e))

        async def _gen():
            import json

            async for chunk in processes.follow_process(label, include_existing=True):
                if await request.is_disconnected():
                    return
                yield {
                    "event": "chunk",
                    "data": json.dumps(chunk),
                }
            yield {"event": "end", "data": "{}"}

        return EventSourceResponse(_gen())

    return router
