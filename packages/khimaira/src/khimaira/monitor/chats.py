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
TASK_VERDICT = "task_verdict"  # B3 structured gate-verdict event

# Task status values.
TASK_PENDING = "pending"
TASK_IN_PROGRESS = "in_progress"
TASK_DONE = "done"
TASK_APPROVED = "approved"
TASK_CHANGES_REQUESTED = "changes_requested"
TASK_CANCELLED = "cancelled"

# Phase B v2: explicit member roles in room.meta.member_roles.
# Single-master-with-delegation invariant (audit F2): exactly one session
# holds ROLE_MASTER at a time. v1-era chats with no explicit `member_roles`
# fall back to implicit master = `created_by` via `_is_master`.
ROLE_MASTER = "master"
ROLE_AGENT = "agent"
ROLE_OBSERVER = "observer"
ROLE_CRITIC = "critic"
ROLE_ARCHITECT = "architect"
ROLE_INTAKE = "intake"
ROLE_ANALYST = "analyst"
ROLE_VERIFIER = "verifier"
ROLE_TRACKER = "tracker"
ROLE_MEMBER = "member"  # neutral catch-all; empty Themis ruleset (see member.yaml)
try:
    # Single-source registry: themis.data.VALID_ROLES is glob-derived from rule yamls
    # and auto-includes prefixed leads (e.g. jp-frontend-lead). Reuses the existing
    # khimaira→themis dependency (_call_engine already lazy-imports themis.engine).
    # D7 fallback: if themis is not installed, use the core hardcoded set — leads
    # don't enforce without themis anyway.
    from themis.data import VALID_ROLES as _VALID_ROLES
except ImportError:
    # D7 fallback: include all named ROLE_* constants so the set won't lag future
    # additions of named roles (e.g. analyst/verifier/tracker were missing pre-fix).
    # Lead roles (dynamic, prefixed) can't be enumerated here; they require themis.
    _VALID_ROLES: frozenset[str] = frozenset(
        {
            ROLE_MASTER,
            ROLE_AGENT,
            ROLE_OBSERVER,
            ROLE_CRITIC,
            ROLE_ARCHITECT,
            ROLE_INTAKE,
            ROLE_ANALYST,
            ROLE_VERIFIER,
            ROLE_TRACKER,
            ROLE_MEMBER,
        }
    )

# Phase B v1.5: recommended model + effort budget per role.
# Used by `_emit_role_directive` to surface slash-command suggestions to the
# target session at role-change points (chat_create_room, chat_grant_role,
# chat_set_creator, chat_transfer_membership). Critic is intentionally absent
# — no default; orchestrator picks per scope, and the directive emit is a
# silent no-op when the role has no entry here.
ROLE_BUDGET: dict[str, dict[str, str]] = {
    ROLE_MASTER: {"model": "opus", "effort": "max"},
    ROLE_AGENT: {"model": "sonnet", "effort": "medium"},
    ROLE_OBSERVER: {"model": "haiku", "effort": "low"},
    ROLE_ARCHITECT: {"model": "opus", "effort": "max"},  # synthesis/design sidecar
    ROLE_INTAKE: {"model": "sonnet", "effort": "medium"},  # user-facing front-end
    ROLE_ANALYST: {"model": "opus", "effort": "max"},  # spec disambiguation, idle-by-default
    ROLE_VERIFIER: {"model": "opus", "effort": "max"},  # test coverage gate, idle-by-default
    ROLE_TRACKER: {"model": "haiku", "effort": "medium"},  # checklist curator + Linear filer
    # Domain leads: sonnet/medium default; escalate to opus only for rare decomposition-heavy
    # initiatives (per lead-role.md.j2 convention). Entries here must match the themis rule
    # yaml filenames in packages/themis/src/themis/rules/ — ROLE_BUDGET keys must be in
    # _VALID_ROLES (enforced by test_role_budget_keys_subset_of_valid_roles).
    "backend-lead": {"model": "sonnet", "effort": "medium"},
    "data-lead": {"model": "sonnet", "effort": "medium"},
    "jp-backend-lead": {"model": "sonnet", "effort": "medium"},
    "jp-data-lead": {"model": "sonnet", "effort": "medium"},
    "jp-frontend-lead": {"model": "sonnet", "effort": "medium"},
}


def infer_role_from_name(session_name: str) -> str | None:
    """Return the validated role from a session name, or None if not a registry role.

    Strips a trailing -<number> suffix via rsplit, then validates the remainder
    against _VALID_ROLES (derived from themis rule yamls). Falls back to checking
    the full name if no numeric suffix is present.

    "jp-frontend-lead-1" → "jp-frontend-lead" ✓
    "agent-1" → "agent" ✓
    "janice-0" → "janice" ✗ → None ✓
    """
    parts = session_name.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        candidate = parts[0]
        if candidate in _VALID_ROLES:
            return candidate
    if session_name in _VALID_ROLES:
        return session_name
    return None


# (from_status, to_status) → roles allowed to perform the transition.
# "master" = chat creator; "assignee_or_any" = assignee if set, else any accepted member.
_TASK_TRANSITIONS: dict[tuple[str, str], set[str]] = {
    (TASK_PENDING, TASK_IN_PROGRESS): {"assignee_or_any"},
    (TASK_IN_PROGRESS, TASK_DONE): {"assignee_or_any"},
    (TASK_DONE, TASK_APPROVED): {"master"},
    (TASK_DONE, TASK_CHANGES_REQUESTED): {"master"},
    (TASK_CHANGES_REQUESTED, TASK_IN_PROGRESS): {"assignee_or_any"},
    (TASK_PENDING, TASK_CANCELLED): {"master"},
    (TASK_IN_PROGRESS, TASK_CANCELLED): {"master"},
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


def _cursors_path() -> Path:
    return _chat_dir() / "cursors.jsonl"


# ---------------------------------------------------------------------------
# Per-(session_id, chat_id) delivery cursors
#
# Each SSE subscriber tracks the last event_id it successfully yielded
# per chat. On reconnect, backfill uses these cursors rather than the
# client-supplied Last-Event-ID header, which is global (single value)
# and therefore can't address per-chat positioning.
#
# Cursor advances AFTER the SSE yield succeeds (in api/chats.py's
# event_generator) — not on enqueue. This means a ClientDisconnect during
# yield leaves the cursor at the prior position, so the next reconnect
# backfills from there with no loss.
# ---------------------------------------------------------------------------

_CURSORS: dict[tuple[str, str], str] = {}  # (session_id, chat_id) → last yielded event_id
_CURSORS_DIRTY: bool = False               # true when _CURSORS has unsaved changes


def _cursor_for(session_id: str, chat_id: str) -> str | None:
    """Return the last yielded event_id for this (session, chat) pair, or None."""
    return _CURSORS.get((session_id, chat_id))


def _advance_cursor(session_id: str, chat_id: str, event_id: str) -> None:
    """Update the cursor after a successful SSE yield. Call-site: api/chats.py event_generator."""
    global _CURSORS_DIRTY
    _CURSORS[(session_id, chat_id)] = event_id
    _CURSORS_DIRTY = True


def load_cursors() -> None:
    """Read cursors.jsonl into _CURSORS at daemon startup.

    Takes the last entry per (session_id, chat_id) so repeated daemon
    restarts correctly restore the most-recently-advanced positions.
    Silently ignores missing or corrupt files — fail-open is correct
    (missing cursor → last-50 backfill, which is safe).
    """
    global _CURSORS_DIRTY
    path = _cursors_path()
    if not path.exists():
        return
    latest: dict[tuple[str, str], str] = {}
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                sid = entry.get("session_id")
                cid = entry.get("chat_id")
                eid = entry.get("last_event_id")
                if sid and cid and eid:
                    latest[(sid, cid)] = eid
            except (json.JSONDecodeError, KeyError):
                continue
    except OSError:
        return
    _CURSORS.update(latest)
    _CURSORS_DIRTY = False
    log.info("chats: loaded %d cursor(s) from disk", len(latest))


def save_cursors() -> None:
    """Persist _CURSORS to cursors.jsonl as a compact snapshot.

    Each call writes exactly one line per (session_id, chat_id) key —
    the current in-memory value. Atomic-rename ensures readers never
    see a partial write.
    """
    global _CURSORS_DIRTY
    if not _CURSORS:
        return
    path = _cursors_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    now_ts = datetime.now(UTC).isoformat()
    lines = [
        json.dumps({"session_id": sid, "chat_id": cid, "last_event_id": eid, "ts": now_ts},
                   separators=(",", ":"))
        for (sid, cid), eid in _CURSORS.items()
    ]

    tmp = path.with_suffix(".tmp")
    try:
        tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
        tmp.replace(path)
        _CURSORS_DIRTY = False
    except OSError:
        log.warning("chats: failed to persist cursors to %s", path)


def _resolve_sender_name(session_id: str, fallback: str) -> str:
    """Return the session's CURRENT friendly name from status.json.

    Reads only the status.json file for speed. Falls back to `fallback`
    (typically the stored sender_name snapshot) if the session is deleted
    or the file is unreadable. Used at both SSE publish-time (_broadcast)
    and read-time (api/chats.get_history) so names stay current after renames.
    """
    try:
        sd = sessions_mod._session_dir_read(session_id)
        if sd is None:
            return fallback
        status_path = sd / "status.json"
        data = json.loads(status_path.read_text())
        return data.get("name") or fallback
    except Exception:
        return fallback


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


def _emit_role_directive(
    chat_id: str,
    target_session_id: str,
    role: str,
    *,
    ts: str | None = None,
) -> None:
    """Phase B v1.5: append a role-grant directive as a system msg.

    Fires at role-change points (chat_create_room, chat_grant_role,
    chat_set_creator, chat_transfer_membership) to tell the target
    session their role changed and which `/model` + `/effort` slash
    commands to type. Targeted via `to=[target_session_id]` so SSE only
    pushes to the recipient — sibling agents see the role change via
    member_roles META, not via the slash-command guidance.

    **Silent-skip when role is not in ROLE_BUDGET** (currently just
    `critic`). The directive's purpose is slash-command guidance; for
    roles without a default, there's nothing to recommend, and the
    role-change is still visible via member_roles META. No-op return.

    `ts`: optional override so multi-emit calls (e.g. master-swap in
    chat_grant_role) can group two directives under the same timestamp
    for audit-pairing. Omit to let each emit stamp itself.

    Fire-and-forget; caller doesn't use the return value (None).
    """
    budget = ROLE_BUDGET.get(role)
    if budget is None:
        return  # no default for this role; silent skip
    body = (
        f"🎚️ Role updated: you are now {role}. "
        f"Recommended budget: /model {budget['model']}, /effort {budget['effort']}. "
        f"Type those in this window to match. "
        f"See docs/khimaira-chat.md#token-cost-budgeting."
    )
    record = {
        "kind": MSG,
        "event_id": _new_event_id(),
        "id": "msg-" + uuid.uuid4().hex[:12],
        "ts": ts or _now_iso(),
        "chat_id": chat_id,
        "sender_id": SYSTEM_SENDER_ID,
        "sender_name": SYSTEM_SENDER_ID,
        "body": body,
        "to": [target_session_id],
        "meta": {
            "event_type": "role_directive",
            "role": role,
            "target": target_session_id,
            "model": budget["model"],
            "effort": budget["effort"],
        },
    }
    _append(chat_id, record)
    log.info(
        "chats: role_directive chat=%s target=%s role=%s budget=%s",
        chat_id,
        target_session_id,
        role,
        budget,
    )


def _is_master(room: dict[str, Any], sid: str) -> bool:
    """Phase B v2 master check.

    Returns True iff `sid` holds the master role in this chat.

    Resolution order:
      1. If `room.meta.member_roles` is present (post-v2 chat OR a v1-era
         chat that has had any `chat_grant_role` call), it is the SOLE
         source of truth. Check `member_roles.get(sid) == ROLE_MASTER`.
      2. Otherwise (v1-era chat, never had an explicit role write), fall
         back to implicit master = `created_by`.

    First `chat_grant_role` call on a v1-era chat materializes the implicit
    master into the explicit dict, so the fallback fires only for chats
    that have NEVER had a role write. This removes the implicit/explicit
    duality after the first explicit role mutation — auditability +
    enforcement consistency.
    """
    member_roles = room["meta"].get("member_roles")
    if member_roles is not None:
        role = member_roles.get(sid)
        if role is not None:
            return role == ROLE_MASTER
        # member_roles present but sid absent — e.g. a creator omitted from the
        # dict at create_room (pre-#67 chats). Fall back to created_by so the
        # creator is never self-locked out of master authority. created_by
        # tracks the current master (transfer/resume swap it), so this stays
        # correct after dethroning. (#68)
    return room["meta"].get("created_by") == sid


_UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")

# Matches the canonical ack body that agents send when they confirm budget compliance:
#   ✅ ready [task-id: task-<hex>] | model=<name> effort=<name>
_ACK_RE = re.compile(
    r"✅ ready \[task-id:\s*(task-[a-f0-9]+)\]\s*\|\s*model=(\w+)\s+effort=(\w+)",
    re.IGNORECASE,
)


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
        elif kind in (MSG, TASK, TASK_UPDATE, TASK_SIGNAL, TASK_VERDICT):
            messages.append(line)
    return {"meta": meta, "members": members, "messages": messages}


def _resolve_session_name(session_id: str) -> str | None:
    """Look up friendly name from the session's status.json (best-effort)."""
    try:
        sd = sessions_mod._session_dir_read(session_id)
        if sd is None:
            return None
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


_VALID_TOPOLOGIES: frozenset[str] = frozenset({"flat", "hierarchical", "custom"})


def create_room(
    creator_session_id: str,
    member_session_ids: list[str],
    *,
    title: str | None = None,
    fresh: bool = False,
    topology: str = "flat",
    member_roles: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Create a new chat room. Creator is auto-`accepted`; other members
    start `pending` and must call `accept()` to receive notifications.

    `topology` controls privacy semantics for targeted messages:
      - "flat" (default): send_to pushes to `to` only; history visible to all.
      - "hierarchical": send_to auto-defaults private=True when not explicitly passed.
      - "custom": no automatic privacy defaults; caller drives privacy explicitly.
    Existing chats without a topology field are backward-compatible with "flat".
    """
    if topology not in _VALID_TOPOLOGIES:
        raise ValueError(
            f"Invalid topology {topology!r}. Valid values: {sorted(_VALID_TOPOLOGIES)}."
        )
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

    meta: dict[str, Any] = {
        "kind": META,
        "event_id": _new_event_id(),
        "chat_id": chat_id,
        "ts": _now_iso(),
        "created_at": _now_iso(),
        "created_by": creator_session_id,
        "created_by_name": creator_name,
        "title": derived_title,
        "fresh_suffix": fresh_suffix,
        "topology": topology,
    }
    if member_roles is not None:
        # Always include the creator as master. A member_roles dict that omits
        # the creator makes them unresolvable to the Themis role gate (Layer-1
        # miss → fail-closed lockout of EVERY tool for the chat creator). The
        # creator holds master implicitly via created_by; materialize it so role
        # resolution is consistent whether or not member_roles was passed. (#67)
        member_roles = dict(member_roles)
        member_roles.setdefault(creator_session_id, ROLE_MASTER)
        meta["member_roles"] = member_roles
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

    # Phase B v1.5: emit role-grant directive to the creator. They hold
    # implicit master role (via room.meta.created_by / _is_master fallback);
    # the directive surfaces the recommended /model + /effort slash commands.
    _emit_role_directive(chat_id, creator_session_id, ROLE_MASTER)

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


def _check_not_observer(room: dict[str, Any], sid: str, action: str) -> None:
    """Phase B v2: observers can read everything but cannot write. Pre-check
    helper for send_message / create_task / update_task_status."""
    member_roles = room["meta"].get("member_roles") or {}
    if member_roles.get(sid) == ROLE_OBSERVER:
        raise ValueError(
            f"Session {sid!r} is an observer in {room['meta'].get('chat_id', '?')!r}; "
            f"observers cannot {action}."
        )


def send_message(
    chat_id: str,
    sender_session_id: str,
    body: str,
    *,
    to: list[str] | None = None,
    private: bool | None = None,
) -> dict[str, Any]:
    """Append a message. Sender must be an accepted member.

    Optional `to`: list of session_ids/names. When set, real-time SSE
    broadcast goes only to those sessions (plus sender for echo-drop).

    `private=True`: message is hidden from non-recipients in chat_history.
    Requires `to` to be non-empty (private with no recipients is meaningless).

    `private=None` (default): resolved against the chat's topology field.
    In "hierarchical" chats, targeted messages (with `to`) default to
    private=True. In "flat" or "custom" chats, defaults to False.

    Phase B v2: observers (member_roles[sid] == "observer") cannot send;
    raises ValueError. Critic role behaves identically to agent for write
    paths — opinion-only role, label visible in member listings.
    """
    sender_session_id = _resolve_or_uuid(sender_session_id)
    if private is True and not to:
        raise ValueError(
            "private=True requires a non-empty `to` list — "
            "a private message with no recipients is meaningless."
        )
    room = load_room(chat_id)
    member = room["members"].get(sender_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {sender_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can send messages."
        )
    _check_not_observer(room, sender_session_id, "send messages")

    # v1.9.5: topology-based privacy default. "hierarchical" chats treat
    # targeted messages as private by default when caller didn't specify.
    effective_topology = room["meta"].get("topology", "flat")
    if private is None and to and effective_topology == "hierarchical":
        effective_private = True
    else:
        effective_private = bool(private)

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
        "private": effective_private,
    }
    _append(chat_id, record)

    # Loud-fail: surface target reachability for `to`-targeted sends.
    if resolved_to:
        target_states = []
        for rid in resolved_to:
            try:
                ts = sessions_mod.state(rid)
                ts_status = ts.get("status") or {}
                target_states.append(
                    {
                        "session_id": rid,
                        "target_reachable": ts_status.get("effective_status")
                        in sessions_mod._USABLE_STATUSES,
                        "target_status": ts_status.get("effective_status", "unknown"),
                        "reason_if_not_ok": ts_status.get("demoted_reason"),
                    }
                )
            except (ValueError, OSError):
                target_states.append(
                    {
                        "session_id": rid,
                        "target_reachable": False,
                        "target_status": "unknown",
                        "reason_if_not_ok": "could not resolve target state",
                    }
                )
        record["targets_reachability"] = target_states

    log.info(
        "chats: msg from %s to %s (to=%s private=%s topology=%s)",
        sender_session_id,
        chat_id,
        resolved_to or "*",
        effective_private,
        effective_topology,
    )
    return record


# ---------------------------------------------------------------------------
# Phase B: tasks
# ---------------------------------------------------------------------------


def create_task(
    chat_id: str,
    sender_session_id: str,
    body: str,
    assignee_session_id: str | None = None,
    *,
    private: bool = False,
) -> dict[str, Any]:
    """Append a TASK record (status=pending). Sender must be an accepted member;
    if assignee_session_id is set, that session must also be accepted.

    `private=True`: task hidden from non-assignee members in chat_history.
    Requires assignee_session_id (private task with no assignee is meaningless).

    Phase B v2: observers cannot create tasks.
    """
    sender_session_id = _resolve_or_uuid(sender_session_id)
    if private and assignee_session_id is None:
        raise ValueError(
            "private=True requires assignee_session_id — "
            "a private task with no assignee is meaningless."
        )
    room = load_room(chat_id)
    member = room["members"].get(sender_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {sender_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can create tasks."
        )
    _check_not_observer(room, sender_session_id, "create tasks")

    # Phase B v2: only the master may create tasks. Mirrors the signal_start +
    # approve-only-for-master pattern — task creation is a master coordination
    # primitive, not a general-member action. (B-M4: closes the class where
    # non-master sessions bypassed master and created tasks directly.)
    if not _is_master(room, sender_session_id):
        raise ValueError(
            f"Session {sender_session_id!r} is not the master of {chat_id!r}; "
            f"only the master can create tasks."
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
        "private": private,
        # to=[assignee] normalises the private filter path so history() can
        # use a single check across all private record types.
        "to": [assignee_resolved] if private and assignee_resolved else None,
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
    *,
    private: bool = False,
) -> dict[str, Any]:
    """Append a TASK_UPDATE record. Validates the from→to transition and
    the caller's role (master vs assignee vs accepted-member).

    `private=True`: update hidden from non-assignee members in chat_history.
    Uses the task's assignee_id as the implicit recipient; raises ValueError
    if the task has no assignee.

    Phase B v2: observers cannot update task status (no write paths for
    observer role).
    """
    by_session_id = _resolve_or_uuid(by_session_id)
    room = load_room(chat_id)
    member = room["members"].get(by_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {by_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can update task status."
        )
    _check_not_observer(room, by_session_id, "update task status")

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

    # Phase B v2: master check goes through _is_master so member_roles
    # (when present) is the source of truth; falls back to created_by
    # for v1-era chats.
    is_master = _is_master(room, by_session_id)
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

    if private and not assignee:
        raise ValueError(
            f"private=True on task_update requires the task to have an assignee — "
            f"task {task_id!r} has no assignee_id."
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
        "private": private,
        "to": [assignee] if private and assignee else None,
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

    # Phase B v2: master check via _is_master so granted masters can
    # signal-start in v2 chats with explicit member_roles.
    if not _is_master(room, by_session_id):
        raise ValueError(
            f"Session {by_session_id!r} is not the master of {chat_id!r}; "
            f"only the master can signal start on pending tasks."
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


def record_gate_verdict(
    chat_id: str,
    by_session_id: str,
    task_id: str,
    verdict: str,
) -> dict[str, Any]:
    """Append a structured gate-verdict event (B3 Slice B-1).

    verdict ∈ {"approve", "changes", "ship", "hold"}:
      - "approve" / "changes": written by critic
      - "ship" / "hold": written by verifier

    Caller must be an accepted member. Verdict events are TASK_SIGNAL-shape
    records that `get_gate_verdicts` reads back to compute the tri-state
    (present+complete | absent | error) for the B3 commit/approve gate.
    """
    by_session_id = _resolve_or_uuid(by_session_id)
    valid_verdicts = frozenset({"approve", "changes", "ship", "hold"})
    if verdict not in valid_verdicts:
        raise ValueError(
            f"Invalid verdict {verdict!r}; must be one of {sorted(valid_verdicts)}"
        )
    room = load_room(chat_id)
    member = room["members"].get(by_session_id)
    if not member or member["state"] != ACCEPTED:
        raise ValueError(
            f"Session {by_session_id!r} is not an accepted member of {chat_id!r}."
        )
    # Author-role-binding: only critic can write approve/changes; only verifier ship/hold.
    # Prevents master or any non-reviewer from self-posting structured verdicts
    # and bypassing IN-MASTER-9 (B3 follow-up fix).
    _VERDICT_AUTHOR_ROLES: dict[str, str] = {
        "approve": ROLE_CRITIC,
        "changes": ROLE_CRITIC,
        "ship": ROLE_VERIFIER,
        "hold": ROLE_VERIFIER,
    }
    required_role = _VERDICT_AUTHOR_ROLES[verdict]
    caller_role = (room.get("meta") or {}).get("member_roles", {}).get(by_session_id)
    if caller_role != required_role:
        raise ValueError(
            f"verdict={verdict!r} requires {required_role!r} role; "
            f"caller {by_session_id!r} resolved to role {caller_role!r}. "
            "Only the designated reviewer role may write this verdict."
        )

    # Verify the task exists
    task_record = None
    for msg in room.get("messages", []):
        if msg.get("kind") == TASK and msg.get("id") == task_id:
            task_record = msg
            break
    if task_record is None:
        raise ValueError(f"No task with id={task_id!r} in {chat_id!r}.")

    record = {
        "kind": TASK_VERDICT,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "task_id": task_id,
        "verdict": verdict,
        "by_session_id": by_session_id,
        "by_name": member.get("session_name") or by_session_id[:8],
    }
    _append(chat_id, record)
    log.info(
        "chats: task %s verdict=%r by %s in %s", task_id, verdict, by_session_id, chat_id
    )
    return record


# Sentinel constants for the tri-state gate-verdict lookup.
_GATE_ABSENT = "absent"
_GATE_ERROR = "error"


def get_gate_verdicts(session_id: str) -> dict[str, Any] | str:
    """Return the gate-verdict state for the session's current active task.

    TRI-STATE return (per B3 spec):
      - dict {task_id, critic_approved, verifier_shipped} — verdicts found
      - _GATE_ABSENT ("absent") — task found but no verdict events yet
      - _GATE_ERROR ("error") — verdict read failed (corrupt/unreadable)
      - None — no active task for this session (ad-hoc commit; allow)

    Resolution:
      1. Find the session's most-recent task in any chat where
         assignee_id == session AND status ∈ {in_progress, done}.
      2. Scan that chat for TASK_VERDICT events with matching task_id.
      3. Last verdict from each role wins (critic last approve/changes;
         verifier last ship/hold).
    """
    try:
        session_id = _resolve_or_uuid(session_id)
    except Exception:
        return None

    try:
        chat_dir = _chat_dir()
        if not chat_dir.exists():
            return None

        # Step 1: find active task (most-recently-updated in_progress or done)
        best_task: dict[str, Any] | None = None
        best_chat_id: str | None = None
        best_ts: str = ""

        for path in chat_dir.glob("chat-*.jsonl"):
            try:
                room = load_room(path.stem)
            except Exception:
                continue
            member = room["members"].get(session_id)
            if not member or member["state"] != ACCEPTED:
                continue
            # Fold task states from messages
            task_latest: dict[str, dict[str, Any]] = {}
            for msg in room.get("messages", []):
                kind = msg.get("kind")
                if kind == TASK and msg.get("assignee_id") == session_id:
                    tid = msg.get("id")
                    if tid:
                        task_latest[tid] = {**msg, "status": TASK_PENDING}
                elif kind == TASK_UPDATE:
                    tid = msg.get("task_id")
                    if tid and tid in task_latest:
                        new_status = msg.get("new_status") or msg.get("status")
                        if new_status:
                            task_latest[tid]["status"] = new_status
                        task_latest[tid]["last_ts"] = msg.get("ts", "")

            for tid, task in task_latest.items():
                if task.get("status") not in (TASK_IN_PROGRESS, TASK_DONE):
                    continue
                ts = task.get("last_ts") or task.get("ts", "")
                if ts > best_ts:
                    best_ts = ts
                    best_task = task
                    best_chat_id = path.stem

        if best_task is None:
            return None  # no active task → ad-hoc commit allowed

        # Step 2: scan for verdict events
        task_id = best_task.get("id")
        try:
            room = load_room(best_chat_id)
        except Exception:
            return _GATE_ERROR

        critic_verdict: str | None = None
        verifier_verdict: str | None = None
        for msg in room.get("messages", []):
            if msg.get("kind") != TASK_VERDICT:
                continue
            if msg.get("task_id") != task_id:
                continue
            v = msg.get("verdict")
            if v in ("approve", "changes"):
                critic_verdict = v
            elif v in ("ship", "hold"):
                verifier_verdict = v

        if critic_verdict is None and verifier_verdict is None:
            return _GATE_ABSENT  # task found but no verdicts yet

        return {
            "task_id": task_id,
            "critic_approved": critic_verdict == "approve",
            "verifier_shipped": verifier_verdict == "ship",
        }

    except Exception:
        return _GATE_ERROR


def get_gate_verdicts_by_task(session_id: str, task_id: str) -> dict[str, Any] | str:
    """Look up gate verdicts by explicit task_id (for master's approve gate).

    Same tri-state return as get_gate_verdicts, but scans by task_id directly
    instead of inferring the task from the caller's assignee relationship.
    Used when the caller is the reviewer (master), not the assignee (agent).
    """
    try:
        chat_dir = _chat_dir()
        if not chat_dir.exists():
            return None

        for path in chat_dir.glob("chat-*.jsonl"):
            try:
                room = load_room(path.stem)
            except Exception:
                continue
            # Check if task exists in this chat
            task_record = None
            for msg in room.get("messages", []):
                if msg.get("kind") == TASK and msg.get("id") == task_id:
                    task_record = msg
                    break
            if task_record is None:
                continue
            # Verify caller is a member
            try:
                sid = _resolve_or_uuid(session_id)
            except Exception:
                continue
            member = room["members"].get(sid)
            if not member or member["state"] != ACCEPTED:
                continue
            # Scan verdicts
            critic_verdict: str | None = None
            verifier_verdict: str | None = None
            for msg in room.get("messages", []):
                if msg.get("kind") != TASK_VERDICT:
                    continue
                if msg.get("task_id") != task_id:
                    continue
                v = msg.get("verdict")
                if v in ("approve", "changes"):
                    critic_verdict = v
                elif v in ("ship", "hold"):
                    verifier_verdict = v

            if critic_verdict is None and verifier_verdict is None:
                return _GATE_ABSENT
            return {
                "task_id": task_id,
                "critic_approved": critic_verdict == "approve",
                "verifier_shipped": verifier_verdict == "ship",
            }

        return None  # task_id not found in any chat → no active task

    except Exception:
        return _GATE_ERROR


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

    is_req_master = _is_master(room, requester_session_id)
    tasks: dict[str, dict[str, Any]] = {}
    for line in _read(chat_id):
        k = line.get("kind")
        if k == TASK:
            # Apply same private filter as history(): private tasks are only
            # visible to sender, explicit recipients (to=[]), and the master.
            if line.get("private") and not (
                line.get("sender_id") == requester_session_id
                or requester_session_id in (line.get("to") or [])
                or is_req_master
            ):
                continue
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
    # Private-message filter: records with private=True are only visible to
    # the sender, their explicit recipients, and the chat master (audit).
    # Non-private records and all non-msg/task/task_update records pass through.
    is_req_master = _is_master(room, requester_session_id)
    msgs = [
        m
        for m in msgs
        if not m.get("private")
        or m.get("sender_id") == requester_session_id
        or requester_session_id in (m.get("to") or [])
        or is_req_master
    ]
    return msgs[-limit:]


def leave(chat_id: str, session_id: str) -> dict[str, Any]:
    """Mark caller as `left`. They no longer receive notifications.

    Phase B v2: refuses if the caller is the chat's current master.
    Master must first transfer their seat via chat_transfer_membership or
    promote another member to master via chat_grant_role, then leave.
    Closes the v1 footgun where a creator's chat_leave made `done →
    approved` unreachable for the rest of the chat's lifetime.
    """
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
    if _is_master(room, session_id):
        raise ValueError(
            f"Session {session_id!r} is the master of {chat_id!r} and cannot leave "
            f"directly. Use chat_transfer_membership to hand off membership, or "
            f"chat_grant_role to promote another accepted member to master, then leave."
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
    *,
    as_deputize: bool = False,
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

    **Phase B v1.6 deputize marker (`as_deputize=True`):** two effects,
    both load-bearing for the pause-and-handoff semantic.

    1. The same creator-transfer META mutation ALSO sets
       `meta.deputized_original_master = from_session_id`. `chat_resume_master`
       reads this field on resume to validate the caller is the original
       donor and atomically swap master role back. The kwarg also forces
       `member_roles` materialization (even on v1-era chats) so the resume
       primitive doesn't need fallback logic.

    2. The donor's `TRANSFERRED_OUT` MEMBER write is SKIPPED — donor stays
       in state ACCEPTED throughout the deputize→resume cycle. This is
       LOCK v3 Decision 10: deputize is a pause, not a goodbye;
       TRANSFERRED_OUT means "permanent departure" and using it for a
       reversible pause produces a wrong-shape state that breaks `chat_send`
       and `_broadcast` (both gate on ACCEPTED) for the donor post-resume.
       Vice's `ACCEPTED` MEMBER write happens unchanged — vice still needs
       to be added to the chat.

    Atomic with the transfer; no inconsistent intermediate state. Non-creator
    transfers or `as_deputize=False` (default) preserve the original terminal-
    handoff semantics (donor → TRANSFERRED_OUT, no marker written). Pairs
    with `/khimaira-deputize` (skill) on the write side and
    `chat_resume_master` (this module) on the read+clear side.

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
        # Phase B v2: propagate master role in member_roles too. The v1.3
        # created_by swap fixed the implicit-master fallback; v2 needs the
        # explicit member_roles entry updated so that, after a future
        # chat_grant_role on this chat materializes the dict, the
        # successor's master status survives. If member_roles was already
        # explicit, demote the old creator to agent and promote the new
        # creator to master in the same write.
        member_roles = dict(existing_meta.get("member_roles") or {})
        if member_roles or as_deputize:
            # Explicit dict pre-existed OR we're entering deputize mode:
            # materialize the master swap into member_roles explicitly.
            # For deputize, this is load-bearing — chat_resume_master
            # reads member_roles to find the current master on resume;
            # leaving it implicit via created_by would require the resume
            # primitive to special-case v1-era fallback logic.
            if member_roles.get(from_session_id) == ROLE_MASTER:
                member_roles[from_session_id] = ROLE_AGENT
            elif as_deputize and not member_roles:
                # First-time materialization on a v1-era chat entering
                # deputize. The donor was implicit-master via created_by;
                # mark them explicitly as agent (post-swap).
                member_roles[from_session_id] = ROLE_AGENT
            member_roles[to_session_id] = ROLE_MASTER
            creator_meta_update["member_roles"] = member_roles
        # Phase B v1.6 deputize marker: when caller requests as_deputize,
        # mark the chat as in deputize mode atomically with the
        # creator-transfer META mutation. `chat_resume_master` reads this
        # field to validate the resumption and atomic-swap back.
        if as_deputize:
            creator_meta_update["deputized_original_master"] = from_session_id

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
    # Phase B v1.6 LOCK v3 Decision 10: under as_deputize=True, the donor
    # stays ACCEPTED throughout the pause-and-handoff cycle. TRANSFERRED_OUT
    # semantically means "permanent goodbye"; deputize is non-permanent.
    # The sys_msg body and meta.event_type also shift to reflect the
    # pause semantics so the audit trail is honest about what happened.
    if as_deputize:
        sys_msg_body = (
            f"📦 {from_name} deputized this chat to {to_name} "
            f"— pause-and-handoff; resume via /khimaira-resume"
        )
        sys_msg_event_type = "deputize"
    else:
        sys_msg_body = f"📦 {from_name} transferred this chat to {to_name} — full context handoff"
        sys_msg_event_type = "transfer"
    sys_msg = {
        "kind": MSG,
        "event_id": _new_event_id(),
        "id": "msg-" + uuid.uuid4().hex[:12],
        "ts": ts,
        "chat_id": chat_id,
        "sender_id": SYSTEM_SENDER_ID,
        "sender_name": SYSTEM_SENDER_ID,
        "body": sys_msg_body,
        "to": None,
        "meta": {
            "event_type": sys_msg_event_type,
            "transfer_id": transfer_id,
            "from": from_session_id,
            "to": to_session_id,
        },
    }
    if creator_meta_update is not None:
        _append(chat_id, creator_meta_update)
    # Skip donor's TRANSFERRED_OUT MEMBER write under as_deputize=True —
    # donor's chat membership is preserved (state stays ACCEPTED) because
    # the deputize is a pause, not a permanent departure. chat_resume_master
    # restores their master role; their MEMBER state never needed mutation.
    if not as_deputize:
        _append(chat_id, out_record)
    _append(chat_id, in_record)
    _append(chat_id, sys_msg)
    # Phase B v1.5: if this transfer was a master-swap (creator handoff), the
    # receiving session inherits the master role. Emit a role directive to
    # them so they know to switch their `/model` + `/effort` to the master-tier
    # budget. Donor session becomes TRANSFERRED_OUT — they're departing, no
    # directive needed. Non-creator transfers leave roles untouched, so no
    # directive fires in that branch.
    if creator_meta_update is not None:
        _emit_role_directive(chat_id, to_session_id, ROLE_MASTER, ts=ts)
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


def set_creator(chat_id: str, new_creator_session_id: str) -> dict[str, Any]:
    """Re-anchor master/creator for an orphaned chat (Phase B v2).

    Use when the current `created_by` session is TRANSFERRED_OUT — a pre-v1.3
    transfer_membership left the chat with no surviving master, so `done →
    approved` is unreachable. Any accepted member can call set_creator on
    an orphaned-by-transfer chat to claim master and unstick the lifecycle.

    Strictly TRANSFERRED_OUT only — set_creator does NOT unlock chats where
    the master is LEFT (voluntary departure should have been refused by the
    v2 master-leave guard) or REMOVED (eviction is a different story). For
    chats with a still-ACCEPTED master, use chat_grant_role instead.

    Emits a fresh META record with updated created_by, created_by_name, and
    member_roles[new_creator] = "master". Single-master invariant preserved:
    the TRANSFERRED_OUT predecessor's role entry (if any) is left alone — they
    no longer have an ACCEPTED seat, so the role is operationally moot.
    """
    new_creator_session_id = _resolve_or_uuid(new_creator_session_id)
    room = load_room(chat_id)

    member = room["members"].get(new_creator_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {new_creator_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can be set as creator."
        )

    current_creator = room["meta"].get("created_by")
    if current_creator:
        current_state = (room["members"].get(current_creator) or {}).get("state", "non-member")
        if current_state != TRANSFERRED_OUT:
            raise ValueError(
                f"Current creator {current_creator!r} state is {current_state!r}, not "
                f"{TRANSFERRED_OUT!r}; chat_set_creator only unlocks chats orphaned by "
                f"transfer_membership. For active creators use chat_grant_role; for LEFT "
                f"creators the chat stays archived (master-leave-guard should have refused)."
            )

    existing_meta = {k: v for k, v in room["meta"].items() if k != "event_id"}
    new_meta = {
        **existing_meta,
        "kind": META,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "created_by": new_creator_session_id,
        "created_by_name": member.get("session_name") or new_creator_session_id[:8],
    }
    member_roles = dict(existing_meta.get("member_roles") or {})
    member_roles[new_creator_session_id] = "master"
    new_meta["member_roles"] = member_roles
    _append(chat_id, new_meta)
    # Phase B v1.5: new creator inherits master role unconditionally on
    # set_creator — emit directive so they know to switch their `/model` +
    # `/effort` to master-tier budget. set_creator is only callable on
    # orphaned chats (current creator TRANSFERRED_OUT), so the new creator
    # is unambiguously becoming master; no gate needed.
    _emit_role_directive(chat_id, new_creator_session_id, ROLE_MASTER)
    log.info(
        "chats: set_creator %s for orphaned chat %s (was %s)",
        new_creator_session_id,
        chat_id,
        current_creator,
    )
    return new_meta


def chat_grant_role(
    chat_id: str,
    by_session_id: str,
    target_session_id: str,
    role: str,
    *,
    demote_to: str = ROLE_AGENT,
) -> dict[str, Any]:
    """Phase B v2: master-only role-grant primitive.

    Sets `target_session_id`'s role in `room.meta.member_roles`. Atomic
    promote-demote when `role == ROLE_MASTER`: the existing master is
    demoted to `demote_to` in the SAME META write that promotes the new
    master, preserving the single-master-with-delegation invariant (no
    window where two sessions both hold master).

    **First-grant materialization**: on a v1-era chat with no explicit
    `member_roles`, the first `chat_grant_role` call materializes the
    implicit creator-master into the new dict BEFORE applying the
    requested grant. After the first explicit role write, `member_roles`
    is the sole source of truth — `_is_master`'s fallback to `created_by`
    only fires for chats that have never had any role mutation.

    Raises ValueError if:
      - caller is not currently master
      - target is not an ACCEPTED member of the chat
      - `role` is not in _VALID_ROLES
      - `demote_to` is not in _VALID_ROLES
      - `demote_to == ROLE_MASTER` (closes the quorum loophole)
    """
    by_session_id = _resolve_or_uuid(by_session_id)
    target_session_id = _resolve_or_uuid(target_session_id)

    if role not in _VALID_ROLES:
        raise ValueError(f"Invalid role {role!r}. Valid roles: {sorted(_VALID_ROLES)}.")
    if demote_to not in _VALID_ROLES:
        raise ValueError(f"Invalid demote_to {demote_to!r}. Valid roles: {sorted(_VALID_ROLES)}.")
    if demote_to == ROLE_MASTER:
        raise ValueError(
            "demote_to cannot be 'master' — single-master-with-delegation "
            "invariant requires at most one session holds master at a time."
        )

    room = load_room(chat_id)

    if not _is_master(room, by_session_id):
        raise ValueError(
            f"Session {by_session_id!r} is not the master of {chat_id!r}; "
            f"only the master can grant roles."
        )

    target_member = room["members"].get(target_session_id)
    if not target_member or target_member["state"] != ACCEPTED:
        state = (target_member or {}).get("state", "non-member")
        raise ValueError(
            f"Target {target_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can be assigned a role."
        )

    existing_meta = dict(room.get("meta") or {})
    member_roles = dict(existing_meta.get("member_roles") or {})
    first_explicit_write = "member_roles" not in existing_meta

    # First-grant materialization: capture the implicit master before any
    # demote/promote logic so the resulting dict reflects the v1-era state
    # explicitly. The implicit master is room.meta.created_by.
    if first_explicit_write:
        implicit_master = existing_meta.get("created_by")
        if implicit_master:
            member_roles[implicit_master] = ROLE_MASTER

    # Atomic promote-demote when promoting a new master. Find the current
    # master (after materialization, this comes from member_roles); demote
    # them unless they're the same as the target (no-op promotion).
    demoted_master_sid: str | None = None
    if role == ROLE_MASTER:
        current_master = None
        for sid, r in member_roles.items():
            if r == ROLE_MASTER:
                current_master = sid
                break
        if current_master and current_master != target_session_id:
            member_roles[current_master] = demote_to
            demoted_master_sid = current_master

    # Apply the requested grant.
    member_roles[target_session_id] = role

    ts = _now_iso()
    new_meta = {
        **existing_meta,
        "kind": META,
        "event_id": _new_event_id(),
        "ts": ts,
        "member_roles": member_roles,
    }
    _append(chat_id, new_meta)

    # Invalidate Themis role cache for affected sessions so enforcement
    # picks up the new role on the NEXT tool call, not after the 300s TTL.
    # Lazy import avoids a circular dependency (api.themis imports chats);
    # fail-open mirrors api/chats._inval — a stale cache entry expires via
    # the 5-min safety TTL at worst.
    try:
        from khimaira.monitor.api.themis import invalidate_role_cache  # noqa: PLC0415

        invalidate_role_cache(target_session_id)
        if demoted_master_sid is not None:
            invalidate_role_cache(demoted_master_sid)
    except Exception:
        pass

    # Phase B v1.5: emit role-grant directives. Target always gets one
    # (silent-skipped only if the new role has no ROLE_BUDGET default —
    # currently just `critic`). On master-swap, the demoted prior master
    # also gets a directive announcing their new (lower) role's budget.
    # Both directives share the META write's timestamp so audit-trail
    # pairing reads cleanly.
    _emit_role_directive(chat_id, target_session_id, role, ts=ts)
    if demoted_master_sid is not None:
        _emit_role_directive(chat_id, demoted_master_sid, demote_to, ts=ts)

    log.info(
        "chats: grant_role chat=%s target=%s role=%s by=%s%s",
        chat_id,
        target_session_id,
        role,
        by_session_id,
        " (first explicit write — materialized implicit master)" if first_explicit_write else "",
    )
    return new_meta


def chat_resume_master(
    chat_id: str,
    by_session_id: str,
    *,
    demote_to: str = ROLE_AGENT,
) -> dict[str, Any]:
    """Phase B v1.6: restore master role to the original master after a
    deputize swap.

    Admin-style restoration analogous to v2's `chat_set_creator` (which
    unlocks orphaned chats). Where `chat_set_creator` recovers from a
    pre-v1.3 transfer that left no surviving accepted creator,
    `chat_resume_master` reverses a deliberate `as_deputize=True`
    transfer where the original master is paused, awaiting their return.

    Validates: `room.meta.deputized_original_master` is set AND equals
    `by_session_id`. Atomically swaps current master (the vice) back to
    `by_session_id`; v1.5 directive emit fires for both sides (caller →
    master, vice → `demote_to` default agent). Clears
    `meta.deputized_original_master` and `member_roles[vice]` entry
    (demoted to `demote_to`) and `member_roles[by_session_id]` (= master)
    in the same META write.

    `created_by` swaps back to `by_session_id` as well — keeps the
    v1.3-established invariant that `created_by` tracks the current
    master across atomic swaps.

    Raises ValueError if:
      - chat is not currently deputized (no `deputized_original_master` field)
      - caller is not the recorded original master per that field
      - `demote_to` is not in _VALID_ROLES
      - `demote_to == ROLE_MASTER` (closes the quorum loophole, mirrors
        chat_grant_role)
    """
    if demote_to not in _VALID_ROLES:
        raise ValueError(f"Invalid demote_to {demote_to!r}. Valid roles: {sorted(_VALID_ROLES)}.")
    if demote_to == ROLE_MASTER:
        raise ValueError(
            "demote_to cannot be 'master' — single-master-with-delegation "
            "invariant requires at most one session holds master at a time."
        )

    by_session_id = _resolve_or_uuid(by_session_id)
    room = load_room(chat_id)
    existing_meta = dict(room.get("meta") or {})
    deputized_donor = existing_meta.get("deputized_original_master")

    if deputized_donor is None:
        raise ValueError(
            f"Chat {chat_id!r} is not in deputize mode "
            f"(no meta.deputized_original_master set); cannot resume master role."
        )
    if deputized_donor != by_session_id:
        raise ValueError(
            f"Session {by_session_id!r} is not the original master of {chat_id!r} "
            f"(recorded: {deputized_donor!r}); only the original master can resume."
        )

    # Find current master from member_roles (post-deputize this is the vice).
    member_roles = dict(existing_meta.get("member_roles") or {})
    current_master = None
    for sid, r in member_roles.items():
        if r == ROLE_MASTER:
            current_master = sid
            break
    if current_master is None:
        # Defensive: shouldn't happen post-deputize (transfer_membership
        # sets member_roles[to_session_id] = ROLE_MASTER). If it does,
        # the donor (caller) is being restored as master without an
        # explicit demote step.
        log.warning(
            "chats: resume_master found no current master in member_roles "
            "for chat=%s; promoting donor without explicit demote",
            chat_id,
        )

    # Atomic swap: demote current master (vice), promote donor.
    if current_master and current_master != by_session_id:
        member_roles[current_master] = demote_to
    member_roles[by_session_id] = ROLE_MASTER

    # Determine donor's display name for created_by_name update.
    donor_name = _resolve_session_name(by_session_id) or by_session_id[:8]
    ts = _now_iso()

    new_meta = {
        **existing_meta,
        "kind": META,
        "event_id": _new_event_id(),
        "ts": ts,
        "member_roles": member_roles,
        "created_by": by_session_id,
        "created_by_name": donor_name,
    }
    # Clear the deputize marker — chat is no longer in deputize mode.
    new_meta.pop("deputized_original_master", None)

    _append(chat_id, new_meta)

    # Phase B v1.5 role-directive emits: caller becomes master, vice
    # (if any) demotes to `demote_to`. Shared ts for audit pairing.
    _emit_role_directive(chat_id, by_session_id, ROLE_MASTER, ts=ts)
    if current_master and current_master != by_session_id:
        _emit_role_directive(chat_id, current_master, demote_to, ts=ts)

    log.info(
        "chats: resume_master chat=%s donor=%s demoted_vice=%s",
        chat_id,
        by_session_id,
        current_master if current_master and current_master != by_session_id else "(none)",
    )
    return new_meta


def find_chats_deputized_by(by_session_id: str) -> list[str]:
    """Phase B v1.6: list chat_ids where `by_session_id` is the recorded
    original master in deputize mode.

    Used by `/khimaira-resume` to enumerate which chats the caller is
    currently awaiting resume on. Scans the chat directory; for each
    chat, reads META and checks `deputized_original_master == by_session_id`.

    Returns chat_ids in arbitrary order. Empty list if no chats are
    deputized by this session.
    """
    by_session_id = _resolve_or_uuid(by_session_id)
    out: list[str] = []
    if not _chat_dir().exists():
        return out
    for path in _chat_dir().glob("chat-*.jsonl"):
        chat_id = path.stem
        try:
            room = load_room(chat_id)
        except ValueError:
            continue
        if room["meta"].get("deputized_original_master") == by_session_id:
            out.append(chat_id)
    return out


def delete(chat_id: str, by_session_id: str) -> dict[str, Any]:
    """Archive the chat JSONL. Only the master can call this (v2:
    `_is_master` reads `member_roles`; v1-era chats fall back to
    `created_by`)."""
    by_session_id = _resolve_or_uuid(by_session_id)
    room = load_room(chat_id)
    if not _is_master(room, by_session_id):
        creator = room["meta"].get("created_by")
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
# v1.9 assign-batch coordinator
# ---------------------------------------------------------------------------


def _format_assignment_block(
    task_id: str,
    from_name: str,
    task_body: str,
    required_model: str,
    required_effort: str,
) -> str:
    return (
        f"🔔 TASK ASSIGNMENT [task-id: {task_id}]\n"
        f"From: {from_name}\n"
        f"Task: {task_body}\n"
        f"\n"
        f"⚠️ ENFORCEMENT GATE ACTIVE — suppress default reflexes:\n"
        f"- DO NOT start work on the task body\n"
        f"- DO NOT pre-read files (settings.json, project files, etc.) — verification happens AT ready, not before\n"
        f"- DO NOT pre-plan or gather reconnaissance state\n"
        f'- Override the "research before implementing" default for the duration of this gate\n'
        f"- Hold silently; only respond if you have a question about the protocol itself\n"
        f"\n"
        f'The "ready" signal comes from the USER in your window (not from master).\n'
        f"They will type `/model {required_model}` + `/effort {required_effort}`, then run `/agent-ready` (auto-fills task-id)"
        f" — or type `ready [task-id: {task_id}]` manually as fallback.\n"
        f"\n"
        f'ON RECEIVING "ready" from the user (and ONLY then):\n'
        f"  1. Read ~/.claude/settings.json\n"
        f"  2. If model == {required_model} and effortLevel == {required_effort} → chat_send ack to master with"
        f' "ready [task-id: {task_id}] | model={required_model} effort={required_effort}"\n'
        f"  3. If non-compliant → DO NOT ack; tell user what's still wrong\n"
        f"  4. Wait for master's 🟢 ALL AGENTS CONFIRMED — BEGIN signal\n"
        f"\n"
        f"Master fires begin once ALL agents ack. Do not start work until you receive 🟢.\n"
        f"This message was delivered automatically via SSE — no typing was needed to receive it."
    )


def _format_begin_block(task_body: str) -> str:
    return (
        "🟢 ALL AGENTS CONFIRMED — BEGIN IMPLEMENTATION\n"
        f"Task: {task_body}\n"
        "\n"
        "All agents have set their required budget. Start working on your assigned task now.\n"
        "No further input from the user is needed — proceed autonomously and report progress\n"
        "via chat when done or when blocked."
    )


def _scan_acks(chat_id: str, task_ids: dict[str, str]) -> dict[str, dict]:
    """Scan chat JSONL for ack messages; return confirmed agents.

    Args:
        chat_id: chat to scan.
        task_ids: {agent_session_id → task_id} mapping from the coordinator.

    Returns:
        {agent_session_id: {"model": str, "effort": str, "ts": str}}
        Only includes agents whose ack was found. Keeps the LATEST ack per
        task_id (handles re-ack after restart).
    """
    reverse: dict[str, str] = {tid: sid for sid, tid in task_ids.items()}
    found: dict[str, dict] = {}
    for r in _read(chat_id):
        if r.get("kind") != MSG:
            continue
        m = _ACK_RE.search(r.get("body") or "")
        if not m:
            continue
        tid = m.group(1)
        agent_id = reverse.get(tid)
        if not agent_id:
            continue
        found[agent_id] = {
            "model": m.group(2).lower(),
            "effort": m.group(3).lower(),
            "ts": r.get("ts") or "",
        }
    return found


async def assign_batch(
    chat_id: str,
    from_session_id: str,
    assignments: list[dict],
    *,
    timeout_s: int = 600,
    wait_for_acks: bool = True,
    fire_begin_on_partial: bool = False,
) -> dict:
    """v1.9 coordinator: fan-out assignments + collect acks + fire begin.

    Collapses the master's manual 3N+K+2 call loop into a single daemon
    HTTP call. The daemon creates tasks, sends SSE assignment blocks,
    polls for acks, and fires the begin signal — all server-side.

    Args:
        chat_id: the shared chat.
        from_session_id: master session (must be an accepted member).
        assignments: list of dicts with keys {agent_session_id, task_body,
            required_model, required_effort}.
        timeout_s: ack-poll deadline in seconds. 0 = fire-and-forget.
        wait_for_acks: False → return immediately after fan-out (no polling).
        fire_begin_on_partial: True → fire begin for acked subset on timeout.

    Returns:
        {task_ids, acks, missing_acks, begin_fired, elapsed_ms}
    """
    import time

    t0 = time.monotonic()
    from_session_id = _resolve_or_uuid(from_session_id)
    room = load_room(chat_id)
    member = room["members"].get(from_session_id)
    if not member or member["state"] != ACCEPTED:
        state = (member or {}).get("state", "non-member")
        raise ValueError(
            f"Session {from_session_id!r} is {state!r} in {chat_id!r}; "
            f"only accepted members can coordinate assignments."
        )

    from_name = member.get("session_name") or from_session_id[:8]

    # CREATE_TASKS — one tracking task per agent.
    task_ids: dict[str, str] = {}  # agent_session_id → task_id
    for spec in assignments:
        agent_id = _resolve_or_uuid(spec["agent_session_id"])
        task = create_task(
            chat_id,
            from_session_id,
            spec.get("task_body") or "",
            assignee_session_id=agent_id,
        )
        task_ids[agent_id] = task["id"]

    # NOTIFY_AGENTS — SSE assignment block per agent.
    for spec in assignments:
        agent_id = _resolve_or_uuid(spec["agent_session_id"])
        tid = task_ids[agent_id]
        block = _format_assignment_block(
            tid,
            from_name,
            spec.get("task_body") or "",
            spec.get("required_model") or "sonnet",
            spec.get("required_effort") or "medium",
        )
        send_message(chat_id, from_session_id, block, to=[agent_id])

    # AWAIT_ACKS — poll every 2s until all acked or timeout.
    # Skipped entirely for fire-and-forget (wait_for_acks=False or timeout_s==0).
    acks: dict[str, dict] = {}
    polled = False
    if wait_for_acks and timeout_s > 0:
        polled = True
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            acks = _scan_acks(chat_id, task_ids)
            if len(acks) == len(assignments):
                break
            await asyncio.sleep(2.0)
        if not acks:
            acks = _scan_acks(chat_id, task_ids)

    # FIRE_BEGIN — send begin block to confirmed agents (if any).
    confirmed = set(acks.keys())
    all_agent_ids = set(task_ids.keys())
    # missing_acks is only meaningful when we actually polled; fire-and-forget = unknown.
    missing = sorted(all_agent_ids - confirmed) if polled else []

    begin_fired = False
    if polled and (confirmed == all_agent_ids or (fire_begin_on_partial and confirmed)):
        begin_body = (assignments[0].get("task_body") or "") if assignments else ""
        send_message(
            chat_id,
            from_session_id,
            _format_begin_block(begin_body),
            to=sorted(confirmed),
        )
        begin_fired = True

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    return {
        "task_ids": {sid: tid for sid, tid in task_ids.items()},
        "acks": acks,
        "missing_acks": missing,
        "begin_fired": begin_fired,
        "elapsed_ms": elapsed_ms,
    }


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
    sessions_mod.set_session_ppid(session_id, ppid)


def get_session_ppid(session_id: str) -> int | None:
    """Return the Claude Code process PID for this session, or None if unknown."""
    return sessions_mod.get_session_ppid(session_id)


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


_DAEMON_SENDER_ID = "khimaira-daemon"
_DAEMON_SENDER_NAME = "🩺 khimaira-daemon"


async def _post_synthetic_message(
    chat_id: str,
    body: str,
    kind: str = MSG,
) -> "dict[str, Any] | None":
    """Write a synthetic message into chat JSONL + broadcast via SSE.

    Daemon-internal only. NOT exposed via MCP — no FastMCP decorator, no
    @mcp.tool registration. Used for diagnostic probes that need to elicit
    agent response without a real sender session.

    The agent receiving this message processes it via the same SSE/hook
    path as any normal chat message. Agent's reply (any broadcast in the
    chat) is observed by Pattern 5's existing broadcast-resolve mechanism
    (broadcast-resolve clears (sender, *) entries on any chat broadcast).

    Returns the record dict on success; None on error (fail-open).
    """
    try:
        path = _chat_path(chat_id)
        if not path.exists():
            return None
        record: dict[str, Any] = {
            "kind": kind,
            "event_id": _new_event_id(),
            "id": "msg-" + uuid.uuid4().hex[:12],
            "ts": _now_iso(),
            "chat_id": chat_id,
            "sender_id": _DAEMON_SENDER_ID,
            "sender_name": _DAEMON_SENDER_NAME,
            "body": body,
            "to": None,
            "private": False,
        }
        _ensure_dir()
        sessions_mod._append_jsonl(path, record)
        _broadcast(chat_id, record)
        return record
    except Exception:
        return None


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

    # Resolve sender_name to current value at publish-time so subscribers
    # see renames immediately rather than stale snapshots. JSONL is already
    # written before _broadcast is called, so mutating here is safe.
    sid = record.get("sender_id")
    if sid and record.get("sender_name"):
        record["sender_name"] = _resolve_sender_name(sid, record["sender_name"])

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
            log.warning(
                "chats: dropped event for disconnected subscriber sid=%s chat=%s "
                "event_id=%s reason=no_subscriber",
                sid,
                chat_id,
                record.get("event_id"),
            )
            continue
        for q in queues:
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                log.warning(
                    "chats: dropped event for sid=%s chat=%s event_id=%s reason=queue_full",
                    sid,
                    chat_id,
                    record.get("event_id"),
                )


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

        # Backfill phase: per-(session, chat) cursor positioning.
        #
        # For each accepted chat, use the daemon-side cursor (last yielded
        # event_id for this session+chat) to replay missed events. Falls
        # back to the Last-Event-ID header hint only when no cursor exists
        # for that specific chat. Without per-chat cursors, the prior code
        # used a single global since_event_id which silently skipped any
        # chat whose JSONL didn't contain that event_id — the root cause
        # of the 2026-05-22 jp roster jp-intake-1 message-loss incident.
        #
        # Fresh first-time connects (no cursor AND no since_event_id) skip
        # backfill entirely — they should only receive real-time messages.
        # The original behavior for fresh subscribes is preserved here.
        has_reconnect_hint = since_event_id is not None
        any_cursor = any(_cursor_for(session_id, m["chat_id"]) is not None
                         for m in my_chats(session_id)
                         if m.get("my_state") == ACCEPTED)
        if has_reconnect_hint or any_cursor:
            for chat_meta in my_chats(session_id):
                if chat_meta.get("my_state") != ACCEPTED:
                    continue
                chat_id = chat_meta["chat_id"]
                lines = _read(chat_id)

                cursor = _cursor_for(session_id, chat_id)
                if cursor is not None:
                    # Daemon-side cursor wins. Events after the cursor position.
                    idx = next(
                        (i for i, line in enumerate(lines) if line.get("event_id") == cursor),
                        None,
                    )
                    start = idx + 1 if idx is not None else max(0, len(lines) - 50)
                    for line in lines[start:]:
                        yield line
                elif since_event_id:
                    # No daemon-side cursor: treat Last-Event-ID as a per-chat hint.
                    # If found in this chat → replay from there. If NOT found
                    # (the prior bug: since_event_id belongs to a different chat)
                    # → deliver last 50 instead of silently skipping.
                    idx = next(
                        (i for i, line in enumerate(lines) if line.get("event_id") == since_event_id),
                        None,
                    )
                    if idx is not None:
                        for line in lines[idx + 1:]:
                            yield line
                    else:
                        for line in lines[-50:]:
                            yield line
                else:
                    # Session IS reconnecting (other chats have cursors) but this
                    # specific chat has neither cursor nor hint. Deliver last 50
                    # so missed messages aren't silently dropped.
                    for line in lines[-50:]:
                        yield line
        while True:
            record = await queue.get()
            yield record
    finally:
        _subscribers[session_id].discard(queue)
        if not _subscribers[session_id]:
            _subscribers.pop(session_id, None)
