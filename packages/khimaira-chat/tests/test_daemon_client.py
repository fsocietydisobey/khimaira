"""Tests for khimaira_chat.daemon_client.

Covers the SSE parser (offline, byte-stream) and the HTTP wrappers
(via httpx mock transport). The MCP server in server.py is harder to
unit test in isolation — its smoke is the two-window real exchange.
"""

from __future__ import annotations

import json

import httpx
import pytest
from khimaira_chat import daemon_client

# ---------------------------------------------------------------------------
# SSE parser
# ---------------------------------------------------------------------------


async def _alines(lines: list[str]):
    for line in lines:
        yield line


async def _drain(gen):
    out = []
    async for record in gen:
        out.append(record)
    return out


@pytest.mark.asyncio
async def test_parse_sse_lines_single_event():
    lines = [
        "id: abc123",
        "event: msg",
        'data: {"event_id":"abc123","kind":"msg","body":"hello"}',
        "",
    ]
    out = await _drain(daemon_client._parse_sse_lines(_alines(lines)))
    assert len(out) == 1
    assert out[0]["body"] == "hello"


@pytest.mark.asyncio
async def test_parse_sse_lines_multiple_events():
    lines = [
        'data: {"event_id":"a","body":"one"}',
        "",
        'data: {"event_id":"b","body":"two"}',
        "",
    ]
    out = await _drain(daemon_client._parse_sse_lines(_alines(lines)))
    assert [r["body"] for r in out] == ["one", "two"]


@pytest.mark.asyncio
async def test_parse_sse_lines_skips_invalid_json():
    lines = [
        "data: not-json",
        "",
        'data: {"event_id":"b","body":"valid"}',
        "",
    ]
    out = await _drain(daemon_client._parse_sse_lines(_alines(lines)))
    assert [r["body"] for r in out] == ["valid"]


@pytest.mark.asyncio
async def test_parse_sse_lines_strips_leading_space():
    # SSE spec: one optional space after the colon is trimmed.
    lines = [
        'data: {"body":"with-space"}',
        "",
        'data:{"body":"no-space"}',
        "",
    ]
    out = await _drain(daemon_client._parse_sse_lines(_alines(lines)))
    assert out[0]["body"] == "with-space"
    assert out[1]["body"] == "no-space"


# ---------------------------------------------------------------------------
# HTTP wrappers via httpx mock transport
# ---------------------------------------------------------------------------


def _mock_transport(handler):
    return httpx.MockTransport(handler)


def _patch_request(monkeypatch, handler):
    """Route httpx.request() calls through a MockTransport for tests."""
    transport = _mock_transport(handler)
    monkeypatch.setattr(
        httpx,
        "request",
        lambda method, url, **kw: httpx.Client(transport=transport).request(method, url, **kw),
    )


def test_create_room_happy_path(monkeypatch):
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "meta": {"chat_id": "chat-test123abcde", "title": "alice + bob"},
                "members": {},
                "messages": [],
            },
        )

    _patch_request(monkeypatch, handler)

    result = daemon_client.create_room("alice", ["bob"], title="alice + bob")
    assert result["meta"]["chat_id"] == "chat-test123abcde"
    assert sent["body"]["creator_session_id"] == "alice"
    assert sent["body"]["member_session_ids"] == ["bob"]


def test_create_room_raises_on_404(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "unknown member"})

    _patch_request(monkeypatch, handler)

    with pytest.raises(daemon_client.DaemonError) as excinfo:
        daemon_client.create_room("alice", ["ghost"])
    assert excinfo.value.status_code == 404
    assert "unknown member" in excinfo.value.detail


def test_send_message_raises_on_403(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "non-member can't send"})

    _patch_request(monkeypatch, handler)

    with pytest.raises(daemon_client.DaemonError) as excinfo:
        daemon_client.send_message("chat-x", "eve", "hostile")
    assert excinfo.value.status_code == 403


def test_request_with_retry_recovers_after_transient_connect_error(monkeypatch):
    """Brief connection refused → retry → success on second attempt."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise httpx.ConnectError("connection refused (test)")
        return httpx.Response(200, json={"chats": []})

    transport = _mock_transport(handler)

    def fake_request(method, url, **kw):
        return httpx.Client(transport=transport).request(method, url, **kw)

    monkeypatch.setattr(httpx, "request", fake_request)
    # Stub time.sleep so the test doesn't actually pause _RETRY_DELAY_S.
    monkeypatch.setattr("time.sleep", lambda _: None)

    result = daemon_client.my_chats("alice")
    assert result == []
    assert attempts["count"] == 2  # one failure + one success


def test_request_with_retry_gives_up_after_max_attempts(monkeypatch):
    """Persistent connection failure → all retries fail → raise."""
    attempts = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        raise httpx.ConnectError("daemon down (test)")

    transport = _mock_transport(handler)

    def fake_request(method, url, **kw):
        return httpx.Client(transport=transport).request(method, url, **kw)

    monkeypatch.setattr(httpx, "request", fake_request)
    monkeypatch.setattr("time.sleep", lambda _: None)

    with pytest.raises(httpx.ConnectError):
        daemon_client.my_chats("alice")
    assert attempts["count"] == daemon_client._RETRY_ATTEMPTS


def test_my_chats_returns_list(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "chats": [
                    {"chat_id": "chat-aaa", "my_state": "accepted", "title": "x"},
                    {"chat_id": "chat-bbb", "my_state": "pending", "title": "y"},
                ]
            },
        )

    _patch_request(monkeypatch, handler)

    result = daemon_client.my_chats("alice")
    assert len(result) == 2
    assert result[0]["chat_id"] == "chat-aaa"
