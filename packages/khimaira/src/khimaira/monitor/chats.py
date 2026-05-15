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
REJECTED = "rejected"  # invitee declined; can be re-invited later
LEFT = "left"
REMOVED = "removed"
TRANSFERRED_OUT = "transferred-out"  # session handed membership to another via transfer_membership

# Sender id used for daemon-synthesized system messages (e.g. the
# transfer_membership broadcast). Stable constant so future audit /
# filter code can match without sniffing body text.
SYSTEM_SENDER_ID = "khimaira-system"

# Line kinds in the JSONL.
META = "meta"
MEMBER = "member"
MSG = "msg"
TASK = "task"
TASK_UPDATE = "task_update"
TASK_SIGNAL = "task_signal"

# Task status values.
TASK_PENDING = "pending"
TASK_IN_PROGRESS = "in_progress"
TASK_DONE = "done"
TASK_APPROVED = "approved"
TASK_CHANGES_REQUESTED = "changes_requested"

# (from_status, to_status) → roles allowed to perform the transition.
# "master" = chat creator; "assignee_or_any" = assignee if set, else any accepted member.
_TASK_TRANSITIONS: dict[tuple[str, str], set[str]] = {
    (TASK_PENDING, TASK_IN_PROGRESS): {"assignee_or_any"},
    (TASK_IN_PROGRESS, TASK_DONE): {"assignee_or_any"},
    (TASK_DONE, TASK_APPROVED): {"master"},
    (TASK_DONE, TASK_CHANGES_REQUESTED): {"master"},
    (TASK_CHANGES_REQUESTED, TASK_IN_PROGRESS): {"assignee_or_any"},
}


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

    # Creator auto-accepted; others either auto-accepted (allowlist) or pending.
    for sid in resolved_members:
        if sid == creator_session_id:
            state = ACCEPTED
        elif should_auto_accept(sid, creator_session_id):
            state = ACCEPTED
            log.info(
                "chats: %s auto-accepted invite from %s into %s",
                sid,
                creator_session_id,
                chat_id,
            )
        else:
            state = PENDING
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


def reject(chat_id: str, session_id: str) -> dict[str, Any]:
    """Decline a pending invite. The chat continues without this session;
    creator can re-invite later if desired."""
    session_id = _resolve_or_uuid(session_id)
    room = load_room(chat_id)
    member = room["members"].get(session_id)
    if not member:
        raise ValueError(
            f"Session {session_id!r} is not a member of {chat_id!r}; nothing to reject."
        )
    if member["state"] != PENDING:
        raise ValueError(
            f"Session {session_id!r} is in state {member['state']!r}, not 'pending'. "
            f"Can only reject a pending invite."
        )
    record = {
        "kind": MEMBER,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "session_id": session_id,
        "session_name": _resolve_session_name(session_id) or session_id[:8],
        "state": REJECTED,
    }
    _append(chat_id, record)
    log.info("chats: %s rejected %s", session_id, chat_id)
    return record


def latest_pending_chat_id(session_id: str) -> str | None:
    """Find the most-recently-invited pending chat for this session.

    Used by `/khimaira-chat-accept` and `/khimaira-chat-reject` so the
    user doesn't have to know the chat_id — the latest pending invite
    is almost always the one they meant to act on. Returns None if no
    pending invites; the slash command should surface that as
    'no pending invites'.
    """
    session_id = _resolve_or_uuid(session_id)
    chats = my_chats(session_id)
    pending = [c for c in chats if c.get("my_state") == PENDING]
    if not pending:
        return None
    pending.sort(key=lambda c: c.get("last_message_ts") or "", reverse=True)
    return pending[0]["chat_id"]


# Sanitizer lives in sessions.py (re-used by post_answer / post_notice / chat
# message bodies). Re-exported here for backward-compat with existing tests.
_sanitize_message_body = sessions_mod.sanitize_agent_text


def send_message(
    chat_id: str,
    sender_session_id: str,
    body: str,
    *,
    to: list[str] | None = None,
) -> dict[str, Any]:
    """Append a message. Sender must be an accepted member.

    Optional `to`: list of session_ids/names. When set, real-time SSE
    broadcast goes only to those sessions (plus sender for echo-drop).
    The message is still appended to the JSONL — chat_history shows it
    for everyone. Private-in-real-time, public-in-record.
    """
    sender_session_id = _resolve_or_uuid(sender_session_id)
    room = load_room(chat_id)
    member = room["members"].get(sender_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {sender_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can send messages."
        )

    resolved_to: list[str] | None = None
    if to:
        resolved_to = []
        for r in to:
            rid = _resolve_or_uuid(r)
            rmember = room["members"].get(rid)
            if not rmember or rmember["state"] != ACCEPTED:
                rstate = (rmember or {}).get("state", "non-member")
                raise ValueError(
                    f"Recipient {r!r} is {rstate!r} in {chat_id!r}; "
                    f"only accepted members can be `to` targets."
                )
            resolved_to.append(rid)

    record = {
        "kind": MSG,
        "event_id": _new_event_id(),
        "id": "msg-" + uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "chat_id": chat_id,
        "sender_id": sender_session_id,
        "sender_name": member.get("session_name") or sender_session_id[:8],
        "body": _sanitize_message_body(body),
        "to": resolved_to,
    }
    _append(chat_id, record)
    log.info("chats: msg from %s to %s (to=%s)", sender_session_id, chat_id, resolved_to or "*")
    return record


# ---------------------------------------------------------------------------
# Phase B: tasks
# ---------------------------------------------------------------------------


def create_task(
    chat_id: str,
    sender_session_id: str,
    body: str,
    assignee_session_id: str | None = None,
) -> dict[str, Any]:
    """Append a TASK record (status=pending). Sender must be an accepted member;
    if assignee_session_id is set, that session must also be accepted."""
    sender_session_id = _resolve_or_uuid(sender_session_id)
    room = load_room(chat_id)
    member = room["members"].get(sender_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {sender_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can create tasks."
        )

    assignee_resolved = None
    assignee_name = None
    if assignee_session_id is not None:
        assignee_resolved = _resolve_or_uuid(assignee_session_id)
        amember = room["members"].get(assignee_resolved)
        if not amember or amember["state"] != ACCEPTED:
            astate = (amember or {}).get("state", "non-member")
            raise ValueError(
                f"Assignee {assignee_session_id!r} is {astate!r} in {chat_id!r}; "
                f"only accepted members can be assignees."
            )
        assignee_name = amember.get("session_name") or assignee_resolved[:8]

    record = {
        "kind": TASK,
        "event_id": _new_event_id(),
        "id": "task-" + uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "chat_id": chat_id,
        "sender_id": sender_session_id,
        "sender_name": member.get("session_name") or sender_session_id[:8],
        "body": _sanitize_message_body(body),
        "assignee_id": assignee_resolved,
        "assignee_name": assignee_name,
        "status": TASK_PENDING,
    }
    _append(chat_id, record)
    log.info(
        "chats: task %s created in %s by %s (assignee=%s)",
        record["id"],
        chat_id,
        sender_session_id,
        assignee_resolved or "(none)",
    )
    return record


def update_task_status(
    chat_id: str,
    task_id: str,
    by_session_id: str,
    new_status: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Append a TASK_UPDATE record. Validates the from→to transition and
    the caller's role (master vs assignee vs accepted-member)."""
    by_session_id = _resolve_or_uuid(by_session_id)
    room = load_room(chat_id)
    member = room["members"].get(by_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {by_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can update task status."
        )

    task_record = None
    current_status = None
    for line in _read(chat_id):
        k = line.get("kind")
        if k == TASK and line.get("id") == task_id:
            task_record = line
            current_status = line.get("status")
        elif k == TASK_UPDATE and line.get("task_id") == task_id:
            current_status = line.get("status")

    if task_record is None:
        raise ValueError(f"No task with id={task_id!r} in {chat_id!r}.")

    transition = (current_status, new_status)
    allowed_roles = _TASK_TRANSITIONS.get(transition)
    if allowed_roles is None:
        valid_targets = [t for (f, t) in _TASK_TRANSITIONS if f == current_status]
        raise ValueError(
            f"Invalid transition {current_status!r} → {new_status!r} for task {task_id!r}. "
            f"From {current_status!r} you can go to: {valid_targets or '(terminal)'}."
        )

    creator = room["meta"].get("created_by")
    is_master = by_session_id == creator
    assignee = task_record.get("assignee_id")
    is_assignee = assignee is not None and by_session_id == assignee

    authorized = False
    if "master" in allowed_roles and is_master:
        authorized = True
    if "assignee_or_any" in allowed_roles and (assignee is None or is_assignee):
        authorized = True

    if not authorized:
        raise ValueError(
            f"Session {by_session_id!r} not authorized for {current_status!r} → {new_status!r} "
            f"on task {task_id!r}. Required roles: {sorted(allowed_roles)}."
        )

    record = {
        "kind": TASK_UPDATE,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "task_id": task_id,
        "status": new_status,
        "by_session_id": by_session_id,
        "by_name": member.get("session_name") or by_session_id[:8],
        "note": note,
    }
    _append(chat_id, record)
    log.info(
        "chats: task %s %s → %s by %s in %s",
        task_id,
        current_status,
        new_status,
        by_session_id,
        chat_id,
    )
    return record


def signal_task_start(
    chat_id: str,
    task_id: str,
    by_session_id: str,
    note: str | None = None,
) -> dict[str, Any]:
    """Master-only "go" signal on a pending task.

    Doesn't change task status — the assignee still drives pending → in_progress.
    Just appends a TASK_SIGNAL record so the assignee sees a channel block
    indicating they're cleared to start (closes the friction where v1 had no
    first-class signal beyond free-form chat_send).

    Validates: caller is an accepted member; caller is the chat creator
    (master); task exists; task is currently in pending status.

    The `signal` field is hardcoded to "start" in v1 — leaves room for future
    signals (priority bump, deadline warning, abandonment) without renaming.
    """
    by_session_id = _resolve_or_uuid(by_session_id)
    room = load_room(chat_id)
    member = room["members"].get(by_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {by_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can signal tasks."
        )

    task_record = None
    current_status = None
    for line in _read(chat_id):
        k = line.get("kind")
        if k == TASK and line.get("id") == task_id:
            task_record = line
            current_status = line.get("status")
        elif k == TASK_UPDATE and line.get("task_id") == task_id:
            current_status = line.get("status")

    if task_record is None:
        raise ValueError(f"No task with id={task_id!r} in {chat_id!r}.")

    if current_status != TASK_PENDING:
        raise ValueError(
            f"Task {task_id!r} in {chat_id!r} is {current_status!r}, not 'pending'; "
            f"signal_start only valid on pending tasks."
        )

    creator = room["meta"].get("created_by")
    if by_session_id != creator:
        raise ValueError(
            f"Session {by_session_id!r} is not the master (creator={creator!r}) of "
            f"{chat_id!r}; only the master can signal start on pending tasks."
        )

    record = {
        "kind": TASK_SIGNAL,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "task_id": task_id,
        "signal": "start",
        "by_session_id": by_session_id,
        "by_name": member.get("session_name") or by_session_id[:8],
        "assignee_id": task_record.get("assignee_id"),
        "note": note,
    }
    _append(chat_id, record)
    log.info("chats: task %s signal=start by %s in %s", task_id, by_session_id, chat_id)
    return record


def task_status(chat_id: str, requester_session_id: str) -> list[dict[str, Any]]:
    """Return all tasks in this chat with their current folded status.
    Requester must be an accepted member."""
    requester_session_id = _resolve_or_uuid(requester_session_id)
    room = load_room(chat_id)
    member = room["members"].get(requester_session_id)
    if not member or member["state"] != ACCEPTED:
        raise ValueError(
            f"Session {requester_session_id!r} is not an accepted member of {chat_id!r}; "
            f"cannot read task status."
        )

    tasks: dict[str, dict[str, Any]] = {}
    for line in _read(chat_id):
        k = line.get("kind")
        if k == TASK:
            tid = line["id"]
            tasks[tid] = {
                "task_id": tid,
                "body": line.get("body"),
                "assignee_id": line.get("assignee_id"),
                "assignee_name": line.get("assignee_name"),
                "sender_id": line.get("sender_id"),
                "sender_name": line.get("sender_name"),
                "status": line.get("status"),
                "created_ts": line.get("ts"),
                "last_update_ts": line.get("ts"),
                "last_note": None,
            }
        elif k == TASK_UPDATE:
            tid = line.get("task_id")
            if tid in tasks:
                tasks[tid]["status"] = line.get("status")
                tasks[tid]["last_update_ts"] = line.get("ts")
                tasks[tid]["last_note"] = line.get("note")

    return sorted(tasks.values(), key=lambda t: t["created_ts"] or "")


# ---------------------------------------------------------------------------
# Phase B: auto-accept allowlist
# ---------------------------------------------------------------------------


def _auto_accept_path(session_id: str) -> Path:
    return _chat_dir() / f"auto-accept-{session_id}.json"


def _auto_accept_by_name_path(name: str) -> Path:
    return _chat_dir() / f"auto-accept-by-name-{name}.json"


def set_auto_accept(session_id: str, allowlist: list[str]) -> dict[str, Any]:
    """Replace this session's auto-accept allowlist. Pass [] to clear.

    If the session has a friendly name (via `session_set_name`), the
    allowlist is persisted under that name so it survives session UUID
    churn — Claude Code regenerates UUIDs each boot, but names are
    durable. Unnamed sessions fall back to UUID-keyed storage (works
    only within the session's lifetime).
    """
    session_id = _resolve_or_uuid(session_id)
    _ensure_dir()
    payload = {"allow": list(allowlist), "updated_at": _now_iso()}
    name = _resolve_session_name(session_id)
    if name:
        _auto_accept_by_name_path(name).write_text(json.dumps(payload), encoding="utf-8")
        log.info(
            "chats: set auto-accept for %s (by-name=%s) → %d allowed peers",
            session_id,
            name,
            len(allowlist),
        )
    else:
        _auto_accept_path(session_id).write_text(json.dumps(payload), encoding="utf-8")
        log.info(
            "chats: set auto-accept for %s (UUID-only, unnamed) → %d allowed peers",
            session_id,
            len(allowlist),
        )
    return payload


def get_auto_accept(session_id: str) -> dict[str, Any]:
    """Read this session's auto-accept allowlist; returns {'allow': []} if unset.

    Prefers the durable by-name file when the session has a name —
    this is what makes the allowlist survive session UUID churn.
    Falls back to UUID-keyed file for unnamed sessions or legacy state.
    """
    session_id = _resolve_or_uuid(session_id)
    name = _resolve_session_name(session_id)
    if name:
        path = _auto_accept_by_name_path(name)
        if path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pass
    path = _auto_accept_path(session_id)
    if not path.is_file():
        return {"allow": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"allow": []}


def apply_auto_accept_by_name(session_id: str, name: str) -> dict[str, Any]:
    """Surface a by-name allowlist file for a freshly-named session.

    Called by the chat MCP subprocess at boot, right after the dual-name
    auto-bridge detects `-n NAME` and registers it via `set_session_name`.
    Functionally a no-op — `get_auto_accept` already prefers the by-name
    file when the session has a name — but it returns whether the file
    existed so the caller can log the boot-time application.
    """
    session_id = _resolve_or_uuid(session_id)
    path = _auto_accept_by_name_path(name)
    if not path.is_file():
        return {"applied": False, "allow": []}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"applied": False, "allow": []}
    log.info(
        "chats: by-name allowlist surfaced for %s (name=%s) — %d allowed peers",
        session_id,
        name,
        len(data.get("allow", [])),
    )
    return {"applied": True, **data}


def should_auto_accept(invitee_session_id: str, inviter_session_id: str) -> bool:
    """True iff invitee has inviter (by UUID OR friendly name) in their allowlist."""
    allow = get_auto_accept(invitee_session_id).get("allow", [])
    if not allow:
        return False
    if inviter_session_id in allow:
        return True
    inviter_name = _resolve_session_name(inviter_session_id)
    return bool(inviter_name and inviter_name in allow)


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


def transfer_membership(
    chat_id: str,
    from_session_id: str,
    to_session_id: str,
) -> dict[str, Any]:
    """Hand `from`'s chat membership to `to`, preserving full history.

    Used by /khimaira-transfer-session so an orchestrator session can hand
    off ALL its active chats to a fresh successor session in one shot. The
    receiving session lands ACCEPTED immediately (no handshake); the donor
    becomes TRANSFERRED_OUT (no future pushes, no send rights). Other
    accepted members see a synthetic system message in the chat so the
    handoff is visible in transcript and post-hoc audit.

    **Phase B v1.3 creator-role propagation:** when `from` is the chat
    creator (master), the master role transfers with the membership — a
    fresh META record is emitted with `created_by` / `created_by_name`
    updated to `to`. Without this, the successor inherits membership but
    not approval-gating rights (chat_task_update done→approved checks
    `room.meta.created_by`). Non-creator transfers leave META alone.

    State transitions are passive: the `state != ACCEPTED` gate in
    `_broadcast` and `send_message` handles cutoff — no special teardown.
    A previously-transferred-out session can be re-invited, accept, and
    transfer out again — each transfer is independent.

    Raises:
        ValueError: if `from` is not currently ACCEPTED in this chat (403),
            if `to` resolves to no known session (404), or if `to` is
            already ACCEPTED in this chat (409 — would be a no-op clash).
    """
    from_session_id = _resolve_or_uuid(from_session_id)
    to_session_id = _resolve_or_uuid(to_session_id)
    room = load_room(chat_id)

    from_member = room["members"].get(from_session_id)
    if not from_member or from_member["state"] != ACCEPTED:
        state = (from_member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {from_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can transfer their membership."
        )

    to_member = room["members"].get(to_session_id)
    if to_member and to_member["state"] == ACCEPTED:
        raise ValueError(
            f"Session {to_session_id!r} is already accepted in {chat_id!r}; "
            f"transfer target must not already be a member."
        )

    transfer_id = "xfer-" + uuid.uuid4().hex[:12]
    from_name = from_member.get("session_name") or from_session_id[:8]
    to_name = _resolve_session_name(to_session_id) or to_session_id[:8]
    ts = _now_iso()

    # Phase B v1.3: propagate master/creator role on creator-transfer.
    # `load_room` folds META last-write-wins, so a fresh META record with
    # updated `created_by` swaps the master designation atomically with
    # the membership transfer. Surfaced organically when the v1.2
    # khimaira-21 → khimaira-0 transfer left the successor with
    # membership but no approval-gating rights — the v1.2 round-trip test
    # never read META post-transfer, so the gap was invisible.
    creator_meta_update: dict[str, Any] | None = None
    existing_meta = room.get("meta") or {}
    if existing_meta.get("created_by") == from_session_id:
        creator_meta_update = dict(existing_meta)
        creator_meta_update["created_by"] = to_session_id
        creator_meta_update["created_by_name"] = to_name
        creator_meta_update["event_id"] = _new_event_id()
        creator_meta_update["ts"] = ts

    out_record = {
        "kind": MEMBER,
        "event_id": _new_event_id(),
        "ts": ts,
        "chat_id": chat_id,
        "session_id": from_session_id,
        "session_name": from_name,
        "state": TRANSFERRED_OUT,
        "transferred_to": to_session_id,
        "transfer_id": transfer_id,
    }
    in_record = {
        "kind": MEMBER,
        "event_id": _new_event_id(),
        "ts": ts,
        "chat_id": chat_id,
        "session_id": to_session_id,
        "session_name": to_name,
        "state": ACCEPTED,
        "invited_by": from_session_id,
        "transferred_from": from_session_id,
        "transfer_id": transfer_id,
    }
    sys_msg = {
        "kind": MSG,
        "event_id": _new_event_id(),
        "id": "msg-" + uuid.uuid4().hex[:12],
        "ts": ts,
        "chat_id": chat_id,
        "sender_id": SYSTEM_SENDER_ID,
        "sender_name": SYSTEM_SENDER_ID,
        "body": f"📦 {from_name} transferred this chat to {to_name} — full context handoff",
        "to": None,
        "meta": {
            "event_type": "transfer",
            "transfer_id": transfer_id,
            "from": from_session_id,
            "to": to_session_id,
        },
    }
    if creator_meta_update is not None:
        _append(chat_id, creator_meta_update)
    _append(chat_id, out_record)
    _append(chat_id, in_record)
    _append(chat_id, sys_msg)
    log.info(
        "chats: transfer chat=%s from=%s to=%s transfer_id=%s%s",
        chat_id,
        from_session_id,
        to_session_id,
        transfer_id,
        " (creator role propagated)" if creator_meta_update is not None else "",
    )
    return {
        "chat_id": chat_id,
        "transfer_id": transfer_id,
        "from": out_record,
        "to": in_record,
    }


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


# ---------------------------------------------------------------------------
# PPID-bridged session registration — solves the lazy-registration problem
# for cold sessions. Claude Code spawns the chat MCP subprocess at session
# boot, but the subprocess can't know its session_id from env (no
# CLAUDE_SESSION_ID is set). The SessionStart hook *does* know the
# session_id; both the hook and the subprocess share the same parent
# process (Claude Code's PID). The hook posts {ppid, session_id} here at
# boot; the subprocess reads it back at startup and registers + subscribes
# without waiting for the agent's first chat tool call.
#
# Map is TTL'd (5 min) so stale entries don't accumulate.
# ---------------------------------------------------------------------------

_PPID_TTL_SECONDS = 300  # 5 min — subprocess should fetch within this window
# ppid → (session_id, registered_at_unix_ts)
_pending_session_by_ppid: dict[int, tuple[str, float]] = {}


def register_session_by_ppid(ppid: int, session_id: str) -> None:
    """SessionStart hook calls this with the session_id it knows about."""
    import time

    _gc_pending_sessions()
    _pending_session_by_ppid[ppid] = (session_id, time.time())


def lookup_session_by_ppid(ppid: int) -> str | None:
    """Subprocess at startup calls this to find its session_id.
    Returns None if no entry or entry expired."""
    _gc_pending_sessions()
    entry = _pending_session_by_ppid.get(ppid)
    if entry is None:
        return None
    return entry[0]


def _gc_pending_sessions() -> None:
    """Drop entries older than _PPID_TTL_SECONDS."""
    import time

    now = time.time()
    expired = [
        ppid
        for ppid, (_sid, ts) in _pending_session_by_ppid.items()
        if now - ts > _PPID_TTL_SECONDS
    ]
    for ppid in expired:
        _pending_session_by_ppid.pop(ppid, None)


def _broadcast(chat_id: str, record: dict[str, Any]) -> None:
    """Push a new chat event to the right subscribers.

    Routing rules:
      - kind=member with state=pending → push ONLY to the invitee. They
        haven't accepted yet, so the existing "accepted only" filter
        would skip them — but we WANT them to see "you've been invited"
        without having to poll chat_my_chats. Subprocess maps this
        record to a channel notification with kind=invite.
      - All other records (msg, member transitions to accepted/left/etc,
        meta updates) → push to every accepted member.

    Sender's own session is included for non-invite records — the
    subprocess MCP filters its own messages out before emitting channel
    notifications. Server-side filtering would miss the case where
    sender has multiple subprocesses (e.g., two terminals open).
    """
    try:
        room = load_room(chat_id)
    except ValueError:
        return

    # Invite-targeted broadcast: deliver to invitee only.
    if record.get("kind") == MEMBER and record.get("state") == PENDING:
        invitee = record.get("session_id")
        for q in _subscribers.get(invitee, ()):
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                log.warning("chats: dropping invite for %s (queue full)", invitee)
        return

    # Phase B: per-recipient targeting on msg records.
    targeted: set[str] | None = None
    if record.get("kind") == MSG and record.get("to"):
        targeted = set(record["to"]) | {record.get("sender_id")}

    # Default broadcast: to every accepted member (or filtered by `to`).
    for sid, member in room["members"].items():
        if member["state"] != ACCEPTED:
            continue
        if targeted is not None and sid not in targeted:
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
        # Pending-invite catch-up. The invite broadcast at create_room
        # time goes to active subscribers only — if the invitee's
        # subprocess wasn't subscribed yet (lazy registration hadn't
        # fired), the broadcast lands in an empty queue. Replay any
        # currently-pending invites for this session on subscribe so
        # the subprocess always sees its outstanding invites. Cheap
        # because pending invites are bounded (one per chat the user
        # is invited to but hasn't accepted/rejected).
        for chat_meta in my_chats(session_id):
            if chat_meta.get("my_state") != PENDING:
                continue
            chat_id = chat_meta["chat_id"]
            for line in _read(chat_id):
                if (
                    line.get("kind") == MEMBER
                    and line.get("session_id") == session_id
                    and line.get("state") == PENDING
                ):
                    yield line
                    break  # only the most recent pending record per chat

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
