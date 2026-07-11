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
import time
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
TRANSFERRED_OUT = (
    "transferred-out"  # session handed membership to another via transfer_membership
)

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
ROLE_CONSULTANT = "consultant"  # lean roster: architect + analyst merged (design + analysis)
ROLE_GATEKEEPER = "gatekeeper"  # lean roster: critic + verifier merged (the commit gate)
ROLE_MEMBER = "member"  # neutral catch-all; empty Themis ruleset (see member.yaml)

# Idle-by-default consult roles: wake ONLY on directed consults/assignments,
# never on undirected broadcasts. SINGLE SOURCE OF TRUTH — the _broadcast wake-
# filter, the lint test, and any future Themis rule import THIS set. Do NOT infer
# from ROLE_BUDGET comment text (only analyst/verifier are tagged there → inference
# fails open on architect+critic). Critic is included (consult-idle in roster
# operation) despite having no ROLE_BUDGET entry (its budget is orchestrator-chosen).
IDLE_CONSULT_ROLES: frozenset[str] = frozenset(
    {
        ROLE_ARCHITECT,
        ROLE_ANALYST,
        ROLE_CRITIC,
        ROLE_VERIFIER,
        ROLE_CONSULTANT,  # lean: idle-by-default design/analysis seat
        ROLE_GATEKEEPER,  # lean: idle-by-default commit gate
    }
)

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
            ROLE_CONSULTANT,
            ROLE_GATEKEEPER,
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
    ROLE_MASTER: {"model": "opus[1m]", "effort": "max"},  # 1M context 2026-06-08
    ROLE_AGENT: {"model": "sonnet", "effort": "medium"},
    ROLE_OBSERVER: {"model": "sonnet", "effort": "low"},  # haiku→sonnet 2026-06-06
    ROLE_ARCHITECT: {
        "model": "opus",
        "effort": "max",
    },  # synthesis/design sidecar, idle-by-default
    ROLE_INTAKE: {"model": "opus[1m]", "effort": "medium"},  # 1M context 2026-06-08; user-facing front-end
    ROLE_ANALYST: {
        "model": "opus",
        "effort": "max",
    },  # spec disambiguation, idle-by-default
    ROLE_VERIFIER: {
        "model": "sonnet",
        "effort": "medium",
    },  # test coverage gate, idle-by-default (sonnet matches the bin/roster spawn tier)
    ROLE_TRACKER: {
        "model": "sonnet",  # haiku→sonnet 2026-06-06 (synthesis role; haiku mis-dispatched boot registration)
        "effort": "medium",
    },  # checklist curator + Linear filer
    # Lean roster (replaces architect+analyst→consultant, critic+verifier→gatekeeper).
    # Tiers signed off by Joseph 2026-06-28; mirror C1 in LEAN-ROSTER-SPEC.md / bin/roster.
    ROLE_CONSULTANT: {"model": "opus", "effort": "max"},  # design + analysis (idle-by-default)
    ROLE_GATEKEEPER: {"model": "sonnet", "effort": "high"},  # commit gate (idle-by-default; demoted 2026-06-29)
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

# K3b: freshness horizon for roster-overlap detection (seconds). Reuses the
# same env var as sessions.py alive-guard so both subsystems agree on "stale".
_ROSTER_OVERLAP_FRESHNESS_S: float = float(
    os.environ.get("KHIMAIRA_ALIVE_DELETE_GUARD_S", "900")
)
_ROSTER_OVERLAP_THRESHOLD: float = 0.5

# States that count as "active" (not departed) for overlap liveness check.
_ACTIVE_MEMBER_STATES: frozenset[str] = frozenset({PENDING, ACCEPTED})


class RosterOverlapError(Exception):
    """Raised by create_room when a new chat would fork an existing live roster.

    The guard fires when the new chat's member-set overlaps an existing live
    chat by ≥50%. Surface as HTTP 409 via the API layer.
    """

    def __init__(self, existing_chat_id: str, overlap_members: list[str]) -> None:
        self.existing_chat_id = existing_chat_id
        self.overlap_members = overlap_members
        super().__init__(
            f"Roster overlap with existing live chat {existing_chat_id!r} "
            f"({len(overlap_members)} shared member(s)). "
            f"Use allow_overlap=True to override, or chat_invite to add missing members "
            f"to the existing chat instead."
        )


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

_CURSORS: dict[tuple[str, str], str] = (
    {}
)  # (session_id, chat_id) → last yielded event_id
_CURSORS_DIRTY: bool = False  # true when _CURSORS has unsaved changes


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
        json.dumps(
            {"session_id": sid, "chat_id": cid, "last_event_id": eid, "ts": now_ts},
            separators=(",", ":"),
        )
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


def _is_role_directive(record: dict[str, Any]) -> bool:
    """True if this record is a role_directive system message."""
    return (
        record.get("kind") == MSG
        and record.get("sender_id") == SYSTEM_SENDER_ID
        and (record.get("meta") or {}).get("event_type") == "role_directive"
    )


def gc_role_directives_in_chat(chat_id: str) -> int:
    """Compact role_directive bloat: keep only the latest directive per target,
    drop historical duplicates. All other records are preserved exactly.
    Returns count of records dropped.

    Role-directive emission is event-driven (role-change points), never periodic.
    Historic duplicates accumulate when storms or repeated grants fire; this
    GC de-dupes them so the JSONL stays lean.
    """
    path = _chat_path(chat_id)
    if not path.exists():
        return 0
    lines = _read(chat_id)
    # Pass 1: find the index of the LAST role_directive per target session.
    last_idx_per_target: dict[str, int] = {}
    for i, line in enumerate(lines):
        if _is_role_directive(line):
            target = (line.get("meta") or {}).get("target", "")
            if target:
                last_idx_per_target[target] = i
    # Pass 2: build the compacted list — drop all but the last per target.
    keep_set = set(last_idx_per_target.values())
    compacted = []
    dropped = 0
    for i, line in enumerate(lines):
        if _is_role_directive(line):
            target = (line.get("meta") or {}).get("target", "")
            if target and i not in keep_set:
                dropped += 1
                continue  # drop historical duplicate
        compacted.append(line)
    if dropped == 0:
        return 0
    # Atomic rewrite via temp file + rename (same pattern as sessions.py).
    tmp = path.with_suffix(".jsonl.gc_tmp")
    try:
        import json as _json

        with tmp.open("w") as fh:
            for rec in compacted:
                fh.write(_json.dumps(rec, separators=(",", ":")) + "\n")
        tmp.rename(path)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    return dropped


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


def _assert_session_registered(session_id: str) -> None:
    """Raise ValueError if session_id is not in the session registry.

    Closes the phantom-member gap (#1): prevents adding non-existent sessions to
    a chat, which would create silent phantoms (session_post_notice 404s, no SSE
    delivery, Themis can't resolve role for them).

    LAZY-REGISTRATION EXCEPTION: if session_id is UUID-shaped (the canonical
    form from SessionStart), we warn but do NOT raise. Fresh sessions that just
    registered via SessionStart don't have a state dir yet — the lazy-registration
    design explicitly allows inviting them before they've written any state. The
    phantom risk for UUID-shaped IDs is low (callers must know the UUID).

    For non-UUID names, there's no lazy-registration exception — a name that
    doesn't resolve to a registered session is a genuine phantom.
    """
    if _UUID_RE.match(session_id):
        # UUID fast-path: apply lazy-registration exception — warn if no dir but
        # don't block. A fresh session from SessionStart has no dir yet.
        try:
            sd = sessions_mod._session_dir_read(session_id)
        except Exception:
            sd = None
        if sd is None:
            log.warning(
                "chats: session %r has no registry state dir (fresh/phantom?) — "
                "adding to chat anyway (lazy-registration). "
                "If this session never registers, post_notice will fail silently.",
                session_id,
            )
        return

    # Non-UUID names: must resolve to a registered session.
    try:
        sd = sessions_mod._session_dir_read(session_id)
    except Exception:
        sd = None
    if sd is None:
        raise ValueError(
            f"Session {session_id!r} is not registered (no session state found). "
            f"Only registered sessions can be added to a chat. "
            f"Ensure the session has registered via SessionStart or chat_my_chats first."
        )


def _slot_heal_member_key(
    room: dict,
    sid: str,
) -> "tuple[str | None, dict | None]":
    """Drift-healing member lookup for paths 3/4/6/8 (roster-identity Phase-B Part E).

    Returns (canonical_key, member_dict) where canonical_key is the member_roles
    key that this session maps to — either the presented sid (already registered)
    OR the prior member key (reattached session healing through the slot registry).

    FAIL-CLOSED: returns (None, None) for:
    - Superseded sids beyond the last-1 bound (the harvest target; `slot_resolve→None`)
    - Sids never in any slot (unregistered callers remain at current behavior)

    NEVER use `or sid` as a fallback — that defeats the security bound.
    A None return MUST be treated as DENY at every call-site.
    """
    try:
        from khimaira.monitor.sessions import _read_slot_registry

        registry = _read_slot_registry()

        # INERT-DENIAL must run BEFORE the direct member lookup (lines below).
        # A revoked sid still has a member entry (the old binding is never removed),
        # so the direct lookup would succeed and bypass the security bound.
        # Revoked = superseded beyond the last-1 window → fail-closed immediately.
        # if sid is in any slot's revoked_sids, it was superseded beyond the last-1
        # bound. Deny immediately — the member list still has the old entry so the
        # direct lookup would succeed, bypassing the security bound. Revoked check
        # gates it here first. This is the "or sid" anti-pattern prevention.
        for _slot, entry in registry.items():
            if sid in entry.get("revoked_sids", []):
                return None, None  # fail-closed

        # SLOT-HEAL: presented sid is current for a slot whose prior sid(s)
        # are the member keys (reattached session after reconnect).
        for _slot, entry in registry.items():
            if entry.get("current_sid") != sid:
                continue
            # (a) Current sid is also a direct member (freshly-added, no healing needed)
            direct_member = room["members"].get(sid)
            if direct_member is not None:
                return sid, direct_member
            # (b) Heal via prior sids: current reconnected; member entry keyed to old sid
            for prior_sid in entry.get("prior_sids", []):
                old_member = room["members"].get(prior_sid)
                if old_member is not None and old_member.get("state") == ACCEPTED:
                    log.debug(
                        "chats: slot-heal resolved %s → prior_sid=%s via slot %s",
                        sid[:8],
                        prior_sid[:8],
                        _slot,
                    )
                    return prior_sid, old_member
    except Exception:
        pass  # probe failure → fall through to direct lookup (fail-open for probe errors)

    # Not in slot registry → direct member lookup (existing behavior for un-slotted sessions).
    member = room["members"].get(sid)
    if member is not None:
        return sid, member
    return None, None


def _resolve_or_uuid(session_id_or_name: str, *, chat_id: str | None = None) -> str:
    """Resolve a session name → UUID, OR accept a UUID verbatim.

    `_resolve_or_uuid` requires the session's state dir to
    exist (the session has logged decisions / set status / etc). Fresh
    Claude Code sessions don't have a dir until they write something —
    but they DO have a session_id from the SessionStart hook, and the
    chat lazy-registration design depends on accepting that id even
    before the session has any other state.

    Resolution order (P2):
      1. If the input matches a canonical UUID format → trust it
         verbatim. Cost: a chat targeted at a non-existent UUID is
         silently a no-op (no subscriber to deliver to). Acceptable
         wart for the lazy-registration win.
      2. CHAT-SCOPED when chat_id is provided → resolve within that
         chat's accepted members; ≥2 same-named → abort.
      3. ROSTER-SCOPED when chat_id is None → resolve within
         active_roster_member_ids (MEMBERSHIP); ≥2 → abort.
      4. Legacy global fallback when roster empty (test isolation /
         first-run).
    Pass chat_id from the calling function whenever a chat context is
    available — this is the P2 routing fix.
    """
    if _UUID_RE.match(session_id_or_name):
        return session_id_or_name
    return sessions_mod.resolve_session_id(session_id_or_name, chat_id=chat_id)


def derive_chat_id(
    member_session_ids: list[str], fresh_suffix: str | None = None
) -> str:
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
                    "session_name": line.get("session_name")
                    or existing.get("session_name"),
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


def _check_roster_overlap(new_members: set[str]) -> None:
    """K3b guard: raise RosterOverlapError if new_members overlaps a live roster ≥50%.

    Scans all existing chat files. A chat is "live" when it has ≥1 active (pending
    or accepted) member AND its last activity is within _ROSTER_OVERLAP_FRESHNESS_S.
    Overlap ratio = |existing_active ∩ new_members| / |new_members|.
    """
    if not _chat_dir().exists() or not new_members:
        return

    threshold = _ROSTER_OVERLAP_THRESHOLD * len(new_members)
    now_dt = datetime.now(UTC)

    for path in _chat_dir().glob("chat-*.jsonl"):
        chat_id = path.stem
        try:
            room = load_room(chat_id)
        except (ValueError, OSError):
            continue

        members = room["members"]
        # Only count non-departed members toward overlap.
        active_sids = {
            sid for sid, m in members.items() if m.get("state") in _ACTIVE_MEMBER_STATES
        }
        if not active_sids:
            continue

        # Check freshness via last activity timestamp.
        messages = room.get("messages", [])
        last_ts_str: str | None = (
            messages[-1]["ts"] if messages else room["meta"].get("created_at")
        )
        if last_ts_str is None:
            continue
        try:
            last_dt = datetime.fromisoformat(last_ts_str)
        except ValueError:
            continue
        age_s = (now_dt - last_dt).total_seconds()
        if age_s > _ROSTER_OVERLAP_FRESHNESS_S:
            continue

        overlap = active_sids & new_members
        if len(overlap) >= threshold:
            raise RosterOverlapError(chat_id, sorted(overlap))


_VALID_TOPOLOGIES: frozenset[str] = frozenset({"flat", "hierarchical", "custom"})


def create_room(
    creator_session_id: str,
    member_session_ids: list[str],
    *,
    title: str | None = None,
    fresh: bool = False,
    topology: str = "flat",
    member_roles: dict[str, str] | None = None,
    allow_overlap: bool = False,
) -> dict[str, Any]:
    """Create a new chat room. Creator is auto-`accepted`; other members
    start `pending` and must call `accept()` to receive notifications.

    `topology` controls privacy semantics for targeted messages:
      - "flat" (default): send_to pushes to `to` only; history visible to all.
      - "hierarchical": send_to auto-defaults private=True when not explicitly passed.
      - "custom": no automatic privacy defaults; caller drives privacy explicitly.
    Existing chats without a topology field are backward-compatible with "flat".

    `allow_overlap=True` bypasses the K3b roster-overlap guard for the rare
    deliberate parallel-chat case. Default off.
    """
    if topology not in _VALID_TOPOLOGIES:
        raise ValueError(
            f"Invalid topology {topology!r}. Valid values: {sorted(_VALID_TOPOLOGIES)}."
        )
    creator_session_id = _resolve_or_uuid(creator_session_id)
    # #1 phantom-member guard: validate creator exists in the registry.
    _assert_session_registered(creator_session_id)
    resolved_members = [_resolve_or_uuid(m) for m in member_session_ids]
    # #1 phantom-member guard: validate all initial members exist.
    for sid in resolved_members:
        _assert_session_registered(sid)
    if creator_session_id not in resolved_members:
        resolved_members.append(creator_session_id)

    # K3b: roster-overlap guard — refuse to fork a live roster whose member-set
    # overlaps ours by ≥50%. Runs before any write so no partial file is created.
    if not allow_overlap:
        _check_roster_overlap(set(resolved_members))

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


def invite(
    chat_id: str,
    by_session_id: str,
    invitee_session_id: str,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    """Add a new member in `pending` state. Caller must be an accepted member.

    `role`: optional; if provided, atomically binds the invitee's role in
    `member_roles` at invite-time (#3 role-binding). This closes the bypass
    where a role-unbound lead could skip the NO_DIRECT_CODING enforcement gate
    (Themis resolves role→None → lead-base rules never load → Edit passes through).
    Belt-and-suspenders: #61 UNRESOLVABLE-blocking also applies to unbound roster
    members, so even an unbound invite still blocks after accept.
    """
    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
    invitee_session_id = _resolve_or_uuid(invitee_session_id)
    room = load_room(chat_id)
    members = room["members"]
    # Check caller's membership BEFORE registry validation (better error messages).
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

    # #1 phantom-member guard: invitee must be in the session registry.
    # (Checked after caller-membership so that error is surfaced first.)
    _assert_session_registered(invitee_session_id)

    # Privileged role-assignment authority: master or *-lead roles may only be
    # assigned by the current master — accepted-member-can-invite ≠ can-assign-any-role.
    if role is not None and (role == ROLE_MASTER or role.endswith("-lead")):
        if not _is_master(room, by_session_id):
            raise ValueError(
                f"Assigning privileged role {role!r} via invite requires master authority; "
                f"session {by_session_id!r} is not the master of {chat_id!r}."
            )

    # #3 ATOMIC ROLE-BINDING: if a role is provided, write it to member_roles now
    # so the invitee is never role-unbound. member_roles is authoritative for Themis.
    if role is not None:
        existing_meta = room["meta"]
        meta_patch: dict[str, Any] = {
            "kind": META,
            "event_id": _new_event_id(),
            "ts": _now_iso(),
            "chat_id": chat_id,
        }
        member_roles = dict(existing_meta.get("member_roles") or {})
        member_roles[invitee_session_id] = role
        meta_patch["member_roles"] = member_roles
        _append(chat_id, meta_patch)
        log.info(
            "chats: invite bound role=%s for %s in %s",
            role,
            invitee_session_id,
            chat_id,
        )

    record = {
        "kind": MEMBER,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "session_id": invitee_session_id,
        "session_name": _resolve_session_name(invitee_session_id)
        or invitee_session_id[:8],
        "state": PENDING,
        "invited_by": by_session_id,
    }
    _append(chat_id, record)
    log.info("chats: %s invited %s to %s", by_session_id, invitee_session_id, chat_id)
    return record


def accept(chat_id: str, session_id: str) -> dict[str, Any]:
    """Move a pending member to accepted. Required before they receive notifications."""
    session_id = _resolve_or_uuid(session_id, chat_id=chat_id)
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
    session_id = _resolve_or_uuid(session_id, chat_id=chat_id)
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

# Chat-body fan-out cap (2026-06-07 — the money-printer fix). A chat message
# fans out over SSE to EVERY accepted member; an uncapped body means one large
# post (a design doc, a 270k-token LangGraph trace) is ingested by all N members
# and re-bills each member's full context window — the dominant roster cost
# driver. Over the cap, the daemon OFFLOADS the full body to a per-chat artifact
# file and stores a short preview + pointer in the chat. Members get the gist and
# read the file on demand only if it's relevant to their task. Content is never
# lost; fan-out is bounded.
_CHAT_BODY_CAP_CHARS = 4000
_CHAT_BODY_PREVIEW_CHARS = 800


def _artifacts_dir(chat_id: str) -> Path:
    return _chat_dir() / "artifacts" / chat_id


def _offload_large_body(chat_id: str, record_id: str, body: str) -> str:
    """If ``body`` exceeds the fan-out cap, write it to a per-chat artifact file
    and return a preview + pointer; otherwise return ``body`` unchanged.

    Fail-open: if the artifact write fails for any reason, fall back to a
    hard truncation with a pointer-less marker so an oversized body can NEVER
    fan out in full (the whole point of the cap).
    """
    if len(body) <= _CHAT_BODY_CAP_CHARS:
        return body

    preview = body[:_CHAT_BODY_PREVIEW_CHARS].rstrip()
    total = len(body)
    try:
        adir = _artifacts_dir(chat_id)
        adir.mkdir(parents=True, exist_ok=True)
        path = adir / f"{record_id}.md"
        path.write_text(body, encoding="utf-8")
        return (
            f"{preview}\n\n"
            f"… ✂️ body truncated for chat fan-out ({total:,} chars total). "
            f"FULL CONTENT: `{path}` — Read it ONLY if it's relevant to your task; "
            f"do not pull it into context reflexively."
        )
    except OSError:
        return (
            f"{preview}\n\n"
            f"… ✂️ body truncated for chat fan-out ({total:,} chars; "
            f"artifact write failed — ask the sender to re-post as a file pointer)."
        )


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
    sender_session_id = _resolve_or_uuid(sender_session_id, chat_id=chat_id)
    if private is True and not to:
        raise ValueError(
            "private=True requires a non-empty `to` list — "
            "a private message with no recipients is meaningless."
        )
    room = load_room(chat_id)
    # path-4 drift-healing: resolve a reattached session's sid to its member key.
    # Fail-closed: if (None, None) is returned, the session is DENIED (not a valid
    # current identity — superseded beyond bound or unregistered).
    canonical_sender, member = _slot_heal_member_key(room, sender_session_id)
    if canonical_sender is None:
        member = None  # explicit fail-closed
    else:
        sender_session_id = canonical_sender  # use the canonical key going forward
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
            rid = _resolve_or_uuid(r, chat_id=chat_id)
            rmember = room["members"].get(rid)
            if not rmember or rmember["state"] != ACCEPTED:
                rstate = (rmember or {}).get("state", "non-member")
                raise ValueError(
                    f"Recipient {r!r} is {rstate!r} in {chat_id!r}; "
                    f"only accepted members can be `to` targets."
                )
            resolved_to.append(rid)

    _msg_id = "msg-" + uuid.uuid4().hex[:12]
    record = {
        "kind": MSG,
        "event_id": _new_event_id(),
        "id": _msg_id,
        "ts": _now_iso(),
        "chat_id": chat_id,
        "sender_id": sender_session_id,
        "sender_name": member.get("session_name") or sender_session_id[:8],
        "body": _offload_large_body(chat_id, _msg_id, _sanitize_message_body(body)),
        "to": resolved_to,
        "private": effective_private,
    }
    _append(chat_id, record)

    # Dispatch-wake: a TARGETED send to an idle agent is a dispatch the agent
    # won't see until it turns — wake it. Broadcasts (no `to`) don't wake.
    if resolved_to:
        _auto_wake_targeted_idle(
            [(rid, (room["members"].get(rid) or {}).get("session_name")) for rid in resolved_to]
        )

    # Verdict-via-prose nudge (2026-06-09): critic/verifier repeatedly post a
    # thorough prose review to the chat but never make the structured
    # chat_task_verdict call the B3 gate reads — so the task sits done-not-approved
    # (3rd recurrence in one jeevy session). Behavioral→structural: when a
    # verdict-role posts to a chat with a done gate-task awaiting THEIR verdict and
    # they haven't recorded it, nudge them once. Best-effort; never breaks send.
    try:
        _maybe_nudge_missing_verdict(chat_id, sender_session_id, room)
    except Exception:
        log.debug("chats: verdict-nudge raised (non-fatal)", exc_info=True)

    # #14 auto-BEGIN: if this message is a compliant ready-ack, check the auto-BEGIN gate.
    _m_ack = _ACK_RE.search(record.get("body") or "")
    if _m_ack:
        _try_auto_begin(chat_id, _m_ack.group(1))

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


# Assign-time domain injection (tasks/domain-specialist/IMPLEMENTATION.md):
# leads are retired; implementer agents get specialist context attached to the
# task brief itself instead of from a standing domain-lead session.
VALID_TASK_DOMAINS = frozenset(
    {"backend", "frontend", "data", "devops", "orchestration"}
)
_DOMAIN_CONTEXT_CHAR_CAP = 3000


def _inject_domain_context(body: str, domain: str, sender_session_id: str) -> str:
    """Append PROVISIONAL mnemosyne knowledge for ``domain`` to a task body.

    Qualified key is ``<project>:<domain>`` where project comes from the
    sender session's recorded workspace (same source as the FLAG-B path);
    falls back to the bare domain when no project is detectable.

    Fail-open EVERYWHERE: mnemosyne down, no workspace, empty answer, any
    exception → body returned unchanged. Task creation must never block on
    the knowledge store.
    """
    try:
        from khimaira.hooks.mnemosyne_client import query as _mnemosyne_query
        from khimaira.hooks.session_end_utils import detect_project

        project = ""
        try:
            from khimaira.monitor import sessions as sessions_mod

            ws = sessions_mod.state(sender_session_id, recent=0).get("workspace") or ""
            if ws:
                project = detect_project(ws) or ""
        except Exception:
            project = ""
        qualified = (
            f"{project}:{domain}" if project and project != "unknown" else domain
        )

        result = _mnemosyne_query(qualified)
        answer = (result or {}).get("answer") or ""
        if not answer:
            return body
        if len(answer) > _DOMAIN_CONTEXT_CHAR_CAP:
            answer = answer[:_DOMAIN_CONTEXT_CHAR_CAP] + " … (truncated)"
        return (
            f"{body}\n\n🧠 domain context ({qualified}, auto-injected, "
            f"PROVISIONAL — verify against authoritative docs):\n{answer}"
        )
    except Exception:
        return body


def create_task(
    chat_id: str,
    sender_session_id: str,
    body: str,
    assignee_session_id: str | None = None,
    assignee_role: str | None = None,
    gate_required: bool = False,
    gate_for: str | None = None,
    verdict_role: str | None = None,
    *,
    private: bool = False,
    required_agents: list[str] | None = None,
    auto_begin: bool = True,
    required_model: str | None = None,
    required_effort: str | None = None,
    begin_gate_task_id: str | None = None,
    domain: str | None = None,
    high_stakes: bool = False,
) -> dict[str, Any]:
    """Append a TASK record (status=pending). Sender must be an accepted member;
    if assignee_session_id is set, that session must also be accepted.

    `private=True`: task hidden from non-assignee members in chat_history.
    Requires assignee_session_id (private task with no assignee is meaningless).

    Guard-5 Part A additions:
    - `assignee_role`: role-class assignee ("critic"/"verifier"/etc). Obligation binds
       to the ROLE, satisfiable by any holder. Use instead of assignee_session_id for
       review-tasks so a dead/wedged named reviewer can't deadlock the gate.
    - `gate_required=True`: daemon will AUTO-create review-tasks (one per verdict_role
       that lacks an existing task) when this task transitions to done.
    - `gate_for`: str task_id — this task is a review-gate for that work-task. Set by
       the daemon when auto-creating review-tasks; marks the obligation-wrapper shape.
    - `verdict_role`: "critic"|"verifier" — which role produces the verdict that closes
       this review-task. Used by _get_session_obligations role-class scanning.

    Phase B v2: observers cannot create tasks.
    """
    sender_session_id = _resolve_or_uuid(sender_session_id, chat_id=chat_id)
    if domain is not None and domain not in VALID_TASK_DOMAINS:
        raise ValueError(
            f"Unknown task domain {domain!r}; valid: {sorted(VALID_TASK_DOMAINS)}."
        )
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
        # path-8 drift-healing: resolve a reattached session's sid to its member key.
        canonical_assignee, amember = _slot_heal_member_key(room, assignee_resolved)
        if canonical_assignee is None:
            amember = None
        else:
            assignee_resolved = canonical_assignee
        if not amember or amember["state"] != ACCEPTED:
            astate = (amember or {}).get("state", "non-member")
            raise ValueError(
                f"Assignee {assignee_session_id!r} is {astate!r} in {chat_id!r}; "
                f"only accepted members can be assignees."
            )
        assignee_name = amember.get("session_name") or assignee_resolved[:8]

    if domain:
        body = _inject_domain_context(body, domain, sender_session_id)

    _task_id = "task-" + uuid.uuid4().hex[:12]
    record = {
        "kind": TASK,
        "event_id": _new_event_id(),
        "id": _task_id,
        "ts": _now_iso(),
        "chat_id": chat_id,
        "sender_id": sender_session_id,
        "sender_name": member.get("session_name") or sender_session_id[:8],
        "body": _offload_large_body(chat_id, _task_id, _sanitize_message_body(body)),
        "assignee_id": assignee_resolved,
        "assignee_name": assignee_name,
        "status": TASK_PENDING,
        "private": private,
        # to=[assignee] normalises the private filter path so history() can
        # use a single check across all private record types.
        "to": [assignee_resolved] if private and assignee_resolved else None,
        # Guard-5 Part A fields
        "assignee_role": assignee_role,
        "gate_required": gate_required,
        # Lean commit gate: high_stakes → N=2 distinct gatekeeper ships required.
        "high_stakes": high_stakes,
        "gate_for": gate_for,
        "verdict_role": verdict_role,
        # #14 auto-BEGIN fields
        "required_agents": [_resolve_or_uuid(sid) for sid in (required_agents or [])],
        "auto_begin": auto_begin,
        "required_model": required_model,
        "required_effort": required_effort,
        "begin_gate_task_id": begin_gate_task_id,
        # domain-specialist: which knowledge profile was injected (None = none)
        "domain": domain,
    }
    _append(chat_id, record)

    # Dispatch-wake: a task assigned to an idle agent won't surface until it turns.
    if assignee_resolved:
        _auto_wake_targeted_idle([(assignee_resolved, assignee_name)])

    log.info(
        "chats: task %s created in %s by %s (assignee=%s assignee_role=%s gate_required=%s auto_begin=%s required_agents=%s)",
        record["id"],
        chat_id,
        sender_session_id,
        assignee_resolved or "(none)",
        assignee_role or "(none)",
        gate_required,
        auto_begin,
        record["required_agents"] or "(none)",
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
    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
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
    first_claim: dict[str, Any] | None = (
        None  # first TASK_UPDATE that set status=in_progress
    )
    for line in _read(chat_id):
        k = line.get("kind")
        if k == TASK and line.get("id") == task_id:
            task_record = line
            current_status = line.get("status")
        elif k == TASK_UPDATE and line.get("task_id") == task_id:
            current_status = line.get("status")
            if current_status == TASK_IN_PROGRESS and first_claim is None:
                first_claim = line

    if task_record is None:
        raise ValueError(f"No task with id={task_id!r} in {chat_id!r}.")

    # CAS guard: detect a duplicate claim before the generic transition check so
    # the claimant gets a structured "already claimed by X" rejection (409) rather
    # than a confusing "Invalid transition in_progress → in_progress" (403).
    if current_status == TASK_IN_PROGRESS and new_status == TASK_IN_PROGRESS:
        who = (
            (
                first_claim.get("by_name")
                or (first_claim.get("by_session_id") or "unknown")[:8]
            )
            if first_claim
            else "unknown"
        )
        when = first_claim.get("ts", "") if first_claim else ""
        raise ValueError(
            f"Task {task_id!r} already claimed by {who!r} at {when}. "
            f"Stand down — first claim wins atomically."
        )

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
    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
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


# Dispatch-wake (2026-06-10) — close the dispatch-to-idle loop. When master
# routes work to an idle agent (targeted send / task assignment), the SSE push is
# TURN-GATED: an idle Claude Code loop won't take a turn on it, so the dispatch
# sits unsurfaced until something else wakes the agent. The daemon already detects
# this (deaf/busy/dead classifier) and notifies master — but master nudging is
# drift-prone (muther GAP refinement). This CLOSES the loop: fire the kitty wake
# itself, removing master from the wake loop. Auto-wake (Joseph-chosen).
_DISPATCH_WAKE_ENABLED = os.environ.get("KHIMAIRA_DISPATCH_WAKE", "1") != "0"
_DISPATCH_WAKE_IDLE_MIN_S = float(os.environ.get("KHIMAIRA_DISPATCH_WAKE_IDLE_S", "20"))
_DISPATCH_WAKE_COOLDOWN_S = float(os.environ.get("KHIMAIRA_DISPATCH_WAKE_COOLDOWN_S", "30"))
_last_dispatch_wake: dict[str, float] = {}


_DEFAULT_DISPATCH_WAKE_MSG = (
    "⏰ dispatch from master — call chat_my_chats(session_id=<yours>) to "
    "re-register SSE, then act on the task/message just routed to you. The "
    "push was enqueued; this nudge surfaces it. Act now — don't wait."
)


def _dispatch_wake_worker(
    target_id: str,
    target_name: str,
    message: str | None = None,
    *,
    cooldown_key: str | None = None,
    role_hint: str | None = None,
) -> None:
    """Thread entry point for `_dispatch_wake_worker_async` (see there for the
    contract). Runs in a bare `threading.Thread` with no event loop of its own
    (both call sites use `threading.Thread(target=_dispatch_wake_worker, ...)`),
    so it opens a fresh loop via `asyncio.run` to drive the async roster_recovery
    kitty chain (`_discover_roster_windows`/`_get_screen`/`_inject_text_and_submit`
    are `async def` — see the kitty-in-async-loop chokepoint fix).
    """
    import asyncio

    asyncio.run(
        _dispatch_wake_worker_async(
            target_id, target_name, message,
            cooldown_key=cooldown_key, role_hint=role_hint,
        )
    )


async def _dispatch_wake_worker_async(
    target_id: str,
    target_name: str,
    message: str | None = None,
    *,
    cooldown_key: str | None = None,
    role_hint: str | None = None,
) -> None:
    """Wake one idle target via kitty. Runs in a daemon thread (via the
    `_dispatch_wake_worker` sync entry point) so the send never pays the ~0.3s
    inject latency. Conservative: idle-only (active sessions see the push),
    window busy-check (don't inject over a spinner), and a cooldown (don't
    re-wake on a burst). `message` overrides the default dispatch text.

    Observability (F1, muther GAP #1 2026-06-11): EVERY suppression path logs its
    reason at INFO. The prior silent `return`s made a non-firing wake impossible to
    diagnose — 12 verdicts produced zero wake lines and no clue why.

    cooldown_key (F2): the cooldown dedup bucket. Defaults to target_id, but the
    gate-complete wake passes f"{master_id}:{task_id}" so a burst of DISTINCT-task
    completions each wakes the master instead of collapsing to the first.

    role_hint (F4): prefer matching the target window by ROLE (robust — the
    auto_dispatch master-wake does this) before falling back to the fragile exact
    raw_name match. A session_name ≠ window-title mismatch silently killed every
    wake before this.
    """
    ck = cooldown_key or target_id
    try:
        now = time.time()
        if now - _last_dispatch_wake.get(ck, 0.0) < _DISPATCH_WAKE_COOLDOWN_S:
            log.info("chats: wake skipped — cooldown (key=%s) for %s", ck, target_name)
            return
        summ = sessions_mod.summary(target_id)
        idle_s = float((summ or {}).get("last_active_age_s") or 0)
        if idle_s < _DISPATCH_WAKE_IDLE_MIN_S:
            log.info(
                "chats: wake skipped — target active (idle %.0fs < %ds) %s",
                idle_s, int(_DISPATCH_WAKE_IDLE_MIN_S), target_name,
            )
            return

        from khimaira.monitor import roster_recovery as rr

        wins = await rr._discover_roster_windows()
        win = None
        if role_hint:
            win = next((w for w in wins if w.get("role") == role_hint), None)
        if win is None:
            win = next((w for w in wins if w.get("raw_name") == target_name), None)
        if win is None:
            # Cross-roster fallback: _discover_roster_windows is scoped to THIS
            # daemon's roster, so a targeted wake for another roster's session
            # (one daemon, many rosters) finds nothing. Look the exact session up
            # unscoped by name (muther note-2: dual-verdict wakes hit 0 windows).
            win = await rr._window_for_session_name(target_name)
        if win is None:
            log.info(
                "chats: wake skipped — no window for %s (name=%r role=%r, %d roster windows)",
                target_id[:8], target_name, role_hint, len(wins),
            )
            return
        wid = win["window_id"]
        screen = await rr._get_screen(wid)
        if screen is not None and rr._is_busy(screen):
            log.info("chats: wake skipped — window busy %s", target_name)
            return
        if await rr._inject_text_and_submit(wid, message or _DEFAULT_DISPATCH_WAKE_MSG, target_name):
            _last_dispatch_wake[ck] = now
            log.info(
                "chats: wake → %s (%s, idle %.0fs, key=%s)",
                target_id[:8], target_name, idle_s, ck,
            )
        else:
            log.info("chats: wake FAILED — inject returned false for %s", target_name)
    except Exception:
        log.warning("chats: wake worker raised for %s", target_name, exc_info=True)


def _maybe_wake_master_on_gate_complete(
    chat_id: str, task_id: str, room: dict[str, Any]
) -> None:
    """When the verdict just recorded COMPLETES a dual-verdict gate (both critic
    AND verifier have now voted on this task), wake the master to act on it.

    Closes the dead-SSE commit-miss (muther GAP #1, 2026-06-11): the master's SSE
    subscriber doesn't survive compaction, so it can miss the verdict-completion
    event and strand a fully-approved task uncommitted. The daemon now drives the
    master on completion instead of relying on it to see the event live. Reuses
    the (now duplicate-safe) wake; conservative guards live in the worker.
    """
    tally = _gate_tally(room)
    entry = tally.get(task_id)
    if not entry:
        return
    committable = _is_committable(entry)
    gk = entry.get("gk_latest") or {}
    has_hold = (
        any(v == "hold" for v in gk.values())
        or entry.get("ver") == "hold"
        or entry.get("crit") == "changes"
    )
    # Only wake once the gate reaches a DECISION: committable (N distinct gatekeeper
    # ships / legacy dual-positive), or a blocking hold on a done task. Otherwise the
    # gate isn't complete yet (N not reached, only some reviewers voted).
    if not committable and not (has_hold and entry.get("status") == TASK_DONE):
        return  # gate not yet at a decision

    member_roles = (room.get("meta") or {}).get("member_roles") or {}
    master_id = next((sid for sid, r in member_roles.items() if r == ROLE_MASTER), None)
    if not master_id:
        log.info(
            "chats: gate-complete wake — no master in member_roles for %s (task %s)",
            chat_id, task_id,
        )
        return
    master_name = (room["members"].get(master_id) or {}).get("session_name")
    if not master_name:
        log.info(
            "chats: gate-complete wake — master %s has no session_name in %s",
            master_id[:8], chat_id,
        )
        return

    n = _effective_gate_n(entry)
    if committable:
        msg = (
            f"⏰ COMMIT-READY for {task_id}: the commit gate is SATISFIED "
            f"({n} distinct gatekeeper ship(s) recorded / legacy dual-positive) — you "
            "may not have seen it live if your SSE dropped post-compaction. Call "
            "chat_my_chats to re-register, then COMMIT + approve the task now. Don't "
            "wait for an event — this IS the event."
        )
    else:
        msg = (
            f"⏰ gate decided for {task_id} with a HOLD (not commit-ready). Call "
            "chat_my_chats, then dispatch rework per the gatekeeper verdict. Don't "
            "wait for an event."
        )

    import threading

    threading.Thread(
        target=_dispatch_wake_worker,
        args=(master_id, master_name, msg),
        kwargs={"cooldown_key": f"{master_id}:{task_id}", "role_hint": ROLE_MASTER},
        daemon=True,
    ).start()


# ---------------------------------------------------------------------------
# Gate tally — single source of truth for the commit gate (lean Option B,
# signed off 2026-06-28). LEAN model: a GATEKEEPER-role session posts ONE
# ship/hold verdict (its reasoning covers BOTH axes — correctness + verification).
# A task is committable when >= N DISTINCT gatekeeper sessions have a latest
# verdict of `ship` AND no gatekeeper has an outstanding `hold`. N = 2 when the
# task is high_stakes OR any gatekeeper verdict escalated (gatekeeper self-
# escalation safety-default), else 1. The LEGACY dual (critic=approve +
# verifier=ship) is kept as an alternate committable path so pre-cutover in-flight
# tasks don't strand. DISTINCT-SESSION counting is load-bearing: one session
# voting twice is NOT two independent verdicts (the independence property).
# ---------------------------------------------------------------------------


def _gate_tally(room: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Per-task gate state keyed by task_id. Each entry:
    {status, high_stakes, escalated, gk_latest:{sid:'ship'|'hold'}, crit, ver}.
    `gk_latest` holds the LATEST ship/hold per gatekeeper-role session (later
    events overwrite earlier — JSONL is append/ts order)."""
    member_roles = (room.get("meta") or {}).get("member_roles") or {}
    tally: dict[str, dict[str, Any]] = {}

    def _entry(tid: str) -> dict[str, Any]:
        return tally.setdefault(
            tid,
            {
                "status": TASK_PENDING,
                "high_stakes": False,
                "escalated": False,
                "gk_latest": {},
                "crit": None,
                "ver": None,
            },
        )

    for m in room.get("messages", []):
        k = m.get("kind")
        if k == TASK and m.get("id"):
            e = _entry(m["id"])
            e["status"] = m.get("status") or TASK_PENDING
            e["high_stakes"] = bool(m.get("high_stakes"))
        elif k == TASK_UPDATE and m.get("task_id") and m.get("status"):
            _entry(m["task_id"])["status"] = m["status"]
        elif k == TASK_VERDICT and m.get("task_id"):
            e = _entry(m["task_id"])
            v = m.get("verdict")
            sid = m.get("by_session_id")
            if v in ("approve", "changes"):
                e["crit"] = v
            elif v in ("ship", "hold"):
                e["ver"] = v
            # Lean: a ship/hold from a GATEKEEPER-role session is a gate vote;
            # latest per session wins (changed-mind handling).
            if sid and v in ("ship", "hold") and member_roles.get(sid) == ROLE_GATEKEEPER:
                e["gk_latest"][sid] = v
            if m.get("escalate"):
                e["escalated"] = True
    return tally


def _effective_gate_n(entry: dict[str, Any]) -> int:
    """Distinct independent gatekeeper ships required: 2 for high-stakes (explicit
    flag OR gatekeeper self-escalation), else 1."""
    return 2 if (entry.get("high_stakes") or entry.get("escalated")) else 1


def _is_committable(entry: dict[str, Any]) -> bool:
    """True if a done task's commit gate is satisfied: lean N-distinct-gatekeeper
    ships (no outstanding gatekeeper hold), OR the legacy critic=approve +
    verifier=ship dual (pre-cutover compat)."""
    if entry.get("status") != TASK_DONE:
        return False
    gk = entry.get("gk_latest") or {}
    ships = {s for s, v in gk.items() if v == "ship"}
    holds = {s for s, v in gk.items() if v == "hold"}
    if not holds and len(ships) >= _effective_gate_n(entry):
        return True
    # Legacy dual-positive (pre-cutover in-flight tasks).
    return entry.get("crit") == "approve" and entry.get("ver") == "ship"


def committable_gate_tasks(chat_id: str) -> list[str]:
    """Task ids that are `done` and commit-gate-satisfied (lean N-distinct-
    gatekeeper-ship per `_is_committable`, or the legacy critic+verifier dual) but
    NOT yet `approved`/`changes_requested` — fully reviewed, awaiting master commit.

    The level-triggered backstop (F3, muther GAP #1) reads this every sweep so a
    commit-ready task can't be stranded by a missed edge-event. Excludes anything
    the master already acted on (status moved off `done`).
    """
    return _committable_task_ids(load_room(chat_id))


def _committable_task_ids(room: dict[str, Any]) -> list[str]:
    """Pure core of committable_gate_tasks — operates on a loaded room dict so it
    can be unit-tested without touching disk. Delegates the gate decision to the
    single-source `_gate_tally` / `_is_committable`."""
    return [tid for tid, e in _gate_tally(room).items() if _is_committable(e)]


def _auto_wake_targeted_idle(targets: list[tuple[str, str | None]]) -> None:
    """Spawn a wake worker per (session_id, session_name) dispatch target.
    Best-effort, threaded; never blocks or breaks the caller.
    """
    if not _DISPATCH_WAKE_ENABLED:
        return
    import threading

    for sid, name in targets:
        if sid and name:
            threading.Thread(
                target=_dispatch_wake_worker, args=(sid, name), daemon=True
            ).start()


# Verdict-nudge dedup — (chat_id, task_id, sender_id) already nudged this daemon
# lifetime. In-memory: a re-nudge after a daemon restart is harmless.
_VERDICT_NUDGED: set[tuple[str, str, str]] = set()

# Which verdicts each reviewer role is responsible for (mirror of
# record_gate_verdict's _VERDICT_AUTHOR_ROLES, inverted). Lean: gatekeeper authors
# the ship/hold gate verdict (critic/verifier kept for pre-cutover compat).
_ROLE_VERDICTS: dict[str, frozenset[str]] = {
    ROLE_CRITIC: frozenset({"approve", "changes"}),
    ROLE_VERIFIER: frozenset({"ship", "hold"}),
    ROLE_GATEKEEPER: frozenset({"ship", "hold"}),
}


def _maybe_nudge_missing_verdict(
    chat_id: str, sender_session_id: str, room: dict[str, Any]
) -> None:
    """If a verdict-role member posts to a chat that has a done gate-task
    awaiting THEIR structured verdict — and they haven't recorded it — post a
    one-line notice reminding them to call chat_task_verdict (not prose).

    Targeted + deduped: nudges once per (chat, task, reviewer) per daemon
    lifetime. Structural trigger (role + open gate + no verdict), NOT prose
    content-matching — so it can't misread a nuanced review.
    """
    member_roles = (room.get("meta") or {}).get("member_roles") or {}
    sender_role = member_roles.get(sender_session_id)
    if sender_role not in _ROLE_VERDICTS:
        return  # not a reviewer role — nothing to nudge

    messages = room.get("messages", [])

    # Current status per task + which tasks this sender already verdicted.
    status_by_task: dict[str, str] = {}
    gate_role_by_task: dict[str, str | None] = {}
    gate_required_by_task: dict[str, bool] = {}
    verdicted_by_sender: set[str] = set()
    for m in messages:
        k = m.get("kind")
        if k == TASK:
            tid = m.get("id")
            if tid:
                status_by_task[tid] = m.get("status") or TASK_PENDING
                gate_role_by_task[tid] = m.get("verdict_role")
                gate_required_by_task[tid] = bool(m.get("gate_required"))
        elif k == TASK_UPDATE:
            tid = m.get("task_id")
            if tid and m.get("status"):
                status_by_task[tid] = m["status"]
        elif k == TASK_VERDICT:
            if m.get("by_session_id") == sender_session_id and m.get("task_id"):
                verdicted_by_sender.add(m["task_id"])

    # A task awaits THIS reviewer's verdict if it is `done`, wants this role's
    # verdict (explicit verdict_role match, or a gate_required work-task), and
    # this reviewer hasn't recorded a verdict for it.
    for tid, status in status_by_task.items():
        if status != TASK_DONE:
            continue
        wants_this_role = (
            gate_role_by_task.get(tid) == sender_role
            or gate_required_by_task.get(tid)
        )
        if not wants_this_role:
            continue
        if tid in verdicted_by_sender:
            continue
        key = (chat_id, tid, sender_session_id)
        if key in _VERDICT_NUDGED:
            continue
        _VERDICT_NUDGED.add(key)
        verdict_opts = " | ".join(sorted(_ROLE_VERDICTS[sender_role]))
        try:
            sessions_mod.post_notice(
                target_session_id=sender_session_id,
                text=(
                    f"⚖️ VERDICT NOT RECORDED — task {tid} is done and awaiting your "
                    f"{sender_role} verdict, but the B3 gate reads only the STRUCTURED "
                    f"event, not chat prose. A prose review does NOT clear the gate. "
                    f"Call the TOOL: chat_task_verdict(chat_id={chat_id!r}, "
                    f"task_id={tid!r}, verdict=<{verdict_opts}>). Your written review "
                    f"stands as the rationale; this is the one structured call that "
                    f"actually records it."
                ),
                from_session_id="khimaira-daemon",
            )
            log.info(
                "chats: verdict-nudge → %s (role=%s, task=%s, chat=%s)",
                sender_session_id[:8], sender_role, tid, chat_id,
            )
        except Exception:
            log.debug("chats: verdict-nudge post_notice failed", exc_info=True)


def _maybe_auto_advance_gate_complete(chat_id: str, task_id: str) -> bool:
    """Auto-advance a SOLO-assignee task to `done` when its gate just completed.

    ISSUE 3 hybrid (muther 2026-06-18): when both gate verdicts (critic approve/changes
    + verifier ship/hold — PRESENCE, both reviewers acted) are recorded on a task that
    is still pending/in_progress AND has a single assignee (no multi-agent required_agents
    set), the assignee-driven done-transition often never fires, leaving the status stuck
    at in_progress forever — false AWAITING-ACK + false owing-idle wake target. The gate
    completing IS sufficient evidence the agent's work is finished, so the system advances
    it to `done` on the agent's behalf. Master still commits via the normal done → approved
    path (the human/master gate is preserved). Multi-agent gated tasks are EXCLUDED — they
    keep the explicit per-agent ack.

    Returns True if it advanced the status, else False. Idempotent: a task already past
    in_progress is left untouched.
    """
    status: str | None = None
    assignee_id: str | None = None
    required_agents: list[str] = []
    high_stakes = False
    escalated = False
    crit_present = False
    ver_present = False
    member_roles: dict[str, str] = {}
    gk_authors: set[str] = set()  # distinct ship/hold authors (role-filtered below)
    for line in _read(chat_id):
        k = line.get("kind")
        if k == META and line.get("member_roles"):
            member_roles = line.get("member_roles") or member_roles  # latest META wins
        elif k == TASK and line.get("id") == task_id:
            status = line.get("status")
            assignee_id = line.get("assignee_id")
            required_agents = list(line.get("required_agents") or [])
            high_stakes = bool(line.get("high_stakes"))
        elif k == TASK_UPDATE and line.get("task_id") == task_id:
            status = line.get("status")
        elif k == TASK_VERDICT and line.get("task_id") == task_id:
            v = line.get("verdict", "")
            sid = line.get("by_session_id")
            if v in ("approve", "changes"):
                crit_present = True
            elif v in ("ship", "hold"):
                ver_present = True
            if sid and v in ("ship", "hold"):
                gk_authors.add(sid)
            if line.get("escalate"):
                escalated = True

    # Lean gate-quorum: N DISTINCT gatekeeper-role ship/hold authors (they acted),
    # N=2 high-stakes else 1; OR the legacy critic+verifier both-present dual.
    gk_distinct = {s for s in gk_authors if member_roles.get(s) == ROLE_GATEKEEPER}
    n = 2 if (high_stakes or escalated) else 1
    gate_reached = (len(gk_distinct) >= n) or (crit_present and ver_present)

    if status not in (TASK_PENDING, TASK_IN_PROGRESS):
        return False  # already terminal / past in_progress — nothing to advance
    if not gate_reached:
        return False  # gate not complete yet
    if not assignee_id:
        return False  # unassigned task — no solo owner to advance on behalf of
    # SOLO check: no multi-agent gate (required_agents empty or just the assignee).
    if required_agents and set(required_agents) - {assignee_id}:
        return False  # multi-agent gated task — keep the explicit per-agent ack

    record = {
        "kind": TASK_UPDATE,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "task_id": task_id,
        "status": TASK_DONE,
        "by_session_id": assignee_id,
        "by_name": "khimaira-daemon (gate-auto)",
        "note": (
            "auto-advanced → done: gate complete (critic + verifier verdicts recorded) "
            "on a solo-assignee task whose done-transition never fired (ISSUE 3 hybrid)."
        ),
        "private": False,
        "to": None,
    }
    _append(chat_id, record)
    log.info(
        "chats: task %s AUTO-ADVANCED %s → done (gate complete, solo-assignee) in %s",
        task_id,
        status,
        chat_id,
    )
    return True


def record_gate_verdict(
    chat_id: str,
    by_session_id: str,
    task_id: str,
    verdict: str,
    *,
    escalate: bool = False,
) -> dict[str, Any]:
    """Append a structured gate-verdict event (B3 Slice B-1).

    verdict ∈ {"approve", "changes", "ship", "hold"}:
      - "ship" / "hold": the GATE verdict — written by gatekeeper (lean) or verifier
        (legacy compat). The gatekeeper's ship/hold reasoning must cover BOTH axes:
        correctness/critique AND verification/tests.
      - "approve" / "changes": legacy critic critique verdict (pre-cutover compat). In
        the lean model the critique rides the ship/hold reason or the task note, not a
        separate gate verdict.

    `escalate=True` (gatekeeper self-escalation): marks the task high-stakes so the
    commit gate requires N=2 distinct gatekeeper ships (the safety-default for when a
    gatekeeper judges a change high-stakes that master didn't flag).

    Caller must be an accepted member holding a role authorized for the verdict.
    """
    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
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
    # Author-role-binding: ship/hold = gatekeeper (lean) or verifier (legacy);
    # approve/changes = critic. Prevents master / any non-reviewer from self-posting
    # structured verdicts and bypassing IN-MASTER-9 (B3 follow-up fix).
    _VERDICT_AUTHOR_ROLES: dict[str, frozenset[str]] = {
        "approve": frozenset({ROLE_CRITIC}),
        "changes": frozenset({ROLE_CRITIC}),
        "ship": frozenset({ROLE_GATEKEEPER, ROLE_VERIFIER}),
        "hold": frozenset({ROLE_GATEKEEPER, ROLE_VERIFIER}),
    }
    allowed_roles = _VERDICT_AUTHOR_ROLES[verdict]
    caller_role = (room.get("meta") or {}).get("member_roles", {}).get(by_session_id)
    if caller_role not in allowed_roles:
        raise ValueError(
            f"verdict={verdict!r} requires one of {sorted(allowed_roles)} role(s); "
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
        # Gatekeeper self-escalation → commit gate requires N=2 distinct ships.
        "escalate": bool(escalate),
    }
    _append(chat_id, record)
    # Hybrid lifecycle auto-advance (ISSUE 3 / muther 2026-06-18): if this verdict
    # completes the gate (critic + verifier both recorded) on a SOLO-assignee task
    # still stuck at pending/in_progress, advance its status to `done` so it stops
    # reading in_progress forever (the agent did the work + passed the gate but the
    # assignee-driven done-transition never fired). Solo-only per the ruling; master
    # then commits via the normal done → approved path. Fail-open.
    try:
        _maybe_auto_advance_gate_complete(chat_id, task_id)
    except Exception as exc:
        log.warning("chats: gate-complete auto-advance failed for %s: %s", task_id, exc)
    log.info(
        "chats: task %s verdict=%r by %s in %s",
        task_id,
        verdict,
        by_session_id,
        chat_id,
    )
    # If this verdict completes a dual-verdict gate, wake the master to act —
    # closes the dead-SSE commit-miss. Re-load so the just-appended verdict is
    # visible. Best-effort; never affects the verdict-record result.
    try:
        _maybe_wake_master_on_gate_complete(chat_id, task_id, load_room(chat_id))
    except Exception:
        log.debug("chats: gate-complete wake raised (non-fatal)", exc_info=True)
    return record


_VERDICT_TRIGGER_QUORUM = "quorum_timeout"
_VERDICT_TRIGGER_MANUAL = "manual_deadlock"
_VERDICT_TRIGGERS: frozenset[str] = frozenset(
    {_VERDICT_TRIGGER_QUORUM, _VERDICT_TRIGGER_MANUAL}
)


def master_override_verdict(
    chat_id: str,
    by_session_id: str,
    task_id: str,
    verdict: str,
    reason: str,
    trigger: str,
) -> dict[str, Any]:
    """Audited gate-close for master — unifies master-collapse + B3-emergency-bypass.

    Guard-5 Part A: when a review-gate stalls (quorum-timeout or verified deadlock),
    master can self-post an override verdict with explicit reason + trigger. This is
    the quorum-timeout escape valve — NOT a rubber-stamp bypass.

    IN-MASTER-9 permits this verdict when is_override=True + reason + trigger are
    present on the TASK_VERDICT record. A bare verdict (missing these) is still blocked.

    Only the chat master may call this. trigger ∈ {quorum_timeout, manual_deadlock}.
    reason must be non-empty (the override is logged, never silent).
    """
    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
    valid_verdicts = frozenset({"approve", "changes", "ship", "hold"})
    if verdict not in valid_verdicts:
        raise ValueError(
            f"Invalid verdict {verdict!r}; must be one of {sorted(valid_verdicts)}"
        )
    if trigger not in _VERDICT_TRIGGERS:
        raise ValueError(
            f"Invalid trigger {trigger!r}; must be one of {sorted(_VERDICT_TRIGGERS)}"
        )
    if not reason or not reason.strip():
        raise ValueError("reason must be non-empty — the override must be auditable.")

    room = load_room(chat_id)
    if not _is_master(room, by_session_id):
        raise ValueError(
            f"Session {by_session_id!r} is not the master of {chat_id!r}; "
            f"only the master can post an override verdict."
        )

    task_record = None
    for msg in room.get("messages", []):
        if msg.get("kind") == TASK and msg.get("id") == task_id:
            task_record = msg
            break
    if task_record is None:
        raise ValueError(f"No task with id={task_id!r} in {chat_id!r}.")

    member = room["members"].get(by_session_id)
    record = {
        "kind": TASK_VERDICT,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "task_id": task_id,
        "verdict": verdict,
        "by_session_id": by_session_id,
        "by_name": (
            member.get("session_name") or by_session_id[:8]
            if member
            else by_session_id[:8]
        ),
        "is_override": True,
        "override_reason": reason.strip(),
        "override_trigger": trigger,
    }
    _append(chat_id, record)
    log.warning(
        "chats: OVERRIDE verdict task=%s verdict=%r trigger=%r reason=%r by=%s in=%s",
        task_id,
        verdict,
        trigger,
        reason,
        by_session_id,
        chat_id,
    )
    return record


# Sentinel constants for the tri-state gate-verdict lookup.
_GATE_ABSENT = "absent"
_GATE_ERROR = "error"


def _last_round_reset_ts(room: dict[str, Any], task_id: str) -> str:
    """Timestamp of the most recent transition that REOPENS a task for a fresh
    review round (done → any non-terminal status: pending / in_progress /
    changes_requested = sent back for rework). Verdicts recorded BEFORE this are
    stale prior-round verdicts that must not satisfy the current gate; verdicts
    AFTER it (or all of them, if never reopened) are current.

    Keys on the REOPEN, deliberately NOT on the last done-transition. A
    done-transition is the gate CLOSING — including the daemon's gate-auto-advance
    (`_maybe_auto_advance_gate_complete`) and a manual master →done — not a new
    round; `done → approved` is the gate SUCCEEDING, also not a reopen. Keying on
    last-done wrongly invalidated the very critic+verifier verdicts that TRIGGERED
    an auto-advance→done: master's subsequent →approved then saw zero verdicts and
    IN-MASTER-9 false-blocked it (audit 2026-06-21, muther task 625; the identical
    task 623 succeeded only because no auto-advance had fired yet).
    """
    transitions: list[tuple[str, str]] = []
    for m in room.get("messages", []):
        k = m.get("kind")
        if k == TASK and m.get("id") == task_id:
            transitions.append((m.get("ts", ""), m.get("status") or TASK_PENDING))
        elif k == TASK_UPDATE and m.get("task_id") == task_id:
            st = m.get("status") or m.get("new_status")
            if st:
                transitions.append((m.get("ts", ""), st))
    transitions.sort(key=lambda t: t[0])
    last_reset = ""
    prev: str | None = None
    for ts, st in transitions:
        # A reopen = leaving DONE for any non-terminal status. NOT done→done (no-op)
        # and NOT done→approved (gate success). Anything else (pending/in_progress/
        # changes_requested) is rework → resets the verdict round.
        if prev == TASK_DONE and st not in (TASK_DONE, TASK_APPROVED):
            last_reset = max(last_reset, ts)
        prev = st
    return last_reset


def _round_aware_gate_entry(room: dict[str, Any], task_id: str) -> dict[str, Any]:
    """Single-task gate entry (same shape as `_gate_tally`'s per-task value),
    computed ROUND-AWARE: verdicts recorded BEFORE the last round-reset
    (`_last_round_reset_ts`) are excluded, so a reopened task's stale prior-round
    verdicts can't satisfy the current gate (Guard-5 Part A).

    Use THIS — not `_gate_tally` — wherever round-reset matters (the Themis
    enrichment path: `get_gate_verdicts` / `get_gate_verdicts_by_task`).
    `_gate_tally` deliberately omits round-reset (its level-triggered backstop
    callers tolerate it); the Themis gate must not. Returns the entry dict
    `_is_committable` consumes: {status, high_stakes, escalated, gk_latest, crit, ver}.
    """
    member_roles = (room.get("meta") or {}).get("member_roles") or {}
    last_reset_ts = _last_round_reset_ts(room, task_id)
    entry: dict[str, Any] = {
        "status": TASK_PENDING,
        "high_stakes": False,
        "escalated": False,
        "gk_latest": {},
        "crit": None,
        "ver": None,
    }
    for m in room.get("messages", []):
        k = m.get("kind")
        if k == TASK and m.get("id") == task_id:
            entry["status"] = m.get("status") or TASK_PENDING
            entry["high_stakes"] = bool(m.get("high_stakes"))
        elif k == TASK_UPDATE and m.get("task_id") == task_id and m.get("status"):
            entry["status"] = m["status"]
        elif k == TASK_VERDICT and m.get("task_id") == task_id:
            # Skip stale prior-round verdicts (before the last reopen).
            if last_reset_ts and m.get("ts", "") < last_reset_ts:
                continue
            v = m.get("verdict")
            sid = m.get("by_session_id")
            if v in ("approve", "changes"):
                entry["crit"] = v
            elif v in ("ship", "hold"):
                entry["ver"] = v
            # Lean: latest ship/hold per GATEKEEPER-role session is a gate vote.
            if sid and v in ("ship", "hold") and member_roles.get(sid) == ROLE_GATEKEEPER:
                entry["gk_latest"][sid] = v
            if m.get("escalate"):
                entry["escalated"] = True
    return entry


def session_has_in_progress_assigned_task(session_id: str) -> bool:
    """True if any chat has a task assigned to ``session_id`` with status in_progress.

    Used by Themis IN-AGENT-7 (NO_SELF_DISPATCH_EDIT) to warn when an agent edits
    files while holding NO active assignment — i.e. self-dispatch (no task at all) or
    jumping the BEGIN gate (a pending task it hasn't been signalled to start). Keys on
    IN_PROGRESS specifically: a pending-but-not-started task does NOT license edits, so
    an agent editing against only a pending task still trips the warn (correctly — it
    jumped BEGIN). Fail-open: any read error → False (the condition then fail-opens to
    no-warn, so a transient error never spuriously nags a legitimately-working agent).
    """
    try:
        sid = _resolve_or_uuid(session_id)
    except Exception:
        return False
    try:
        chat_dir = _chat_dir()
        if not chat_dir.exists():
            return False
        for path in chat_dir.glob("chat-*.jsonl"):
            status_by_task: dict[str, str] = {}
            assignee_by_task: dict[str, str | None] = {}
            for m in _read(path.stem):
                k = m.get("kind")
                if k == TASK and m.get("id"):
                    status_by_task[m["id"]] = m.get("status") or TASK_PENDING
                    assignee_by_task[m["id"]] = m.get("assignee_id")
                elif k == TASK_UPDATE and m.get("task_id") and m.get("status"):
                    status_by_task[m["task_id"]] = m["status"]
            for tid, st in status_by_task.items():
                if st == TASK_IN_PROGRESS and assignee_by_task.get(tid) == sid:
                    return True
        return False
    except Exception:
        return False


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

        # Step 2: scan for verdict events — only from the CURRENT done round.
        # Guard-5 Part A: changes_requested invalidates prior-round verdicts.
        # When a work-task goes done→changes_requested→in_progress→done again,
        # stale round-1 verdicts must not satisfy the round-2 gate (B3 gate bypass).
        # Fix: find the timestamp of the most recent 'done' transition; only count
        # TASK_VERDICT events AFTER that timestamp (the current review round).
        task_id = best_task.get("id")
        try:
            room = load_room(best_chat_id)
        except Exception:
            return _GATE_ERROR

        # Round-aware single-source entry (excludes stale prior-round verdicts;
        # the round resets on a REOPEN, not the last done-transition — see
        # _round_aware_gate_entry / _last_round_reset_ts).
        entry = _round_aware_gate_entry(room, task_id)

        if entry["crit"] is None and entry["ver"] is None:
            return _GATE_ABSENT  # task found but no verdicts yet

        # `committable` is the SINGLE SOURCE OF TRUTH (lean N-distinct-gatekeeper-ship
        # OR legacy critic-approve+verifier-ship, via _is_committable). The legacy
        # critic_approved/verifier_shipped fields are kept for observability only.
        return {
            "task_id": task_id,
            "critic_approved": entry["crit"] == "approve",
            "verifier_shipped": entry["ver"] == "ship",
            "committable": _is_committable(entry),
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
            # Round-aware single-source entry (excludes stale prior-round verdicts on
            # a REOPEN, NOT on the last done-transition — last-done is poisoned by the
            # daemon's gate-auto-advance, which writes a done FROM these very verdicts;
            # keying on it would make IN-MASTER-9 false-block master's →approved
            # (audit 2026-06-21, task 625). See _round_aware_gate_entry.
            entry = _round_aware_gate_entry(room, task_id)

            if entry["crit"] is None and entry["ver"] is None:
                return _GATE_ABSENT
            # `committable` = SINGLE SOURCE OF TRUTH (lean N-distinct-gatekeeper-ship OR
            # legacy dual, via _is_committable). Legacy fields kept for observability.
            return {
                "task_id": task_id,
                "critic_approved": entry["crit"] == "approve",
                "verifier_shipped": entry["ver"] == "ship",
                "committable": _is_committable(entry),
            }

        return None  # task_id not found in any chat → no active task

    except Exception:
        return _GATE_ERROR


def task_status(chat_id: str, requester_session_id: str) -> list[dict[str, Any]]:
    """Return all tasks in this chat with their current folded status.
    Requester must be an accepted member."""
    requester_session_id = _resolve_or_uuid(requester_session_id, chat_id=chat_id)
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
        _auto_accept_by_name_path(name).write_text(
            json.dumps(payload), encoding="utf-8"
        )
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
    requester_session_id = _resolve_or_uuid(requester_session_id, chat_id=chat_id)
    room = load_room(chat_id)
    # path-3 drift-healing: resolve a reattached session's sid to its member key.
    # Fail-closed on None — superseded/unregistered sids are DENIED.
    canonical_req, member = _slot_heal_member_key(room, requester_session_id)
    if canonical_req is None:
        member = None
    else:
        requester_session_id = canonical_req
    if not member or member["state"] != ACCEPTED:
        raise ValueError(
            f"Session {requester_session_id!r} is not an accepted member of {chat_id!r}; "
            f"cannot read history."
        )
    msgs = room["messages"]
    if since_event_id:
        # Skip everything up to and including since_event_id.
        idx = next(
            (i for i, m in enumerate(msgs) if m.get("event_id") == since_event_id), None
        )
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
    session_id = _resolve_or_uuid(session_id, chat_id=chat_id)
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


def remove_member(
    chat_id: str, by_session_id: str, target_session_id: str
) -> dict[str, Any]:
    """Master/creator evicts `target_session_id` from the chat (#2 remove-member).

    Transitions target to REMOVED state AND discards them from _subscribers
    (reachability hygiene: a removed member must not read is_reachable=True).

    Only the chat's current master may remove members — creators who have
    transferred master should first reclaim master if they need to evict.

    `by_session_id` must be an accepted master. Target must be an accepted
    or pending member. Master cannot evict themselves — use `leave()`.
    """
    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
    target_session_id = _resolve_or_uuid(target_session_id, chat_id=chat_id)
    room = load_room(chat_id)

    if not _is_master(room, by_session_id):
        raise ValueError(
            f"Session {by_session_id!r} is not the master of {chat_id!r}; "
            f"only the master can remove members."
        )
    if by_session_id == target_session_id:
        raise ValueError(
            f"Master cannot remove themselves from {chat_id!r}. Use leave() instead."
        )

    target_member = room["members"].get(target_session_id)
    if not target_member:
        raise ValueError(
            f"Session {target_session_id!r} is not a member of {chat_id!r}."
        )
    if target_member.get("state") in (LEFT, REMOVED):
        raise ValueError(
            f"Session {target_session_id!r} is already in state "
            f"{target_member['state']!r}; nothing to remove."
        )

    record = {
        "kind": MEMBER,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "chat_id": chat_id,
        "session_id": target_session_id,
        "session_name": target_member.get("session_name"),
        "state": REMOVED,
        "removed_by": by_session_id,
    }
    _append(chat_id, record)

    # Reachability hygiene: discard the removed session's SSE subscriber so
    # is_reachable(target) stops returning True for a removed member.
    # This mirrors the P3 design's "eviction must un-subscribe from _subscribers".
    _subscribers.pop(target_session_id, None)

    log.info("chats: %s removed %s from %s", by_session_id, target_session_id, chat_id)
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
    from_session_id = _resolve_or_uuid(from_session_id, chat_id=chat_id)
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
        sys_msg_body = (
            f"📦 {from_name} transferred this chat to {to_name} — full context handoff"
        )
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
    # Part F MIGRATION BRIDGE: re-key any sid-keyed SSE subscribers from the old
    # session to the slot key so they survive the transfer. Needed when the
    # transferring session subscribed BEFORE slot-keying was deployed (pre-migration
    # old-style subscriber in _subscribers[from_session_id]).
    # Post-migration, fresh sessions subscribe with the slot key already, so this
    # is a no-op; it only fires for pre-migration sid-keyed queues.
    old_queues = _subscribers.pop(from_session_id, set())
    if old_queues:
        to_slot_key = _slot_subscriber_key(to_session_id)
        _subscribers.setdefault(to_slot_key, set()).update(old_queues)
        log.info(
            "chats: Part F migration bridge — re-keyed %d SSE queue(s) from %s → %s",
            len(old_queues),
            from_session_id[:8],
            to_slot_key,
        )

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
        current_state = (room["members"].get(current_creator) or {}).get(
            "state", "non-member"
        )
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


def _session_is_live(session_id: str) -> bool:
    """True if `session_id` has a registry dir AND was active recently.

    Reaped sessions (dir gone) and sessions stale beyond
    ``KHIMAIRA_MASTER_LIVE_S`` (default 1200s) are treated as dead. This is the
    safety property of :func:`reseat_master` — it fires only when the old master
    is genuinely gone, never to hijack a live one.
    """
    from . import sessions as _sessions

    if not (_sessions._BASE_DIR / session_id).is_dir():
        return False
    try:
        age = _sessions.summary(session_id).get("last_active_age_s")
    except Exception:  # noqa: BLE001 — any lookup failure = treat as not-live
        return False
    if age is None:
        return False
    return age < int(os.environ.get("KHIMAIRA_MASTER_LIVE_S", "1200"))


def reseat_master(chat_id: str, new_master_session_id: str) -> dict[str, Any]:
    """Recover an orphaned roster: seat a NEW session as master after the prior
    master session has DIED (window/process exited; registry-GC'd).

    Fills the dead-master gap. ``chat_grant_role`` is master-only (the dead
    master can't call it) and ``transfer_membership`` needs the dead session as
    the live donor — neither works when the master is gone. ``reseat_master``
    needs neither, but **REFUSES while the current master is still live**
    (registered + active within ``KHIMAIRA_MASTER_LIVE_S``), so it can't hijack
    an active roster — use ``chat_grant_role`` / ``chat_transfer_membership`` for
    a live handoff.

    Atomic from the caller's view:
      1. ``new_master`` must be a registered session.
      2. The incumbent master (if any) must NOT be live.
      3. ``new_master`` is added as an ACCEPTED member if not already one (admin
         add — no invite/accept handshake, since the inviter-of-record is gone).
      4. A single META write promotes ``new_master`` to master, demotes the dead
         incumbent to agent, and sets ``created_by = new_master``.
      5. The master role-directive is emitted to ``new_master``.

    Raises ValueError if ``new_master`` is unregistered or the incumbent is live.
    """
    new_master_session_id = _resolve_or_uuid(new_master_session_id)
    _assert_session_registered(new_master_session_id)
    room = load_room(chat_id)

    existing_meta = dict(room.get("meta") or {})
    member_roles = dict(existing_meta.get("member_roles") or {})

    # Identify the incumbent master: member_roles is authoritative, created_by
    # is the v1-era fallback.
    incumbent = next(
        (
            sid
            for sid, r in member_roles.items()
            if r == ROLE_MASTER and sid != new_master_session_id
        ),
        None,
    )
    if incumbent is None:
        cb = existing_meta.get("created_by")
        if cb and cb != new_master_session_id:
            incumbent = cb

    if incumbent and _session_is_live(incumbent):
        raise ValueError(
            f"Current master {incumbent!r} of {chat_id!r} is still live; "
            f"reseat_master only recovers a DEAD master. For a live handoff use "
            f"chat_grant_role or chat_transfer_membership."
        )

    # Admin member-add: the dead incumbent who would normally invite is gone, so
    # add new_master directly as ACCEPTED if not already a member.
    member = room["members"].get(new_master_session_id)
    if not member or member.get("state") != ACCEPTED:
        _append(
            chat_id,
            {
                "kind": MEMBER,
                "event_id": _new_event_id(),
                "ts": _now_iso(),
                "chat_id": chat_id,
                "session_id": new_master_session_id,
                "session_name": _resolve_session_name(new_master_session_id)
                or new_master_session_id[:8],
                "state": ACCEPTED,
            },
        )

    # Single META write: promote new master, demote the dead incumbent. Mirrors
    # the single-master invariant chat_grant_role preserves on a live swap.
    member_roles[new_master_session_id] = ROLE_MASTER
    if incumbent:
        member_roles[incumbent] = ROLE_AGENT
    new_name = (
        _resolve_session_name(new_master_session_id) or new_master_session_id[:8]
    )
    new_meta = {
        **{k: v for k, v in existing_meta.items() if k != "event_id"},
        "kind": META,
        "event_id": _new_event_id(),
        "ts": _now_iso(),
        "created_by": new_master_session_id,
        "created_by_name": new_name,
        "member_roles": member_roles,
    }
    _append(chat_id, new_meta)
    _emit_role_directive(chat_id, new_master_session_id, ROLE_MASTER)
    log.info(
        "chats: reseat_master %s for orphaned chat %s (dead incumbent=%s)",
        new_master_session_id,
        chat_id,
        incumbent,
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
    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
    target_session_id = _resolve_or_uuid(target_session_id, chat_id=chat_id)

    if role not in _VALID_ROLES:
        raise ValueError(f"Invalid role {role!r}. Valid roles: {sorted(_VALID_ROLES)}.")
    if demote_to not in _VALID_ROLES:
        raise ValueError(
            f"Invalid demote_to {demote_to!r}. Valid roles: {sorted(_VALID_ROLES)}."
        )
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
        (
            " (first explicit write — materialized implicit master)"
            if first_explicit_write
            else ""
        ),
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
        raise ValueError(
            f"Invalid demote_to {demote_to!r}. Valid roles: {sorted(_VALID_ROLES)}."
        )
    if demote_to == ROLE_MASTER:
        raise ValueError(
            "demote_to cannot be 'master' — single-master-with-delegation "
            "invariant requires at most one session holds master at a time."
        )

    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
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
        (
            current_master
            if current_master and current_master != by_session_id
            else "(none)"
        ),
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
    by_session_id = _resolve_or_uuid(by_session_id, chat_id=chat_id)
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


# Short-TTL cache for my_chats — avoids re-reading 24+ JSONL files on every
# agent turn. my_chats() measured at 78ms+ on a 24-file state; under 20
# concurrent agents this blocks the event loop for ~1.5s per wave.
# Agents call chat_my_chats() on every turn (SSE re-registration); membership
# changes are rare, so 2s staleness is acceptable.
_MY_CHATS_CACHE: dict[str, tuple[float, list]] = {}
_MY_CHATS_TTL_S: float = 2.0


def my_chats(session_id: str) -> list[dict[str, Any]]:
    """List chats where session is an accepted member, with brief metadata."""
    session_id = _resolve_or_uuid(session_id)
    now = time.monotonic()
    if session_id in _MY_CHATS_CACHE:
        cached_at, cached_result = _MY_CHATS_CACHE[session_id]
        if now - cached_at < _MY_CHATS_TTL_S:
            return cached_result
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
                "member_count": sum(
                    1 for m in room["members"].values() if m["state"] == ACCEPTED
                ),
                "message_count": len(room["messages"]),
                "last_message_ts": (
                    room["messages"][-1]["ts"] if room["messages"] else None
                ),
            }
        )
    out.sort(key=lambda c: c["last_message_ts"] or "", reverse=True)
    _MY_CHATS_CACHE[session_id] = (time.monotonic(), out)
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


def _is_task_begun(chat_id: str, task_id: str) -> bool:
    """Return True if a TASK_SIGNAL 'start' event exists for this task.

    Guards against double-fire in _try_auto_begin and provides the
    Guard-4 2D pending-gate truth value: pending+no_begin = waiting,
    pending+begun = wedged.
    Fail-open: returns False on any read error so callers stay safe.
    """
    try:
        for line in _read(chat_id):
            if (
                line.get("kind") == TASK_SIGNAL
                and line.get("task_id") == task_id
                and line.get("signal") == "start"
            ):
                return True
    except Exception:
        pass
    return False


def _try_auto_begin(chat_id: str, task_id: str) -> bool:
    """Auto-fire BEGIN if all gate conditions are satisfied. (#14)

    Called from send_message on every compliant ready-ack. Makes a
    single-pass JSONL scan to gather:
      - the task record (auto_begin flag, required_agents, required tier, begin_gate_task_id)
      - whether BEGIN was already fired (idempotency guard)
      - the set of acks received so far

    Returns True if BEGIN was fired this call; False otherwise.
    Fail-open: any unexpected exception returns False.
    """
    try:
        task_record: dict | None = None
        begun = False
        acks: dict[str, dict] = {}  # sender_id → {model, effort, ts}

        for line in _read(chat_id):
            k = line.get("kind")
            if k == TASK and line.get("id") == task_id:
                task_record = line
            elif (
                k == TASK_SIGNAL
                and line.get("task_id") == task_id
                and line.get("signal") == "start"
            ):
                begun = True
            elif k == MSG:
                m = _ACK_RE.search(line.get("body") or "")
                if m and m.group(1) == task_id:
                    sender_id = line.get("sender_id")
                    if sender_id:
                        # Keep the latest ack per sender (handles re-ack).
                        acks[sender_id] = {
                            "model": m.group(2).lower(),
                            "effort": m.group(3).lower(),
                            "ts": line.get("ts") or "",
                        }

        if begun or task_record is None:
            return False
        if not task_record.get("auto_begin", True):
            return False
        required_agents: list[str] = task_record.get("required_agents") or []
        if not required_agents:
            return False

        req_model: str | None = task_record.get("required_model")
        req_effort: str | None = task_record.get("required_effort")

        # Verdict/budget gate: block if a prior task's B3 verdicts aren't in.
        begin_gate_task_id: str | None = task_record.get("begin_gate_task_id")
        if begin_gate_task_id:
            verdicts = get_gate_verdicts_by_task(
                task_record.get("sender_id") or required_agents[0],
                begin_gate_task_id,
            )
            # Lean-aware: `committable` covers BOTH the lean N-distinct-gatekeeper-ship
            # gate AND the legacy critic-approve+verifier-ship dual (single source).
            if not (isinstance(verdicts, dict) and verdicts.get("committable")):
                log.debug(
                    "chats: auto-BEGIN blocked by verdict gate task=%s gate=%s verdicts=%r",
                    task_id,
                    begin_gate_task_id,
                    verdicts,
                )
                return False

        # All required agents must have compliant acks.
        for agent_id in required_agents:
            ack = acks.get(agent_id)
            if ack is None:
                return False
            if req_model and ack["model"] != req_model.lower():
                return False
            if req_effort and ack["effort"] != req_effort.lower():
                return False

        # Gate satisfied — find master and fire BEGIN.
        room = load_room(chat_id)
        master_id: str | None = None
        for sid, role in (room["meta"].get("member_roles") or {}).items():
            if role == ROLE_MASTER:
                master_id = sid
                break
        if master_id is None:
            master_id = room["meta"].get("created_by")
        if master_id is None:
            log.warning(
                "chats: auto-BEGIN could not resolve master for chat=%s", chat_id
            )
            return False

        try:
            signal_task_start(
                chat_id,
                task_id,
                master_id,
                note="auto-BEGIN: all required agents confirmed",
            )
        except ValueError:
            # Task moved out of PENDING via race; treat as already begun.
            return False

        begin_body = _format_begin_block(task_record.get("body") or "")
        for agent_id in required_agents:
            try:
                send_message(chat_id, master_id, begin_body, to=[agent_id])
            except Exception:
                pass  # fail-open; agents can detect BEGIN via TASK_SIGNAL too

        log.info(
            "chats: auto-BEGIN fired task=%s agents=%s chat=%s",
            task_id,
            required_agents,
            chat_id,
        )
        return True
    except Exception:
        log.exception(
            "chats: _try_auto_begin failed for task=%s chat=%s", task_id, chat_id
        )
        return False


def roster_progress(chat_id: str, requester_session_id: str) -> list[dict[str, Any]]:
    """Observable-truth aggregator for roster member work state.

    Computes per-member status from OBSERVABLE signals — NOT the manual status
    string, which goes stale the moment an agent stops updating it. When manual
    status and observable signals disagree, the disagreement IS surfaced (that gap
    is the stale-status signal — the janice JEEVY-573 case).

    Signal ranking (encoded in the derived_label — NOT flat):
    1. disk-WIP (_session_has_recent_wip, 740bc1d) — hook-INDEPENDENT. PRIMARY.
       Survives an SSE/hook drop; catches silent-completion.
    2. owed-task state (chat task pending/in_progress/done/approved).
    3. done-reports (✅ messages in recent chat history) — ASYMMETRIC: present →
       reliable positive; absent ≠ not-done. disk-WIP is the silent-completion
       backstop; never infer "not done" from done-msg absence.
    4. file_touched / last_active — hook-DEPENDENT, SECONDARY liveness hints.
       file_touched stales EXACTLY when editing-but-deaf (the #7 false-dark); it
       belongs alongside last_active, NOT alongside disk-WIP.

    FLAG-B: precision bounded by assignee→sid binding. A drifted assignee_session_id
    (roster-identity drift bug, task-2837) → disk-WIP probe reads the wrong task's
    files → mis-attribution. Until slot-binding (task-2837 P1) lands, this is a
    residual surfaced in the output field `assignee_binding_note`.
    """
    import time

    requester_session_id = _resolve_or_uuid(requester_session_id, chat_id=chat_id)
    room = load_room(chat_id)
    member = room["members"].get(requester_session_id)
    if not member or member["state"] != ACCEPTED:
        raise ValueError(
            f"Session {requester_session_id!r} is not an accepted member of {chat_id!r}."
        )

    member_roles_map = (room.get("meta") or {}).get("member_roles") or {}

    # Single-pass scan: build owed-task map, task statuses, and done-reports.
    owed: dict[str, dict] = {}  # assignee_sid → {task_id, body, status}
    task_statuses: dict[str, str] = {}  # task_id → latest status
    done_reports: dict[str, str] = {}  # sender_sid → latest ✅ done-report ts
    for line in _read(chat_id):
        k = line.get("kind")
        if k == TASK:
            assignee = line.get("assignee_id")
            if assignee:
                # Last task per assignee wins (latest task creation in JSONL order).
                owed[assignee] = {
                    "task_id": line["id"],
                    "body": line.get("body", ""),
                    "status": TASK_PENDING,
                }
        elif k == "task_update":
            tid = line.get("task_id", "")
            if tid:
                task_statuses[tid] = line.get("status", "")
        elif k == "msg":
            body = line.get("body") or ""
            # ✅ is U+2705; catch both forms.
            if body.startswith("✅") or body.startswith("✅"):
                sid = line.get("sender_id", "")
                if sid:
                    done_reports[sid] = line.get("ts", "")

    # Apply latest task-status updates.
    for task in owed.values():
        if task["task_id"] in task_statuses:
            task["status"] = task_statuses[task["task_id"]]

    results = []
    now = time.time()

    for sid, mdata in room["members"].items():
        if mdata.get("state") != ACCEPTED:
            continue

        role = member_roles_map.get(sid) or "unknown"
        session_name = mdata.get("session_name") or sid[:8]

        # ── Manual status (secondary — may be stale) ──
        manual_status = ""
        last_active_s: float | None = None
        try:
            ss = sessions_mod.state(sid, recent=1)
            manual_status = (ss.get("status") or {}).get("detail") or ""
            la_iso = ss.get("last_active")
            if la_iso:
                from datetime import datetime as _dt

                last_active_s = now - _dt.fromisoformat(la_iso).timestamp()
        except Exception:
            pass

        # ── Owed task ──
        task_info = owed.get(sid)

        # ── disk-WIP (PRIMARY — hook-independent, per-session-precise) ──
        has_wip = False
        assignee_binding_note = ""
        if task_info:
            try:
                from khimaira.monitor.roster_recovery import _session_has_recent_wip

                # Use daemon's project root as fallback; cross-project sessions
                # need project_root from the session's recorded cwd (FLAG-B follow-up
                # per architect msg-fa3ba046b93a + analyst criterion-4).
                project_root = Path.cwd()
                try:
                    ws = sessions_mod.state(sid, recent=0).get("workspace")
                    if ws:
                        project_root = Path(ws)
                except Exception:
                    pass
                has_wip = _session_has_recent_wip(
                    sid, task_info["body"], project_root, threshold_s=900.0
                )
            except Exception:
                pass
            # FLAG-B: signal if the assignee sid might be drifted (task-2837).
            # We can't detect drift here without slot-binding; just note it.
            assignee_binding_note = (
                "assignee-sid may drift pre-task-2837; roster_progress precision "
                "improves after slot-binding lands"
            )

        # ── file_touched recency (SECONDARY — hook-dependent) ──
        last_touch_s: float | None = None
        try:
            touches = sessions_mod.recent_touches(sid, limit=1)
            if touches:
                from datetime import datetime as _dt

                last_touch_s = now - _dt.fromisoformat(touches[0]["ts"]).timestamp()
        except Exception:
            pass

        # ── done-report ──
        last_done_ts = done_reports.get(sid)

        # ── derived_label (encode signal ranking + no-WIP disambiguation) ──
        task_status = task_info["status"] if task_info else None
        if has_wip:
            derived = "working"
        elif task_status in (TASK_DONE, TASK_APPROVED):
            derived = "completed"
        elif task_status == TASK_IN_PROGRESS and last_done_ts:
            # Has done-msg even if no WIP → completed (may have stopped after posting)
            derived = "completed"
        elif task_status == TASK_IN_PROGRESS:
            # No WIP, no done-msg, task still in_progress → silent or stalled
            derived = "stalled-or-silent"
        elif task_status == TASK_PENDING:
            derived = "waiting-for-begin"
        else:
            derived = "idle"

        # ── stale-status flag (manual vs observable disagreement) ──
        stale = False
        if manual_status:
            ml = manual_status.lower()
            if derived in ("working", "completed") and any(
                w in ml for w in ("idle", "standby", "listening")
            ):
                stale = True
            elif derived == "idle" and any(
                w in ml
                for w in ("implementing", "working", "in_progress", "researching")
            ):
                stale = True

        entry: dict[str, Any] = {
            "session_id": sid,
            "name": session_name,
            "role": role,
            "owed_task": task_info,
            "has_recent_wip": has_wip,
            "last_done_report_ts": last_done_ts,
            "last_active_s": (
                round(last_active_s, 1) if last_active_s is not None else None
            ),
            "last_touch_s": (
                round(last_touch_s, 1) if last_touch_s is not None else None
            ),
            "manual_status": manual_status,
            "derived_label": derived,
            "stale_status": stale,
        }
        if task_info:
            entry["assignee_binding_note"] = assignee_binding_note
        results.append(entry)

    return results


# Stagger between BEGIN signals when multiple agents are dispatched simultaneously.
# Prevents burst API calls all hitting Anthropic in the same second (server-side 429).
# KHIMAIRA_DISPATCH_STAGGER_S env var overrides; 0 disables. Staggers FIRST call only.
_DISPATCH_STAGGER_S: float = float(os.environ.get("KHIMAIRA_DISPATCH_STAGGER_S", "2.5"))


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
    from_session_id = _resolve_or_uuid(from_session_id, chat_id=chat_id)
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
        # Stagger BEGIN signals across confirmed agents to prevent simultaneous
        # first-API-calls all hitting Anthropic in the same second (server-side 429).
        # Each agent receives an individual BEGIN targeted to it; _DISPATCH_STAGGER_S
        # (default 2.5s) between each. Staggers FIRST call only — subsequent calls
        # in each agent's turn are naturally unsynchronised.
        agents_in_order = sorted(confirmed)
        for i, agent_id in enumerate(agents_in_order):
            if i > 0 and _DISPATCH_STAGGER_S > 0:
                await asyncio.sleep(_DISPATCH_STAGGER_S)
            send_message(
                chat_id,
                from_session_id,
                _format_begin_block(begin_body),
                to=[agent_id],
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


def _slot_subscriber_key(sid: str) -> str:
    """Return the _subscribers key for a given session sid.

    Phase-B Part F: key by SLOT string when the session is slot-bound, so SSE
    delivery follows the roster identity across transfer/reattach. If sid is not
    slot-bound (pre-migration or non-roster session), falls through to sid itself.

    Why slot-key beats sid-key for SSE delivery:
    - After transfer_membership(A→B), SID_A is the accepted member in chat, but
      SID_B is the LIVE session (process). SID_B opens SSE → keyed by slot.
      `_broadcast` looks up slot(SID_A) == slot(SID_B) → finds B's queue.
    - Without slot-key, delivery goes to _subscribers[SID_A] = empty → dropped.

    Inert-denial: revoked sids (beyond-last-1-bound) should NOT hold SSE queues.
    If slot_resolve(sid) returns None, the session is revoked — a subscribe call
    for a revoked sid should be a no-op (subscriber never delivers).
    """
    try:
        from khimaira.monitor.sessions import get_session_slot

        slot = get_session_slot(sid)
        return slot if slot else sid
    except Exception:
        return sid


def is_reachable(session_id: str) -> bool:
    """Return True if session_id has a live SSE subscription (open /events connection).

    Phase-B Part F: checks both the slot-key (primary, for stamped sessions) and
    the sid-key (fallback, for un-slotted/legacy sessions).

    Caveat: lags a silent TCP-drop by ≤15s.
    """
    try:
        sid = _resolve_or_uuid(session_id)
    except Exception:
        return False
    slot_key = _slot_subscriber_key(sid)
    return bool(
        _subscribers.get(slot_key) or (slot_key != sid and _subscribers.get(sid))
    )


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
    to: "list[str] | None" = None,
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
            "to": to,
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
        # Part F: resolve invitee → slot key for delivery.
        inv_key = _slot_subscriber_key(invitee) if invitee else invitee
        for q in _subscribers.get(inv_key, ()):
            try:
                q.put_nowait(record)
            except asyncio.QueueFull:
                log.warning("chats: dropping invite for %s (queue full)", invitee)
        return

    # Phase B: per-recipient targeting on msg records.
    targeted: set[str] | None = None
    if record.get("kind") == MSG and record.get("to"):
        targeted = set(record["to"]) | {record.get("sender_id")}

    # Wake-filter (Class A): an UNDIRECTED msg must not wake idle-by-default
    # consult roles — they react to every broadcast (the cost leak) and then get
    # false-flagged for the silence. Directed msgs (targeted is not None) always
    # deliver. Unaddressed missed undirected msgs backfill via the next-turn
    # catch-up poll (_poll_missed_chat_events in the UPS hook) — lossless ONLY
    # within that poll's staleness window AND only once a turn fires. An ADDRESSED
    # undirected msg to a dead-SSE seat is the exception (it can outlive the catch-up
    # window before any wake lands), so it gets a durable notice below. See
    # tasks/sse-deaf-idle-wake/SPEC.md (muther ISSUE 1, 2026-06-18).
    member_roles = (room.get("meta", {}) or {}).get("member_roles") or {}

    # Default broadcast: to every accepted member (or filtered by `to`).
    for sid, member in room["members"].items():
        if member["state"] != ACCEPTED:
            continue
        if targeted is not None and sid not in targeted:
            continue
        if (
            targeted is None
            and record.get("kind") == MSG
            and member_roles.get(sid) in IDLE_CONSULT_ROLES
        ):
            # Consult-role wake-filter. Undirected broadcasts don't wake idle-by-
            # default consult seats (they'd react to every broadcast — the cost
            # leak the suppression was added to fix). BUT an explicit @mention is
            # the sender saying "you specifically": it must be honored.
            #
            # RESTORED ORIGINAL CONTRACT (author-confirmed by Joseph, 2026-06-27,
            # issue #29 sibling): the original roster design was broadcast-to-all +
            # @mention with UNIVERSAL real-time delivery — you @ a seat and it reads
            # it live. The consult-suppression optimization (correct in instinct:
            # don't wake every consult seat on every broadcast) severed that by
            # IGNORING the @. That omission is the regression — it caused the
            # JEEVY-605 stall (an @-addressed architect that was SSE-subscribed yet
            # idle got neither the live push nor a notice, so it sat 22 min).
            #
            # Fix: an @mention (by seat-name or role) restores live delivery to the
            # MENTIONED member only — fall through to the normal push below. Non-
            # mentioned consult seats stay suppressed (efficiency preserved). For an
            # @-mentioned seat with NO live subscriber, drop a durable notice as the
            # backstop so it survives to the agent's next turn. Weaker addressing
            # (bare name / role:) also gets the notice backstop but NOT a live push;
            # unaddressed chatter stays fully suppressed (no busy-room spam).
            _c_role = (member_roles.get(sid) or "").lower()
            _c_name = ((member or {}).get("session_name") or "").lower()
            _c_body = (record.get("body") or "").lower()
            _at_mentioned = any(
                tok and tok in _c_body for tok in (f"@{_c_name}", f"@{_c_role}")
            )
            _has_sub = bool(
                _subscribers.get(_slot_subscriber_key(sid)) or _subscribers.get(sid)
            )
            if _at_mentioned and _has_sub:
                pass  # restored @-contract: fall through to live real-time delivery
            else:
                _addressed = (
                    _at_mentioned
                    or (bool(_c_name) and _c_name in _c_body)
                    or any(
                        tok and tok in _c_body
                        for tok in (f"{_c_role}:", f"{_c_role}-")
                    )
                )
                if _addressed:
                    try:
                        sessions_mod.post_notice(
                            target_session_id=sid,
                            text=(
                                f"📨 You were addressed in an undirected chat message "
                                f"in {chat_id} from "
                                f"{record.get('sender_name') or record.get('sender_id')} "
                                f"that was not delivered in real time (consult-role "
                                f"wake-suppression). "
                                f"Call chat_history(chat_id='{chat_id}') to read it."
                            ),
                            from_session_id="khimaira-daemon",
                        )
                    except Exception:
                        pass
                continue  # not an @-mentioned live seat → suppress wake
        # Part F: resolve member sid → slot key so delivery follows the live
        # session across transfer/reattach. Slot-keyed subscriber receives the
        # event even when its membership entry is keyed to the prior sid.
        deliver_key = _slot_subscriber_key(sid)
        queues = _subscribers.get(deliver_key) or _subscribers.get(sid)
        if not queues:
            log.warning(
                "chats: dropped event for disconnected subscriber sid=%s chat=%s "
                "event_id=%s reason=no_subscriber",
                sid,
                chat_id,
                record.get("event_id"),
            )
            # Directed-delivery durability (Class C): a DIRECTED msg to a member
            # with no live SSE subscriber would be silently lost (the agent-4/6
            # SSE-deaf black-hole). Drop a durable inbox notice so it survives to
            # the target's next SessionStart/turn. Only for DIRECTED msgs —
            # undirected broadcasts aren't worth a durable per-member notice.
            if targeted is not None and record.get("kind") == MSG:
                try:
                    sessions_mod.post_notice(
                        target_session_id=sid,
                        text=(
                            f"📨 Undelivered DIRECTED chat message in {chat_id} "
                            f"from {record.get('sender_name') or record.get('sender_id')} "
                            f"(SSE was not connected when sent). "
                            f"Call chat_history(chat_id='{chat_id}') to read it."
                        ),
                        from_session_id="khimaira-daemon",
                    )
                except Exception:
                    pass
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

    # SSE inert-denial: if this sid is revoked (superseded beyond last-1 bound),
    # no-op the subscribe — the session must not hold a queue and receive deliveries.
    # Mirrors polled paths 10/11 inert-denial (revoked_sids check in sessions.py).
    # Uses revoked_sids explicitly to distinguish "revoked" from "un-slotted":
    # un-slotted sessions (not in registry) must still subscribe normally.
    try:
        from khimaira.monitor.sessions import _read_slot_registry as _rsr

        _rr = _rsr()
        _is_revoked_sse = any(
            session_id in entry.get("revoked_sids", []) for entry in _rr.values()
        )
    except Exception:
        _is_revoked_sse = False
    if _is_revoked_sse:
        log.info(
            "chats: SSE subscribe no-op for revoked session %s — "
            "beyond last-1 bound, must not hold a subscriber queue (inert-denial)",
            session_id[:8],
        )
        return  # ends the async generator immediately; no queue added

    queue: asyncio.Queue = asyncio.Queue(maxsize=200)
    # Phase-B Part F: key by slot when stamped so delivery follows the identity.
    _sub_key = _slot_subscriber_key(session_id)
    _subscribers.setdefault(_sub_key, set()).add(queue)
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
        any_cursor = any(
            _cursor_for(session_id, m["chat_id"]) is not None
            for m in my_chats(session_id)
            if m.get("my_state") == ACCEPTED
        )
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
                        (
                            i
                            for i, line in enumerate(lines)
                            if line.get("event_id") == cursor
                        ),
                        None,
                    )
                    start = idx + 1 if idx is not None else max(0, len(lines) - 50)
                    for line in lines[start:]:
                        if _is_role_directive(line):
                            continue  # suppress backfill replay — idempotent resync handles this
                        yield line
                elif since_event_id:
                    # No daemon-side cursor: treat Last-Event-ID as a per-chat hint.
                    # If found in this chat → replay from there. If NOT found
                    # (the prior bug: since_event_id belongs to a different chat)
                    # → deliver last 50 instead of silently skipping.
                    idx = next(
                        (
                            i
                            for i, line in enumerate(lines)
                            if line.get("event_id") == since_event_id
                        ),
                        None,
                    )
                    if idx is not None:
                        for line in lines[idx + 1 :]:
                            if _is_role_directive(line):
                                continue
                            yield line
                    else:
                        for line in lines[-50:]:
                            if _is_role_directive(line):
                                continue
                            yield line
                else:
                    # Session IS reconnecting (other chats have cursors) but this
                    # specific chat has neither cursor nor hint. Deliver last 50
                    # so missed messages aren't silently dropped.
                    for line in lines[-50:]:
                        if _is_role_directive(line):
                            continue
                        yield line
        while True:
            record = await queue.get()
            yield record
    finally:
        _subscribers[_sub_key].discard(queue)
        if not _subscribers[_sub_key]:
            _subscribers.pop(_sub_key, None)
