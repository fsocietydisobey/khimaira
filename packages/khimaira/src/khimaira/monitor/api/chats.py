"""`/api/chats` — REST + SSE for cross-session chat.

Endpoints:
  POST   /api/chats                              — create_room
  POST   /api/chats/{chat_id}/invite             — invite member
  POST   /api/chats/{chat_id}/accept             — accept invite
  POST   /api/chats/{chat_id}/messages           — send a message
  GET    /api/chats/{chat_id}/messages           — paginated history
  GET    /api/chats/{chat_id}                    — room metadata + members + history
  POST   /api/chats/{chat_id}/leave              — leave
  DELETE /api/chats/{chat_id}                    — archive (creator only)
  GET    /api/chats?session_id=…                 — my chats
  GET    /api/chats/events?session_id=…          — SSE event stream
                                                   (Last-Event-ID header → backfill from JSONL)
"""

from __future__ import annotations

import asyncio
import json
import os
import time

from fastapi import Request
from pydantic import BaseModel

_PENDING_POLL_INTERVAL = 5.0  # seconds between polls when a `to` recipient is pending
_PENDING_WAIT_DEADLINE = 30.0  # total seconds to wait for recipient to accept

# Expected-reply registry — tracks targeted sends that expect a reply back.
# Key: (to_id, from_id) meaning "to_id owes a reply to from_id".
# Value: {"ts": wall_clock_time, "from": from_id, "to": to_id,
#          "chat_id": str, "threshold_s": float}
_EXPECTED_REPLIES: dict[tuple[str, str], dict] = {}
_REPLY_OVERDUE_DEFAULT_S = 90.0  # default threshold before a missing reply is diagnosed
_REPLY_WATCH_INTERVAL = 30.0  # seconds between watcher scans
_REGISTRY_LOCK = asyncio.Lock()

# Per-role thresholds — known-long-work roles get more time before presumed-dead.
_REPLY_OVERDUE_BY_ROLE: dict[str, float] = {
    "architect": 180.0,
    "analyst": 180.0,
    "verifier": 300.0,
    "critic": 120.0,
    # Domain leads do substantive research; 90s default is too tight.
    "backend-lead": 180.0,
    "data-lead": 180.0,
    "jp-backend-lead": 180.0,
    "jp-data-lead": 180.0,
    "jp-frontend-lead": 180.0,
}
_LIVENESS_WINDOW_S = 60.0  # activity within this window = session is alive

_RECENTLY_PRESUMED_DEAD: dict[tuple[str, str], dict] = {}
_PRESUMED_DEAD_TTL_S = 300.0  # 5 minutes for late-reply supersede


def _threshold_for_session(to_id: str, chat_id: str) -> float:
    """Per-role threshold lookup.

    Lookup order:
    1. chat.meta.member_roles[to_id] — canonical (set by chat_create_room v1.9.6+)
    2. infer_role_from_name(session_name) — fallback for chats created before
       member_roles was a chat param, or sessions added without role binding
    3. _REPLY_OVERDUE_DEFAULT_S (90s) — last resort
    """
    try:
        room = chats.load_room(chat_id)
        role = (room.get("meta", {}).get("member_roles") or {}).get(to_id)
        if role and role in _REPLY_OVERDUE_BY_ROLE:
            return _REPLY_OVERDUE_BY_ROLE[role]

        # Fallback: infer role from session name. Scan each dash-segment left-to-right
        # so both "architect-1" and prefixed forms like "jp-architect-1" match.
        from khimaira.monitor import sessions as sessions_mod

        session_state = sessions_mod.state(to_id)
        name = (session_state.get("status") or {}).get("name") or ""
        for segment in name.split("-"):
            if segment in _REPLY_OVERDUE_BY_ROLE:
                return _REPLY_OVERDUE_BY_ROLE[segment]
    except Exception:
        pass
    return _REPLY_OVERDUE_DEFAULT_S


def _session_active_within(session_id: str, window_s: float) -> bool:
    """Liveness check: True if session logged any tool_call or file_touch
    within the last window_s seconds.
    Fail-open: any error returns False (treat as silent — conservative).
    """
    try:
        from datetime import datetime
        from khimaira.monitor import sessions as sessions_mod

        def _ts_float(iso_str: str) -> float:
            return datetime.fromisoformat(iso_str).timestamp()

        now = time.time()
        recent = sessions_mod.recent_tool_calls(session_id, limit=1)
        if (
            recent
            and (now - _ts_float(recent[0].get("ts", "1970-01-01T00:00:00+00:00")))
            < window_s
        ):
            return True
        touches = (
            sessions_mod.recent_touches(session_id, limit=1)
            if hasattr(sessions_mod, "recent_touches")
            else []
        )
        if (
            touches
            and (now - _ts_float(touches[0].get("ts", "1970-01-01T00:00:00+00:00")))
            < window_s
        ):
            return True
    except Exception:
        return False
    return False


def _resolve_master_session_id(chat_id: str) -> str | None:
    """Find the session_id with role=master in this chat. None if not found."""
    try:
        room = chats.load_room(chat_id)
        member_roles = room.get("meta", {}).get("member_roles") or {}
        for sid, role in member_roles.items():
            if role == chats.ROLE_MASTER:
                return sid
    except Exception:
        pass
    return None


async def _send_diagnostic_probe(
    chat_id: str,
    to_id: str,
    from_id: str,
    elapsed_s: float,
) -> bool:
    """Send a synthetic ping via daemon-internal primitive.

    Returns True on success; False on error (fail-open — caller proceeds
    to presumed-dead path on next tick regardless).
    """
    if os.environ.get("KHIMAIRA_QUIET") == "1":
        return False  # quiet mode — suppress diagnostic liveness pings
    body = (
        f"🩺 ping from diagnostic — `{from_id}` has been waiting since "
        f"T+{elapsed_s:.0f}s. Please broadcast any message to confirm you're alive."
    )
    try:
        from khimaira.monitor.chats import _post_synthetic_message

        result = await _post_synthetic_message(chat_id, body)
        return result is not None
    except Exception:
        return False


def _sweep_presumed_dead(now: float) -> None:
    """Drop _RECENTLY_PRESUMED_DEAD entries older than TTL.
    Called opportunistically from _check_overdue_once.
    """
    expired = [
        key
        for key, entry in _RECENTLY_PRESUMED_DEAD.items()
        if now - entry["notice_ts"] > _PRESUMED_DEAD_TTL_S
    ]
    for key in expired:
        _RECENTLY_PRESUMED_DEAD.pop(key, None)


async def _maybe_supersede_presumed_dead(sender_id: str) -> None:
    """If sender was recently presumed-dead, fire supersede notice to master.

    Called from _resolve_expected_reply and broadcast-resolve paths when a
    session sends ANY message. Clears matching entries from _RECENTLY_PRESUMED_DEAD.
    """
    matched_keys = [
        key
        for key in list(_RECENTLY_PRESUMED_DEAD.keys())
        if key[0] == sender_id  # to_id (the previously-silent session) == sender now
    ]
    for key in matched_keys:
        entry = _RECENTLY_PRESUMED_DEAD.pop(key, None)
        if entry is None:
            continue
        master_sid = _resolve_master_session_id(entry.get("chat_id", ""))
        if master_sid is None:
            continue
        elapsed_since_notice = time.time() - entry["notice_ts"]
        body = (
            f"♻️ SUPERSEDE: session `{sender_id}` replied {elapsed_since_notice:.0f}s "
            f"after presumed-dead notice fired. Original wait was "
            f"{entry['elapsed_s']:.0f}s on chat_send_to from `{entry['from_id']}`. "
            f"`{sender_id}` is alive after all — original notice was a false-positive. "
            f"Recommend retracting any re-dispatch decision based on the prior notice."
        )
        try:
            from khimaira.monitor import sessions as sessions_mod

            sessions_mod.post_notice(
                target_session_id=master_sid,
                text=body,
                from_session_id="khimaira-daemon",
            )
        except Exception:
            pass


async def _register_expected_reply(
    from_id: str, to_ids: list[str], chat_id: str = ""
) -> None:
    async with _REGISTRY_LOCK:
        ts = time.time()
        for to_id in to_ids:
            if to_id == from_id:
                continue
            threshold = _threshold_for_session(to_id, chat_id)
            _EXPECTED_REPLIES[(to_id, from_id)] = {
                "ts": ts,
                "from": from_id,
                "to": to_id,
                "chat_id": chat_id,
                "threshold_s": threshold,
            }


async def _resolve_expected_reply(
    from_id: str, to_ids: list[str], chat_id: str = ""
) -> None:
    async with _REGISTRY_LOCK:
        for to_id in to_ids:
            _EXPECTED_REPLIES.pop((from_id, to_id), None)
    # Supersede check: from_id just sent — if it was previously presumed-dead,
    # master gets a retraction notice.
    await _maybe_supersede_presumed_dead(from_id)


async def _diagnose_and_dispose(key: tuple, entry: dict, ts_now: float) -> None:
    """Diagnostic phase: liveness check → probe → presumed-dead.

    Phase 1 (liveness): if X has recent tool/touch activity, reschedule.
    Phase 2 (probe): if no activity AND no probe sent yet, send probe + reschedule.
    Phase 3 (presumed-dead): if probe already sent and X still silent, fire master notice.
    """
    to_id = entry["to"]
    from_id = entry["from"]
    chat_id = entry.get("chat_id", "")
    elapsed_s = ts_now - entry["ts"]
    threshold = entry.get("threshold_s", _REPLY_OVERDUE_DEFAULT_S)

    # Phase 1: liveness check
    if _session_active_within(to_id, _LIVENESS_WINDOW_S):
        async with _REGISTRY_LOCK:
            existing = _EXPECTED_REPLIES.get(key)
            if existing is not None:
                existing["ts"] = ts_now
                existing["probe_sent_at"] = None  # reset probe state on reschedule
        return

    # Phase 2: probe (only if not yet probed)
    probe_sent_at = entry.get("probe_sent_at")
    if probe_sent_at is None:
        await _send_diagnostic_probe(chat_id, to_id, from_id, elapsed_s)
        async with _REGISTRY_LOCK:
            existing = _EXPECTED_REPLIES.get(key)
            if existing is not None:
                existing["probe_sent_at"] = ts_now
        # Don't fire presumed-dead notice yet — wait one tick for probe response
        return

    # Phase 3: presumed dead (probe was sent on prior tick, X still silent)
    async with _REGISTRY_LOCK:
        _EXPECTED_REPLIES.pop(key, None)

    master_sid = _resolve_master_session_id(chat_id)
    if master_sid is None:
        return

    # Record for late-reply supersede tracking
    _RECENTLY_PRESUMED_DEAD[(to_id, from_id)] = {
        "notice_ts": ts_now,
        "chat_id": chat_id,
        "from_id": from_id,
        "to_id": to_id,
        "elapsed_s": elapsed_s,
    }

    probe_age_s = ts_now - probe_sent_at
    body = (
        f"🚨 PRESUMED-DEAD SESSION — `{to_id}` has not responded to "
        f"chat_send_to from `{from_id}` after {elapsed_s:.0f}s "
        f"(role-threshold: {threshold:.0f}s). Diagnostic probe sent "
        f"{probe_age_s:.0f}s ago also received no reply. session_state shows "
        f"no tool activity in the last {_LIVENESS_WINDOW_S:.0f}s. "
        f"Decide: re-dispatch / retry / investigate. chat_id={chat_id}"
    )
    try:
        from khimaira.monitor import sessions as sessions_mod

        sessions_mod.post_notice(
            target_session_id=master_sid,
            text=body,
            from_session_id="khimaira-daemon",
        )
    except Exception:
        pass


async def _check_overdue_once() -> None:
    now = time.time()
    _sweep_presumed_dead(now)  # opportunistic cleanup of late-reply tracking
    candidates = []
    async with _REGISTRY_LOCK:
        for key, entry in list(_EXPECTED_REPLIES.items()):
            threshold = entry.get("threshold_s", _REPLY_OVERDUE_DEFAULT_S)
            if now - entry["ts"] > threshold:
                candidates.append((key, entry))
    for key, entry in candidates:
        await _diagnose_and_dispose(key, entry, now)


async def _overdue_watcher() -> None:
    import logging

    _log = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(_REPLY_WATCH_INTERVAL)
        try:
            await _check_overdue_once()
        except Exception as exc:
            _log.warning("overdue-reply watcher error: %s", exc)


# ---------------------------------------------------------------------------
# Guard-4 + #13b-light — escalate-on-stall + throttle grace window
# ---------------------------------------------------------------------------

_GUARD4_WATCH_INTERVAL = 60.0  # seconds between scans
_GUARD4_STALLED: dict[tuple[str, str], float] = (
    {}
)  # (session_id, obligation_id) → escalation_ts
_GUARD4_STALLED_TTL_S = 3600.0  # clear debounce entries after 1h
_GUARD4_MIN_SILENCE_S = 120.0  # ignore sessions silent < 2min (normal idle)


def _compute_throttle_ceiling_s() -> float:
    """Compute the generous upper bound for CC's retry+request horizon.

    Reads KHIMAIRA_THROTTLE_GRACE_S for an override, otherwise derives from
    CLAUDE_CODE_MAX_RETRIES (default 10) using a conservative exp-backoff
    ceiling, compared with API_TIMEOUT_MS (default 600000).

    Defaults generous: over-grace only delays crash-detection marginally
    (with /proc-liveness, dead processes escalate immediately regardless);
    under-grace re-introduces the false-kill we're fixing.
    """
    import os

    grace_override = os.environ.get("KHIMAIRA_THROTTLE_GRACE_S")
    if grace_override:
        return float(grace_override)
    max_retries = int(os.environ.get("CLAUDE_CODE_MAX_RETRIES", "10"))
    api_timeout_ms = int(os.environ.get("API_TIMEOUT_MS", "600000"))
    api_timeout_s = api_timeout_ms / 1000.0
    # Conservative exp-backoff ceiling: sum 2^i * base_s, capped per-retry
    base_s = 1.0
    cap_s = 60.0
    backoff_ceiling = sum(min(base_s * (2**i), cap_s) for i in range(max_retries))
    # Add 50% jitter margin; take max with single-request timeout
    return max(backoff_ceiling * 1.5, api_timeout_s)


def _is_process_alive_for_session(session_id: str) -> bool | None:
    """Check if the Claude Code process for this session is alive via /proc.

    Returns True (alive), False (dead), or None (unknown — ppid not registered
    or /proc not available on this platform).
    """
    import pathlib
    import os

    try:
        ppid = chats.get_session_ppid(session_id)
        if ppid is None:
            return None
        # Prefer psutil if available (cross-platform)
        try:
            import psutil

            return psutil.pid_exists(ppid)
        except ImportError:
            pass
        # Fall back to /proc (Linux only)
        proc_path = pathlib.Path(f"/proc/{ppid}")
        return proc_path.exists()
    except Exception:
        return None


def _was_begin_fired(chat_id: str, task_id: str) -> bool:
    """Return True if a TASK_SIGNAL start event exists for this task (BEGIN was fired).

    Used by Guard-4 2D pending-gate: a pending task with no BEGIN is compliant-waiting;
    a pending task WITH BEGIN is begun-but-not-started (potentially wedged).
    Fail-open: returns False on any read error (pending→no-escalate is the safer default).
    """
    try:
        for line in chats._read(chat_id):
            if (
                line.get("kind") == chats.TASK_SIGNAL
                and line.get("task_id") == task_id
                and line.get("signal") == "start"
            ):
                return True
    except Exception:
        pass
    return False


def session_has_task_obligation(
    task: dict,
    current_task_status: str,
    resolved_session_id: str,
    gate_verdict_satisfied: bool | None = None,
) -> bool:
    """Canonical obligation-membership predicate — shared by _get_session_obligations,
    Guard-4, Guard-5, and the watcher wake-gate (_session_has_pending_task).

    Returns True when resolved_session_id owes this task as an obligation:
    - Named assignee: task's assignee_id == resolved_session_id
    - Role-class assignee: task's assignee_role == session's resolved role
    Task must be open (pending or in_progress).

    gate_verdict_satisfied: for review-tasks (gate_for set):
      - None: no gate check (caller lacks verdict context; use for simple wake checks)
      - True: gate already satisfied → NOT an obligation
      - False: gate not yet satisfied → IS an obligation

    DESIGN: session_role is resolved INTERNALLY via resolve_session_role to ensure
    both named and role-class callers get the same resolution path. Avoids the
    divergence class where different callers compute role differently and the
    "shared" predicate silently disagrees.
    """
    open_statuses = {chats.TASK_PENDING, chats.TASK_IN_PROGRESS}
    if current_task_status not in open_statuses:
        return False

    # Gate-verdict check is part of the OBLIGATION DEFINITION for review-tasks.
    # A review-task whose verdict is already satisfied is NOT an obligation —
    # review-tasks are not auto-closed on verdict posting (audit-grade: record_gate_verdict
    # appends TASK_VERDICT only, never calls update_task_status).
    # When gate_verdict_satisfied is None (caller lacks verdict context) AND this is
    # a review-task (gate_for set), conservatively return False — let _get_session_obligations
    # (which computes gate_verdict_satisfied inline) handle it. This avoids false-wakes.
    is_review_task = bool(task.get("gate_for"))
    if is_review_task:
        if gate_verdict_satisfied is None:
            return False  # caller must provide verdict state for review-tasks
        if gate_verdict_satisfied is True:
            return False  # already satisfied — not an obligation

    # Named-assignee check
    if task.get("assignee_id") == resolved_session_id:
        return True

    # Role-class check — resolve role internally so both callers get the same result
    task_assignee_role = task.get("assignee_role")
    if task_assignee_role:
        try:
            from .themis import resolve_session_role

            session_role = resolve_session_role(resolved_session_id)
            if session_role == task_assignee_role:
                return True
        except Exception:
            pass  # fail-open: if role can't be resolved, skip role-class match

    return False


def _find_chat_master(chat_id: str) -> str | None:
    """Return the master session_id for a chat, or None if not found."""
    try:
        room = chats.load_room(chat_id)
        member_roles = (room.get("meta") or {}).get("member_roles") or {}
        for sid, role in member_roles.items():
            if role == chats.ROLE_MASTER:
                return sid
        # v1 fallback: created_by
        return (room.get("meta") or {}).get("created_by")
    except Exception:
        return None


def _auto_create_review_tasks(
    chat_id: str,
    work_task_id: str,
    by_session_id: str,
) -> None:
    """Guard-5 Part A: AUTO-create role-class review-tasks when a gate_required task → done.

    Creates one review-task per verdict_role (critic + verifier) as obligation-wrappers.
    Each review-task carries gate_for=work_task_id and verdict_role so that:
    - _get_session_obligations picks them up for role-holders
    - Guard-4/#14/roster_recovery gain gate-stall visibility for free

    The review-task obligation CLEARS when get_gate_verdicts(work_task_id) shows that
    role's verdict present — the existing B3 verdict machinery; no duplication.

    Fail-open: if the work-task doesn't have gate_required=True, or if any task already
    exists for that verdict_role+gate_for pair, skips silently.
    """
    try:
        # Check if the work-task has gate_required=True
        work_task: dict | None = None
        for line in chats._read(chat_id):
            if line.get("kind") == chats.TASK and line.get("id") == work_task_id:
                work_task = line
                break
        if work_task is None or not work_task.get("gate_required"):
            return

        # Check which OPEN review-tasks already exist for this work-task.
        # Dedup only on open tasks (pending/in_progress) — a satisfied/closed
        # review-task should allow a fresh review obligation on re-done.
        # Fold status updates to find the current status per task.
        review_task_statuses: dict[str, str] = {}  # task_id → current status
        review_task_roles: dict[str, str] = {}  # task_id → verdict_role
        for line in chats._read(chat_id):
            k = line.get("kind")
            if k == chats.TASK and line.get("gate_for") == work_task_id:
                tid = line.get("id")
                vr = line.get("verdict_role")
                if tid and vr:
                    review_task_statuses[tid] = line.get("status", chats.TASK_PENDING)
                    review_task_roles[tid] = vr
            elif k == chats.TASK_UPDATE:
                tid = line.get("task_id")
                if tid in review_task_statuses:
                    review_task_statuses[tid] = line.get(
                        "status", review_task_statuses[tid]
                    )

        open_statuses = {chats.TASK_PENDING, chats.TASK_IN_PROGRESS}
        existing_verdict_roles: set[str] = {
            review_task_roles[tid]
            for tid, status in review_task_statuses.items()
            if status in open_statuses
        }

        # Find the master to send review-task creation as (create_task requires master)
        master_sid = _find_chat_master(chat_id)
        if not master_sid:
            return  # can't create review-tasks without a master

        # Create review-tasks for critic and verifier if not already present
        for verdict_role in (chats.ROLE_CRITIC, chats.ROLE_VERIFIER):
            if verdict_role in existing_verdict_roles:
                continue
            body = (
                f"🔍 Review gate for task {work_task_id} — "
                f"{verdict_role} verdict required.\n\n"
                f"Original task: {(work_task.get('body') or '')[:200]}"
            )
            try:
                chats.create_task(
                    chat_id,
                    master_sid,
                    body,
                    assignee_role=verdict_role,
                    gate_for=work_task_id,
                    verdict_role=verdict_role,
                )
            except Exception:
                pass  # fail-open per review-task
    except Exception:
        pass  # fail-open: AUTO-create is best-effort


def _get_session_obligations(session_id: str) -> list[dict]:
    """Find tasks where session_id is assignee with status pending or in_progress.

    Scans all chat JSONL files. Returns list of {task_id, chat_id, status, begin_fired}.
    Guard-5 Part A: also includes open review-tasks where assignee_role matches the
    session's current role (role-class obligations). A review-task is OPEN until
    get_gate_verdicts(gate_for) shows that verdict_role's verdict is present.
    Fail-open: errors scanning a chat are silently skipped.
    """
    obligations: list[dict] = []
    # Guard-5: resolve the session's current role for role-class obligation matching.
    # Fail-open: if role can't be resolved, only named-assignee obligations apply.
    session_role: str | None = None
    try:
        from .themis import resolve_session_role

        session_role = resolve_session_role(session_id)
    except Exception:
        pass

    try:
        chat_dir = chats._chat_dir()
        if not chat_dir.exists():
            return []
        for chat_path in chat_dir.glob("chat-*.jsonl"):
            chat_id = chat_path.stem
            try:
                # Fold task state from the JSONL without calling load_room first
                # (avoids member validation for a read-only scan).
                tasks: dict[str, dict] = {}
                verdicts: dict[str, dict] = (
                    {}
                )  # task_id → {critic_approved, verifier_shipped}
                for line in chats._read(chat_id):
                    k = line.get("kind")
                    if k == chats.TASK:
                        tid = line.get("id")
                        if tid:
                            tasks[tid] = {
                                "task_id": tid,
                                "assignee_id": line.get("assignee_id"),
                                "assignee_role": line.get("assignee_role"),
                                "gate_for": line.get("gate_for"),
                                "verdict_role": line.get("verdict_role"),
                                "status": line.get("status"),
                                "begin_fired": False,
                            }
                    elif k == chats.TASK_UPDATE:
                        tid = line.get("task_id")
                        if tid and tid in tasks:
                            tasks[tid]["status"] = line.get("status")
                    elif k == chats.TASK_SIGNAL and line.get("signal") == "start":
                        tid = line.get("task_id")
                        if tid and tid in tasks:
                            tasks[tid]["begin_fired"] = True
                    elif k == chats.TASK_VERDICT:
                        # Track verdicts for review-task open-check
                        ref_tid = line.get("task_id")
                        if ref_tid:
                            v = verdicts.setdefault(
                                ref_tid,
                                {
                                    "critic_approved": False,
                                    "verifier_shipped": False,
                                },
                            )
                            verdict_val = line.get("verdict", "")
                            if verdict_val == "approve":
                                v["critic_approved"] = True
                            elif verdict_val == "changes":
                                v["critic_approved"] = False
                            elif verdict_val == "ship":
                                v["verifier_shipped"] = True
                            elif verdict_val == "hold":
                                v["verifier_shipped"] = False

                for task in tasks.values():
                    status = task.get("status")
                    if status not in (chats.TASK_PENDING, chats.TASK_IN_PROGRESS):
                        continue

                    # Named-assignee obligation (existing behavior)
                    if task.get("assignee_id") == session_id:
                        obligations.append(
                            {
                                "task_id": task["task_id"],
                                "chat_id": chat_id,
                                "status": status,
                                "begin_fired": task.get("begin_fired", False),
                            }
                        )
                        continue

                    # Guard-5 Part A: role-class review-task obligation.
                    # A session with role R has an obligation for any open review-task
                    # whose assignee_role==R AND whose gate verdict isn't yet satisfied.
                    task_verdict_role = task.get("verdict_role")
                    task_assignee_role = task.get("assignee_role")
                    gate_for = task.get("gate_for")
                    if (
                        session_role
                        and task_assignee_role == session_role
                        and gate_for
                        and task_verdict_role
                    ):
                        # Open iff the relevant verdict hasn't landed on the work-task.
                        gate_v = verdicts.get(gate_for, {})
                        verdict_satisfied = (
                            gate_v.get("critic_approved")
                            if task_verdict_role == chats.ROLE_CRITIC
                            else (
                                gate_v.get("verifier_shipped")
                                if task_verdict_role == chats.ROLE_VERIFIER
                                else False
                            )
                        )
                        if not verdict_satisfied:
                            obligations.append(
                                {
                                    "task_id": task["task_id"],
                                    "chat_id": chat_id,
                                    "status": status,
                                    "begin_fired": task.get("begin_fired", False),
                                    "gate_for": gate_for,
                                    "verdict_role": task_verdict_role,
                                }
                            )
            except Exception:
                continue
    except Exception:
        pass
    return obligations


async def _guard4_escalate(
    session_id: str,
    obligation: dict,
    session_name: str,
    reason: str,
    silence_s: float,
) -> None:
    """Broadcast escalation notice to the roster chat + notify intake/master."""
    task_id = obligation["task_id"]
    chat_id = obligation["chat_id"]
    task_status = obligation["status"]

    if reason == "crash":
        reason_text = "process appears dead (crash)"
    elif reason == "hung":
        reason_text = (
            f"process alive but silent >{silence_s:.0f}s (possibly hung/paused)"
        )
    else:
        reason_text = f"silent >{silence_s:.0f}s with obligation (liveness unknown)"

    body = (
        f"⚠️ {session_name} unreachable but owes "
        f"[task-{task_id[:8]} ({task_status})] — {reason_text}. "
        f"Roster may be serializing on it; reassign or restart. chat_id={chat_id}"
    )

    try:
        from khimaira.monitor.chats import _post_synthetic_message

        await _post_synthetic_message(chat_id, body)
    except Exception:
        pass

    try:
        from khimaira.monitor import sessions as sessions_mod

        # Notify intake if present; fall back to master
        intake_sid = _resolve_intake_session_id(chat_id)
        target = intake_sid or _resolve_master_session_id(chat_id)
        if target:
            sessions_mod.post_notice(
                target_session_id=target,
                text=body,
                from_session_id="khimaira-daemon",
            )
    except Exception:
        pass


def _resolve_intake_session_id(chat_id: str) -> str | None:
    """Find the session_id with role=intake in this chat."""
    try:
        room = chats.load_room(chat_id)
        member_roles = (room.get("meta") or {}).get("member_roles") or {}
        for sid, role in member_roles.items():
            if role == "intake":
                return sid
    except Exception:
        pass
    return None


# ─── #13b-heavy: throttle-escalation (terminal 529-overload exhaustion) ────
#
# When a roster session's Stop hook detects that the turn ended on an
# unrecovered 529-overload storm (CC exhausted its internal retries — see
# khimaira.hooks.throttle_detect), it POSTs here. The session is now idle
# awaiting USER input and will NOT resume on its own.
#
# Deliberately NOT "re-poke"/auto-resume: the daemon has no active wake
# handle for an operator-launched interactive CC window (audited 2026-05-31 —
# every daemon→session vector is a passive note seen only on the session's
# next user turn; no tmux/pty/SDK injection; the window wasn't daemon-spawned
# so there's no re-invoke handle). The human is the resume actor; khimaira's
# job is to make sure they KNOW — loudly, backoff-paced.
#
#   • DETECT + ALERT — UNIVERSAL. Every terminal-overload (obligation or not)
#     surfaces a 🟡 alert. This is the actual gap: a session that throttled
#     out with no task obligation never tripped Guard-4, so nothing fired.
#   • ESCALATE — OBLIGATION-SCOPED. A throttled session that owes a
#     pending/in_progress task has stalled real work → louder, and after N
#     repeats routes through Guard-4's harder escalation.
#
# Per-session backoff: at most one escalation per session per cooldown window
# (the circuit.py-style cadence gate — don't re-alert on every Stop the
# session fires while still throttled). Daemon-spawned dispatch-run auto-
# resume (RunnerCircuit territory) IS buildable but is a deliberate v2
# deferral — Joseph's failure was terminal interactive windows.
_THROTTLE_COOLDOWN_S = float(
    os.environ.get("KHIMAIRA_THROTTLE_ESCALATE_COOLDOWN_S", "300")
)
_THROTTLE_ESCALATE_AFTER_N = int(
    os.environ.get("KHIMAIRA_THROTTLE_ESCALATE_AFTER_N", "3")
)
_THROTTLE_STATE_TTL_S = 3600.0  # forget a session's throttle streak after 1h quiet
# session_id → {"last_ts": float, "count": int}
_THROTTLE_STATE: dict[str, dict] = {}


def _session_display_name(session_id: str) -> str:
    """Best-effort human name for a session; falls back to the UUID prefix."""
    try:
        from khimaira.monitor import sessions as sessions_mod

        sd = sessions_mod._session_dir_read(session_id)
        if sd:
            status_file = sd / "status.json"
            if status_file.exists():
                data = json.loads(status_file.read_text(encoding="utf-8"))
                name = (data.get("name") or "").strip()
                if name:
                    return name
    except Exception:
        pass
    return session_id[:8]


def _chats_for_session(session_id: str) -> list[str]:
    """Chat ids where `session_id` is a member. Used to surface a 🟡 alert for
    a throttled bare-idle session (no obligation to scope it to). Fail-open."""
    out: list[str] = []
    try:
        chat_dir = chats._chat_dir()
        if not chat_dir.exists():
            return out
        for chat_path in chat_dir.glob("chat-*.jsonl"):
            chat_id = chat_path.stem
            try:
                room = chats.load_room(chat_id)
                members = room.get("members") or {}
                roles = (room.get("meta") or {}).get("member_roles") or {}
                if session_id in members or session_id in roles:
                    out.append(chat_id)
            except Exception:
                continue
    except Exception:
        pass
    return out


async def _handle_throttle_escalation(session_id: str, payload: dict) -> dict:
    """Process a terminal-overload detection from a session's Stop hook.

    Universal 🟡 alert; obligation-scoped harder escalation; per-session
    backoff so we don't re-alert on every Stop while a session stays
    throttled. Returns a small status dict (the route echoes it).
    """
    from khimaira.monitor import sessions as sessions_mod

    now = time.time()

    # GC stale streaks.
    for sid, st in list(_THROTTLE_STATE.items()):
        if now - st.get("last_ts", 0) > _THROTTLE_STATE_TTL_S:
            _THROTTLE_STATE.pop(sid, None)

    prev = _THROTTLE_STATE.get(session_id)
    if prev and (now - prev["last_ts"]) < _THROTTLE_COOLDOWN_S:
        # Within the backoff window — record nothing new, stay quiet.
        return {
            "escalated": False,
            "reason": "cooldown",
            "session_id": session_id,
            "cooldown_remaining_s": round(
                _THROTTLE_COOLDOWN_S - (now - prev["last_ts"]), 1
            ),
        }

    count = (prev["count"] + 1) if prev else 1
    _THROTTLE_STATE[session_id] = {"last_ts": now, "count": count}

    name = _session_display_name(session_id)
    obligations = _get_session_obligations(session_id)

    ra = payload.get("retry_attempt")
    mr = payload.get("max_retries")
    retry_str = f"{ra}/{mr}" if mr else str(ra)
    detail = payload.get("message") or "service overloaded"

    def _notify(chat_id: str, body: str) -> None:
        target = _resolve_intake_session_id(chat_id) or _resolve_master_session_id(
            chat_id
        )
        if target:
            try:
                sessions_mod.post_notice(
                    target_session_id=target,
                    text=body,
                    from_session_id="khimaira-daemon",
                )
            except Exception:
                pass

    # Local import mirrors _guard4_escalate and _send_diagnostic_probe pattern —
    # _post_synthetic_message lives in chats.py and is not a module-level import here.
    try:
        from khimaira.monitor.chats import _post_synthetic_message as _post_msg
    except Exception:

        async def _post_msg(chat_id, body, kind=None):  # type: ignore[assignment]
            return None

    if obligations:
        for obligation in obligations:
            tid = obligation["task_id"]
            chat_id = obligation["chat_id"]
            body = (
                f"🟡 THROTTLE-ESCALATION: {name} exhausted rate-limit retries "
                f"({retry_str}; {detail}) and STOPPED mid-[task-{tid[:8]} "
                f"({obligation['status']})]. Interactive window — daemon can't "
                f"auto-resume; needs a MANUAL re-prompt to continue. (alert #{count})"
            )
            try:
                await _post_msg(chat_id, body)
            except Exception:
                pass
            _notify(chat_id, body)
            # After N repeats with the obligation still open, escalate harder.
            if count >= _THROTTLE_ESCALATE_AFTER_N:
                try:
                    await _guard4_escalate(
                        session_id,
                        obligation,
                        name,
                        "hung",
                        _THROTTLE_COOLDOWN_S * count,
                    )
                except Exception:
                    pass
        return {
            "escalated": True,
            "obligation_scoped": True,
            "obligations": len(obligations),
            "count": count,
            "session_id": session_id,
        }

    # Bare-idle: no work to resume — informational alert only (universal
    # coverage of the no-obligation gap Guard-4 misses).
    body = (
        f"🟡 THROTTLE-ALERT: {name} exhausted rate-limit retries "
        f"({retry_str}; {detail}) and stopped — no active task (informational). "
        f"(alert #{count})"
    )
    chat_ids = _chats_for_session(session_id)
    for chat_id in chat_ids:
        try:
            await _post_msg(chat_id, body)
        except Exception:
            pass
        _notify(chat_id, body)
    return {
        "escalated": True,
        "obligation_scoped": False,
        "chats": len(chat_ids),
        "count": count,
        "session_id": session_id,
    }


async def _guard4_check_once() -> None:
    """Scan sessions for stall conditions; escalate with debounce.

    Fix A: 2D pending-gate (BEGIN-fired × liveness).
    Fix B: obligation-scoped debounce — only clear when the obligation itself clears,
           not when the session has an activity-blip (prevents re-spam).
    Guard-5 Part A: suppress during declared roster wind-down (sessions intentionally
    offline should not be escalated as hung — the 18h overnight false-positive class).
    """
    # Wind-down suppression — the roster is deliberately offline; don't escalate.
    try:
        from khimaira.monitor.sessions import is_roster_wind_down

        if is_roster_wind_down():
            return
    except Exception:
        pass  # fail-open: if the flag can't be read, continue with normal check

    now = time.time()

    # Sweep stale debounce entries (TTL-based fallback)
    expired = [
        k for k, ts in _GUARD4_STALLED.items() if now - ts > _GUARD4_STALLED_TTL_S
    ]
    for k in expired:
        _GUARD4_STALLED.pop(k, None)

    # Fix B: clear debounce entries whose obligations have cleared (task no longer active).
    # Collect current active (session_id, task_id) pairs across all obligations; any stalled
    # key NOT in the current set means the obligation was closed → re-arm for future stalls.
    try:
        current_obligation_keys: set[tuple[str, str]] = set()
        for sid_key in list(_GUARD4_STALLED.keys()):
            sid, tid = sid_key
            # Re-scan obligations for this session and check if this task is still active.
            active_for_session = {
                (sid, o["task_id"]) for o in _get_session_obligations(sid)
            }
            current_obligation_keys |= active_for_session
        cleared = [
            k for k in list(_GUARD4_STALLED.keys()) if k not in current_obligation_keys
        ]
        for k in cleared:
            _GUARD4_STALLED.pop(k, None)
    except Exception:
        pass  # fail-open: stale entries expire via TTL

    try:
        from khimaira.monitor import sessions as sessions_mod

        session_rows = sessions_mod.list_sessions(use_cache=True)
    except Exception:
        return

    ceiling = _compute_throttle_ceiling_s()

    for row in session_rows:
        session_id = row.get("session_id")
        if not session_id:
            continue
        last_age_s = row.get("last_active_age_s") or 0
        if last_age_s < _GUARD4_MIN_SILENCE_S:
            continue  # recently active — skip

        obligations = _get_session_obligations(session_id)
        if not obligations:
            continue  # no obligations — silent demote is fine

        # Session is silent AND has obligations — three-way logic
        alive = _is_process_alive_for_session(session_id)
        silence_s = float(last_age_s)
        status_raw = row.get("status") or {}
        session_name = (
            status_raw.get("name")
            or status_raw.get("effective_status")
            or session_id[:8]
        )

        for obligation in obligations:
            obligation_key = (session_id, obligation["task_id"])
            task_status = obligation.get("status")
            begin_fired = obligation.get("begin_fired", False)

            # Fix B: obligation-scoped debounce — do NOT clear on activity-blips.
            # Only clear when the obligation itself has cleared (status transitions
            # to a terminal/non-active state — detected by the obligation no longer
            # appearing in the obligations list, so once set it stays until it
            # drops off naturally via the 1h TTL, OR we detect the obligation is gone
            # by re-scanning — but since we're iterating current obligations,
            # a key present in _GUARD4_STALLED means the obligation hasn't cleared yet).
            if obligation_key in _GUARD4_STALLED:
                continue  # already escalated for this obligation; don't re-fire

            # Fix A: 2D pending-gate (BEGIN-fired × liveness).
            # pending and in_progress have different stall semantics.
            if task_status == chats.TASK_PENDING:
                if not begin_fired:
                    # pending + NO-BEGIN: compliant BEGIN-waiting (agent told to hold).
                    # Only escalate if the assignee is CONFIRMED-DEAD (then #14a can't recover).
                    if alive is False:
                        # Dead agent + pending + no BEGIN → #14a can't fire (dead never ready-acks)
                        _GUARD4_STALLED[obligation_key] = now
                        await _guard4_escalate(
                            session_id, obligation, session_name, "crash", silence_s
                        )
                    # alive=True or alive=None → compliant BEGIN-waiting → don't escalate
                else:
                    # pending + BEGIN-FIRED + not-started → wedged post-BEGIN
                    # #14b banner nags the agent; Guard-4 escalates to master if it persists.
                    # Escalate regardless of liveness once BEGIN was fired.
                    if silence_s > ceiling:
                        reason = (
                            "crash"
                            if alive is False
                            else "hung" if alive is True else "unknown"
                        )
                        _GUARD4_STALLED[obligation_key] = now
                        await _guard4_escalate(
                            session_id, obligation, session_name, reason, silence_s
                        )
            else:
                # in_progress: existing three-way logic unchanged.
                if alive is False:
                    _GUARD4_STALLED[obligation_key] = now
                    await _guard4_escalate(
                        session_id, obligation, session_name, "crash", silence_s
                    )
                elif alive is True and silence_s <= ceiling:
                    pass  # within grace window — suppress (🟡 throttled)
                elif alive is True and silence_s > ceiling:
                    _GUARD4_STALLED[obligation_key] = now
                    await _guard4_escalate(
                        session_id, obligation, session_name, "hung", silence_s
                    )
                else:  # alive is None
                    if silence_s > ceiling:
                        _GUARD4_STALLED[obligation_key] = now
                        await _guard4_escalate(
                            session_id, obligation, session_name, "unknown", silence_s
                        )


async def _guard4_watcher() -> None:
    import logging

    _log = logging.getLogger(__name__)
    while True:
        await asyncio.sleep(_GUARD4_WATCH_INTERVAL)
        try:
            await _guard4_check_once()
        except Exception as exc:
            _log.warning("guard4-watcher error: %s", exc)


from khimaira.monitor import chats
from khimaira.monitor.chats import _resolve_sender_name  # shared with _broadcast

from .._optional import require


class CreateRoomReq(BaseModel):
    creator_session_id: str
    member_session_ids: list[str]
    title: str | None = None
    fresh: bool = False
    topology: str = "flat"  # v1.9.5: flat | hierarchical | custom
    member_roles: dict[str, str] | None = (
        None  # session_id → role; written to meta at creation
    )


class InviteReq(BaseModel):
    by_session_id: str
    invitee_session_id: str
    role: str | None = None  # #3: optional atomic role-binding at invite time


class RemoveMemberReq(BaseModel):
    by_session_id: str  # must be master
    target_session_id: str  # member to evict


class AcceptReq(BaseModel):
    session_id: str


class SendReq(BaseModel):
    sender_session_id: str
    body: str
    to: list[str] | None = None  # Phase B: optional per-recipient addressing
    private: bool | None = (
        None  # v1.9.2: hide from non-recipients; None = topology default
    )


class CreateTaskReq(BaseModel):
    sender_session_id: str
    body: str
    assignee_session_id: str | None = None
    assignee_role: str | None = None  # Guard-5: role-class assignee
    gate_required: bool = False  # Guard-5: auto-create review-tasks on done
    gate_for: str | None = None  # Guard-5: review-task references this work-task
    verdict_role: str | None = None  # Guard-5: "critic"|"verifier"
    private: bool = False  # v1.9.2: hide from non-assignee in chat_history


class MasterOverrideVerdictReq(BaseModel):
    by_session_id: str
    verdict: str  # "approve" | "changes" | "ship" | "hold"
    reason: str  # non-empty — audited, never silent
    trigger: str  # "quorum_timeout" | "manual_deadlock"


class UpdateTaskStatusReq(BaseModel):
    by_session_id: str
    new_status: str
    note: str | None = None
    private: bool = False  # v1.9.2: hide from non-assignee in chat_history


class SignalTaskStartReq(BaseModel):
    by_session_id: str
    note: str | None = None


class RecordGateVerdictReq(BaseModel):
    by_session_id: str
    verdict: str  # "approve" | "changes" | "ship" | "hold"


class AutoAcceptReq(BaseModel):
    session_id: str
    allowlist: list[str]


class ThrottleReq(BaseModel):
    # #13b-heavy — posted by a session's Stop hook on terminal 529 exhaustion.
    retry_attempt: int | None = None
    max_retries: int | None = None
    overload_count: int | None = None
    last_timestamp: str | None = None
    message: str | None = None


class LeaveReq(BaseModel):
    session_id: str


class TransferMembershipReq(BaseModel):
    from_session_id: str
    to_session_id: str
    # Phase B v1.6: when True, atomically writes
    # meta.deputized_original_master = from_session_id AND skips the donor's
    # TRANSFERRED_OUT MEMBER write so the donor stays ACCEPTED throughout
    # the deputize→resume cycle. Default False = terminal-handoff behavior.
    as_deputize: bool = False


class ResumeMasterReq(BaseModel):
    # Phase B v1.6: caller (must equal recorded meta.deputized_original_master).
    by_session_id: str
    # Role the vice gets demoted to on resume. Defaults to "agent"; cannot be
    # "master" (closes quorum loophole, mirrors chat_grant_role).
    demote_to: str = "agent"


class GrantRoleReq(BaseModel):
    by_session_id: str
    target_session_id: str
    role: str
    demote_to: str = "agent"


class RejectReq(BaseModel):
    session_id: str


class RegisterPpidReq(BaseModel):
    ppid: int
    session_id: str


class AssignmentSpec(BaseModel):
    agent_session_id: str
    task_body: str
    required_model: str = "sonnet"
    required_effort: str = "medium"


class AssignBatchReq(BaseModel):
    from_session_id: str
    assignments: list[AssignmentSpec]
    timeout_s: int = 600
    wait_for_acks: bool = True
    fire_begin_on_partial: bool = False


def build_router():
    fastapi = require("fastapi")
    sse_starlette = require("sse_starlette.sse")
    import logging

    _log = logging.getLogger(__name__)

    router = fastapi.APIRouter()

    # ---------------------------------------------------------------------------
    # TM1 daemon-auth (Phase B.1 — warn+fallback; see SECURITY.md).
    # B.1: reads X-Session-ID header (env-sourced from CLAUDE_CODE_SESSION_ID in
    # honest MCP clients → correct-by-construction). Returns None when absent —
    # callers use body authority-id as fallback + log a WARN. B.2 flip: change the
    # None-return path to raise HTTPException(401).
    # ---------------------------------------------------------------------------

    async def require_actor(request: Request) -> "str | None":
        """TM1 actor identity with P1 slot-resolution (path-7 drift-healing).

        B.1: reads X-Session-ID header (env-sourced from CLAUDE_CODE_SESSION_ID).
        Applies slot_resolve to map a reattached session's old subprocess-bound sid
        to the slot's current authoritative sid — bounded to recent-prior only
        (neutral-by-construction: a harvested ancient sid stays INERT).
        Absent header → None (fallback to body authority-id with WARN, B.1 mode).
        """
        caller = (request.headers.get("x-session-id") or "").strip()
        if not caller:
            return None
        # slot_resolve: if caller is an immediately-prior sid (reattached session),
        # maps it to the current sid. If caller is already current or not in registry,
        # returns the caller unchanged. Bounded bound = neutral-by-construction.
        try:
            from khimaira.monitor import sessions as _sess_mod
            current = _sess_mod.slot_resolve(caller)
            return current if current is not None else caller
        except Exception:
            return caller

    def _actor_or_body(actor_header: "str | None", body_id: str, path: str) -> str:
        """B.1: prefer the X-Session-ID header, but only when it identifies an
        accepted member of the chat being accessed; else fall back to the body
        authority-id (the durable khimaira session id the client passes).

        Why the membership gate: CLAUDE_CODE_SESSION_ID (the header source) is a
        *boot* id that changes on every Claude Code restart, so after a restart
        the header is present-but-not-a-member while the body still carries the
        session's real khimaira member id. Without this gate, a restart 403s every
        session whose boot id != member id (the identity-split bug — observed
        roster-wide post-suspend). The downstream membership check still gates the
        operation; this only picks the id likeliest to be the real member.

        Security: falling back to the client-supplied body id under a non-member
        header is the SAME-UID trust boundary already documented for B.1 (a local
        same-uid process is trusted; cross-uid spoof defense is out of scope). No
        new boundary is crossed — the absent-header path already trusted the body.
        Warns → 0 is the B.2 flip-trigger (see SECURITY.md).
        """
        if actor_header is not None:
            chat_id = None
            _parts = path.strip("/").split("/")
            if len(_parts) >= 2 and _parts[0] == "chats":
                chat_id = _parts[1]
            header_is_member = False
            if chat_id is not None:
                try:
                    _room = load_room(chat_id)
                    header_is_member = (
                        _room.get("members", {}).get(actor_header, {}).get("state")
                        == ACCEPTED
                    )
                except Exception:
                    header_is_member = False
            # Non-chat path (no chat_id to check) → trust the header as before.
            if chat_id is None or header_is_member:
                return actor_header
            _log.warning(
                "daemon-auth B.1: X-Session-ID=%s present but NOT an accepted member "
                "of %s on %s; using body authority-id=%s (stale boot id after restart "
                "— CLAUDE_CODE_SESSION_ID != khimaira member id).",
                actor_header[:8] + "...",
                chat_id,
                path,
                (body_id[:8] + "...") if len(body_id) > 8 else body_id,
            )
            return body_id
        _log.warning(
            "daemon-auth B.1: X-Session-ID absent on %s; "
            "falling back to body authority-id=%s. "
            "Restart session to send header (CLAUDE_CODE_SESSION_ID). "
            "Warns→0 = safe to flip to B.2 hard-reject.",
            path,
            (body_id[:8] + "...") if len(body_id) > 8 else body_id,
        )
        return body_id

    # Lazy import: themis role cache invalidation. Fail-open — if themis router
    # is not loaded (test environments), chat writes proceed unaffected.
    def _inval(*session_ids: str) -> None:
        try:
            from .themis import invalidate_role_cache

            for sid in session_ids:
                invalidate_role_cache(sid)
        except Exception:
            pass  # fail-open: stale cache expires in 5 min (TTL safety net)

    @router.post("/chats")
    async def create_room(req: CreateRoomReq) -> dict:
        try:
            result = chats.create_room(
                req.creator_session_id,
                req.member_session_ids,
                title=req.title,
                fresh=req.fresh,
                topology=req.topology,
                member_roles=req.member_roles,
            )
            # Invalidate creator + all initial members — they may now have roles
            _inval(req.creator_session_id, *req.member_session_ids)
            return result
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.get("/chats/{chat_id}/members/{session_id}/status")
    async def member_status(chat_id: str, session_id: str) -> dict:
        """#10: Return the authoritative membership state from the chat JSONL.

        The chat daemon is the single source of truth for membership state.
        Use this to reconcile any session_state view against the real state.
        """
        try:
            sid = chats._resolve_or_uuid(session_id, chat_id=chat_id)
            room = chats.load_room(chat_id)
            member = room["members"].get(sid)
            if not member:
                raise fastapi.HTTPException(
                    404, f"Session {session_id!r} is not a member of {chat_id!r}."
                )
            return {
                "session_id": sid,
                "chat_id": chat_id,
                "state": member.get("state"),
                "invited_by": member.get("invited_by"),
            }
        except fastapi.HTTPException:
            raise
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.post("/chats/{chat_id}/nudge-pending")
    async def nudge_pending(chat_id: str, req: InviteReq) -> dict:
        """#10: Master re-broadcasts an invite notification to a pending invitee.

        A dormant pending-invitee can't be woken from outside — this lets master
        re-send the invite SSE event so the session sees it on its next turn.
        Idempotent: the member state stays PENDING; this only re-fires the broadcast.
        """
        try:
            by_session_id = chats._resolve_or_uuid(req.by_session_id, chat_id=chat_id)
            target_id = chats._resolve_or_uuid(req.invitee_session_id, chat_id=chat_id)
            room = chats.load_room(chat_id)

            # Only master/accepted members can nudge
            if room["members"].get(by_session_id, {}).get("state") != chats.ACCEPTED:
                raise fastapi.HTTPException(
                    403, f"{by_session_id!r} is not an accepted member of {chat_id!r}"
                )

            target = room["members"].get(target_id)
            if not target or target.get("state") != chats.PENDING:
                raise fastapi.HTTPException(
                    422,
                    f"Session {target_id!r} is not pending in {chat_id!r}; "
                    f"nudge only applies to pending invitees.",
                )

            # Re-broadcast the invite record so the target's SSE subscriber sees it.
            invite_record = {
                "kind": chats.MEMBER,
                "event_id": chats._new_event_id(),
                "ts": chats._now_iso(),
                "chat_id": chat_id,
                "session_id": target_id,
                "session_name": target.get("session_name"),
                "state": chats.PENDING,
                "invited_by": target.get("invited_by"),
                "nudged_by": by_session_id,
            }
            chats._broadcast(chat_id, invite_record)
            return {"nudged": target_id, "chat_id": chat_id}
        except fastapi.HTTPException:
            raise
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.get("/chats")
    async def list_my_chats(session_id: str) -> dict:
        try:
            result = {"chats": chats.my_chats(session_id)}
            # Self-heal IN-MASTER-1: stamp the SSE heartbeat for this session so
            # Themis sees a fresh heartbeat even when the SSE connection died
            # (e.g. after /compact). The heartbeat is the signal IN-MASTER-1 uses;
            # calling chat_my_chats IS the correct fulfillment of that obligation.
            try:
                from khimaira.monitor import sessions as sessions_mod

                sessions_mod.write_sse_heartbeat(session_id)
            except Exception:
                pass  # never block the list response
            return result
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    # Specific routes BEFORE the {chat_id} catch-all, or FastAPI matches
    # the wildcard first (treats "session-by-ppid" as a chat_id).
    @router.post("/chats/register-pending-session")
    async def register_pending_session(req: RegisterPpidReq) -> dict:
        """SessionStart hook posts {ppid, session_id} so the chat MCP
        subprocess (same parent ppid) can self-register at startup
        without waiting for the agent's first chat tool call."""
        chats.register_session_by_ppid(req.ppid, req.session_id)
        return {"ok": True, "ppid": req.ppid, "session_id": req.session_id}

    @router.get("/chats/session-by-ppid")
    async def session_by_ppid(ppid: int) -> dict:
        """Chat MCP subprocess at startup queries by its own getppid().
        Returns the session_id the SessionStart hook registered, or null."""
        return {"session_id": chats.lookup_session_by_ppid(ppid)}

    @router.get("/chats/pending/latest")
    async def latest_pending(session_id: str) -> dict:
        """Return the most-recent pending chat_id for this session, or null.

        Used by /khimaira-chat-accept and /khimaira-chat-reject so the
        slash commands work without the user knowing the chat_id.
        """
        try:
            chat_id = chats.latest_pending_chat_id(session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc
        return {"chat_id": chat_id}

    @router.get("/chats/events")
    async def chat_events(session_id: str, request: Request):
        try:
            chats._resolve_or_uuid(session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

        last_event_id = request.headers.get("last-event-id")

        async def event_generator():
            async for record in chats.subscribe(
                session_id, since_event_id=last_event_id
            ):
                if await request.is_disconnected():
                    break
                yield {
                    "id": record.get("event_id", ""),
                    "event": record.get("kind", "message"),
                    "data": json.dumps(record, separators=(",", ":")),
                }
                # Cursor advances AFTER successful yield — if yield raises
                # (ClientDisconnect, transport error), this line doesn't run,
                # so the next reconnect backfills from the prior position.
                evt_id = record.get("event_id")
                rec_chat_id = record.get("chat_id")
                if evt_id and rec_chat_id:
                    chats._advance_cursor(session_id, rec_chat_id, evt_id)

        # ping=15 makes sse_starlette emit a `: keepalive` comment line
        # every 15 seconds. SSE comments are valid keep-alives that don't
        # show up as events but DO refresh TCP buffers and reset the
        # client-side read timeout. Without this, an SSE connection that
        # survives a laptop suspend (or any long network silence) becomes
        # silently dead — the daemon's view is gone but the client's
        # aiter_lines() blocks forever waiting for events that never come.
        return sse_starlette.EventSourceResponse(event_generator(), ping=15)

    @router.get("/chats/{chat_id}")
    async def get_room(chat_id: str, session_id: str) -> dict:
        try:
            room = chats.load_room(chat_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc
        member = room["members"].get(chats._resolve_or_uuid(session_id))
        if not member or member["state"] not in (chats.PENDING, chats.ACCEPTED):
            raise fastapi.HTTPException(
                403, f"Session {session_id!r} is not a member of {chat_id!r}"
            )
        return room

    @router.post("/chats/{chat_id}/invite")
    async def invite_member(
        chat_id: str,
        req: InviteReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        actor = _actor_or_body(_actor, req.by_session_id, f"/chats/{chat_id}/invite")
        try:
            result = chats.invite(
                chat_id,
                actor,
                req.invitee_session_id,
                role=req.role,
            )
            # If role was bound, invalidate the invitee's Themis role cache.
            if req.role is not None:
                _inval(req.invitee_session_id)
            return result
        except ValueError as exc:
            msg = str(exc)
            if "not registered" in msg:
                code = 422
            elif "not an accepted member" in msg:
                code = 403
            else:
                code = 404
            raise fastapi.HTTPException(code, msg) from exc

    @router.delete("/chats/{chat_id}/members/{target_session_id}")
    async def remove_member(
        chat_id: str,
        target_session_id: str,
        req: RemoveMemberReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        """#2 remove-member: master evicts a member, discards their SSE subscriber."""
        actor = _actor_or_body(
            _actor, req.by_session_id, f"/chats/{chat_id}/members/{target_session_id}"
        )
        try:
            result = chats.remove_member(chat_id, actor, target_session_id)
            # Invalidate the removed session's Themis role cache.
            _inval(target_session_id)
            return result
        except ValueError as exc:
            msg = str(exc)
            if "not the master" in msg:
                code = 403
            elif "not a member" in msg:
                code = 404
            else:
                code = 422
            raise fastapi.HTTPException(code, msg) from exc

    @router.post("/chats/{chat_id}/accept")
    async def accept_invite(chat_id: str, req: AcceptReq) -> dict:
        try:
            result = chats.accept(chat_id, req.session_id)
            _inval(req.session_id)
            return result
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.post("/chats/{chat_id}/reject")
    async def reject_invite(chat_id: str, req: RejectReq) -> dict:
        try:
            return chats.reject(chat_id, req.session_id)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.post("/chats/{chat_id}/messages")
    async def send_message(
        chat_id: str,
        req: SendReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        actor = _actor_or_body(_actor, req.sender_session_id, f"/chats/{chat_id}/messages")
        # Resolve: this send counts as a reply to any peer awaiting a response.
        if req.to:
            # Targeted send: resolve only named recipients.
            await _resolve_expected_reply(actor, req.to)
        else:
            # Broadcast: resolve for ALL accepted chat members (critic/verifier
            # conventionally reply via broadcast, not DM).
            try:
                room = chats.load_room(chat_id)
                member_ids = [
                    sid
                    for sid, m in room["members"].items()
                    if m.get("state") == chats.ACCEPTED and sid != actor
                ]
                if member_ids:
                    await _resolve_expected_reply(actor, member_ids)
            except Exception:
                pass  # fail-open: don't block the send if room lookup fails
        deadline = time.monotonic() + _PENDING_WAIT_DEADLINE
        while True:
            try:
                result = chats.send_message(
                    chat_id,
                    actor,
                    req.body,
                    to=req.to,
                    private=req.private,
                )
                # Register only for targeted sends — broadcasts don't expect a reply.
                if req.to:
                    await _register_expected_reply(actor, req.to, chat_id)
                return result
            except ValueError as exc:
                msg = str(exc)
                if req.to and "pending" in msg:
                    if time.monotonic() >= deadline:
                        raise fastapi.HTTPException(
                            408,
                            f"Timed out after {_PENDING_WAIT_DEADLINE:.0f}s waiting for recipient to accept invite.",
                        ) from exc
                    await asyncio.sleep(_PENDING_POLL_INTERVAL)
                    continue
                raise fastapi.HTTPException(403, msg) from exc

    # ---- Phase B: tasks ----

    @router.post("/chats/{chat_id}/tasks")
    async def create_task(
        chat_id: str,
        req: CreateTaskReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        actor = _actor_or_body(_actor, req.sender_session_id, f"/chats/{chat_id}/tasks")
        try:
            return chats.create_task(
                chat_id,
                actor,
                req.body,
                assignee_session_id=req.assignee_session_id,
                assignee_role=req.assignee_role,
                gate_required=req.gate_required,
                gate_for=req.gate_for,
                verdict_role=req.verdict_role,
                private=req.private,
            )
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    @router.post("/chats/{chat_id}/tasks/{task_id}/status")
    async def update_task_status(
        chat_id: str,
        task_id: str,
        req: UpdateTaskStatusReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        actor = _actor_or_body(
            _actor, req.by_session_id, f"/chats/{chat_id}/tasks/{task_id}/status"
        )
        try:
            result = chats.update_task_status(
                chat_id,
                task_id,
                actor,
                req.new_status,
                note=req.note,
                private=req.private,
            )
        except ValueError as exc:
            # 403 for permission errors (master-only transitions); 404 for unknown task
            msg = str(exc)
            code = (
                403
                if any(w in msg for w in ("creator", "assignee", "transition"))
                else 404
            )
            raise fastapi.HTTPException(code, msg) from exc

        # Guard-5 Part A: AUTO-create review-tasks when gate_required task → done.
        if req.new_status == chats.TASK_DONE:
            try:
                _auto_create_review_tasks(chat_id, task_id, actor)
            except Exception:
                pass  # fail-open: AUTO-create is best-effort
        return result

    @router.post("/chats/{chat_id}/tasks/{task_id}/override-verdict")
    async def master_override_verdict(
        chat_id: str,
        task_id: str,
        req: MasterOverrideVerdictReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        """Audited gate-close for master — quorum-timeout escape valve (Guard-5 Part A).

        Posts a TASK_VERDICT with is_override=True + reason + trigger. IN-MASTER-9
        permits this when the audited fields are present. Only master may call this.
        trigger ∈ {quorum_timeout, manual_deadlock}. reason must be non-empty.
        """
        actor = _actor_or_body(
            _actor,
            req.by_session_id,
            f"/chats/{chat_id}/tasks/{task_id}/override-verdict",
        )
        try:
            return chats.master_override_verdict(
                chat_id,
                actor,
                task_id,
                req.verdict,
                req.reason,
                req.trigger,
            )
        except ValueError as exc:
            msg = str(exc)
            # Distinguish validation errors (400) from auth errors (403)
            is_validation = (
                "Invalid trigger" in msg
                or "Invalid verdict" in msg
                or "reason must be non-empty" in msg
            )
            code = 400 if is_validation else 403
            raise fastapi.HTTPException(code, msg) from exc

    @router.post("/chats/{chat_id}/tasks/{task_id}/signal-start")
    async def signal_task_start(
        chat_id: str,
        task_id: str,
        req: SignalTaskStartReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        actor = _actor_or_body(
            _actor, req.by_session_id, f"/chats/{chat_id}/tasks/{task_id}/signal-start"
        )
        try:
            return chats.signal_task_start(chat_id, task_id, actor, note=req.note)
        except ValueError as exc:
            msg = str(exc)
            if "No task" in msg:
                code = 404
            elif "not 'pending'" in msg:
                code = 409
            else:
                # master-only / non-accepted member → 403
                code = 403
            raise fastapi.HTTPException(code, msg) from exc

    @router.post("/chats/{chat_id}/tasks/{task_id}/verdict")
    async def record_gate_verdict(
        chat_id: str,
        task_id: str,
        req: RecordGateVerdictReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        """Write a structured gate-verdict (B3). critic: approve/changes; verifier: ship/hold."""
        actor = _actor_or_body(
            _actor, req.by_session_id, f"/chats/{chat_id}/tasks/{task_id}/verdict"
        )
        try:
            return chats.record_gate_verdict(chat_id, actor, task_id, req.verdict)
        except ValueError as exc:
            msg = str(exc)
            code = 404 if "No task" in msg else 400 if "Invalid verdict" in msg else 403
            raise fastapi.HTTPException(code, msg) from exc

    @router.get("/chats/{chat_id}/tasks")
    async def list_tasks(chat_id: str, session_id: str) -> dict:
        try:
            return {"tasks": chats.task_status(chat_id, session_id)}
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    # ---- v1.9: assign-batch coordinator ----

    @router.post("/chats/{chat_id}/assign-batch")
    async def assign_batch(
        chat_id: str,
        req: AssignBatchReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        """v1.9 coordinator: fan-out assignments + collect acks + fire begin.

        Collapses the master's 3N+K+2 call loop into one daemon HTTP call.
        Long-running when wait_for_acks=True (up to timeout_s seconds).
        """
        actor = _actor_or_body(_actor, req.from_session_id, f"/chats/{chat_id}/assign-batch")
        try:
            return await chats.assign_batch(
                chat_id,
                actor,
                [a.model_dump() for a in req.assignments],
                timeout_s=req.timeout_s,
                wait_for_acks=req.wait_for_acks,
                fire_begin_on_partial=req.fire_begin_on_partial,
            )
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    # ---- Phase B: auto-accept ----

    @router.post("/sessions/{session_id}/auto-accept")
    async def set_auto_accept(session_id: str, req: AutoAcceptReq) -> dict:
        # session_id from path is the source of truth; req.session_id should match
        # but we accept the path version
        try:
            chats.set_auto_accept(session_id, req.allowlist)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc
        return {"ok": True, "session_id": session_id, "allowlist": req.allowlist}

    @router.post("/sessions/{session_id}/throttle")
    async def throttle_detected(session_id: str, req: ThrottleReq) -> dict:
        """#13b-heavy — a session's Stop hook reports terminal 529 exhaustion.

        Surfaces a universal 🟡 alert + obligation-scoped escalation with
        per-session backoff. Never raises into the hook: the daemon swallows
        handler errors and returns ok=False so a Stop hook never blocks CC
        from exiting on our account.
        """
        try:
            result = await _handle_throttle_escalation(session_id, req.model_dump())
            return {"ok": True, **result}
        except Exception as exc:  # fail-open — the caller is a Stop hook
            return {"ok": False, "session_id": session_id, "error": str(exc)}

    @router.post("/sessions/{session_id}/auto-accept/apply-by-name")
    async def apply_auto_accept_by_name(session_id: str, name: str) -> dict:
        """Surface the by-name allowlist file for a freshly-named session.
        Called by the chat MCP subprocess at boot, after the dual-name
        auto-bridge detects `-n NAME` and registers it. No-op if no
        by-name file exists for `name`."""
        try:
            return chats.apply_auto_accept_by_name(session_id, name)
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.get("/chats/{chat_id}/messages")
    async def get_history(
        chat_id: str,
        session_id: str,
        limit: int = 50,
        since: str | None = None,
    ) -> dict:
        try:
            msgs = chats.history(chat_id, session_id, limit=limit, since_event_id=since)
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc
        # Option A: resolve each sender's CURRENT name at read-time.
        # Per-request cache prevents N lookups for N messages from same sender.
        name_cache: dict[str, str] = {}
        for msg in msgs:
            sid = msg.get("sender_id")
            if not sid:
                continue
            if sid not in name_cache:
                name_cache[sid] = _resolve_sender_name(
                    sid, msg.get("sender_name", sid[:8])
                )
            msg["sender_name"] = name_cache[sid]
        return {"messages": msgs}

    @router.post("/chats/{chat_id}/leave")
    async def leave_chat(chat_id: str, req: LeaveReq) -> dict:
        try:
            result = chats.leave(chat_id, req.session_id)
            _inval(req.session_id)
            return result
        except ValueError as exc:
            raise fastapi.HTTPException(404, str(exc)) from exc

    @router.post("/chats/{chat_id}/transfer-membership")
    async def transfer_membership(
        chat_id: str,
        req: TransferMembershipReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        actor = _actor_or_body(
            _actor, req.from_session_id, f"/chats/{chat_id}/transfer-membership"
        )
        try:
            result = chats.transfer_membership(
                chat_id,
                actor,
                req.to_session_id,
                as_deputize=req.as_deputize,
            )
            # Both sessions swap roles — invalidate both
            _inval(req.from_session_id, req.to_session_id)
            return result
        except ValueError as exc:
            msg = str(exc)
            if "already accepted" in msg:
                code = 409
            elif "only accepted members" in msg:
                code = 403
            else:
                code = 404
            raise fastapi.HTTPException(code, msg) from exc

    @router.post("/chats/{chat_id}/resume-master")
    async def resume_master(
        chat_id: str,
        req: ResumeMasterReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        """Phase B v1.6: caller (original master per meta marker) reclaims
        master role from the vice that's currently holding it. Pairs with
        /khimaira-resume on the skill side. See chats.chat_resume_master for
        semantics + invariants."""
        actor = _actor_or_body(
            _actor, req.by_session_id, f"/chats/{chat_id}/resume-master"
        )
        try:
            result = chats.chat_resume_master(
                chat_id,
                actor,
                demote_to=req.demote_to,
            )
            # by_session_id reclaims master; the former vice's session_id is
            # not in the request — clear entire cache so stale vice role
            # doesn't persist beyond the TTL. Nuclear but correct.
            try:
                from .themis import clear_role_cache

                clear_role_cache()
            except Exception:
                pass
            return result
        except ValueError as exc:
            msg = str(exc)
            if (
                "not currently deputized" in msg
                or "no deputized_original_master" in msg
            ):
                code = 409
            elif "not the recorded original master" in msg or "demote_to" in msg:
                code = 403
            else:
                code = 404
            raise fastapi.HTTPException(code, msg) from exc

    @router.post("/chats/{chat_id}/grant-role")
    async def grant_role(
        chat_id: str,
        req: GrantRoleReq,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        """Phase B v2: master-only role grant. Calls chats.chat_grant_role and
        immediately invalidates the Themis role cache for the affected sessions
        so the new role is enforced on the next tool call (no TTL wait)."""
        actor = _actor_or_body(_actor, req.by_session_id, f"/chats/{chat_id}/grant-role")
        try:
            result = chats.chat_grant_role(
                chat_id,
                actor,
                req.target_session_id,
                req.role,
                demote_to=req.demote_to,
            )
            # chats.chat_grant_role already lazily invalidates the cache, but
            # belt-and-suspenders: _inval here covers the case where the lazy
            # import inside chats.py failed silently (fail-open contract).
            _inval(req.target_session_id)
            if req.role == chats.ROLE_MASTER:
                # Master-swap demotes a second session whose id isn't in req.
                # Clear the entire cache rather than miss the demoted session.
                try:
                    from .themis import clear_role_cache

                    clear_role_cache()
                except Exception:
                    pass
            return result
        except ValueError as exc:
            msg = str(exc)
            if "not the master" in msg:
                code = 403
            elif "only accepted members" in msg or "Invalid role" in msg:
                code = 422
            elif "not in" in msg:
                code = 422
            else:
                code = 404
            raise fastapi.HTTPException(code, msg) from exc

    @router.delete("/chats/{chat_id}")
    async def delete_chat(
        chat_id: str,
        # B.1: by_session_id kept as query-param fallback; removed at B.2.
        by_session_id: "str | None" = None,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        actor = _actor_or_body(
            _actor, by_session_id or "", f"/chats/{chat_id} (DELETE)"
        )
        if not actor:
            raise fastapi.HTTPException(
                401, "X-Session-ID header required (or by_session_id query param in B.1)"
            )
        try:
            return chats.delete(chat_id, actor)
        except ValueError as exc:
            msg = str(exc)
            code = 403 if "creator" in msg else 404
            raise fastapi.HTTPException(code, msg) from exc

    @router.get("/chats/{chat_id}/roster-progress")
    async def roster_progress(
        chat_id: str,
        session_id: str,
        _actor: "str | None" = fastapi.Depends(require_actor),
    ) -> dict:
        """Observable-truth aggregator for roster member work state.

        Returns per-member status derived from observable signals (disk-WIP,
        task state, done-reports) rather than the manual status string. When
        manual status and observable signals disagree, both are surfaced —
        the disagreement is the stale-status signal.
        """
        actor = _actor_or_body(_actor, session_id, f"/chats/{chat_id}/roster-progress")
        try:
            return {"members": chats.roster_progress(chat_id, actor)}
        except ValueError as exc:
            raise fastapi.HTTPException(403, str(exc)) from exc

    return router
