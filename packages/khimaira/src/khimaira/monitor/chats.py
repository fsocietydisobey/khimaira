"""Cross-session chat state — rooms, members, transcripts.

Storage: per-chat JSONL at ~/.local/state/khimaira/chats/<chat_id>.jsonl.
First line is room meta; subsequent lines are member transitions + messages.
Each line carries an `event_id` (12-char hex) so SSE subscribers can
reconnect with `Last-Event-ID` and replay missed events.

Membership state machine: pending → accepted → left | removed. Only
`accepted` members receive channel notifications and can read history.

Sender gating: every write call validates that the caller is a member
in the right state. Channels-reference.md is emphatic that ungated
channels are a prompt-injection vector — gating lives here, not in
the subprocess.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from khimaira.log import get_logger
from khimaira.monitor import sessions as sessions_mod

log = get_logger("monitor.chats")

# Member states.
PENDING = "pending"
ACCEPTED = "accepted"
LEFT = "left"
REMOVED = "removed"

# Line kinds in the JSONL.
META = "meta"
MEMBER = "member"
MSG = "msg"


# ---------------------------------------------------------------------------
# Storage primitives
# ---------------------------------------------------------------------------


def _chat_dir() -> Path:
    xdg = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    return xdg / "khimaira" / "chats"


def _archive_dir() -> Path:
    return _chat_dir() / "archive"


def _chat_path(chat_id: str) -> Path:
    return _chat_dir() / f"{chat_id}.jsonl"


def _ensure_dir() -> None:
    _chat_dir().mkdir(parents=True, exist_ok=True)
    _archive_dir().mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_event_id() -> str:
    return uuid.uuid4().hex[:12]


def _append(chat_id: str, record: dict[str, Any]) -> None:
    _ensure_dir()
    sessions_mod._append_jsonl(_chat_path(chat_id), record)
    _broadcast(chat_id, record)


def _read(chat_id: str) -> list[dict[str, Any]]:
    return sessions_mod._read_jsonl(_chat_path(chat_id))


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def _resolve_or_uuid(session_id_or_name: str) -> str:
    """Resolve a session name → UUID, OR accept a UUID verbatim.

    `_resolve_or_uuid` requires the session's state dir to
    exist (the session has logged decisions / set status / etc). Fresh
    Claude Code sessions don't have a dir until they write something —
    but they DO have a session_id from the SessionStart hook, and the
    chat lazy-registration design depends on accepting that id even
    before the session has any other state.

    Resolution order:
      1. If the input matches a canonical UUID format → trust it
         verbatim. Cost: a chat targeted at a non-existent UUID is
         silently a no-op (no subscriber to deliver to). Acceptable
         wart for the lazy-registration win.
      2. Otherwise treat as a friendly name → resolve via the standard
         path; raises if the name doesn't match any session.
    """
    if _UUID_RE.match(session_id_or_name):
        return session_id_or_name
    return sessions_mod.resolve_session_id(session_id_or_name)


def derive_chat_id(member_session_ids: list[str], fresh_suffix: str | None = None) -> str:
    """sha256 over sorted-members + optional fresh_suffix → 12-char prefix."""
    sorted_members = sorted(member_session_ids)
    payload = "|".join(sorted_members) + "|" + (fresh_suffix or "")
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    return f"chat-{digest}"


def load_room(chat_id: str) -> dict[str, Any]:
    """Fold the JSONL into the current room state.

    Returns: {meta: {...}, members: {session_id: {state, name, ...}}, messages: [...]}
    Raises ValueError if the room doesn't exist.
    """
    lines = _read(chat_id)
    if not lines:
        raise ValueError(
            f"No chat with id={chat_id!r}. "
            f"Use mcp__khimaira__chat_my_chats(session_id) to list active chats."
        )
    meta: dict[str, Any] = {}
    members: dict[str, dict[str, Any]] = {}
    messages: list[dict[str, Any]] = []
    for line in lines:
        kind = line.get("kind")
        if kind == META:
            meta = line
        elif kind == MEMBER:
            sid = line["session_id"]
            existing = members.get(sid, {})
            existing.update(
                {
                    "session_id": sid,
                    "session_name": line.get("session_name") or existing.get("session_name"),
                    "state": line["state"],
                    "last_transition_ts": line["ts"],
                    "last_transition_event_id": line["event_id"],
                }
            )
            members[sid] = existing
        elif kind == MSG:
            messages.append(line)
    return {"meta": meta, "members": members, "messages": messages}


def _resolve_session_name(session_id: str) -> str | None:
    """Look up friendly name from the session's status.json (best-effort)."""
    try:
        sd = sessions_mod._session_dir(session_id)
        status_file = sd / "status.json"
        if status_file.is_file():
            data = json.loads(status_file.read_text(encoding="utf-8"))
            return data.get("name")
    except (OSError, json.JSONDecodeError):
        pass
    return None


# ---------------------------------------------------------------------------
# Public API — chat lifecycle
# ---------------------------------------------------------------------------


def create_room(
    creator_session_id: str,
    member_session_ids: list[str],
    *,
    title: str | None = None,
    fresh: bool = False,
) -> dict[str, Any]:
    """Create a new chat room. Creator is auto-`accepted`; other members
    start `pending` and must call `accept()` to receive notifications."""
    creator_session_id = _resolve_or_uuid(creator_session_id)
    resolved_members = [_resolve_or_uuid(m) for m in member_session_ids]
    if creator_session_id not in resolved_members:
        resolved_members.append(creator_session_id)

    fresh_suffix = _now_iso() if fresh else None
    chat_id = derive_chat_id(resolved_members, fresh_suffix)

    if _chat_path(chat_id).exists():
        raise ValueError(
            f"Chat {chat_id!r} already exists with these members. "
            f"Use --new to start a fresh transcript with the same members."
        )

    creator_name = _resolve_session_name(creator_session_id) or creator_session_id[:8]
    member_names = [_resolve_session_name(m) or m[:8] for m in resolved_members]
    derived_title = title or " + ".join(member_names)

    meta = {
        "kind": META,
        "event_id": _new_event_id(),
        "chat_id": chat_id,
        "ts": _now_iso(),
        "created_at": _now_iso(),
        "created_by": creator_session_id,
        "created_by_name": creator_name,
        "title": derived_title,
        "fresh_suffix": fresh_suffix,
    }
    _append(chat_id, meta)

    # Creator is auto-accepted; others go pending.
    for sid in resolved_members:
        state = ACCEPTED if sid == creator_session_id else PENDING
        record = {
            "kind": MEMBER,
            "event_id": _new_event_id(),
            "ts": _now_iso(),
            "chat_id": chat_id,
            "session_id": sid,
            "session_name": _resolve_session_name(sid) or sid[:8],
            "state": state,
            "invited_by": creator_session_id,
        }
        _append(chat_id, record)

    log.info("chats: created %s with %d members", chat_id, len(resolved_members))
    return load_room(chat_id)


def invite(chat_id: str, by_session_id: str, invitee_session_id: str) -> dict[str, Any]:
    """Add a new member in `pending` state. Caller must be an accepted member."""
    by_session_id = _resolve_or_uuid(by_session_id)
    invitee_session_id = _resolve_or_uuid(invitee_session_id)
    room = load_room(chat_id)
    members = room["members"]
    if members.get(by_session_id, {}).get("state") != ACCEPTED:
        raise ValueError(
            f"Session {by_session_id!r} is not an accepted member of {chat_id!r}; "
            f"cannot invite others."
        )
    existing = members.get(invitee_session_id)
    if existing and existing.get("state") in (PENDING, ACCEPTED):
        raise ValueError(
            f"Session {invitee_session_id!r} is already a {existing['state']} "
            f"member of {chat_id!r}."
        )
    record = {
        "kind": MEMBER,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "session_id": invitee_session_id,
        "session_name": _resolve_session_name(invitee_session_id) or invitee_session_id[:8],
        "state": PENDING,
        "invited_by": by_session_id,
    }
    _append(chat_id, record)
    log.info("chats: %s invited %s to %s", by_session_id, invitee_session_id, chat_id)
    return record


def accept(chat_id: str, session_id: str) -> dict[str, Any]:
    """Move a pending member to accepted. Required before they receive notifications."""
    session_id = _resolve_or_uuid(session_id)
    room = load_room(chat_id)
    member = room["members"].get(session_id)
    if not member:
        raise ValueError(
            f"Session {session_id!r} is not a member of {chat_id!r}. They must be invited first."
        )
    if member["state"] != PENDING:
        raise ValueError(
            f"Session {session_id!r} is in state {member['state']!r}, not 'pending'. "
            f"Already accepted or has left."
        )
    record = {
        "kind": MEMBER,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "session_id": session_id,
        "session_name": _resolve_session_name(session_id) or session_id[:8],
        "state": ACCEPTED,
    }
    _append(chat_id, record)
    log.info("chats: %s accepted %s", session_id, chat_id)
    return record


def send_message(chat_id: str, sender_session_id: str, body: str) -> dict[str, Any]:
    """Append a message. Sender must be an accepted member; otherwise 403-ish ValueError."""
    sender_session_id = _resolve_or_uuid(sender_session_id)
    room = load_room(chat_id)
    member = room["members"].get(sender_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {sender_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can send messages."
        )
    record = {
        "kind": MSG,
        "event_id": _new_event_id(),
        "id": "msg-" + uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "chat_id": chat_id,
        "sender_id": sender_session_id,
        "sender_name": member.get("session_name") or sender_session_id[:8],
        "body": body,
    }
    _append(chat_id, record)
    log.info("chats: msg from %s to %s", sender_session_id, chat_id)
    return record


def history(
    chat_id: str,
    requester_session_id: str,
    *,
    limit: int = 50,
    since_event_id: str | None = None,
) -> list[dict[str, Any]]:
    """Return messages visible to requester. Must be an accepted member."""
    requester_session_id = _resolve_or_uuid(requester_session_id)
    room = load_room(chat_id)
    member = room["members"].get(requester_session_id)
    if not member or member["state"] != ACCEPTED:
        raise ValueError(
            f"Session {requester_session_id!r} is not an accepted member of {chat_id!r}; "
            f"cannot read history."
        )
    msgs = room["messages"]
    if since_event_id:
        # Skip everything up to and including since_event_id.
        idx = next((i for i, m in enumerate(msgs) if m.get("event_id") == since_event_id), None)
        if idx is not None:
            msgs = msgs[idx + 1 :]
    return msgs[-limit:]


def leave(chat_id: str, session_id: str) -> dict[str, Any]:
    """Mark caller as `left`. They no longer receive notifications."""
    session_id = _resolve_or_uuid(session_id)
    room = load_room(chat_id)
    member = room["members"].get(session_id)
    if not member:
        raise ValueError(
            f"Session {session_id!r} is not a member of {chat_id!r}; nothing to leave."
        )
    if member["state"] in (LEFT, REMOVED):
        raise ValueError(
            f"Session {session_id!r} already in state {member['state']!r}; cannot leave again."
        )
    record = {
        "kind": MEMBER,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "session_id": session_id,
        "session_name": member.get("session_name"),
        "state": LEFT,
    }
    _append(chat_id, record)
    log.info("chats: %s left %s", session_id, chat_id)
    return record


def delete(chat_id: str, by_session_id: str) -> dict[str, Any]:
    """Archive the chat JSONL. Only the creator can call this."""
    by_session_id = _resolve_or_uuid(by_session_id)
    room = load_room(chat_id)
    creator = room["meta"].get("created_by")
    if creator != by_session_id:
        raise ValueError(
            f"Only the chat creator ({creator!r}) can delete {chat_id!r}. "
            f"Non-creators should use chat_leave instead."
        )
    src = _chat_path(chat_id)
    dst = _archive_dir() / f"{chat_id}.jsonl"
    if dst.exists():
        dst = _archive_dir() / f"{chat_id}-{_now_iso().replace(':', '_')}.jsonl"
    shutil.move(str(src), str(dst))
    log.info("chats: deleted %s (archived to %s)", chat_id, dst)
    return {"chat_id": chat_id, "archived_to": str(dst), "deleted_at": _now_iso()}


def my_chats(session_id: str) -> list[dict[str, Any]]:
    """List chats where session is an accepted member, with brief metadata."""
    session_id = _resolve_or_uuid(session_id)
    out: list[dict[str, Any]] = []
    if not _chat_dir().exists():
        return out
    for path in _chat_dir().glob("chat-*.jsonl"):
        chat_id = path.stem
        try:
            room = load_room(chat_id)
        except ValueError:
            continue
        member = room["members"].get(session_id)
        if not member or member["state"] not in (PENDING, ACCEPTED):
            continue
        out.append(
            {
                "chat_id": chat_id,
                "title": room["meta"].get("title"),
                "my_state": member["state"],
                "member_count": sum(1 for m in room["members"].values() if m["state"] == ACCEPTED),
                "message_count": len(room["messages"]),
                "last_message_ts": room["messages"][-1]["ts"] if room["messages"] else None,
            }
        )
    out.sort(key=lambda c: c["last_message_ts"] or "", reverse=True)
    return out


# ---------------------------------------------------------------------------
# Pub/sub for SSE — in-memory queue per active subscriber
# ---------------------------------------------------------------------------

# session_id → set[asyncio.Queue]. One queue per active subscription.
_subscribers: dict[str, set[asyncio.Queue]] = {}


def _broadcast(chat_id: str, record: dict[str, Any]) -> None:
    """Push a new chat event to every accepted member's active subscribers.

    Sender's own session is included — the subprocess MCP filters its own
    messages out before emitting channel notifications. Doing the filter
    server-side would miss the case where sender has multiple subprocesses
    (e.g., two terminals open).
    """
    try:
        room = load_room(chat_id)
    except ValueError:
        return
    for sid, member in room["members"].items():
        if member["state"] != ACCEPTED:
            continue
        queues = _subscribers.get(sid)
        if not queues:
            continue
        for q in queues:
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                log.warning("chats: dropping event for %s (queue full)", sid)


async def subscribe(session_id: str, since_event_id: str | None = None) -> Any:
    """Async generator yielding chat events for this session.

    On connect, replays events from since_event_id (across all chats this
    session is accepted in) for backfill, then streams new events as they
    arrive. Yielding stops only when the caller cancels.
    """
    session_id = _resolve_or_uuid(session_id)
    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    _subscribers.setdefault(session_id, set()).add(queue)
    try:
        if since_event_id:
            # v1.1 follow-up: when since_event_id is unrecognized in any
            # chat (cursor older than chat history, archived chat,
            # cross-chat id confusion), we silently skip backfill for
            # that chat. The JSONL is append-only so this *shouldn't*
            # happen in practice, but the silent gap risks "phantom
            # missing messages" with no signal. Add a sentinel record
            # like {"kind": "backfill_gap", "chat_id": ...} so the
            # subprocess can render "events older than your cursor were
            # not found in this chat — call chat_history for full
            # transcript" instead of just delivering nothing.
            for chat_meta in my_chats(session_id):
                chat_id = chat_meta["chat_id"]
                lines = _read(chat_id)
                idx = next(
                    (i for i, line in enumerate(lines) if line.get("event_id") == since_event_id),
                    None,
                )
                if idx is None:
                    continue
                for line in lines[idx + 1 :]:
                    yield line
        while True:
            record = await queue.get()
            yield record
    finally:
        _subscribers[session_id].discard(queue)
        if not _subscribers[session_id]:
            _subscribers.pop(session_id, None)
