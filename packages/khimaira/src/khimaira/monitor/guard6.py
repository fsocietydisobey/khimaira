"""Guard-6 — heartbeat-liveness detector (obligation-INDEPENDENT).

Closes the gap where a roster session goes DARK (no activity) with nothing
owed — invisible to Guard-4 (requires obligation) and Guard-5 (requires open
gate). A dead observer-1 type scenario: session crashes, ows nothing, is
never detected.

FIRE CONDITION:
  session is a known roster member
  AND last_active_age_s > T_DARK
  AND NOT roster_in_wind_down

T_DARK default: 2700s (45 min).
Justification: normal idle sessions are quiet up to ~30 min between turns;
45 min is comfortably above that noise floor. Guard-4 fires at ~10-20 min but
only when sessions OWE work — no-obligation sessions can legitimately be quiet
longer. 45 min catches genuinely dead sessions (crashed, lost-in-background)
without false-positives on slow/thinking sessions. Operators can tune via
KHIMAIRA_DARK_THRESHOLD_S.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any

from khimaira.monitor.sessions import is_roster_wind_down as _is_roster_wind_down

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_T_DARK_S = float(os.environ.get("KHIMAIRA_DARK_THRESHOLD_S", str(45 * 60)))  # 45 min
_WATCH_INTERVAL_S = float(os.environ.get("KHIMAIRA_GUARD6_WATCH_S", "300"))  # 5 min sweep
_BOOTSTRAP_GRACE_S = float(os.environ.get("KHIMAIRA_GUARD6_BOOTSTRAP_S", str(5 * 60)))  # 5 min
# Disk-WIP threshold for TRAP-3 alive-but-deaf guard: how recently a task-target
# file must have been modified to count as "actively working but SSE-deaf."
# Same recoverable-default as #7: errs long (false-no-escalation is recoverable;
# false-escalation of a working session is disruptive).
_GUARD6_WIP_THRESHOLD_S = float(os.environ.get("KHIMAIRA_GUARD6_WIP_S", "900"))  # 15 min

# Scoping gate: only include members of chats that have had activity within this window.
# Prevents cross-project leakage: 16-day-old test sessions, jeevy_portal jp-* sessions,
# and abandoned seats are NOT in recently-active chats → not swept by Guard-6.
# Default: 7 days. KHIMAIRA_GUARD6_CHAT_WINDOW_S for operators.
_ACTIVE_CHAT_WINDOW_S = float(os.environ.get("KHIMAIRA_GUARD6_CHAT_WINDOW_S", str(7 * 24 * 3600)))

# ---------------------------------------------------------------------------
# Debounce state
# ---------------------------------------------------------------------------

# session_id → timestamp of last dark escalation. Re-armed when session revives.
_GUARD6_DARK: dict[str, float] = {}
_GUARD6_DARK_TTL_S = 4 * 3600.0  # 4-hour fallback expiry


# ---------------------------------------------------------------------------
# Roster member discovery
# ---------------------------------------------------------------------------


def _get_roster_session_ids() -> set[str]:
    """Return the canonical set of active roster session_ids.

    Delegates to sessions.active_roster_member_ids() — the single shared predicate
    (master ruling: all guards + watcher must use ONE definition, not roll their own).
    Definition: accepted member of a recently-active chat (last-msg-ts < 7d), durable reads.

    Falls back to the recency-filter inline implementation until agent-1 exposes
    active_roster_member_ids() in sessions.py. Once that lands, this function
    automatically delegates to it with no further changes needed.

    Fail-open: returns empty set on any error.
    """
    # Try the canonical shared predicate first (agent-1's sessions.py function)
    try:
        from khimaira.monitor import sessions as sessions_mod
        result = sessions_mod.active_roster_member_ids()
        log.info("guard6 roster source: canonical (sessions.active_roster_member_ids)")
        return result
    except AttributeError:
        log.warning(
            "guard6 roster source: FALLBACK recency-filter "
            "(sessions.active_roster_member_ids not yet available — agent-1 hasn't landed it)"
        )
    except Exception as exc:
        log.warning("guard6 roster source: FALLBACK recency-filter (canonical raised %s)", exc)

    # Fallback: recency-filter inline (same definition; runs until agent-1 lands)
    members: set[str] = set()
    now = time.time()
    try:
        from datetime import datetime, timezone

        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return members
        for path in chat_dir.glob("chat-*.jsonl"):
            try:
                room = chats_mod.load_room(path.stem)
                messages = room.get("messages", [])
                if not messages:
                    continue
                last_ts_str = messages[-1].get("ts", "")
                if last_ts_str:
                    try:
                        last_dt = datetime.fromisoformat(last_ts_str.replace("Z", "+00:00"))
                        last_ts = last_dt.timestamp()
                        if (now - last_ts) > _ACTIVE_CHAT_WINDOW_S:
                            continue  # stale chat — skip
                    except (ValueError, OSError):
                        continue
                else:
                    continue
                for sid, m in room.get("members", {}).items():
                    if m.get("state") == chats_mod.ACCEPTED:
                        members.add(sid)
            except Exception:
                continue
    except Exception:
        pass
    return members


# ---------------------------------------------------------------------------
# Reachability (reuses guard5's pattern)
# ---------------------------------------------------------------------------


def _is_reachable(session_id: str) -> bool:
    try:
        from khimaira.monitor import chats as chats_mod
        return chats_mod.is_reachable(session_id)
    except Exception:
        return True  # fail-open


# ---------------------------------------------------------------------------
# Escalation target resolution
# ---------------------------------------------------------------------------


def _find_escalation_target(session_id: str) -> str | None:
    """Return a reachable escalation target (master → coordinator → None)."""
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return None
        for path in chat_dir.glob("chat-*.jsonl"):
            try:
                room = chats_mod.load_room(path.stem)
                member_roles = (room.get("meta") or {}).get("member_roles") or {}
                for sid, role in member_roles.items():
                    if role == chats_mod.ROLE_MASTER and _is_reachable(sid):
                        return sid
            except Exception:
                continue
        # Fallback: coordinator roles
        for path in chat_dir.glob("chat-*.jsonl"):
            try:
                room = chats_mod.load_room(path.stem)
                member_roles = (room.get("meta") or {}).get("member_roles") or {}
                for sid, role in member_roles.items():
                    if role in ("intake", "data-lead") and _is_reachable(sid):
                        return sid
            except Exception:
                continue
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Escalation
# ---------------------------------------------------------------------------


async def _guard6_escalate(
    dark_session_id: str,
    dark_age_s: float,
    session_name: str,
    session_role: str | None,
) -> None:
    """Post a dark-session liveness alert."""
    age_min = dark_age_s / 60
    role_label = session_role or "unknown"
    name_label = session_name or dark_session_id[:8]

    body = (
        f"🫀 Guard-6 dark-session: {name_label} (role={role_label}) "
        f"has been DARK for {age_min:.0f} min with no obligation "
        f"(session_id={dark_session_id[:8]}). "
        f"Likely dead — respawn or drop from roster."
    )

    target = _find_escalation_target(dark_session_id)
    log.warning(
        "guard6: dark session %s (role=%s) silent %d min — escalating to %s",
        dark_session_id[:8], role_label, int(age_min), target or "NONE"
    )

    # Post to any chat this session is in
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if chat_dir.exists():
            for path in chat_dir.glob("chat-*.jsonl"):
                try:
                    room = chats_mod.load_room(path.stem)
                    if dark_session_id in room.get("members", {}):
                        if room["members"][dark_session_id].get("state") == chats_mod.ACCEPTED:
                            from khimaira.monitor.chats import _post_synthetic_message
                            await _post_synthetic_message(path.stem, body)
                            break
                except Exception:
                    continue
    except Exception:
        pass

    if target:
        try:
            from khimaira.monitor import sessions as sessions_mod
            sessions_mod.post_notice(
                target_session_id=target,
                text=body,
                from_session_id="khimaira-daemon",
            )
        except Exception:
            pass
    else:
        log.warning("guard6: no reachable escalation target for dark session %s", dark_session_id[:8])
        try:
            from khimaira.monitor import sessions as sessions_mod
            sessions_mod.post_handoff(
                from_session_id="khimaira-daemon",
                text=f"⚠️ Guard-6: {name_label} (role={role_label}) DARK {age_min:.0f} min, no obligation, zero reachable escalation targets.",
                scope_cwd=None,
                expires_in_hours=8,  # machine alert, not a human handoff
            )
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Main sweep
# ---------------------------------------------------------------------------


async def _guard6_check_once() -> None:
    """One sweep: find dark roster members with no recent activity."""
    if _is_roster_wind_down():
        return

    now = time.time()

    # Expire stale debounce entries
    expired = [sid for sid, ts in _GUARD6_DARK.items() if now - ts > _GUARD6_DARK_TTL_S]
    for sid in expired:
        _GUARD6_DARK.pop(sid, None)

    # Get all sessions with their last_active_age_s
    try:
        from khimaira.monitor import sessions as sessions_mod
        all_sessions = sessions_mod.list_sessions(use_cache=True)
    except Exception:
        return

    session_map: dict[str, dict] = {s["session_id"]: s for s in all_sessions if s.get("session_id")}

    # Get roster members
    roster_ids = _get_roster_session_ids()
    if not roster_ids:
        return

    for sid in roster_ids:
        row = session_map.get(sid)
        if row is None:
            continue  # session not in known-sessions — too new or external

        last_age_s = row.get("last_active_age_s") or 0
        if last_age_s < _T_DARK_S:
            # Session recently active — if it was dark before, re-arm debounce
            if sid in _GUARD6_DARK:
                log.info("guard6: session %s revived (age %ds) — debounce re-armed", sid[:8], int(last_age_s))
                _GUARD6_DARK.pop(sid, None)
            continue

        # Skip sessions in bootstrap window (no activity yet doesn't mean dark)
        joined_age_s = row.get("last_active_age_s") or 0  # use as proxy
        if joined_age_s < _BOOTSTRAP_GRACE_S:
            continue

        # Reachability gate: a session with an open SSE connection is ALIVE (just idle),
        # not dark. "Dark" = inactive (last_active>T_DARK) AND unreachable (no SSE stream).
        # Without this check, any idle-but-connected session false-dark-flags after 45min
        # (holding/awaiting-gate sessions, architect/analyst between bursts) — false alarms
        # that undermine Guard-6's purpose.
        if _is_reachable(sid):
            if sid in _GUARD6_DARK:
                log.info("guard6: session %s now reachable — debounce re-armed", sid[:8])
                _GUARD6_DARK.pop(sid, None)
            continue

        # Disk-WIP guard (TRAP-3 — roster-identity Phase-B Part D).
        # An alive-but-SSE-deaf session has stale last_active but IS editing:
        # the #7 disk-WIP probe reads owed-task target-file mtimes directly
        # (hook-independent, per-session-precise via owed-task-target-files —
        # never a shared-cwd workspace scan). SHARED fn: reap + wake use ONE
        # attribution fn (_session_has_recent_wip, 740bc1d).
        # Recoverable-default: err toward NOT escalating (false-no-escalation
        # self-heals next cycle; false-escalation of a working session is disruptive).
        try:
            from pathlib import Path
            from khimaira.monitor.roster_recovery import (
                _get_session_active_task_body,
                _session_has_recent_wip,
            )

            task_body = _get_session_active_task_body(sid)
            has_wip = _session_has_recent_wip(
                sid, task_body, Path.cwd(), _GUARD6_WIP_THRESHOLD_S
            )
            if has_wip:
                log.debug(
                    "guard6: session %s has recent disk-WIP — alive-but-deaf, skip dark escalation",
                    sid[:8],
                )
                _GUARD6_DARK.pop(sid, None)  # re-arm if previously flagged
                continue
        except Exception:
            pass  # fail-open: probe failure → proceed with escalation (conservative)

        # Dark session detected — debounce: one alert per session
        if sid in _GUARD6_DARK:
            continue  # already alerted

        _GUARD6_DARK[sid] = now

        session_name = row.get("name") or sid[:8]
        try:
            from khimaira.monitor.api.themis import resolve_session_role
            session_role = resolve_session_role(sid)
        except Exception:
            session_role = None

        await _guard6_escalate(sid, last_age_s, session_name, session_role)


async def guard6_loop() -> None:
    """Background loop: sweep for dark sessions every _WATCH_INTERVAL_S."""
    log.info("guard6: liveness monitor started (T_DARK=%ds, interval=%ds)", int(_T_DARK_S), int(_WATCH_INTERVAL_S))
    while True:
        try:
            await _guard6_check_once()
        except Exception:
            log.exception("guard6: sweep error")
        await asyncio.sleep(_WATCH_INTERVAL_S)
