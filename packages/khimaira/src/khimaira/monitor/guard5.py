"""Guard-5 — roster-progress monitor.

Catches the emergent roster-level standstill that no per-session watcher can
see: the whole roster is idle behind an open BLOCKING GATE (an unstarted task,
a done-awaiting-verdict task, or a Part-A review-task) that nobody is
progressing.

Guard-4 watches individual obligations. Guard-5 watches the ROSTER — it asks
"are ≥K sessions idle AND is there a gate with no state-change >T_stall?"  If
yes, it escalates to a REACHABLE target (same-role → master → coordinator →
loud-log-and-handoff) with specifics.

FIRE CONDITION (flat-K v1):
  (≥K sessions in {idle,listening,awaiting-review}, no obligation-progress)
  AND (≥1 OPEN BLOCKING GATE, no state-change >T_stall)
  AND NOT roster_in_wind_down

PRECISION GUARD: only fires when an open blocking gate EXISTS.  K-idle with
no gate = healthy lull → STAY QUIET.

Reachability (Part A sync-point):
  chats.is_reachable(session_id) → bool  (agent-4 exposes this in chats.py).
  Until that interface is merged, _is_reachable() delegates there and stubs
  gracefully to True-for-all if the symbol is absent.

Suppress escalation during:
  - Declared roster wind-down   (_ROSTER_WIND_DOWN flag)
  - Bootstrap window            (reuses BOOTSTRAP_GRACE_TURNS from conditions)
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from khimaira.monitor.sessions import (
    is_roster_wind_down as _is_roster_wind_down_shared,
    set_roster_wind_down as _set_roster_wind_down_shared,
)

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration — all environment-overridable
# ---------------------------------------------------------------------------

# Number of idle sessions that must be seen before firing (flat-K v1).
_K_IDLE = int(os.environ.get("KHIMAIRA_GUARD5_K_IDLE", "2"))

# Seconds a blocking gate must be stale (no state-change) before escalation.
_T_STALL_S = float(os.environ.get("KHIMAIRA_GUARD5_T_STALL_S", str(8 * 60)))  # 8 min

# Sweep interval.
_WATCH_INTERVAL_S = float(os.environ.get("KHIMAIRA_GUARD5_WATCH_S", "90"))

# Bootstrap grace: sessions with fewer than this many tool calls are exempt
# from unreachable classification (mirrors conditions.BOOTSTRAP_GRACE_TURNS).
_BOOTSTRAP_GRACE_TURNS = 3

# ---------------------------------------------------------------------------
# Shared daemon state
# ---------------------------------------------------------------------------

# Debounce: gate_key → timestamp of last escalation.  Re-armed when the gate
# state-changes (verdict posted / task status changes / new event).
# gate_key = (chat_id, task_id)
_GUARD5_STALLED: dict[tuple[str, str], float] = {}
_GUARD5_STALLED_TTL_S = 2 * 3600.0  # 2-hour fallback expiry


def set_wind_down(active: bool) -> None:
    """Daemon API: enter/exit roster wind-down mode. Both Guard-4 and Guard-5
    check the shared flag before escalating (Guard-5 Part A: implement once,
    consume everywhere — flag lives in sessions.py)."""
    _set_roster_wind_down_shared(active)
    log.info("guard5: roster wind-down = %s", active)


def is_wind_down() -> bool:
    return _is_roster_wind_down_shared()


# ---------------------------------------------------------------------------
# Reachability (Part A sync-point)
# ---------------------------------------------------------------------------


def _is_reachable(session_id: str) -> bool:
    """True iff the session has a live SSE connection open at the daemon.

    Delegates to chats.is_reachable() once agent-4 exposes it (Part A
    sync-point).  Falls back to True-for-all (fail-open — we never want to
    suppress escalation to someone who IS reachable just because the signal
    is temporarily absent).
    """
    try:
        from khimaira.monitor import chats as chats_mod

        return chats_mod.is_reachable(session_id)
    except AttributeError:
        # agent-4's Part A not yet merged — accept all (fail-open).
        return True
    except Exception:
        return True


def _is_bootstrap_session(session_id: str) -> bool:
    """True for sessions that haven't completed their bootstrap window yet."""
    try:
        from khimaira.monitor import sessions as sessions_mod

        session_dir = sessions_mod._BASE_DIR / session_id
        tc_path = session_dir / "tool_calls.jsonl"
        if not tc_path.exists():
            return True  # no tool calls yet = bootstrapping
        count = sum(1 for _ in tc_path.open())
        return count < _BOOTSTRAP_GRACE_TURNS
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Blocking gate scanner
# ---------------------------------------------------------------------------

# Status values that count as "gate open / obligation-not-progressing".
_OPEN_GATE_STATUSES = {
    "pending",       # assigned-no-BEGIN
    "in_progress",   # active but may have stalled
    "done",          # done-awaiting-verdict (B3 gate not cleared)
}

# Terminal statuses — gate is closed.
_CLOSED_GATE_STATUSES = {
    "approved",
    "changes_requested",
    "cancelled",
}


def _scan_blocking_gates() -> list[dict[str, Any]]:
    """Scan all chat JSONLs for open blocking gates.

    Returns list of dicts: {task_id, chat_id, status, assignee_id,
    assignee_role, last_event_ts, has_verdict, begin_fired, preview}.

    A "blocking gate" for Guard-5's purposes is any task in a non-terminal
    status that has had no state-change for >T_stall.  We return ALL open
    gates and let the caller decide which are stale.

    Fail-open: errors scanning a single chat are silently skipped.
    """
    gates: list[dict[str, Any]] = []
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return []

        for chat_path in chat_dir.glob("chat-*.jsonl"):
            chat_id = chat_path.stem
            try:
                _scan_chat_for_gates(chat_id, chats_mod, gates)
            except Exception:
                continue
    except Exception:
        pass
    return gates


def _scan_chat_for_gates(
    chat_id: str, chats_mod: Any, gates: list[dict[str, Any]]
) -> None:
    """Fold one chat JSONL into gate records (mutates gates in place)."""
    tasks: dict[str, dict[str, Any]] = {}
    last_verdict_ts: dict[str, str] = {}
    last_state_change_ts: dict[str, str] = {}

    for line in chats_mod._read(chat_id):
        kind = line.get("kind")
        ts = line.get("ts", "")
        if kind == chats_mod.TASK:
            tid = line.get("id")
            if not tid:
                continue
            tasks[tid] = {
                "task_id": tid,
                "chat_id": chat_id,
                "status": chats_mod.TASK_PENDING,
                "assignee_id": line.get("assignee_id"),
                "assignee_role": line.get("assignee_role"),
                "preview": (line.get("body") or "")[:120],
                "begin_fired": False,
                "has_verdict": False,
                "last_event_ts": ts,
            }
            last_state_change_ts[tid] = ts
        elif kind == chats_mod.TASK_UPDATE:
            tid = line.get("task_id")
            if tid and tid in tasks:
                new_status = line.get("new_status") or line.get("status")
                if new_status:
                    tasks[tid]["status"] = new_status
                    last_state_change_ts[tid] = ts
                tasks[tid]["last_event_ts"] = ts
        elif kind == chats_mod.TASK_SIGNAL:
            tid = line.get("task_id")
            if tid and tid in tasks:
                if line.get("signal") == "start":
                    tasks[tid]["begin_fired"] = True
                    last_state_change_ts[tid] = ts
                tasks[tid]["last_event_ts"] = ts
        elif kind == chats_mod.TASK_VERDICT:
            tid = line.get("task_id")
            if tid and tid in tasks:
                tasks[tid]["has_verdict"] = True
                last_verdict_ts[tid] = ts
                last_state_change_ts[tid] = ts

    for tid, task in tasks.items():
        if task["status"] in _CLOSED_GATE_STATUSES:
            continue
        if task["status"] not in _OPEN_GATE_STATUSES:
            continue
        # done + has_verdict = gate cleared (both signals present)
        if task["status"] == "done" and task["has_verdict"]:
            # Cheaply check if critic+verifier both gave a verdict.
            # Full check is expensive; a "has any verdict" heuristic is enough
            # to avoid over-escalating — the gate check below supplements this.
            # We leave done+has_verdict in for the caller to evaluate staleness.
            pass
        task["last_state_change_ts"] = last_state_change_ts.get(tid, task["last_event_ts"])
        gates.append(task)


def _gate_is_stale(gate: dict[str, Any], now_ts: float) -> bool:
    """True if the gate has had no state-change for >T_stall seconds."""
    last_ts_str = gate.get("last_state_change_ts") or gate.get("last_event_ts") or ""
    if not last_ts_str:
        return True
    try:
        import datetime

        # Parse ISO-8601 (handles both Z and +00:00)
        ts_str = last_ts_str.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(ts_str)
        age_s = now_ts - dt.timestamp()
        return age_s > _T_STALL_S
    except Exception:
        return False  # can't parse — don't escalate


# ---------------------------------------------------------------------------
# Idle-session scanner
# ---------------------------------------------------------------------------

_IDLE_STATUSES = {"idle", "listening", "awaiting-review", ""}


def _count_idle_sessions(
    session_rows: list[dict[str, Any]],
) -> tuple[int, list[str]]:
    """Return (count_idle, list_of_idle_session_ids).

    A session is "idle for Guard-5 purposes" when:
      - Its effective_status is in _IDLE_STATUSES
      - It is NOT in bootstrap window (has stamped enough tool calls)
      - It is reachable (has open SSE connection)
    """
    idle_ids: list[str] = []
    for row in session_rows:
        sid = row.get("session_id")
        if not sid:
            continue
        status_obj = row.get("status") or {}
        eff_status = status_obj.get("effective_status") or ""
        if eff_status not in _IDLE_STATUSES:
            continue
        if _is_bootstrap_session(sid):
            continue
        if not _is_reachable(sid):
            continue
        idle_ids.append(sid)
    return len(idle_ids), idle_ids


# ---------------------------------------------------------------------------
# Reachable-target resolution (re-target order per analyst spec)
# ---------------------------------------------------------------------------


def _find_reachable_master(chat_id: str) -> str | None:
    """Return the master's session_id in this chat if reachable, else None."""
    try:
        from khimaira.monitor import chats as chats_mod

        room = chats_mod.load_room(chat_id)
        member_roles = (room.get("meta") or {}).get("member_roles") or {}
        for sid, role in member_roles.items():
            if role == "master" and _is_reachable(sid):
                return sid
    except Exception:
        pass
    return None


def _find_reachable_coordinator(chat_id: str) -> str | None:
    """Return a reachable intake or data-lead in this chat, else None."""
    try:
        from khimaira.monitor import chats as chats_mod

        room = chats_mod.load_room(chat_id)
        member_roles = (room.get("meta") or {}).get("member_roles") or {}
        for sid, role in member_roles.items():
            if role in ("intake", "data-lead") and _is_reachable(sid):
                return sid
    except Exception:
        pass
    return None


def _find_reachable_same_role(
    gate: dict[str, Any], session_rows: list[dict[str, Any]]
) -> str | None:
    """Return a reachable session holding the same assignee_role, if any."""
    assignee_role = gate.get("assignee_role")
    if not assignee_role:
        return None
    try:
        from khimaira.monitor import chats as chats_mod

        room = chats_mod.load_room(gate["chat_id"])
        member_roles = (room.get("meta") or {}).get("member_roles") or {}
        for sid, role in member_roles.items():
            if role == assignee_role and _is_reachable(sid):
                # Don't re-target to the same session that's stuck.
                if sid != gate.get("assignee_id"):
                    return sid
    except Exception:
        pass
    return None


def _resolve_escalation_target(
    gate: dict[str, Any], session_rows: list[dict[str, Any]]
) -> str | None:
    """Re-target order per analyst spec:
    1. Another reachable holder of the same role-class
    2. Reachable master (reassign or master_override_verdict)
    3. Reachable coordinator (intake, data-lead)
    4. None → caller logs loud + posts handoff
    """
    same_role = _find_reachable_same_role(gate, session_rows)
    if same_role:
        return same_role
    master = _find_reachable_master(gate["chat_id"])
    if master:
        return master
    coordinator = _find_reachable_coordinator(gate["chat_id"])
    return coordinator  # may be None → caller handles terminal case


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


async def _guard5_escalate(
    gate: dict[str, Any],
    k_idle: int,
    target_session_id: str | None,
) -> None:
    """Post a roster-level stall escalation for the given blocking gate."""
    task_id = gate["task_id"]
    chat_id = gate["chat_id"]
    status = gate["status"]
    assignee_role = gate.get("assignee_role") or ""
    assignee_id = gate.get("assignee_id") or ""
    preview = (gate.get("preview") or "")[:80]
    last_ts = gate.get("last_state_change_ts") or gate.get("last_event_ts") or "?"

    owner_label = assignee_role or assignee_id[:8] or "?"

    unreachable_note = ""
    if assignee_id and not _is_reachable(assignee_id):
        unreachable_note = (
            f" (owner {assignee_id[:8]} UNREACHABLE — SSE connection absent; re-targeting)"
        )

    body = (
        f"🚦 Guard-5 roster-stall: {k_idle} session(s) idle but gate "
        f"[task-{task_id[:8]} / {status}]{unreachable_note} has no state-change since "
        f"{last_ts} (>{_T_STALL_S/60:.0f} min). Owner-role: {owner_label}. "
        f"Preview: {preview!r}. "
        "Assign a reviewer, collapse the gate, or escalate to master."
    )

    try:
        from khimaira.monitor.chats import _post_synthetic_message

        await _post_synthetic_message(chat_id, body)
    except Exception:
        pass

    if target_session_id:
        try:
            from khimaira.monitor import sessions as sessions_mod

            sessions_mod.post_notice(
                target_session_id=target_session_id,
                text=body,
                from_session_id="khimaira-daemon",
            )
        except Exception:
            pass

        # Path 3: a notice does NOT wake a turn-gated session. If the escalation
        # target is the chat's MASTER, also wake its window. (Same-role-peer targets
        # that owe a verdict are already woken by roster_recovery._process_window via
        # the Path-1 owed-verdict obligation — this bridge closes only the master case.)
        await _wake_master_window_on_stall(gate, k_idle, target_session_id)
    else:
        # Terminal case: no reachable target — loud log + handoff
        log.warning(
            "guard5: ROSTER-UNREACHABLE — gate %s stale, zero reachable escalation targets. "
            "Posting handoff.",
            task_id[:8],
        )
        try:
            from khimaira.monitor import sessions as sessions_mod

            sessions_mod.post_handoff(
                from_session_id="khimaira-daemon",
                text=(
                    f"⚠️ Guard-5: gate task-{task_id[:8]} stale >{_T_STALL_S/60:.0f} min, "
                    f"{k_idle} idle sessions, ZERO reachable targets. "
                    f"Chat: {chat_id}. Status: {status}. Last change: {last_ts}."
                ),
                scope_cwd=None,
                expires_in_hours=8,  # machine alert, not a human handoff — stale once the gate resolves
            )
        except Exception:
            pass


async def _wake_master_window_on_stall(
    gate: dict[str, Any], k_idle: int, target_session_id: str
) -> None:
    """If the escalation target is the chat's master, wake its window once.

    A turn-gated master receives the Guard-5 notice but never gets a turn to act on
    it (the muther stall). Bridge into auto_dispatch._maybe_wake_idle_master — which
    wraps roster_recovery's window discovery + kitty inject and carries its own
    per-master 300s cooldown + idle/busy/unreachable guards — so a confirmed stall
    actually nudges the master. We do NOT add a parallel actuator; we reuse that one.
    Fires only when the target is the master (per-chat member_roles); same-role-peer
    targets are handled by the Path-1 owed-verdict obligation. Best-effort; never
    raises.
    """
    try:
        from khimaira.monitor import chats as chats_mod

        room = chats_mod.load_room(gate["chat_id"])
        member_roles = (room.get("meta") or {}).get("member_roles") or {}
        if member_roles.get(target_session_id) != chats_mod.ROLE_MASTER:
            return  # target is a same-role peer / coordinator — not the master case

        master_name = (room.get("members", {}).get(target_session_id) or {}).get(
            "session_name", ""
        )
        task_id = gate.get("task_id") or ""
        role = gate.get("assignee_role") or "reviewer"
        wake_text = (
            f"⏰ pipeline stalled: task-{task_id[:8]} awaiting {role}'s verdict "
            f"({k_idle} session(s) idle, >{_T_STALL_S / 60:.0f} min no change). Call "
            "roster_progress + chat_my_chats, then nudge the reviewer or reassign / "
            "collapse the gate. Don't wait for an event — this IS it."
        )

        from khimaira.monitor import auto_dispatch as ad_mod

        await ad_mod._maybe_wake_idle_master(
            target_session_id,
            owed_count=1,
            master_name=master_name or "",
            chat_id=gate["chat_id"],
            wake_text=wake_text,
        )
    except Exception:
        log.debug("guard5: master-wake bridge failed", exc_info=True)


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------


async def _guard5_check_once() -> None:
    """One sweep: scan for blocking gates, count idle sessions, escalate."""
    if _is_roster_wind_down_shared():
        return

    now = time.time()

    # Sweep stale debounce entries.
    expired = [k for k, ts in _GUARD5_STALLED.items() if now - ts > _GUARD5_STALLED_TTL_S]
    for k in expired:
        _GUARD5_STALLED.pop(k, None)

    # Clear debounce when the gate has progressed (task status changed or
    # verdict posted → gate_key will no longer appear in stale gates).
    blocking_gates = _scan_blocking_gates()
    stale_gate_keys: set[tuple[str, str]] = set()
    for gate in blocking_gates:
        if _gate_is_stale(gate, now):
            stale_gate_keys.add((gate["chat_id"], gate["task_id"]))

    # Re-arm debounce for any previously-stalled gate that is no longer stale.
    cleared_keys = [k for k in list(_GUARD5_STALLED.keys()) if k not in stale_gate_keys]
    for k in cleared_keys:
        _GUARD5_STALLED.pop(k, None)

    # PRECISION GUARD: only fire when at least one stale blocking gate exists.
    if not stale_gate_keys:
        return

    # Count idle sessions.
    try:
        from khimaira.monitor import sessions as sessions_mod

        session_rows = sessions_mod.list_sessions(use_cache=True)
    except Exception:
        return

    k_idle, idle_ids = _count_idle_sessions(session_rows)

    if k_idle < _K_IDLE:
        return  # not enough idle sessions to warrant escalation

    # For each stale gate, escalate once (debounced).
    for gate in blocking_gates:
        gate_key = (gate["chat_id"], gate["task_id"])
        if gate_key not in stale_gate_keys:
            continue
        if gate_key in _GUARD5_STALLED:
            continue  # already escalated; wait for state-change to re-arm

        target = _resolve_escalation_target(gate, session_rows)
        await _guard5_escalate(gate, k_idle, target)
        _GUARD5_STALLED[gate_key] = now


async def guard5_loop() -> None:
    """Async loop wired at daemon startup (server.py)."""
    if os.environ.get("KHIMAIRA_GUARD5", "1") == "0":
        log.info("guard5: disabled via KHIMAIRA_GUARD5=0")
        return
    log.info("guard5: starting roster-progress monitor (K=%d, T_stall=%ds)", _K_IDLE, int(_T_STALL_S))
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_S)
        try:
            await _guard5_check_once()
        except Exception as exc:
            log.warning("guard5-watcher error: %s", exc)
