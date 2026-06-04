"""Anthropic concurrency-proxy server.

A local reverse-proxy that sits between the ~32 Claude Code sessions and
the Anthropic API. Adds the cross-session coordination the CLI structurally
lacks:

  1. Cross-session concurrency-cap — one shared asyncio.Condition guard
     across ALL sessions; ≤N concurrent upstream requests, excess QUEUE.
  2. Adaptive retry — honor Retry-After + jittered exponential backoff;
     CLI 429s never surface to the session.

FLAG-1 (NEVER-CRASH → DEGRADE-TO-PASS-THROUGH): every throttle branch is
wrapped best-effort; ANY self-error falls through to a dumb direct-forward.
A crashed proxy = connection-refused for all 32 sessions, worse than the
storm — so the proxy NEVER crashes on bad input or internal errors.

FLAG-2 (CONSERVATIVE-N + AIMD): N starts LOW (env-configurable default=6),
instruments in-flight-count at each 429, and uses AIMD to adapt:
  - Multiplicative decrease on 429-rate rise (N *= 0.75, floor N_MIN)
  - Additive increase when clean for AIMD_INCREASE_INTERVAL_S (N += 1, cap N_MAX)

See `khimaira proxy --help` for usage.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from typing import AsyncIterator

import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (all env-tunable, never hardcoded)
# ---------------------------------------------------------------------------

_ANTHROPIC_API_BASE = os.environ.get(
    "KHIMAIRA_PROXY_UPSTREAM", "https://api.anthropic.com"
)

_N_DEFAULT = int(os.environ.get("KHIMAIRA_PROXY_N", "6"))  # conservative start
_N_MIN = max(1, int(os.environ.get("KHIMAIRA_PROXY_N_MIN", "2")))
_N_MAX = int(os.environ.get("KHIMAIRA_PROXY_N_MAX", "32"))
_AIMD_DECREASE_FACTOR = 0.75
_AIMD_INCREASE_INTERVAL_S = float(os.environ.get("KHIMAIRA_PROXY_AIMD_INTERVAL", "30"))

_MAX_RETRIES = int(os.environ.get("KHIMAIRA_PROXY_MAX_RETRIES", "5"))
_RETRY_BASE_DELAY_S = 1.0
_RETRY_MAX_DELAY_S = 30.0
_RETRY_AFTER_CAP_S = 30.0  # cap on Retry-After header value

# Request headers to forward to upstream
_FORWARD_REQ_HEADERS = frozenset(
    {
        "authorization",
        "x-api-key",
        "anthropic-version",
        "anthropic-beta",
        "x-claude-code-session-id",
        "content-type",
        "accept",
    }
)

# Response headers to forward back to client
_FORWARD_RESP_HEADERS = frozenset(
    {
        "content-type",
        "anthropic-request-id",
        "request-id",
    }
)


# ---------------------------------------------------------------------------
# Concurrency controller (AIMD)
# ---------------------------------------------------------------------------


class _ConcurrencyController:
    """Cross-session concurrency cap with AIMD adaptation.

    Uses an asyncio.Condition over an in_flight counter (instead of
    asyncio.Semaphore) so that N can be adjusted dynamically without
    blocking — waiters re-evaluate the condition whenever N changes.
    """

    def __init__(self, initial_n: int) -> None:
        self._n = max(_N_MIN, min(_N_MAX, initial_n))
        self._in_flight: int = 0
        self._cond = asyncio.Condition()
        # AIMD state
        self._last_429_ts: float = 0.0
        self._last_increase_ts: float = time.monotonic()
        self._total_429s: int = 0
        self._total_requests: int = 0

    @property
    def current_n(self) -> int:
        return self._n

    @property
    def in_flight(self) -> int:
        return self._in_flight

    async def acquire(self) -> None:
        async with self._cond:
            while self._in_flight >= self._n:
                await self._cond.wait()
            self._in_flight += 1
            self._total_requests += 1

    async def release(self) -> None:
        async with self._cond:
            self._in_flight = max(0, self._in_flight - 1)
            self._cond.notify_all()

    async def on_429(self, session_id: str | None = None) -> None:
        """Record a 429; apply AIMD multiplicative decrease."""
        async with self._cond:
            self._total_429s += 1
            self._last_429_ts = time.monotonic()
            in_flight_at_trip = self._in_flight
            old_n = self._n
            self._n = max(_N_MIN, int(self._n * _AIMD_DECREASE_FACTOR))
            self._cond.notify_all()
        logger.warning(
            "proxy: 429 — session=%s in_flight_at_trip=%d old_N=%d new_N=%d",
            session_id or "?",
            in_flight_at_trip,
            old_n,
            self._n,
        )

    async def maybe_increase(self) -> None:
        """Additive increase if we've been clean for AIMD_INCREASE_INTERVAL_S."""
        now = time.monotonic()
        async with self._cond:
            if (
                self._n < _N_MAX
                and (now - self._last_429_ts) >= _AIMD_INCREASE_INTERVAL_S
                and (now - self._last_increase_ts) >= _AIMD_INCREASE_INTERVAL_S
            ):
                self._n = min(_N_MAX, self._n + 1)
                self._last_increase_ts = now
                logger.info("proxy: AIMD increase N → %d", self._n)

    def metrics(self) -> dict:
        return {
            "current_n": self._n,
            "in_flight": self._in_flight,
            "total_requests": self._total_requests,
            "total_429s": self._total_429s,
            "last_429_age_s": (
                round(time.monotonic() - self._last_429_ts, 1)
                if self._last_429_ts
                else None
            ),
        }


# ---------------------------------------------------------------------------
# Module-level state (initialised in lifespan)
# ---------------------------------------------------------------------------

_controller: _ConcurrencyController | None = None
_client: httpx.AsyncClient | None = None
_aimd_task: asyncio.Task | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _controller, _client, _aimd_task

    _controller = _ConcurrencyController(_N_DEFAULT)
    _client = httpx.AsyncClient(
        base_url=_ANTHROPIC_API_BASE,
        timeout=httpx.Timeout(connect=10.0, read=300.0, write=60.0, pool=10.0),
        follow_redirects=True,
    )

    async def _aimd_tick() -> None:
        while True:
            await asyncio.sleep(10)
            if _controller is not None:
                try:
                    await _controller.maybe_increase()
                except Exception:
                    pass

    _aimd_task = asyncio.create_task(_aimd_tick())
    logger.info(
        "proxy: started — upstream=%s N=%d port=see-server",
        _ANTHROPIC_API_BASE,
        _N_DEFAULT,
    )

    try:
        yield
    finally:
        if _aimd_task:
            _aimd_task.cancel()
        if _client:
            await _client.aclose()


app = FastAPI(title="khimaira-proxy", lifespan=_lifespan)


# ---------------------------------------------------------------------------
# Header utilities
# ---------------------------------------------------------------------------


def _extract_forward_headers(request: Request) -> dict[str, str]:
    """Pull the headers we forward to upstream."""
    return {
        k: v
        for k, v in request.headers.items()
        if k.lower() in _FORWARD_REQ_HEADERS
    }


def _extract_resp_headers(response: httpx.Response) -> dict[str, str]:
    return {
        k: v
        for k, v in response.headers.items()
        if k.lower() in _FORWARD_RESP_HEADERS
    }


# ---------------------------------------------------------------------------
# Upstream forwarding with retry
# ---------------------------------------------------------------------------


async def _direct_forward_bytes(
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
) -> httpx.Response:
    """Single upstream request; no retry, no throttle."""
    assert _client is not None
    return await _client.request(method, path, headers=headers, content=body)


async def _upstream_with_retry(
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    session_id: str | None,
) -> httpx.Response:
    """Forward with retry on 429/5xx, honoring Retry-After + jitter."""
    assert _controller is not None
    last_exc: Exception | None = None

    for attempt in range(_MAX_RETRIES + 1):
        try:
            resp = await _direct_forward_bytes(method, path, headers, body)
        except Exception as exc:
            last_exc = exc
            if attempt >= _MAX_RETRIES:
                raise
            delay = min(
                _RETRY_MAX_DELAY_S,
                _RETRY_BASE_DELAY_S * (1.5**attempt) * (0.5 + random.random()),
            )
            logger.warning(
                "proxy: network error attempt %d/%d (%s); retrying in %.1fs",
                attempt + 1,
                _MAX_RETRIES,
                exc,
                delay,
            )
            await asyncio.sleep(delay)
            continue

        if resp.status_code == 429 or resp.status_code == 529:
            await _controller.on_429(session_id)
            if attempt >= _MAX_RETRIES:
                return resp  # surface after max retries exhausted

            retry_after_raw = resp.headers.get("retry-after")
            if retry_after_raw:
                try:
                    delay = min(_RETRY_AFTER_CAP_S, float(retry_after_raw))
                except ValueError:
                    delay = _retry_jitter(attempt)
            else:
                delay = _retry_jitter(attempt)

            logger.info(
                "proxy: 429 attempt %d/%d; waiting %.1fs", attempt + 1, _MAX_RETRIES, delay
            )
            # Release the concurrency slot during backoff so other sessions can proceed
            await _controller.release()
            try:
                await asyncio.sleep(delay)
            finally:
                await _controller.acquire()

            continue

        return resp

    raise RuntimeError(f"proxy: all retries exhausted") if last_exc is None else last_exc


def _retry_jitter(attempt: int) -> float:
    return min(
        _RETRY_MAX_DELAY_S,
        _RETRY_BASE_DELAY_S * (1.5**attempt) * (0.5 + random.random()),
    )


# ---------------------------------------------------------------------------
# FLAG-1 helpers — never-crash degrade path
# ---------------------------------------------------------------------------


async def _passthrough_response(
    request: Request, method: str, path: str, body: bytes
) -> Response:
    """Dumb pass-through with no throttle — FLAG-1 degrade path."""
    headers = _extract_forward_headers(request)
    try:
        resp = await _direct_forward_bytes(method, path, headers, body)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(_extract_resp_headers(resp)),
        )
    except Exception as exc:
        logger.error("proxy: pass-through also failed: %s", exc)
        return JSONResponse(
            {"error": {"type": "proxy_error", "message": str(exc)}},
            status_code=503,
        )


async def _passthrough_stream(
    request: Request, path: str, body: bytes
) -> StreamingResponse:
    """Streaming pass-through with no throttle — FLAG-1 degrade path."""
    headers = _extract_forward_headers(request)
    assert _client is not None

    async def _gen() -> AsyncIterator[bytes]:
        try:
            async with _client.stream("POST", path, headers=headers, content=body) as resp:
                async for chunk in resp.aiter_bytes():
                    yield chunk
        except Exception as exc:
            logger.error("proxy: streaming pass-through error: %s", exc)

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Throttled proxy core
# ---------------------------------------------------------------------------


def _wants_stream(body: bytes) -> bool:
    """Check if the request body has stream=true."""
    try:
        data = json.loads(body)
        return bool(data.get("stream", False))
    except Exception:
        return False


async def _throttled_non_stream(
    method: str,
    path: str,
    headers: dict[str, str],
    body: bytes,
    session_id: str | None,
) -> Response:
    """Throttled + retried non-streaming request."""
    assert _controller is not None

    await _controller.acquire()
    try:
        resp = await _upstream_with_retry(method, path, headers, body, session_id)
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            headers=dict(_extract_resp_headers(resp)),
        )
    finally:
        await _controller.release()


async def _throttled_stream(
    path: str,
    headers: dict[str, str],
    body: bytes,
    session_id: str | None,
) -> StreamingResponse:
    """Throttled streaming request — semaphore slot held for full stream duration."""
    assert _controller is not None
    assert _client is not None

    await _controller.acquire()
    # Track whether we've already released so the finally-guard is idempotent.
    slot_released = False

    async def _gen() -> AsyncIterator[bytes]:
        nonlocal slot_released
        try:
            # Retry loop wraps the entire stream attempt; on 429 we re-acquire.
            for attempt in range(_MAX_RETRIES + 1):
                try:
                    async with _client.stream(
                        "POST", path, headers=headers, content=body
                    ) as resp:
                        if resp.status_code in (429, 529) and attempt < _MAX_RETRIES:
                            # 429 on a stream: drain nothing, retry with backoff
                            await _controller.on_429(session_id)
                            retry_after_raw = resp.headers.get("retry-after")
                            if retry_after_raw:
                                try:
                                    delay = min(_RETRY_AFTER_CAP_S, float(retry_after_raw))
                                except ValueError:
                                    delay = _retry_jitter(attempt)
                            else:
                                delay = _retry_jitter(attempt)

                            logger.info(
                                "proxy: stream 429 attempt %d/%d; waiting %.1fs",
                                attempt + 1,
                                _MAX_RETRIES,
                                delay,
                            )
                            await _controller.release()
                            slot_released = True
                            await asyncio.sleep(delay)
                            await _controller.acquire()
                            slot_released = False
                            continue

                        async for chunk in resp.aiter_bytes():
                            yield chunk
                        return  # success
                except Exception as exc:
                    if attempt >= _MAX_RETRIES:
                        logger.error("proxy: stream error after retries: %s", exc)
                        return
                    delay = _retry_jitter(attempt)
                    logger.warning(
                        "proxy: stream error attempt %d/%d (%s); retrying in %.1fs",
                        attempt + 1, _MAX_RETRIES, exc, delay,
                    )
                    await _controller.release()
                    slot_released = True
                    await asyncio.sleep(delay)
                    await _controller.acquire()
                    slot_released = False
        finally:
            if not slot_released:
                await _controller.release()
                slot_released = True

    return StreamingResponse(_gen(), media_type="text/event-stream")


# ---------------------------------------------------------------------------
# Route handlers
# ---------------------------------------------------------------------------


async def _handle_proxy(request: Request, method: str, path: str) -> Response:
    """Common handler for all proxied endpoints.

    FLAG-1: ALL throttle/retry logic is wrapped in try/except. ANY error
    falls through to a direct pass-through. The proxy NEVER returns 500 on
    a self-error.
    """
    body = await request.body()
    session_id = request.headers.get("x-claude-code-session-id")
    headers = _extract_forward_headers(request)
    is_stream = method == "POST" and _wants_stream(body)

    try:
        if is_stream:
            return await _throttled_stream(path, headers, body, session_id)
        else:
            return await _throttled_non_stream(method, path, headers, body, session_id)
    except Exception as exc:
        logger.warning(
            "proxy: throttle error, degrading to pass-through (session=%s): %s",
            session_id or "?",
            exc,
        )
        if is_stream:
            return await _passthrough_stream(request, path, body)
        else:
            return await _passthrough_response(request, method, path, body)


@app.post("/v1/messages")
async def proxy_messages(request: Request) -> Response:
    return await _handle_proxy(request, "POST", "/v1/messages")


@app.post("/v1/messages/count_tokens")
async def proxy_count_tokens(request: Request) -> Response:
    return await _handle_proxy(request, "POST", "/v1/messages/count_tokens")


@app.get("/v1/models")
async def proxy_models(request: Request) -> Response:
    return await _handle_proxy(request, "GET", "/v1/models")


@app.get("/health")
async def health() -> dict:
    ctrl = _controller
    return {
        "status": "ok",
        "upstream": _ANTHROPIC_API_BASE,
        **(ctrl.metrics() if ctrl else {}),
    }


@app.get("/metrics")
async def metrics() -> dict:
    ctrl = _controller
    if ctrl is None:
        return {"error": "not_initialized"}
    return ctrl.metrics()


# ---------------------------------------------------------------------------
# Serve entry point
# ---------------------------------------------------------------------------


def serve(*, port: int = 8741) -> None:
    """Start the proxy in the foreground (blocks until shutdown)."""
    import uvicorn

    print(f"khimaira proxy: starting on http://127.0.0.1:{port}")
    print(f"  upstream: {_ANTHROPIC_API_BASE}")
    print(f"  N={_N_DEFAULT} (AIMD range: {_N_MIN}–{_N_MAX})")
    print(f"  max_retries={_MAX_RETRIES}")
    print()
    print("  Configure sessions:")
    print(f"    ANTHROPIC_BASE_URL=http://127.0.0.1:{port}")
    print(f"    ENABLE_TOOL_SEARCH=1  # required to keep MCP tool-search working")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
