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
    resp = httpx.post(
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
    resp = httpx.post(
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
    resp = httpx.post(
        f"{base}/api/chats/{chat_id}/accept",
        json={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def reject(chat_id: str, session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = httpx.post(
        f"{base}/api/chats/{chat_id}/reject",
        json={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def latest_pending(session_id: str, *, base: str = DEFAULT_BASE) -> str | None:
    """Return the chat_id of the most recent pending invite for this
    session, or None if no pending invites."""
    resp = httpx.get(
        f"{base}/api/chats/pending/latest",
        params={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json().get("chat_id")


def send_message(
    chat_id: str, sender_session_id: str, body: str, *, base: str = DEFAULT_BASE
) -> dict[str, Any]:
    resp = httpx.post(
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
    resp = httpx.get(f"{base}/api/chats/{chat_id}/messages", params=params, timeout=10.0)
    _raise_for_status(resp)
    return resp.json().get("messages", [])


def my_chats(session_id: str, *, base: str = DEFAULT_BASE) -> list[dict[str, Any]]:
    resp = httpx.get(f"{base}/api/chats", params={"session_id": session_id}, timeout=10.0)
    _raise_for_status(resp)
    return resp.json().get("chats", [])


def leave(chat_id: str, session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = httpx.post(
        f"{base}/api/chats/{chat_id}/leave",
        json={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def delete_chat(chat_id: str, by_session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = httpx.delete(
        f"{base}/api/chats/{chat_id}",
        params={"by_session_id": by_session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def get_room(chat_id: str, session_id: str, *, base: str = DEFAULT_BASE) -> dict[str, Any]:
    resp = httpx.get(f"{base}/api/chats/{chat_id}", params={"session_id": session_id}, timeout=10.0)
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
    reconnect_backoff_s: float = 2.0,
) -> AsyncIterator[dict[str, Any]]:
    """Yield chat events from the daemon's SSE stream.

    Reconnects forever with the last-seen event id so a daemon restart
    or transient network blip doesn't lose messages. Caller cancels the
    iterator to stop.
    """
    url = f"{base}/api/chats/events"
    while True:
        headers: dict[str, str] = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        try:
            async with (
                httpx.AsyncClient(timeout=None) as client,
                client.stream(
                    "GET", url, params={"session_id": session_id}, headers=headers
                ) as resp,
            ):
                resp.raise_for_status()
                async for record in _parse_sse_lines(resp.aiter_lines()):
                    evt_id = record.get("event_id")
                    if evt_id:
                        last_event_id = evt_id
                    yield record
        except (httpx.HTTPError, httpx.NetworkError):
            # Transient failure — back off and retry.
            await asyncio.sleep(reconnect_backoff_s)
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
