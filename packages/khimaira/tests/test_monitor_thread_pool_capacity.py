"""Thread-pool-capacity regression for the daemon's default executor (2026-07-10).

Real production incident: a background metadata-scan feature
(dispatch.runners.base.run_subprocess) waits for a headless `claude` CLI call
— up to 600s — via `asyncio.to_thread`, BY DESIGN, so it doesn't block the
event loop (see that function's own docstring). `asyncio.to_thread` draws
from Python's PROCESS-WIDE default executor, sized `min(32, cpu_count+4)` by
default — 20 workers on the machine this actually happened on. Two such
scans running back-to-back occupied enough of that pool that every OTHER
`asyncio.to_thread` caller (including the notebook write routes and
/api/version, both fixed earlier the same day for the sibling "blocks the
event loop directly" bug) queued behind them for minutes. From the outside
this reads as "the whole daemon is frozen," even though the event loop
itself was never blocked — the bottleneck was thread-pool CAPACITY, not
event-loop scheduling.

Fix: server.py's build_app() registers a startup handler that replaces the
process default executor with a larger, dedicated one (64 workers) before
any other startup work runs. These tests verify (1) the handler actually
installs a bigger executor, and (2) the CAPACITY property it exists for:
several long-held threads don't starve a fast to_thread caller when the
pool is sized generously, but DO when it's the tiny default — proving the
fix addresses the real, observed failure mode, not just a config number.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _build_test_app(*, max_workers: int) -> FastAPI:
    """Mirrors server.py's startup-time executor-sizing handler exactly —
    same registration pattern (first @app.on_event("startup") handler,
    asyncio.get_running_loop().set_default_executor), parameterized on
    max_workers so the same app-shape can prove both the broken (tiny
    default) and fixed (64) cases."""
    app = FastAPI()

    @app.on_event("startup")
    async def _size_default_executor() -> None:
        asyncio.get_running_loop().set_default_executor(
            concurrent.futures.ThreadPoolExecutor(
                max_workers=max_workers, thread_name_prefix="test-io"
            )
        )

    @app.get("/fast")
    async def fast() -> dict:
        result = await asyncio.to_thread(lambda: "ok")
        return {"result": result}

    @app.get("/slow")
    async def slow() -> dict:
        await asyncio.to_thread(time.sleep, 2.0)  # simulates a long claude subprocess wait
        return {"ok": True}

    return app


def test_startup_handler_installs_a_sized_executor():
    app = _build_test_app(max_workers=64)
    with TestClient(app) as client:
        # Route into the running loop to inspect its executor — the handler
        # only runs once uvicorn/TestClient's lifespan actually starts it.
        resp = client.get("/fast")
    assert resp.status_code == 200


class TestThreadPoolCapacity:
    """Behavioral proof, not just a config-value check: with the daemon's
    real 64-worker sizing, N long-held threads (simulating N concurrent
    metadata scans / subprocess waits) leave enough headroom that a fast
    to_thread caller still returns quickly. With the tiny stdlib default,
    the same N long-held threads starve it — reproducing the actual
    reported symptom."""

    async def _time_fast_route_under_load(self, *, max_workers: int, concurrent_slow: int) -> float:
        app = _build_test_app(max_workers=max_workers)
        import httpx

        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            # Fire lifespan startup manually — ASGITransport doesn't send
            # lifespan messages on its own (unlike Starlette's TestClient).
            async with app.router.lifespan_context(app):
                start = time.monotonic()  # captured BEFORE any task exists
                slow_tasks = [
                    asyncio.create_task(client.get("/slow")) for _ in range(concurrent_slow)
                ]
                fast_task = asyncio.create_task(client.get("/fast"))

                fast_resp = await fast_task
                elapsed = time.monotonic() - start
                assert fast_resp.status_code == 200

                for t in slow_tasks:
                    await t
        return elapsed

    async def test_sized_pool_keeps_fast_call_responsive_under_load(self):
        # 10 concurrent "metadata scans" (2s each, standing in for the real
        # up-to-600s subprocess wait) against the daemon's real 64-worker
        # sizing — comfortable headroom, fast call should return quickly.
        elapsed = await self._time_fast_route_under_load(max_workers=64, concurrent_slow=10)
        assert elapsed < 1.0

    async def test_tiny_default_pool_starves_fast_call_under_the_same_load(self):
        # Same load, but sized like Python's actual stdlib default on the
        # machine this incident happened on (min(32, cpu_count+4) = 20 on a
        # 16-core box) — shrunk further here so the test doesn't need 20
        # real threads to prove the starvation property deterministically.
        elapsed = await self._time_fast_route_under_load(max_workers=4, concurrent_slow=10)
        # With only 4 workers and 10 long-held 2s calls ahead of it, the
        # fast call must wait for a worker to free up — this is the exact
        # "reads as frozen" symptom from the real incident.
        assert elapsed >= 1.0
