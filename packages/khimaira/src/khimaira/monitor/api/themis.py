"""`/api/themis` — Themis role-invariant enforcement daemon endpoints.

Endpoints:
  GET  /api/sessions/{session_id}/role      — resolve role from chat membership
  POST /api/themis/check                    — combined role-resolve + rule-check
  POST /api/themis/violations               — append to violations log
  GET  /api/themis/violations               — query violations log (read-auth D12)

Role resolution: live-queried at every call (D4). Looks up member_roles from
the most-recently-active chat where the session holds a role assignment.

Fail-open contract (D7): if the themis package is not yet installed (Phase 1
ships before agent-1's package is installed), all enforcement is bypassed.
The daemon does NOT crash — callers continue to work, violations are not recorded.
This state is visible via ImportError in the endpoint response for /check.

Read-auth (D12): caller session_id is pulled from X-Session-ID header.
Callers may read their own session_id's violations only, unless role ∈
{master, observer, critic}. Unauthorised cross-session reads return empty
list and append a warning line to ~/.claude/hooks/themis_authviolations.log.
"""

from __future__ import annotations

import importlib
import json
import logging
import time
import uuid
from pathlib import Path

from fastapi import Request
from pydantic import BaseModel

from khimaira.monitor import chats

from .._optional import require

logger = logging.getLogger(__name__)

# Violations log path (mirrors spec §Violations log)
_VIOLATIONS_PATH = (
    Path.home() / ".local" / "state" / "khimaira" / "themis_violations.jsonl"
)
# Auth-violation warning log (D12)
_AUTH_VIOLATIONS_LOG = Path.home() / ".claude" / "hooks" / "themis_authviolations.log"
# D13 fast-rollback: overrides file (append-only JSONL)
_OVERRIDES_PATH = (
    Path.home() / ".local" / "state" / "khimaira" / "themis_overrides.jsonl"
)

# Roles that may read any session's violations (D12)
_ROLES_ALLOWED_CROSS_SESSION_READ: frozenset[str] = frozenset(
    {"master", "observer", "critic"}
)

# ---------------------------------------------------------------------------
# Daemon-side per-session role cache (architect-1 must-fix #2)
#
# Role resolution requires a glob scan of all chat JSONLs.  Worst-case
# p99 was 46.7ms before caching (p50 was 7.5ms — tail-latency driven by
# IO jitter on large chat directories).  The cache eliminates the scan on
# warm paths.
#
# Invalidation: chat membership operations call invalidate_role_cache()
# directly (same process — no HTTP hop needed).  The external endpoint
# POST /api/themis/invalidate-role-cache exists for the chat-server daemon
# when it changes member_roles through a path that bypasses the monitor API.
#
# Concurrency: reads + writes are single-threaded under FastAPI's default
# async event loop.  If threading is ever introduced, wrap cache accesses
# in a threading.Lock.
# ---------------------------------------------------------------------------

# session_id -> (role: str | None, cached_at_monotonic: float)
_ROLE_CACHE: dict[str, tuple[str | None, float]] = {}
_ROLE_CACHE_TTL_S: float = 300.0  # 5-min safety TTL; invalidation is the primary signal


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def clear_role_cache() -> None:
    """Clear the entire role cache. Use when multiple sessions change roles
    simultaneously and individual session_ids are not all known (e.g., resume-master
    demotes the vice whose session_id isn't in the request)."""
    _ROLE_CACHE.clear()


def _load_disabled_rules() -> set[str]:
    """Read themis_overrides.jsonl and return the set of currently-disabled rule_ids.

    Processes entries in order: the LAST entry for each rule_id wins.
    Returns an empty set if the file is absent, empty, or unreadable.
    """
    if not _OVERRIDES_PATH.exists():
        return set()
    try:
        lines = _OVERRIDES_PATH.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()

    last_action: dict[str, str] = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        rule_id = entry.get("rule_id")
        action = entry.get("action")
        if rule_id and action in ("disable", "enable"):
            last_action[rule_id] = action

    return {rule_id for rule_id, action in last_action.items() if action == "disable"}


def invalidate_role_cache(session_id: str) -> None:
    """Remove session_id from the role cache so the next check re-scans.

    Called directly by api/chats.py after membership-changing operations
    (accept, leave, transfer, resume-master, create-room).  Also exposed
    as POST /api/themis/invalidate-role-cache for external callers.

    Fail-safe: silently no-ops if session_id is absent.
    """
    _ROLE_CACHE.pop(session_id, None)


_UNRESOLVABLE = "__unresolvable__"


def _resolve_role_from_jsonl(sid: str) -> str | None:
    """Scan chat JSONLs and return the most-recently-active role for sid.

    Resolution order per chat (4-layer):
      1. member_roles[sid] — authoritative; wins when present.
      2. created_by master fallback — when member_roles dict is absent entirely
         (v1-era chat) and sid is the room creator, resolves to "master".
         Mirrors chats._is_master's v1 fallback; real signal, not inference.
      3. registry-validated name inference — rsplit trailing -<N>, validate
         remainder against themis.data.VALID_ROLES. Resolves role-named sessions
         (e.g. jp-frontend-lead-1) in chats that pre-date member_roles.
      4. fail-closed backstop — ONLY for chats where member_roles IS present.
         Known member with unresolvable role → "_unresolvable__" sentinel →
         BLOCK+loud in _call_engine. Chats without member_roles get fail-open
         for unresolved sessions (prerequisite: backfill writes member_roles).

    Returns None when sid is not an accepted member of any chat.
    Returns _UNRESOLVABLE when sid IS a member but role can't be resolved in
    a chat that already has explicit member_roles (fail-closed backstop fired).
    """
    chat_dir = chats._chat_dir()
    if not chat_dir.exists():
        return None

    candidates: list[tuple[str, str]] = []  # (last_ts, role)
    unresolvable_member = False
    for path in chat_dir.glob("chat-*.jsonl"):
        chat_id = path.stem
        try:
            room = chats.load_room(chat_id)
        except Exception:
            # ValueError (no room), json.JSONDecodeError, OSError on malformed JSONL
            continue
        member = room["members"].get(sid)
        if not member or member["state"] != chats.ACCEPTED:
            continue
        # Layer 1: explicit member_roles (authoritative)
        member_roles_dict = room["meta"].get("member_roles")
        member_roles = member_roles_dict or {}
        role = member_roles.get(sid)
        if role is None:
            # Layer 2: created_by master fallback.
            # Original: delegated to _is_master() which short-circuits to
            # member_roles[sid] == ROLE_MASTER — returns False when member_roles
            # dict exists but doesn't contain the creator (e.g. after a bootstrap
            # that wrote member_roles for all invited members but skipped the creator).
            # Fix: check created_by directly regardless of member_roles state. The
            # creator is always master until dethroned via chat_grant_role(role=master)
            # which atomically writes the new master AND demotes the old one.
            if room["meta"].get("created_by") == sid:
                role = chats.ROLE_MASTER
        if role is None:
            # Layer 3: registry-validated name inference
            session_name = member.get("session_name", "")
            role = chats.infer_role_from_name(session_name)
        if role is None:
            # Layer 4: fail-closed gated on member_roles present for this chat.
            # Chats without member_roles have not had roles recorded yet — fail-open
            # there until backfill writes member_roles (at which point future calls
            # resolve via Layer 1, and any genuinely-unresolvable session hits this).
            if member_roles_dict:
                unresolvable_member = True
            continue
        last_ts = room["messages"][-1]["ts"] if room["messages"] else ""
        candidates.append((last_ts, role))

    if not candidates:
        if unresolvable_member:
            return _UNRESOLVABLE
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    return candidates[0][1]


def resolve_session_role(session_id: str) -> str | None:
    """Return the most-recently-active chat role for session_id, or None.

    Cache-first: checks _ROLE_CACHE before doing the JSONL glob scan.
    Cache miss or expired entry triggers a full scan and writes the result
    back to cache.

    Implements D4: role is always current within the cache TTL (300s) or
    immediately after invalidation by a membership-changing operation.

    Returns None when:
    - session has no accepted chat memberships with a role assignment
    - session_id is a non-UUID name that can't be resolved
    """
    try:
        sid = chats._resolve_or_uuid(session_id)
    except (ValueError, Exception):
        return None

    # Cache read — skip JSONL scan on warm path
    cached = _ROLE_CACHE.get(sid)
    if cached is not None:
        role, cached_at = cached
        if (time.monotonic() - cached_at) < _ROLE_CACHE_TTL_S:
            return role

    # Cache miss or expired — scan JSONLs
    role = _resolve_role_from_jsonl(sid)

    # Cache write (None is a valid cached value: "no role found")
    _ROLE_CACHE[sid] = (role, time.monotonic())
    return role


def _call_engine(
    role: str | None,
    tool_name: str,
    tool_input: dict,
    cwd: str,
) -> dict:
    """Call themis.engine.evaluate. Fail-open if themis not installed yet (D7).

    Agent-1 interface: evaluate(role, tool_name, tool_input, conditions_payload?, rule_set?)
    → EvalResult with .ok, .violation (optional ViolationDetail with .rule_id, .name,
      .message, .severity).
    """
    if role is None:
        # Not a member of any chat → passthrough, no rules apply
        return {"ok": True}
    if role == _UNRESOLVABLE:
        # Known member in a chat with explicit member_roles, but role cannot be
        # resolved via member_roles, created_by, or name-inference. Fail-closed
        # (S4 backstop): block until role is explicitly granted via chat_grant_role.
        logger.warning(
            "themis: known chat member with unresolvable role — blocking (S4 fail-closed)"
        )
        return {
            "ok": False,
            "violation": {
                "rule_id": "IN-UNRESOLVABLE",
                "name": "UNRESOLVABLE_ROLE",
                "message": (
                    "🛑 Themis: this session is a known chat member but its role cannot be "
                    "resolved (not in member_roles, not the chat creator, and session name "
                    "is not role-inferable). Tool blocked until role is explicitly granted "
                    "via chat_grant_role."
                ),
                "severity": "block",
            },
        }
    try:
        engine = importlib.import_module("themis.engine")
        result = engine.evaluate(role, tool_name, tool_input)
        # EvalResult is a dataclass/namedtuple — serialize to dict for the response
        if hasattr(result, "__dict__"):
            out: dict = {"ok": result.ok}
            if result.violation is not None:
                v = result.violation
                # D13: check overrides — if this rule is disabled, treat as audit (allow)
                disabled = _load_disabled_rules()
                if v.rule_id in disabled:
                    logger.debug("themis: rule %s disabled via override — allowing", v.rule_id)
                    return {"ok": True, "_rule_disabled": v.rule_id}
                out["violation"] = {
                    "rule_id": v.rule_id,
                    "name": v.name,
                    "message": v.message,
                    "severity": v.severity,
                }
            return out
        # Already a dict (future-proof)
        return dict(result)  # type: ignore[arg-type]
    except ImportError:
        # Phase 1: themis package not yet installed — fail-open (D7)
        return {"ok": True}
    except FileNotFoundError:
        # Missing yaml file for this role = no rules defined yet = allow-all.
        # A new role with no yaml file has no constraints; fail-open so a missing
        # placeholder doesn't hard-block an entire session (observed: frontend-lead
        # blocked until frontend-lead.yaml was created, commit 4f6d097).
        logger.debug("themis: no rule file for role %r — fail-open (no rules defined)", role)
        return {"ok": True}
    except Exception as exc:
        # Engine runtime error (bad YAML, rule load failure, evaluate exception).
        # Role resolved and yaml exists but rules could not be evaluated → fail-CLOSED.
        # A broken rule file is a real enforcement failure that must not become a
        # silent allow-through; operator must fix the rule file to unblock.
        logger.warning("themis engine error (fail-closed): role=%s exc=%s", role, exc)
        return {
            "ok": False,
            "violation": {
                "rule_id": "IN-ENGINE-ERROR",
                "name": "RULES_LOAD_FAILURE",
                "message": (
                    f"🛑 Themis: role {role!r} resolved but rules could not be evaluated "
                    f"({type(exc).__name__}: {exc}). Tool blocked until rules are fixed."
                ),
                "severity": "block",
            },
        }


def _violations_record(record: dict) -> dict:
    """Append a violation to the log. Falls back to direct JSONL write if
    themis.violations is not installed yet.

    Agent-1 interface: append_violation(record: ViolationRecord, path?)
    ViolationRecord.from_dict() constructs it from a raw dict.
    Returns {logged: True, id: str}.
    """
    try:
        violations = importlib.import_module("themis.violations")
        data_mod = importlib.import_module("themis.data")
        # Ensure ts is present — the hook may omit it for brevity
        if "ts" not in record:
            from datetime import datetime, timezone

            record = {**record, "ts": datetime.now(timezone.utc).isoformat()}
        vr = data_mod.ViolationRecord.from_dict(record)
        violations.append_violation(vr)
        return {"logged": True, "id": record.get("tool_use_id", uuid.uuid4().hex[:8])}
    except ImportError:
        _VIOLATIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
        entry_id = uuid.uuid4().hex[:8]
        entry = {
            **record,
            "id": entry_id,
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        }
        with _VIOLATIONS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry) + "\n")
        return {"logged": True, "id": entry_id}
    except Exception as exc:
        logger.warning("themis violations record error (fail-open): %s", exc)
        return {"logged": False, "error": str(exc)}


def _violations_query(
    session_id: str | None = None,
    role: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Query violations log. Falls back to direct JSONL read if themis not installed.

    Agent-1 interface: read_violations(session_id?, role?, since?, limit?, path?) → list[dict]
    """
    try:
        violations = importlib.import_module("themis.violations")
        return violations.read_violations(
            session_id=session_id, role=role, since=since, limit=limit
        )
    except ImportError:
        # Direct JSONL read fallback when themis not installed
        if not _VIOLATIONS_PATH.exists():
            return []
        results: list[dict] = []
        try:
            lines = _VIOLATIONS_PATH.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if session_id and entry.get("session_id") != session_id:
                continue
            if role and entry.get("role") != role:
                continue
            if since and entry.get("ts", "") < since:
                continue
            results.append(entry)
        return results[-limit:]


def _log_auth_violation(
    caller_session_id: str,
    caller_role: str | None,
    target_session_id: str,
) -> None:
    """Append a warning line when a caller reads another session's violations without auth."""
    _AUTH_VIOLATIONS_LOG.parent.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    line = (
        f"[{ts}] AUTH_VIOLATION caller={caller_session_id!r} "
        f"role={caller_role!r} attempted to read session={target_session_id!r}\n"
    )
    try:
        with _AUTH_VIOLATIONS_LOG.open("a", encoding="utf-8") as f:
            f.write(line)
    except OSError as e:
        logger.warning("themis: could not write auth violation log: %s", e)


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------


class CheckReq(BaseModel):
    session_id: str
    tool_name: str
    tool_input: dict = {}
    cwd: str = ""


class InvalidateCacheReq(BaseModel):
    session_id: str


class ViolationRecordReq(BaseModel):
    record: dict


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_router():
    fastapi = require("fastapi")

    router = fastapi.APIRouter()

    @router.get("/sessions/{session_id}/role")
    async def get_session_role(session_id: str) -> dict:
        """Resolve the role for a session from its most-recently-active chat.

        Returns {role: "master"|"agent"|...|null}. Role is live-queried
        from chat membership (D4) — not cached. Returns null when the session
        has no role assignment in any accepted chat.

        404 when session_id is a name that can't be resolved to any known session.
        """
        # Validate that the session_id is resolvable (not just any UUID string)
        # For UUID format inputs: _resolve_or_uuid passes through verbatim.
        # For name inputs: it raises ValueError if unknown.
        # To give useful 404s on clearly-unknown names, we attempt resolution.
        import re

        _UUID_RE = re.compile(
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
        )
        if not _UUID_RE.match(session_id):
            try:
                from khimaira.monitor import sessions as sessions_mod

                sessions_mod.resolve_session_id(session_id)
            except ValueError as e:
                raise fastapi.HTTPException(404, str(e))

        role = resolve_session_role(session_id)
        return {"session_id": session_id, "role": role}

    @router.post("/themis/check")
    async def themis_check(req: CheckReq) -> dict:
        """Combined role-resolve + rule-check. One HTTP hop per hook fire (D3).

        Body: {session_id, tool_name, tool_input, cwd?}
        Returns: {ok: bool, violation?: {rule_id, name, message, severity}, role: str|null}

        Fail-open (D7): if themis engine not installed, returns ok=true.
        No role assignment → returns ok=true (null role passthrough).
        """
        role = resolve_session_role(req.session_id)
        result = _call_engine(role, req.tool_name, req.tool_input, req.cwd)
        # Normalize internal sentinel to null in the public response role field;
        # the BLOCK verdict already conveys the fail-closed decision.
        response_role = None if role == _UNRESOLVABLE else role
        return {**result, "role": response_role}

    @router.post("/themis/violations")
    async def record_violation(req: ViolationRecordReq) -> dict:
        """Append a violation record to the violations log.

        Body: {record: {...}} matching the violations log schema.
        Returns: {logged: true, id}
        """
        return _violations_record(req.record)

    @router.get("/themis/violations")
    async def query_violations(
        request: Request,
        session_id: str | None = None,
        role: str | None = None,
        since: str | None = None,
        limit: int = 50,
    ) -> dict:
        """Query the violations log with read-auth enforcement (D12).

        Read auth:
        - Caller may read its own session_id's violations.
        - Caller may read any session's violations if role ∈ {master, observer, critic}.
        - Any other cross-session read → empty list + auth violation warning logged.

        Caller session_id is resolved from X-Session-ID request header.
        """
        caller_session_id = request.headers.get("x-session-id", "")
        caller_role = resolve_session_role(caller_session_id) if caller_session_id else None

        # Enforce read-auth when a cross-session query is requested
        if session_id and session_id != caller_session_id:
            if caller_role not in _ROLES_ALLOWED_CROSS_SESSION_READ:
                _log_auth_violation(caller_session_id, caller_role, session_id)
                return {"violations": []}

        records = _violations_query(
            session_id=session_id,
            role=role,
            since=since,
            limit=limit,
        )
        return {"violations": records}

    @router.post("/themis/invalidate-role-cache")
    async def invalidate_role_cache_endpoint(req: InvalidateCacheReq) -> dict:
        """Remove a session's cached role so the next check re-scans.

        Called by chat-server when membership changes (accept, leave,
        transfer-membership, resume-master). Also callable externally for
        debugging / manual cache busting.
        """
        invalidate_role_cache(req.session_id)
        return {"invalidated": True, "session_id": req.session_id}

    return router
