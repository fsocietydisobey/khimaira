"""HTTP client for the khimaira-monitor daemon's /api/chats/* endpoints.

Plus a tiny manual SSE parser for /api/chats/events — avoids pulling in
httpx-sse for a single line-based protocol. Reconnects with the
last-seen event_id (Last-Event-ID header) so daemon restart doesn't
lose chat events.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import AsyncIterator
from typing import Any

import httpx

DEFAULT_BASE = os.environ.get("KHIMAIRA_MONITOR_URL", "http://127.0.0.1:8740")

# Brief retry on connection-refused absorbs a daemon restart (~1-3s
# downtime). 4xx/5xx responses are NOT retried — they're real server
# answers, not transport hiccups.
_RETRY_ATTEMPTS = 3
_RETRY_DELAY_S = 0.5


class DaemonError(Exception):
    """Raised when the daemon returns an HTTP error."""

    def __init__(self, status_code: int, detail: str):
        self.status_code = status_code
        self.detail = detail
        super().__init__(f"daemon HTTP {status_code}: {detail}")


def _raise_for_status(resp: httpx.Response) -> None:
    if resp.status_code >= 400:
        try:
            detail = resp.json().get("detail", resp.text)
        except (ValueError, json.JSONDecodeError):
            detail = resp.text
        raise DaemonError(resp.status_code, detail or f"HTTP {resp.status_code}")


def _request_with_retry(method: str, url: str, **kwargs) -> httpx.Response:
    """httpx call with brief retry on connection refused / network errors.

    Absorbs a daemon restart without surfacing the failure to the agent.
    Last attempt's exception bubbles up unchanged so the caller still
    sees a real DaemonError if the daemon stays down.
    """
    import time

    last_exc: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            return httpx.request(method, url, **kwargs)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError) as exc:
            last_exc = exc
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAY_S)
                continue
            raise
    # Defensive — loop always returns or raises.
    raise last_exc if last_exc else RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# REST calls — thin wrappers over the daemon's /api/chats/* endpoints
# ---------------------------------------------------------------------------


def create_room(
    creator_session_id: str,
    member_session_ids: list[str],
    *,
    title: str | None = None,
    fresh: bool = False,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats",
        json={
            "creator_session_id": creator_session_id,
            "member_session_ids": member_session_ids,
            "title": title,
            "fresh": fresh,
        },
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def invite(
    chat_id: str,
    by_session_id: str,
    invitee_session_id: str,
    *,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/invite",
        json={
            "by_session_id": by_session_id,
            "invitee_session_id": invitee_session_id,
        },
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def accept(chat_id: str, session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/accept",
        json={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def reject(chat_id: str, session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/reject",
        json={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def latest_pending(session_id: str, *, base: str = DEFAULT_BASE) -> str | None:
    """Return the chat_id of the most recent pending invite for this
    session, or None if no pending invites."""
    resp = _request_with_retry(
        "GET",
        f"{base}/api/chats/pending/latest",
        params={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json().get("chat_id")


def send_message(
    chat_id: str, sender_session_id: str, body: str, *, base: str = DEFAULT_BASE
) -> dict[str, Any]:
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/messages",
        json={"sender_session_id": sender_session_id, "body": body},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def history(
    chat_id: str,
    session_id: str,
    *,
    limit: int = 50,
    since: str | None = None,
    base: str = DEFAULT_BASE,
) -> list[dict[str, Any]]:
    params: dict[str, Any] = {"session_id": session_id, "limit": limit}
    if since:
        params["since"] = since
    resp = _request_with_retry(
        "GET", f"{base}/api/chats/{chat_id}/messages", params=params, timeout=10.0
    )
    _raise_for_status(resp)
    return resp.json().get("messages", [])


def my_chats(session_id: str, *, base: str = DEFAULT_BASE) -> list[dict[str, Any]]:
    resp = _request_with_retry(
        "GET", f"{base}/api/chats", params={"session_id": session_id}, timeout=10.0
    )
    _raise_for_status(resp)
    return resp.json().get("chats", [])


def leave(chat_id: str, session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/leave",
        json={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def delete_chat(chat_id: str, by_session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = _request_with_retry(
        "DELETE",
        f"{base}/api/chats/{chat_id}",
        params={"by_session_id": by_session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def lookup_session_by_ppid(ppid: int, *, base: str = DEFAULT_BASE) -> str | None:
    """At subprocess startup, ask the daemon what session_id corresponds
    to this subprocess's parent PID (Claude Code). The SessionStart hook
    posted that mapping at boot. Returns None if no entry — falls back
    to lazy registration via the agent's first chat tool call.
    """
    try:
        resp = _request_with_retry(
            "GET",
            f"{base}/api/chats/session-by-ppid",
            params={"ppid": ppid},
            timeout=2.0,
        )
        if resp.status_code != 200:
            return None
        return resp.json().get("session_id")
    except Exception:
        return None


def set_session_name(session_id: str, name: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    """Register a friendly name for this session in the daemon's session
    registry. Used by the chat MCP subprocess to auto-resolve the
    Claude-Code-side `-n <name>` flag into khimaira's naming, so users
    don't need to manually `/rename` after launching with `-n`."""
    resp = _request_with_retry(
        "POST",
        f"{base}/api/sessions/{session_id}/name",
        json={"name": name},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def get_room(chat_id: str, session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = _request_with_retry(
        "GET", f"{base}/api/chats/{chat_id}", params={"session_id": session_id}, timeout=10.0
    )
    _raise_for_status(resp)
    return resp.json()


# ---------------------------------------------------------------------------
# SSE subscriber — reconnects with Last-Event-ID on daemon restart
# ---------------------------------------------------------------------------


async def subscribe_events(
    session_id: str,
    *,
    last_event_id: str | None = None,
    base: str = DEFAULT_BASE,
    initial_backoff_s: float = 1.0,
    max_backoff_s: float = 30.0,
) -> AsyncIterator[dict[str, Any]]:
    """Yield chat events from the daemon's SSE stream.

    Reconnects forever with the last-seen event id so a daemon restart
    or transient network blip doesn't lose messages. Caller cancels the
    iterator to stop.

    Backoff strategy: exponential — 1s → 2s → 4s → 8s → 16s → 30s (cap)
    — and resets to `initial_backoff_s` after a successful connect (any
    bytes received). This keeps the reconnect prompt during a daemon
    restart while not hammering the daemon if it stays down. Caps at 30s
    so eventual reconnect after a long outage still happens within
    half a minute.
    """
    url = f"{base}/api/chats/events"
    backoff = initial_backoff_s
    while True:
        headers: dict[str, str] = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        connected = False
        try:
            async with (
                httpx.AsyncClient(timeout=None) as client,
                client.stream(
                    "GET", url, params={"session_id": session_id}, headers=headers
                ) as resp,
            ):
                resp.raise_for_status()
                connected = True
                backoff = initial_backoff_s  # successful connect → reset
                async for record in _parse_sse_lines(resp.aiter_lines()):
                    evt_id = record.get("event_id")
                    if evt_id:
                        last_event_id = evt_id
                    yield record
        except (httpx.HTTPError, httpx.NetworkError):
            await asyncio.sleep(backoff)
            if not connected:
                # Failed to connect at all → exponential backoff.
                backoff = min(backoff * 2, max_backoff_s)
            continue


async def _parse_sse_lines(
    line_iter: AsyncIterator[str],
) -> AsyncIterator[dict[str, Any]]:
    """Parse a stream of SSE text lines into one dict per `data:` payload.

    SSE format: events separated by a blank line. Each event is a list of
    `field: value` lines; we only need the `data:` field (one or more
    lines, joined with newlines per spec). The `id:` and `event:` fields
    are present in our daemon's output but redundant — the JSON payload
    includes the same event_id and kind.
    """
    data_buf: list[str] = []
    async for line in line_iter:
        if line == "":
            if data_buf:
                joined = "\n".join(data_buf)
                with contextlib.suppress(json.JSONDecodeError):
                    yield json.loads(joined)
                data_buf = []
            continue
        if line.startswith("data:"):
            # SSE strips one leading space after `data:`.
            data_buf.append(line[5:].lstrip(" "))
