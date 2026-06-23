"""Tests for khimaira.proxy.server.

ALL throttle-logic + FLAG-1 + FLAG-2 AIMD tests run against a MOCK injectable
upstream (controllable fake-Anthropic) — NOT the real Anthropic API.
Testing 429-handling against the real API would reproduce the exact throttle
bug this proxy fixes + is non-deterministic (can't inject exact responses).

The REAL API is only used for passthrough/streaming smoke-tests, which are
NOT included here (they require a live API key + network).

Test categories:
  - Transparent header/body passthrough
  - Cross-session concurrency-cap (shared semaphore)
  - Adaptive-retry (Retry-After + jitter)
  - FLAG-1 never-crash → degrade-to-pass-through on self-errors
  - FLAG-2 conservative-N + AIMD (multiplicative decrease, additive increase)
  - Instrumentation (metrics endpoint, in-flight-at-429 logging)
  - Streaming slot-hold (semaphore released only after stream drains)
  - Load-test ~32 concurrent against mock (cap holds, 429s absorbed)
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from httpx import AsyncClient

# ---------------------------------------------------------------------------
# Helpers — injectable mock upstream
# ---------------------------------------------------------------------------


def _make_response(
    status: int = 200,
    body: bytes = b'{"type":"message","content":[]}',
    headers: dict | None = None,
) -> httpx.Response:
    """Build a fake httpx.Response for mocking."""
    h = {"content-type": "application/json"}
    if headers:
        h.update(headers)
    return httpx.Response(status_code=status, content=body, headers=h)


def _make_stream_response(
    status: int = 200,
    chunks: list[bytes] | None = None,
) -> AsyncMock:
    """Build a fake streaming httpx.Response context-manager."""
    chunks = chunks or [b"data: {}\n\n", b"data: [DONE]\n\n"]

    async def _aiter_bytes() -> AsyncIterator[bytes]:
        for chunk in chunks:
            yield chunk

    resp = AsyncMock()
    resp.status_code = status
    resp.headers = httpx.Headers({"content-type": "text/event-stream"})
    resp.aiter_bytes = _aiter_bytes
    resp.__aenter__ = AsyncMock(return_value=resp)
    resp.__aexit__ = AsyncMock(return_value=False)
    return resp


# ---------------------------------------------------------------------------
# App fixture — reset module-level state for each test
# ---------------------------------------------------------------------------


@pytest.fixture()
def proxy_app():
    """Return fresh proxy server module + TestClient for each test."""
    # Re-import to get a fresh module state
    import importlib

    import khimaira.proxy.server as srv

    importlib.reload(srv)
    return srv


@pytest.fixture()
def client(proxy_app):
    """TestClient that exercises the FastAPI lifespan."""
    with TestClient(proxy_app.app, raise_server_exceptions=False) as c:
        yield c, proxy_app


# ---------------------------------------------------------------------------
# 1. Transparent passthrough — headers + body forwarded correctly
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_passthrough_headers_forwarded(proxy_app):
    """Authorization, anthropic-version, anthropic-beta, X-Claude-Code-Session-Id
    are forwarded to upstream; other headers are NOT."""
    captured = {}

    async def _fake_request(method, path, *, headers, content, **kw):
        captured["headers"] = dict(headers)
        return _make_response()

    proxy_app._client = AsyncMock()
    proxy_app._client.request = _fake_request
    proxy_app._controller = proxy_app._ConcurrencyController(10)

    req = MagicMock()
    req.headers = {
        "authorization": "Bearer sk-test",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "tools-2024-04-04",
        "x-claude-code-session-id": "sess-abc",
        "content-type": "application/json",
        "x-should-not-forward": "secret",
    }
    req.body = AsyncMock(return_value=b'{"model":"claude-3-5-sonnet-20241022","max_tokens":10,"messages":[]}')

    resp = await proxy_app._handle_proxy(req, "POST", "/v1/messages")

    assert "authorization" in captured["headers"]
    assert captured["headers"]["authorization"] == "Bearer sk-test"
    assert "anthropic-version" in captured["headers"]
    assert "anthropic-beta" in captured["headers"]
    assert "x-claude-code-session-id" in captured["headers"]
    assert "x-should-not-forward" not in captured["headers"]


# ---------------------------------------------------------------------------
# 2. Concurrency cap — shared semaphore blocks excess requests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrency_cap_enforced():
    """At most N requests are in-flight simultaneously."""
    import khimaira.proxy.server as srv

    N = 3
    controller = srv._ConcurrencyController(N)
    max_in_flight = 0
    lock = asyncio.Lock()

    async def _task():
        nonlocal max_in_flight
        await controller.acquire()
        async with lock:
            max_in_flight = max(max_in_flight, controller.in_flight)
        await asyncio.sleep(0.05)
        await controller.release()

    await asyncio.gather(*[_task() for _ in range(N * 3)])
    assert max_in_flight <= N


@pytest.mark.asyncio
async def test_concurrency_cap_queues_excess():
    """Excess requests queue and proceed after slots free up."""
    import khimaira.proxy.server as srv

    N = 2
    controller = srv._ConcurrencyController(N)
    completed = []

    async def _task(i: int):
        await controller.acquire()
        await asyncio.sleep(0.02)
        completed.append(i)
        await controller.release()

    # 5 tasks, only 2 slots → all must complete (none dropped)
    await asyncio.gather(*[_task(i) for i in range(5)])
    assert len(completed) == 5


# ---------------------------------------------------------------------------
# 3. Adaptive retry — Retry-After honored, jitter applied
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_retry_after_honored():
    """A 429 with Retry-After causes the proxy to wait (mocked sleep) + retry."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(10)
    call_count = 0
    sleep_delays = []

    original_sleep = asyncio.sleep

    async def _fake_sleep(delay):
        sleep_delays.append(delay)

    async def _fake_upstream(method, path, *, headers, content, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(429, b"rate limited", {"retry-after": "2.5"})
        return _make_response(200, b'{"ok":true}')

    with patch.object(srv, "_controller", controller), \
         patch.object(srv, "_client", AsyncMock(request=_fake_upstream)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        result = await srv._upstream_with_retry("POST", "/v1/messages", {}, b"{}", "sess-1")

    assert call_count == 2
    assert result.status_code == 200
    # Sleep should have been called with the Retry-After value (capped at 30)
    assert any(abs(d - 2.5) < 0.1 for d in sleep_delays), f"sleep delays: {sleep_delays}"


@pytest.mark.asyncio
async def test_retry_jitter_applied_without_retry_after():
    """Without Retry-After, jittered backoff is used (always > 0, within bounds)."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(10)
    call_count = 0
    sleep_delays = []

    async def _fake_sleep(delay):
        sleep_delays.append(delay)

    async def _fake_upstream(method, path, *, headers, content, **kw):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return _make_response(429, b"rate limited")
        return _make_response(200, b'{"ok":true}')

    with patch.object(srv, "_controller", controller), \
         patch.object(srv, "_client", AsyncMock(request=_fake_upstream)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        result = await srv._upstream_with_retry("POST", "/v1/messages", {}, b"{}", "sess-1")

    assert result.status_code == 200
    assert all(0 < d <= srv._RETRY_MAX_DELAY_S for d in sleep_delays), f"delays: {sleep_delays}"


@pytest.mark.asyncio
async def test_slot_released_during_retry_backoff():
    """The concurrency slot is released during retry backoff so others can proceed."""
    import khimaira.proxy.server as srv

    N = 1
    controller = srv._ConcurrencyController(N)
    slot_available_during_backoff = False
    call_count = 0

    async def _fake_sleep(delay):
        nonlocal slot_available_during_backoff
        # During the backoff sleep the slot should have been released
        # → another acquisition should succeed immediately
        slot_available_during_backoff = controller.in_flight == 0

    async def _fake_upstream(method, path, *, headers, content, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_response(429, b"rate limited")
        return _make_response(200, b'{"ok":true}')

    with patch.object(srv, "_controller", controller), \
         patch.object(srv, "_client", AsyncMock(request=_fake_upstream)), \
         patch("asyncio.sleep", side_effect=_fake_sleep):
        await controller.acquire()
        await srv._upstream_with_retry("POST", "/v1/messages", {}, b"{}", "sess-1")

    assert slot_available_during_backoff


# ---------------------------------------------------------------------------
# 4. FLAG-1 — never-crash → degrade-to-pass-through on self-errors
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag1_throttle_error_degrades_to_passthrough():
    """If throttle logic raises ANY error, proxy falls through to pass-through.

    The proxy must NOT return 500 or raise — it degrades gracefully.
    """
    import khimaira.proxy.server as srv

    async def _boom_acquire():
        raise RuntimeError("injected semaphore fault")

    passthrough_called = False

    async def _fake_passthrough_response(request, method, path, body):
        nonlocal passthrough_called
        passthrough_called = True
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})

    req = MagicMock()
    req.headers = {"content-type": "application/json", "x-claude-code-session-id": "sess-1"}
    req.body = AsyncMock(return_value=b'{"model":"claude","max_tokens":1,"messages":[]}')

    bad_controller = AsyncMock()
    bad_controller.acquire = _boom_acquire

    with patch.object(srv, "_controller", bad_controller), \
         patch.object(srv, "_passthrough_response", side_effect=_fake_passthrough_response):
        response = await srv._handle_proxy(req, "POST", "/v1/messages")

    assert passthrough_called, "Expected pass-through to be called after throttle failure"


@pytest.mark.asyncio
async def test_flag1_passthrough_on_upstream_connection_error():
    """Network errors in throttled path degrade to pass-through (not 500)."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(10)
    call_count = 0

    async def _flaky_upstream(method, path, *, headers, content, **kw):
        nonlocal call_count
        call_count += 1
        raise httpx.ConnectError("injected connection error")

    passthrough_called = False

    async def _fake_passthrough(request, method, path, body):
        nonlocal passthrough_called
        passthrough_called = True
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})

    req = MagicMock()
    req.headers = {"content-type": "application/json"}
    req.body = AsyncMock(return_value=b'{"model":"claude","max_tokens":1,"messages":[]}')

    with patch.object(srv, "_controller", controller), \
         patch.object(srv, "_client", AsyncMock(request=_flaky_upstream)), \
         patch("asyncio.sleep", AsyncMock()), \
         patch.object(srv, "_passthrough_response", side_effect=_fake_passthrough):
        response = await srv._handle_proxy(req, "POST", "/v1/messages")

    assert passthrough_called


# ---------------------------------------------------------------------------
# 5. FLAG-2 — AIMD: multiplicative decrease on 429, additive increase when clean
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_aimd_decrease_on_429():
    """N decreases multiplicatively on 429."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(8)
    assert controller.current_n == 8

    await controller.on_429("sess-test")
    new_n = controller.current_n
    # 8 * 0.75 = 6
    assert new_n == 6, f"Expected N=6 after decrease from 8, got {new_n}"


@pytest.mark.asyncio
async def test_aimd_decrease_floors_at_n_min():
    """N never goes below N_MIN regardless of how many 429s."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(srv._N_MIN + 1)
    for _ in range(20):
        await controller.on_429("sess-test")

    assert controller.current_n >= srv._N_MIN


@pytest.mark.asyncio
async def test_aimd_increase_after_clean_window():
    """N increases by 1 after a clean window with no 429s."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(4)
    await controller.on_429("sess-test")
    n_after_decrease = controller.current_n  # should be 3

    # Simulate the clean-window condition
    controller._last_429_ts = time.monotonic() - (srv._AIMD_INCREASE_INTERVAL_S + 1)
    controller._last_increase_ts = time.monotonic() - (srv._AIMD_INCREASE_INTERVAL_S + 1)

    await controller.maybe_increase()
    assert controller.current_n == n_after_decrease + 1


@pytest.mark.asyncio
async def test_aimd_no_increase_before_clean_window():
    """N does NOT increase during the cool-down window after a 429."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(4)
    await controller.on_429("sess-test")
    n_after_decrease = controller.current_n

    # Recent 429 → should NOT increase
    controller._last_429_ts = time.monotonic() - 1.0  # recent
    await controller.maybe_increase()
    assert controller.current_n == n_after_decrease


@pytest.mark.asyncio
async def test_flag2_n_is_configurable_not_hardcoded():
    """N is controlled by env/config, not hardcoded — KHIMAIRA_PROXY_N works."""
    import importlib
    import os

    with patch.dict(os.environ, {"KHIMAIRA_PROXY_N": "12"}):
        import khimaira.proxy.server as srv2

        importlib.reload(srv2)
        assert srv2._N_DEFAULT == 12


# ---------------------------------------------------------------------------
# 6. Instrumentation — in-flight-at-429 logged, metrics endpoint
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_in_flight_logged_at_429(caplog):
    """When a 429 fires, in-flight count is logged at WARNING level."""
    import logging

    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(10)
    # Simulate 3 in-flight requests
    for _ in range(3):
        await controller.acquire()

    with caplog.at_level(logging.WARNING, logger="khimaira.proxy.server"):
        await controller.on_429("sess-abc")

    assert any("429" in r.message for r in caplog.records)
    assert any("in_flight_at_trip=3" in r.message for r in caplog.records)


def test_metrics_endpoint_returns_data(client):
    """GET /metrics returns the AIMD instrumentation dict."""
    c, srv = client
    resp = c.get("/metrics")
    assert resp.status_code == 200
    data = resp.json()
    assert "current_n" in data
    assert "in_flight" in data
    assert "total_requests" in data
    assert "total_429s" in data


def test_health_endpoint(client):
    """GET /health returns status ok + upstream."""
    c, srv = client
    resp = c.get("/health")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"


# ---------------------------------------------------------------------------
# 7. Streaming — slot held for full stream duration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_slot_held_during_stream():
    """The concurrency slot is NOT released until the stream fully drains."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(2)
    stream_in_progress = asyncio.Event()
    stream_can_finish = asyncio.Event()
    slot_count_during_stream = []

    chunks_delivered = []

    class _FakeCM:
        def __init__(self):
            self.status_code = 200
            self.headers = httpx.Headers({"content-type": "text/event-stream"})

        def aiter_bytes(self):
            async def _gen():
                stream_in_progress.set()
                await stream_can_finish.wait()
                yield b"data: done\n\n"
            return _gen()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    def _fake_stream_cm(method, path, *, headers, content, **kw):
        return _FakeCM()

    mock_client = MagicMock()
    mock_client.stream = _fake_stream_cm

    body = b'{"model":"claude","max_tokens":1,"messages":[],"stream":true}'
    headers = {"content-type": "application/json", "authorization": "Bearer sk-test"}

    with patch.object(srv, "_controller", controller), \
         patch.object(srv, "_client", mock_client):
        stream_resp = await srv._throttled_stream("/v1/messages", headers, body, "sess-1")

        # Start consuming the stream in the background
        async def _consume():
            async for chunk in stream_resp.body_iterator:
                chunks_delivered.append(chunk)

        consume_task = asyncio.create_task(_consume())

        # Wait until the stream is in progress, then check slot is still held
        await asyncio.wait_for(stream_in_progress.wait(), timeout=2.0)
        # Slot should still be held (in_flight == 1 for our task)
        assert controller.in_flight >= 1

        # Allow stream to finish
        stream_can_finish.set()
        await consume_task

    # After stream completes, slot is released
    assert controller.in_flight == 0
    assert chunks_delivered  # stream delivered content


# ---------------------------------------------------------------------------
# 8. Load-test ~32 concurrent (mock upstream) — cap holds, 429s absorbed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag1_streaming_degrade_to_passthrough():
    """If throttled_stream raises, proxy falls through to _passthrough_stream (FLAG-1)."""
    import khimaira.proxy.server as srv

    async def _boom_stream(path, headers, body, session_id):
        raise RuntimeError("injected stream throttle fault")

    passthrough_called = False

    async def _fake_passthrough_stream(request, path, body):
        nonlocal passthrough_called
        passthrough_called = True
        from fastapi.responses import StreamingResponse as SR
        return SR(iter([]), media_type="text/event-stream")

    req = MagicMock()
    req.headers = {"content-type": "application/json", "authorization": "Bearer sk-test"}
    req.body = AsyncMock(return_value=b'{"model":"claude","max_tokens":1,"messages":[],"stream":true}')

    with patch.object(srv, "_throttled_stream", side_effect=_boom_stream), \
         patch.object(srv, "_passthrough_stream", side_effect=_fake_passthrough_stream):
        response = await srv._handle_proxy(req, "POST", "/v1/messages")

    assert passthrough_called, "Expected streaming FLAG-1 degrade to _passthrough_stream"


@pytest.mark.asyncio
async def test_streaming_acquire_inside_gen_no_slot_if_never_iterated():
    """If _gen() is never consumed, no slot is acquired (acquire-inside-gen by construction)."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(2)
    headers = {"content-type": "application/json", "authorization": "Bearer sk-test"}
    body = b'{"model":"claude","max_tokens":1,"messages":[],"stream":true}'

    mock_client = MagicMock()

    with patch.object(srv, "_controller", controller), \
         patch.object(srv, "_client", mock_client):
        # Build the StreamingResponse but never consume the body_iterator
        stream_resp = await srv._throttled_stream("/v1/messages", headers, body, "sess-1")
        # Generator was created but never iterated
        assert controller.in_flight == 0, "Slot should NOT be acquired if _gen is never iterated"


@pytest.mark.asyncio
async def test_load_32_concurrent_cap_holds():
    """Under 32 concurrent requests to a mock upstream, cap holds and all succeed."""
    import khimaira.proxy.server as srv

    N = 6
    controller = srv._ConcurrencyController(N)
    peak_in_flight = 0
    lock = asyncio.Lock()
    successes = 0

    async def _slow_upstream(method, path, *, headers, content, **kw):
        async with lock:
            nonlocal peak_in_flight
            peak_in_flight = max(peak_in_flight, controller.in_flight)
        await asyncio.sleep(0.01)  # simulate network latency
        return _make_response(200, b'{"ok":true}')

    body = b'{"model":"claude","max_tokens":1,"messages":[]}'
    headers = {"content-type": "application/json", "authorization": "Bearer sk-test"}

    async def _one_request(i: int):
        nonlocal successes
        with patch.object(srv, "_controller", controller), \
             patch.object(srv, "_client", AsyncMock(request=_slow_upstream)):
            resp = await srv._throttled_non_stream("POST", "/v1/messages", headers, body, f"sess-{i}")
            if resp.status_code == 200:
                successes += 1

    await asyncio.gather(*[_one_request(i) for i in range(32)])

    assert peak_in_flight <= N, f"Peak in-flight {peak_in_flight} exceeded cap {N}"
    assert successes == 32, f"Expected all 32 to succeed, got {successes}"


@pytest.mark.asyncio
async def test_load_32_concurrent_429s_absorbed():
    """429 responses from upstream are absorbed and retried (never surface)."""
    import khimaira.proxy.server as srv

    N = 6
    controller = srv._ConcurrencyController(N)
    call_count = 0
    lock = asyncio.Lock()
    final_statuses = []

    async def _429_then_200(method, path, *, headers, content, **kw):
        nonlocal call_count
        async with lock:
            call_count += 1
            local_count = call_count
        # First call per request returns 429; subsequent calls return 200
        # (in practice, each session's request retries)
        if local_count % 3 == 1:
            return _make_response(429, b"rate limited")
        return _make_response(200, b'{"ok":true}')

    body = b'{"model":"claude","max_tokens":1,"messages":[]}'
    headers = {"content-type": "application/json", "authorization": "Bearer sk-test"}

    async def _one_request(i: int):
        with patch.object(srv, "_controller", controller), \
             patch.object(srv, "_client", AsyncMock(request=_429_then_200)), \
             patch("asyncio.sleep", AsyncMock()):
            resp = await srv._throttled_non_stream("POST", "/v1/messages", headers, body, f"sess-{i}")
            final_statuses.append(resp.status_code)

    await asyncio.gather(*[_one_request(i) for i in range(32)])

    # All requests should eventually succeed (429s retried, not surfaced)
    success_count = sum(1 for s in final_statuses if s == 200)
    assert success_count >= 28, f"Expected most requests to succeed after retry, got {success_count}/32"


@pytest.mark.asyncio
async def test_load_32_fault_injection_never_crash():
    """Under 32 concurrent with injected faults, proxy degrades to pass-through (no crash)."""
    import khimaira.proxy.server as srv

    N = 6
    controller = srv._ConcurrencyController(N)
    passthrough_count = 0
    lock = asyncio.Lock()

    async def _boom_upstream(method, path, *, headers, content, **kw):
        raise RuntimeError("injected upstream fault")

    async def _fake_passthrough(request, method, path, body):
        nonlocal passthrough_count
        async with lock:
            passthrough_count += 1
        from fastapi.responses import JSONResponse
        return JSONResponse({"ok": True})

    body = b'{"model":"claude","max_tokens":1,"messages":[]}'

    async def _one_request(i: int):
        req = MagicMock()
        req.headers = {"content-type": "application/json"}
        req.body = AsyncMock(return_value=body)

        with patch.object(srv, "_controller", controller), \
             patch.object(srv, "_client", AsyncMock(request=_boom_upstream)), \
             patch("asyncio.sleep", AsyncMock()), \
             patch.object(srv, "_passthrough_response", side_effect=_fake_passthrough):
            resp = await srv._handle_proxy(req, "POST", "/v1/messages")
            # Must not raise — response must be returned

    await asyncio.gather(*[_one_request(i) for i in range(32)])
    # All 32 requests should have degraded to pass-through (FLAG-1)
    assert passthrough_count == 32


# ---------------------------------------------------------------------------
# Account failover — switch to a backup account on a sustained cap
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_failover_to_backup_on_sustained_429():
    """A 429 that survives all retries on the primary token → swap to the backup
    account's token, retry once, return its 200. Window is opened."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(10)

    async def _fake_upstream(method, path, *, headers, content, **kw):
        auth = next((v for k, v in headers.items() if k.lower() == "authorization"), "")
        if auth == "Bearer BK":
            return _make_response(200, b'{"ok":true}')
        return _make_response(429, b"weekly limit", {"retry-after": "120"})

    srv._primary_capped_until = 0.0
    srv._failover_events = 0

    async def _noop_sleep(d):
        pass

    with patch.object(srv, "_controller", controller), \
         patch.object(srv, "_client", AsyncMock(request=_fake_upstream)), \
         patch.object(srv, "_backup_bearer", lambda: "BK"), \
         patch.object(srv, "_FAILOVER_ENABLED", True), \
         patch.object(srv, "_MAX_RETRIES", 1), \
         patch("asyncio.sleep", side_effect=_noop_sleep):
        headers = {"authorization": "Bearer PRIMARY", "content-type": "application/json"}
        result = await srv._upstream_with_retry("POST", "/v1/messages", headers, b"{}", "s1")
        # assert INSIDE the patch scope: _FAILOVER_ENABLED still True, and the
        # global _primary_capped_until that _enter_failover set isn't restored yet.
        assert result.status_code == 200  # failed over to backup
        assert srv._in_failover_window() is True  # cap window opened
        assert srv._failover_events == 1

    srv._primary_capped_until = 0.0  # don't leak window state to other tests


@pytest.mark.asyncio
async def test_no_failover_without_backup_token():
    """No backup token → failover no-ops, the primary 429 surfaces (fail-open)."""
    import khimaira.proxy.server as srv

    controller = srv._ConcurrencyController(10)

    async def _fake_upstream(method, path, *, headers, content, **kw):
        return _make_response(429, b"limit", {"retry-after": "10"})

    with patch.object(srv, "_controller", controller), \
         patch.object(srv, "_client", AsyncMock(request=_fake_upstream)), \
         patch.object(srv, "_backup_bearer", lambda: None), \
         patch.object(srv, "_FAILOVER_ENABLED", True), \
         patch.object(srv, "_primary_capped_until", 0.0), \
         patch.object(srv, "_MAX_RETRIES", 1), \
         patch("asyncio.sleep", side_effect=lambda d: asyncio.sleep(0)):
        result = await srv._upstream_with_retry("POST", "/v1/messages", {}, b"{}", "s1")

    assert result.status_code == 429  # no backup → surfaces, no worse than before


def test_with_backup_auth_swaps_only_authorization():
    """_with_backup_auth replaces ONLY the Authorization header, keeps the rest."""
    import khimaira.proxy.server as srv

    with patch.object(srv, "_backup_bearer", lambda: "BK"):
        out = srv._with_backup_auth(
            {"authorization": "Bearer PRIMARY", "anthropic-version": "2023-06-01"}
        )
    assert out["authorization"] == "Bearer BK"
    assert out["anthropic-version"] == "2023-06-01"
    # and None when no backup
    with patch.object(srv, "_backup_bearer", lambda: None):
        assert srv._with_backup_auth({"authorization": "x"}) is None


def test_on_backup_already_detects_backup_headers():
    """A 429 while already on the backup must NOT re-trigger failover."""
    import khimaira.proxy.server as srv

    with patch.object(srv, "_backup_bearer", lambda: "BK"):
        assert srv._on_backup_already({"authorization": "Bearer BK"}) is True
        assert srv._on_backup_already({"authorization": "Bearer PRIMARY"}) is False
