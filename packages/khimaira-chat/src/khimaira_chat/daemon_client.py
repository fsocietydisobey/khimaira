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

# TM1 daemon-auth: send CLAUDE_CODE_SESSION_ID as X-Session-ID header so the
# daemon can derive actor identity from an env-sourced value rather than a
# caller-supplied body field. Matches themis/server.py's _caller_headers().
_CALLER_SESSION_ID: str = os.environ.get("CLAUDE_CODE_SESSION_ID", "")


def _caller_headers() -> dict[str, str]:
    """Return X-Session-ID header when caller ID is known (set by Claude Code)."""
    if _CALLER_SESSION_ID:
        return {"X-Session-ID": _CALLER_SESSION_ID}
    return {}

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

    TM1: all POST requests automatically include X-Session-ID from the
    env-sourced CLAUDE_CODE_SESSION_ID so the daemon can derive actor
    identity without trusting a caller-supplied body field.
    """
    import time

    if method.upper() == "POST":
        caller_headers = _caller_headers()
        if caller_headers:
            merged = dict(caller_headers)
            merged.update(kwargs.get("headers") or {})
            kwargs["headers"] = merged

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
    topology: str = "flat",
    member_roles: dict[str, str] | None = None,
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
            "topology": topology,
            "member_roles": member_roles,
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
    role: str | None = None,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "by_session_id": by_session_id,
        "invitee_session_id": invitee_session_id,
    }
    if role is not None:
        payload["role"] = role
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/invite",
        json=payload,
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def grant_role(
    chat_id: str,
    by_session_id: str,
    target_session_id: str,
    role: str,
    *,
    demote_to: str = "agent",
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Master-only: bind a role onto an existing chat member.

    `by_session_id` must be the authenticated caller's session id (the
    daemon enforces _is_master against this). Do not expose as a
    user-fillable param in MCP tools — always pass `sid` from the
    server's authenticated context.
    """
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/grant-role",
        json={
            "by_session_id": by_session_id,
            "target_session_id": target_session_id,
            "role": role,
            "demote_to": demote_to,
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
    chat_id: str,
    sender_session_id: str,
    body: str,
    *,
    to: list[str] | None = None,
    private: bool | None = None,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Send a message to a chat.

    `to`: optional list of session ids/names. If provided and non-empty,
    only those members receive the channel push. Omit / pass None to
    broadcast to all accepted members as before.

    `private=True`: message hidden from non-recipients in chat_history.
    Requires `to` to be non-empty.

    `private=None` (default): server applies the chat's topology default.
    In "hierarchical" chats, targeted messages default to private=True.
    """
    payload: dict[str, Any] = {"sender_session_id": sender_session_id, "body": body}
    if to:
        payload["to"] = to
    if private is not None:
        payload["private"] = private
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/messages",
        json=payload,
        timeout=35.0,  # server retries up to 30s for pending recipients
    )
    _raise_for_status(resp)
    return resp.json()


def create_task(
    chat_id: str,
    sender_session_id: str,
    body: str,
    *,
    assignee_session_id: str | None = None,
    private: bool = False,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Create a structured task in a chat. Sender must be an accepted
    member. Initial status is `pending`. If `assignee_session_id` is None
    the task is unassigned (the next-up worker can claim it by moving it
    to `in_progress`).

    `private=True`: task hidden from non-assignee members in chat_history.
    Requires assignee_session_id.
    """
    payload: dict[str, Any] = {"sender_session_id": sender_session_id, "body": body}
    if assignee_session_id:
        payload["assignee_session_id"] = assignee_session_id
    if private:
        payload["private"] = True
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/tasks",
        json=payload,
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def update_task_status(
    chat_id: str,
    task_id: str,
    by_session_id: str,
    new_status: str,
    *,
    note: str | None = None,
    private: bool = False,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Move a task between lifecycle states.

    `private=True`: status update hidden from non-assignee members in
    chat_history. Task must have an assignee.
    """
    payload: dict[str, Any] = {"by_session_id": by_session_id, "new_status": new_status}
    if note is not None:
        payload["note"] = note
    if private:
        payload["private"] = True
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/tasks/{task_id}/status",
        json=payload,
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def signal_task_start(
    chat_id: str,
    task_id: str,
    by_session_id: str,
    *,
    note: str | None = None,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Master-only "go" signal on a pending task. Doesn't change task status —
    just emits a task_signal record that surfaces as a channel block for the
    assignee (or broadcast if unassigned). The assignee still drives the
    pending → in_progress transition when they pick the task up."""
    payload: dict[str, Any] = {"by_session_id": by_session_id}
    if note is not None:
        payload["note"] = note
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/tasks/{task_id}/signal-start",
        json=payload,
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def record_gate_verdict(
    chat_id: str,
    by_session_id: str,
    task_id: str,
    verdict: str,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Write a structured gate-verdict event (B3 Slice B-1).

    verdict ∈ {"approve", "changes", "ship", "hold"}.
    critic writes approve/changes; verifier writes ship/hold.
    IN-AGENT-6 GATE_BEFORE_COMMIT and IN-MASTER-9 APPROVE_WITHOUT_REVIEW_VERDICTS
    read these structured events — prose chat messages do NOT satisfy the gate.
    """
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/tasks/{task_id}/verdict",
        json={"by_session_id": by_session_id, "verdict": verdict},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def task_status(chat_id: str, session_id: str, *, base: str = DEFAULT_BASE) -> list[dict[str, Any]]:
    """List tasks in a chat with current status. Requester must be an
    accepted member."""
    resp = _request_with_retry(
        "GET",
        f"{base}/api/chats/{chat_id}/tasks",
        params={"session_id": session_id},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json().get("tasks", [])


def set_auto_accept(
    session_id: str, allowlist: list[str], *, base: str = DEFAULT_BASE
) -> dict[str, Any]:
    """Set this session's auto-accept allowlist. Replaces (not additive).
    Pass [] to clear."""
    resp = _request_with_retry(
        "POST",
        f"{base}/api/sessions/{session_id}/auto-accept",
        json={"session_id": session_id, "allowlist": allowlist},
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def apply_auto_accept_by_name(
    session_id: str, name: str, *, base: str = DEFAULT_BASE
) -> dict[str, Any]:
    """Surface a by-name auto-accept allowlist file for a freshly-named
    session. Called at chat MCP subprocess boot, immediately after the
    dual-name auto-bridge calls `set_session_name`. No-op if no by-name
    file exists for `name` — just enables logging that the boot-time
    apply ran."""
    resp = _request_with_retry(
        "POST",
        f"{base}/api/sessions/{session_id}/auto-accept/apply-by-name",
        params={"name": name},
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


def transfer_membership(
    chat_id: str,
    from_session_id: str,
    to_session_id: str,
    *,
    as_deputize: bool = False,
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Hand `from`'s chat membership to `to` in one atomic call.

    Used by /khimaira-transfer-session so an outgoing orchestrator session
    can hand its active chats to a fresh successor without a handshake on
    the receiving end. The receiving session lands accepted immediately
    and inherits the full chat_history.

    Phase B v1.6: `as_deputize=True` writes
    `meta.deputized_original_master = from_session_id` atomically with the
    transfer AND skips the donor's TRANSFERRED_OUT MEMBER write — donor
    stays ACCEPTED through the deputize→resume cycle. Used by
    /khimaira-deputize.
    """
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/transfer-membership",
        json={
            "from_session_id": from_session_id,
            "to_session_id": to_session_id,
            "as_deputize": as_deputize,
        },
        timeout=10.0,
    )
    _raise_for_status(resp)
    return resp.json()


def resume_master(
    chat_id: str,
    by_session_id: str,
    *,
    demote_to: str = "agent",
    base: str = DEFAULT_BASE,
) -> dict[str, Any]:
    """Phase B v1.6: caller (original master per meta marker) reclaims
    master role from the current vice.

    Pairs with /khimaira-resume. Validates caller matches
    meta.deputized_original_master; atomically swaps master role back via
    v1.5 directive emits; clears the meta marker.
    """
    resp = _request_with_retry(
        "POST",
        f"{base}/api/chats/{chat_id}/resume-master",
        json={
            "by_session_id": by_session_id,
            "demote_to": demote_to,
        },
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

    Reconnects forever with the last-seen event id so a daemon restart,
    transient network blip, or laptop-suspend doesn't lose messages.
    Caller cancels the iterator to stop.

    Suspend/resume protection: the daemon sends an SSE keepalive comment
    every 15s. We set a 45s read timeout (3× ping interval, safe buffer
    for one missed ping). After a laptop wakes from suspend, the dead
    SSE socket triggers httpx.ReadTimeout within 45s → reconnect →
    daemon serves any missed events via Last-Event-ID backfill.

    Backoff strategy: exponential — 1s → 2s → 4s → 8s → 16s → 30s (cap)
    — and resets to `initial_backoff_s` after a successful connect (any
    bytes received). This keeps the reconnect prompt during a daemon
    restart while not hammering the daemon if it stays down. Caps at 30s
    so eventual reconnect after a long outage still happens within
    half a minute.
    """
    url = f"{base}/api/chats/events"
    backoff = initial_backoff_s
    # Read timeout 45s matches the daemon's 15s keepalive ping at 3x —
    # one missed ping is acceptable noise, two means the connection is
    # actually dead and we should reconnect.
    sse_timeout = httpx.Timeout(connect=5.0, read=45.0, write=10.0, pool=5.0)
    while True:
        headers: dict[str, str] = {"Accept": "text/event-stream"}
        if last_event_id:
            headers["Last-Event-ID"] = last_event_id
        connected = False
        try:
            async with (
                httpx.AsyncClient(timeout=sse_timeout) as client,
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
        except (httpx.HTTPError, httpx.NetworkError) as exc:
            # Log distinguishes "couldn't connect" (daemon down) from
            # "lost a working connection" (suspend/resume, server restart).
            # The latter is the load-bearing case for the keepalive fix —
            # if we see ReadTimeout here, the heartbeat is working as
            # intended.
            import logging

            logging.getLogger("khimaira_chat.daemon_client").info(
                "khimaira-chat: SSE %s — %s — reconnecting in %.1fs (last_event_id=%s)",
                "lost (was connected)" if connected else "connect failed",
                type(exc).__name__,
                backoff,
                last_event_id,
            )
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
