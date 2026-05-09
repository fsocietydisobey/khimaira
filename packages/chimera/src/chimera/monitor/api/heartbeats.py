"""`/api/heartbeat` — receive observer events; `/api/heartbeats/...` — read.

POST /api/heartbeat                              receive one event from chimera_observer
GET  /api/heartbeats/{project}                   list active runs in project
GET  /api/heartbeats/{project}/{run_id}          one run's full event buffer
GET  /api/heartbeats/{project}/{run_id}/stream   SSE: new events as they arrive
GET  /api/heartbeats/stats                       store stats (total runs, etc.)
"""

from __future__ import annotations

from typing import Any

from chimera.monitor import heartbeats

from .._optional import require


def build_router():
    fastapi = require("fastapi")
    from starlette.requests import Request
    from sse_starlette.sse import EventSourceResponse

    router = fastapi.APIRouter()

    @router.post("/heartbeat")
    async def post_heartbeat(payload: dict[str, Any]) -> dict[str, str]:
        """Receive one event from a target app's chimera_observer.

        Schema is intentionally open. Required: project, run_id, event, ts.
        Permissive on everything else — the observer can add fields without
        a daemon update.
        """
        await heartbeats.record(payload)
        return {"status": "ok"}

    @router.get("/heartbeats/stats")
    async def get_stats() -> dict[str, Any]:
        return heartbeats.stats()

    @router.get("/heartbeats/{project}")
    async def list_project_runs(project: str) -> dict[str, Any]:
        runs = heartbeats.list_runs(project)
        return {
            "project": project,
            "runs": [
                {
                    "run_id": e.run_id,
                    "current_node": e.current_node,
                    "last_event_ts": e.last_event_ts,
                    "event_count": len(e.events),
                    "latest_event": e.events[-1] if e.events else None,
                }
                for e in runs
            ],
        }

    @router.get("/heartbeats/{project}/{run_id}")
    async def get_run(project: str, run_id: str) -> dict[str, Any]:
        entry = heartbeats.get(project, run_id)
        if entry is None:
            raise fastapi.HTTPException(404, f"no run {run_id!r} in {project!r}")
        return {
            "project": project,
            "run_id": run_id,
            "current_node": entry.current_node,
            "last_event_ts": entry.last_event_ts,
            "events": list(entry.events),
        }

    @router.get("/heartbeats/{project}/{run_id}/stream")
    async def stream_run(project: str, run_id: str, request: Request):
        """SSE stream of events as they arrive for one run.

        Initial event: snapshot of current buffer.
        Subsequent events: each new heartbeat as it lands.
        Heartbeat 'keepalive' every 15s so proxies don't reap idle connections.
        Closes after 30min of inactivity.
        """
        entry = heartbeats.get(project, run_id)
        if entry is None:
            # Don't 404 — the run might not have started yet. Create an empty
            # placeholder; the consumer can wait for the first real event.
            await heartbeats.record({
                "project": project,
                "run_id": run_id,
                "event": "stream_subscribe",
                "ts": 0.0,
            })
            entry = heartbeats.get(project, run_id)

        async def _gen():
            import json
            import time

            cursor = 0
            last_keepalive = time.monotonic()
            last_activity = time.monotonic()

            # Initial snapshot
            for ev in list(entry.events):
                if await request.is_disconnected():
                    return
                yield {"event": "snapshot", "data": json.dumps(ev, default=str)}
                cursor += 1

            while True:
                if await request.is_disconnected():
                    return

                # Drain new events
                while cursor < len(entry.events):
                    ev = entry.events[cursor]
                    cursor += 1
                    yield {"event": "heartbeat", "data": json.dumps(ev, default=str)}
                    last_activity = time.monotonic()

                # Wait for new event OR timeout
                try:
                    import asyncio
                    await asyncio.wait_for(entry.new_event.wait(), timeout=15.0)
                except asyncio.TimeoutError:
                    pass

                now = time.monotonic()
                if now - last_keepalive >= 15.0:
                    yield {"event": "keepalive", "data": "{}"}
                    last_keepalive = now

                if now - last_activity >= 1800.0:
                    yield {"event": "idle_timeout", "data": "{}"}
                    return

        return EventSourceResponse(_gen())

    return router
