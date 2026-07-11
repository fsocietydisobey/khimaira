"""Event-loop-blocking regression for `/api/version` (2026-07-10).

`build_app()` (server.py) does real filesystem project-discovery at
construction time and isn't otherwise exercised by any existing test — too
heavy to invoke here just to reach one route. This mirrors the real
`/api/version` handler exactly (same `deploy_fingerprint` module, same
`asyncio.to_thread` wrapping) in a minimal standalone app, so the fix under
test is the real production code path (`deploy_fingerprint.code_fingerprint`),
not a reimplementation of it.

Reported symptom: `/api/version` — meant to be a trivial "what code is
running" check — took 93+ seconds to respond, then hung outright, and took
the whole daemon down with it (chat routes included), because
`code_fingerprint()` shells out to two real `git` subprocess calls plus a
full directory walk, SYNCHRONOUSLY, directly on the shared event loop. Any
other request queues behind it for however long git takes — and git can be
slow under real contention (this repo saw exactly that: many `git commit`/
`push` cycles running in parallel with this same endpoint being polled).
"""

from __future__ import annotations

import asyncio
import time

import httpx
import pytest
from fastapi import FastAPI

from khimaira.monitor import deploy_fingerprint as _deploy_fp


def _build_test_app() -> FastAPI:
    """Faithful mirror of server.py's `/api/version` registration — same
    module, same asyncio.to_thread wrapping as the real fix."""
    app = FastAPI()
    boot_fingerprint = _deploy_fp.code_fingerprint()

    @app.get("/api/version")
    async def version() -> dict:
        current = await asyncio.to_thread(_deploy_fp.code_fingerprint)
        return {
            "boot": boot_fingerprint,
            "current": current,
            "stale": _deploy_fp.is_stale(boot_fingerprint, current),
        }

    @app.get("/fast")
    async def fast() -> dict:
        return {"ok": True}

    return app


class TestVersionRouteNotBlocking:
    """Methodology note (carried over from test_notebook_api.py's
    TestEventLoopNotBlocked): the timer must start BEFORE either task is
    created, and both tasks must be created back-to-back with no
    intervening `await` — an `await asyncio.sleep()` "head start" is
    unsafe here, since its own resumption also needs the event loop, so if
    the slow call grabs the thread first, the head-start sleep doesn't
    return until the block already cleared, and the timer starts AFTER
    the blocking already happened. Verified this exact false-pass mode
    with a minimal repro before trusting this pattern earlier today."""

    async def test_slow_code_fingerprint_does_not_stall_concurrent_request(self, monkeypatch):
        def slow_fingerprint():
            time.sleep(0.5)  # a REAL blocking sleep — simulates git under contention
            return {"git_sha": "deadbeef", "git_dirty": False, "source_mtime": 0.0}

        monkeypatch.setattr(_deploy_fp, "code_fingerprint", slow_fingerprint)
        app = _build_test_app()
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            fast_start = time.monotonic()  # captured BEFORE either task exists
            slow_task = asyncio.create_task(client.get("/api/version"))
            fast_task = asyncio.create_task(client.get("/fast"))

            fast_resp = await fast_task
            fast_elapsed = time.monotonic() - fast_start

            assert fast_resp.status_code == 200
            # The whole point: a concurrent request must return in well
            # under the slow route's 0.5s sleep — if the event loop were
            # blocked (the pre-fix bug), this would queue behind it instead.
            assert fast_elapsed < 0.3

            slow_resp = await slow_task
            assert slow_resp.status_code == 200

    async def test_version_route_returns_real_fingerprint_shape(self):
        """Sanity check the mirror actually calls the real function (not a
        stub) — same keys the live daemon's /api/version reports."""
        app = _build_test_app()
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/api/version")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body["current"].keys()) == {"git_sha", "git_dirty", "source_mtime"}
