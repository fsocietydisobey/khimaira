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


# ---------------------------------------------------------------------------
# httpx read-timeout pinning — verifies the SSE read-timeout fires when a
# server accepts the TCP connection but never writes a byte. The original
# laptop-suspend bug was that subscribe_events used timeout=None and hung
# silently when the socket died mid-transit. The fix sets read=45.0; this
# test pins down that httpx actually honors the read parameter under the
# slow-loris failure mode the production path is meant to detect.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_httpx_read_timeout_fires_on_silent_server():
    """Slow-loris: server accepts the connection but never writes a byte.
    httpx.Timeout(read=2.0) should raise httpx.ReadTimeout within ~2-3s
    when consuming the stream — pinning the behavior subscribe_events
    depends on for silent-socket-death detection (suspend / NAT timeout)."""
    import asyncio
    import time

    async def silent_handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        # Accept the TCP connection, never write headers/body. reader.read()
        # blocks until httpx closes its side after timeout, then we tidy up.
        try:
            await reader.read()
        finally:
            writer.close()

    server = await asyncio.start_server(silent_handler, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(connect=5.0, read=2.0, write=10.0, pool=5.0)
        ) as client:
            t0 = time.monotonic()
            with pytest.raises(httpx.ReadTimeout):
                async with client.stream("GET", f"http://{host}:{port}/") as resp:
                    async for _ in resp.aiter_lines():
                        break  # pragma: no cover — never reached
            elapsed = time.monotonic() - t0
            # Should fire near the 2.0s mark; allow generous slack for CI load.
            assert elapsed < 4.0, f"read-timeout took {elapsed:.2f}s; expected <4.0s"
    finally:
        server.close()
        await server.wait_closed()


# ---------------------------------------------------------------------------
# invite(role=) and grant_role() (task-b7ddcbf080da)
# ---------------------------------------------------------------------------


def test_invite_without_role_omits_role_field(monkeypatch):
    """invite() without role= sends only by_session_id + invitee_session_id."""
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"kind": "member", "state": "pending"})

    _patch_request(monkeypatch, handler)
    daemon_client.invite("chat-abc", "alice", "bob")
    assert "role" not in sent["body"]
    assert sent["body"]["by_session_id"] == "alice"
    assert sent["body"]["invitee_session_id"] == "bob"


def test_invite_with_role_includes_role_field(monkeypatch):
    """invite(role='agent') sends role in the request body."""
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"kind": "member", "state": "pending"})

    _patch_request(monkeypatch, handler)
    daemon_client.invite("chat-abc", "alice", "bob", role="agent")
    assert sent["body"]["role"] == "agent"


def test_grant_role_sends_correct_payload(monkeypatch):
    """grant_role() POSTs to /grant-role with the expected payload."""
    sent = {}

    def handler(request: httpx.Request) -> httpx.Response:
        sent["path"] = request.url.path
        sent["body"] = json.loads(request.content)
        return httpx.Response(200, json={"member_roles": {"bob": "agent"}})

    _patch_request(monkeypatch, handler)
    result = daemon_client.grant_role("chat-abc", "alice", "bob", "agent")
    assert sent["path"] == "/api/chats/chat-abc/grant-role"
    assert sent["body"]["by_session_id"] == "alice"
    assert sent["body"]["target_session_id"] == "bob"
    assert sent["body"]["role"] == "agent"
    assert sent["body"]["demote_to"] == "agent"
    assert result == {"member_roles": {"bob": "agent"}}


def test_grant_role_daemon_error_propagates(monkeypatch):
    """403 from daemon (non-master caller) raises DaemonError."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, json={"detail": "only the master can grant roles"})

    _patch_request(monkeypatch, handler)
    with pytest.raises(daemon_client.DaemonError, match="403"):
        daemon_client.grant_role("chat-abc", "bob", "carol", "agent")
