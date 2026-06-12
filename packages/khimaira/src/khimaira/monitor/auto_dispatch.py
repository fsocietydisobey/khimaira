"""#14 Auto-dispatch: periodic sweep proposing idle-agent → backlog-task assignments.

Hybrid A+ mode:
  1. Sweep finds idle+available agents AND unassigned/assigned-no-BEGIN pending tasks.
  2. Ranks (task, agent) pairs by Themis-enforced role-fit.
  3. Proposes each pair to master via session notice.
  4. TTL fallback after _PROPOSAL_TTL_S:
     - assigned-no-BEGIN (low-risk): auto-fire BEGIN via signal_task_start as master.
     - unassigned (high-risk):  re-escalate notice to master.

Opt-out: KHIMAIRA_AUTO_DISPATCH=0
Interval: KHIMAIRA_AUTO_DISPATCH_S       (default 90)
Proposal TTL: KHIMAIRA_AUTO_DISPATCH_TTL_S (default 120)
Idle minimum: KHIMAIRA_AUTO_DISPATCH_IDLE_S (default 60)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

_AUTO_DISPATCH_INTERVAL_S = float(os.environ.get("KHIMAIRA_AUTO_DISPATCH_S", "90"))
_PROPOSAL_TTL_S = float(os.environ.get("KHIMAIRA_AUTO_DISPATCH_TTL_S", "120"))
_IDLE_MIN_S = float(os.environ.get("KHIMAIRA_AUTO_DISPATCH_IDLE_S", "60"))
_HEARTBEAT_MAX_AGE_S = 300.0  # SSE heartbeat must be within this window

# In-memory proposal registry — daemon-scoped, survives across sweeps.
# Key: task_id.  Value: {ts, agent_id, chat_id, risk, escalated}.
_PENDING_PROPOSALS: dict[str, dict[str, Any]] = {}

# Role → keywords found in task body that signal role-fit.
_ROLE_KEYWORDS: dict[str, list[str]] = {
    "frontend-lead": ["frontend", "ui", "react", "css", "tailwind", "component", "tsx", "jsx"],
    "jp-frontend-lead": ["frontend", "ui", "react", "css", "tailwind", "component", "tsx", "jsx"],
    "backend-lead": ["backend", "api", "endpoint", "route", "fastapi", "server", "auth"],
    "jp-backend-lead": ["backend", "api", "endpoint", "route", "fastapi", "server", "auth"],
    "data-lead": ["database", "query", "schema", "migration", "postgres", "sql", "data"],
    "jp-data-lead": ["database", "query", "schema", "migration", "postgres", "sql", "data"],
    "architect": ["design", "architecture", "system", "refactor", "pattern", "structure"],
    "analyst": ["analyze", "research", "investigate", "report", "audit"],
}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def _env_auto_dispatch_enabled() -> bool:
    return os.environ.get("KHIMAIRA_AUTO_DISPATCH", "1") != "0"


# ---------------------------------------------------------------------------
# Rail #1 — Backlog scan
# ---------------------------------------------------------------------------

def _get_backlog_tasks() -> list[dict[str, Any]]:
    """Return TASK_PENDING tasks that have no TASK_SIGNAL start (BEGIN not fired).

    Each entry:
      {task_id, chat_id, body, assignee_id, risk}

    risk="unassigned"        — no assignee_id set (high-risk: can't auto-BEGIN)
    risk="assigned_no_begin" — assignee_id set but no start signal (low-risk: auto-BEGIN ok)

    Fail-open: read errors for a chat are silently skipped.
    """
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return []

        backlog: list[dict[str, Any]] = []
        for chat_path in chat_dir.glob("chat-*.jsonl"):
            chat_id = chat_path.stem
            try:
                tasks: dict[str, dict[str, Any]] = {}
                for line in chats_mod._read(chat_id):
                    k = line.get("kind")
                    if k == chats_mod.TASK:
                        tid = line.get("id")
                        if tid:
                            tasks[tid] = {
                                "task_id": tid,
                                "chat_id": chat_id,
                                "body": line.get("body", ""),
                                "assignee_id": line.get("assignee_id"),
                                "status": line.get("status"),
                                "begin_fired": False,
                            }
                    elif k == chats_mod.TASK_UPDATE:
                        tid = line.get("task_id")
                        if tid and tid in tasks:
                            tasks[tid]["status"] = line.get("status")
                    elif k == chats_mod.TASK_SIGNAL and line.get("signal") == "start":
                        tid = line.get("task_id")
                        if tid and tid in tasks:
                            tasks[tid]["begin_fired"] = True

                for task in tasks.values():
                    if task.get("status") != chats_mod.TASK_PENDING:
                        continue
                    if task.get("begin_fired"):
                        continue
                    risk = (
                        "unassigned"
                        if task.get("assignee_id") is None
                        else "assigned_no_begin"
                    )
                    backlog.append(
                        {
                            "task_id": task["task_id"],
                            "chat_id": task["chat_id"],
                            "body": task["body"],
                            "assignee_id": task.get("assignee_id"),
                            "risk": risk,
                        }
                    )
            except Exception:
                continue

        return backlog
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Rail #2 — Available-agent scan
# ---------------------------------------------------------------------------

def _session_state_dir(session_id: str) -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "khimaira" / "sessions" / session_id


def _session_heartbeat_fresh(session_id: str) -> bool:
    """Return True if status.json records a recent SSE heartbeat."""
    status_path = _session_state_dir(session_id) / "status.json"
    try:
        data = json.loads(status_path.read_text())
        hb = data.get("last_sse_heartbeat")
        if hb is None:
            return False
        return time.time() - float(hb) < _HEARTBEAT_MAX_AGE_S
    except Exception:
        return False


def _get_available_agents() -> list[dict[str, Any]]:
    """Return sessions that are alive, idle/listening, fresh heartbeat, no obligations.

    Uses resolve_session_role (Themis-layer, not on-disk member_roles) for the role field.
    Fail-open: per-session errors are skipped; returns an empty list on global failure.
    """
    try:
        from khimaira.monitor import sessions as sessions_mod
        from khimaira.monitor.api.chats import (
            _is_process_alive_for_session,
            _get_session_obligations,
        )
        from khimaira.monitor.api.themis import resolve_session_role

        rows = sessions_mod.list_sessions(use_cache=False)
        agents: list[dict[str, Any]] = []

        for row in rows:
            sid = row.get("session_id")
            if not sid:
                continue

            # Must be idle or listening
            status = row.get("status") or ""
            if status not in ("idle", "listening"):
                continue

            # Must have been idle long enough to not interrupt active work
            idle_s = float(row.get("last_active_age_s") or 0)
            if idle_s < _IDLE_MIN_S:
                continue

            # Process must not be confirmed-dead (None = unknown → allow)
            if _is_process_alive_for_session(sid) is False:
                continue

            # SSE heartbeat must be fresh (session is still running)
            if not _session_heartbeat_fresh(sid):
                continue

            # No existing obligations (pending or in-progress tasks)
            try:
                if _get_session_obligations(sid):
                    continue
            except Exception:
                continue

            # Themis-enforced role — the correct layer per #14 spec
            try:
                role = resolve_session_role(sid)
            except Exception:
                role = None

            agents.append(
                {
                    "session_id": sid,
                    "role": role,
                    "idle_s": idle_s,
                    "status": status,
                    "name": row.get("name"),
                }
            )

        return agents
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Rail #3 — Role-fit ranking
# ---------------------------------------------------------------------------

def _role_fit_score(role: str | None, task_body: str) -> int:
    """Return 0-2 fit score for (agent role, task body).

    2 — role has matching keywords in the task body
    1 — generic "agent" role (can execute any task)
    0 — no match
    """
    if not role:
        return 0
    keywords = _ROLE_KEYWORDS.get(role, [])
    if keywords and any(kw in task_body.lower() for kw in keywords):
        return 2
    if role == "agent" or role.endswith("-agent"):
        return 1
    return 0


def _proposal_expired(proposal: dict[str, Any]) -> bool:
    return time.time() - proposal["ts"] > _PROPOSAL_TTL_S


def _rank_task_agent_pairs(
    tasks: list[dict[str, Any]],
    agents: list[dict[str, Any]],
) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """Return (task, agent) pairs ranked by role-fit, skipping already-proposed tasks.

    For assigned-no-BEGIN tasks there is no candidate agent (just needs BEGIN), so
    agent is an empty dict — the caller handles these via TTL escalation, not proposal.
    New proposals only go to master once per TTL window.
    """
    pairs: list[tuple[dict[str, Any], dict[str, Any], int]] = []
    for task in tasks:
        task_id = task["task_id"]

        # Already have a live (non-expired) proposal — do not re-propose
        existing = _PENDING_PROPOSALS.get(task_id)
        if existing and not _proposal_expired(existing):
            continue

        if task["risk"] == "assigned_no_begin":
            # No agent-ranking needed — we just need master to fire BEGIN
            pairs.append((task, {}, -1))
            continue

        # Rank available agents by fit, pick the best
        best_agent: dict[str, Any] = {}
        best_score = -1
        for agent in agents:
            score = _role_fit_score(agent.get("role"), task.get("body", ""))
            if score > best_score:
                best_score = score
                best_agent = agent

        if best_agent:
            pairs.append((task, best_agent, best_score))
        # If no agents available, don't emit a proposal (nothing to assign to)

    pairs.sort(key=lambda x: x[2], reverse=True)
    return [(t, a) for t, a, _ in pairs]


# ---------------------------------------------------------------------------
# Rail #4 — Dispatch proposal
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Master-drive wake (2026-06-10) — the master-side analog of roster_recovery's
# worker auto-wake. Closes IDLE-ROSTER BLINDNESS (muther GAP report): master is
# purely event-driven, so when the roster goes idle with owed work undispatched,
# master sits passively and the user becomes the scheduler. Conservative trigger:
# wake master ONLY when there is concrete owed work (backlog) AND the master
# session has itself been idle past the threshold. No owed work → never wakes.
# ---------------------------------------------------------------------------

_MASTER_WAKE_ENABLED = os.environ.get("KHIMAIRA_MASTER_WAKE", "1") != "0"
_MASTER_WAKE_IDLE_MIN_S = float(os.environ.get("KHIMAIRA_MASTER_WAKE_IDLE_S", "180"))
_MASTER_WAKE_COOLDOWN_S = float(os.environ.get("KHIMAIRA_MASTER_WAKE_COOLDOWN_S", "300"))
_last_master_wake: dict[str, float] = {}


async def _maybe_wake_idle_master(
    master_id: str, owed_count: int, committable: list[str] | None = None,
    master_name: str = "",
) -> None:
    """Nudge an idle master's window to DRIVE when concrete work is owed.

    Owed work is EITHER dispatch backlog (owed_count) OR commit-ready tasks
    (committable: reviewed dual-positive, awaiting the master's commit). The
    commit-ready case is the level-triggered backstop for muther GAP #1 — if the
    edge-event wake at verdict-completion was suppressed, this catches the stranded
    task on the next sweep. Commit-ready takes message priority since that's the
    exact stranding observed (3 reviewed tasks sat uncommitted until manual nudge).

    Gated by total-owed>0 (the Conservative trigger — never nags on a quiet
    roster), the master's own idle time, a cooldown, and a window busy-check.
    Best-effort; never raises.
    """
    committable = committable or []
    total_owed = owed_count + len(committable)
    if not _MASTER_WAKE_ENABLED or total_owed <= 0:
        return
    now = time.time()
    if now - _last_master_wake.get(master_id, 0.0) < _MASTER_WAKE_COOLDOWN_S:
        return

    loop = asyncio.get_event_loop()
    try:
        from khimaira.monitor import sessions as sessions_mod

        # summary() exposes last_active_age_s; state() does NOT (latent bug — the
        # idle gate would always read 0 and the wake would never fire).
        st = await loop.run_in_executor(None, lambda: sessions_mod.summary(master_id))
        idle_s = float((st or {}).get("last_active_age_s") or 0)
    except Exception:
        _log.warning("auto-dispatch: master-wake summary() failed", exc_info=True)
        return
    if idle_s < _MASTER_WAKE_IDLE_MIN_S:
        # F1 observability: owed work exists but master looks active — say so, so a
        # "why didn't it wake?" is a log lookup, not a forensic.
        _log.info(
            "auto-dispatch: master-wake skipped — master active (idle %.0fs < %ds), "
            "owed=%d commit_ready=%d",
            idle_s, int(_MASTER_WAKE_IDLE_MIN_S), owed_count, len(committable),
        )
        return

    try:
        from khimaira.monitor import roster_recovery as rr

        wins = await loop.run_in_executor(None, rr._discover_roster_windows)
        master_win = next((w for w in wins if w.get("role") == "master"), None)
        if master_win is None and master_name:
            # Cross-roster fallback (#19 class applied to the reconcile wake):
            # _discover_roster_windows is scoped to THIS daemon's roster, so a
            # reconcile for ANOTHER roster's master (one daemon, many rosters)
            # finds 0 windows. Look the exact master up unscoped by name — strips
            # kitty's ✳ activity-marker too. This is what let muther's commit-wake
            # skip "no master window" while her window was live.
            master_win = await loop.run_in_executor(
                None, rr._window_for_session_name, master_name
            )
        if master_win is None:
            _log.info(
                "auto-dispatch: master-wake skipped — no master window discoverable "
                "(%d roster windows, name=%r, owed=%d commit_ready=%d).",
                len(wins), master_name, owed_count, len(committable),
            )
            return
        wid = master_win["window_id"]
        screen = await loop.run_in_executor(None, lambda: rr._get_screen(wid))
        if screen is not None and rr._is_busy(screen):
            _log.info("auto-dispatch: master-wake skipped — master window busy")
            return

        if committable:
            shown = ", ".join(committable[:8])
            more = "" if len(committable) <= 8 else f" (+{len(committable) - 8} more)"
            text = (
                f"⏰ {len(committable)} reviewed task(s) — critic=approve + verifier=ship "
                f"are RECORDED and awaiting your COMMIT + approve: {shown}{more}. Call "
                "chat_my_chats + roster_progress, COMMIT each, then approve "
                "(chat_task_update → approved). Don't wait for an event — this IS it."
            )
        else:
            text = (
                f"⏰ auto-dispatch: roster idle with {owed_count} owed item(s) and no "
                "driver. DRIVE now — call roster_progress + chat_my_chats, then dispatch "
                "the next item (assign + BEGIN) or, if nothing is actionable, surface "
                "'roster idle — here's what's next' to Joseph. Don't wait for an event."
            )
        ok = await loop.run_in_executor(
            None,
            lambda: rr._inject_text_and_submit(wid, text, master_win.get("raw_name", "")),
        )
        if ok:
            _last_master_wake[master_id] = now
            _log.info(
                "auto-dispatch: woke idle master %s (dispatch_owed=%d, commit_ready=%d, "
                "idle=%.0fs)",
                master_id[:8], owed_count, len(committable), idle_s,
            )
    except Exception:
        _log.warning("auto-dispatch: master-wake failed", exc_info=True)


# Per-chat master resolution: a chat's master is unambiguous (one master in its
# member_roles). The GLOBAL _resolve_session_for_role("master") aborts once >1
# session has ever held master across all chats (the normal steady state), which
# silently disabled the entire sweep — including the commit-ready backstop. The
# reconcile below resolves master PER CHAT and filters to live masters, so cross-
# roster history can't disable it.
_CHAT_MASTER_LIVE_MAX_S = float(
    os.environ.get("KHIMAIRA_RECONCILE_MASTER_LIVE_S", "3600")
)


def _active_chat_masters() -> list[tuple[str, str]]:
    """[(chat_id, master_id)] for chats whose master session is currently live
    (active within _CHAT_MASTER_LIVE_MAX_S). Per-chat member_roles resolution —
    never hits the global cross-roster ambiguity abort. Dedups by master so one
    live master with many chats isn't woken N times per sweep.
    """
    from khimaira.monitor import chats as chats_mod
    from khimaira.monitor import sessions as sessions_mod

    out: list[tuple[str, str, str]] = []
    chat_dir = chats_mod._chat_dir()
    if not chat_dir.exists():
        return out
    seen: set[str] = set()
    for path in sorted(chat_dir.glob("chat-*.jsonl")):
        chat_id = path.stem
        try:
            room = chats_mod.load_room(chat_id)
        except Exception:
            continue
        member_roles = (room.get("meta") or {}).get("member_roles") or {}
        master_id = next((s for s, r in member_roles.items() if r == "master"), None)
        if not master_id or master_id in seen:
            continue
        # #18: skip masters whose session dir no longer exists. A reaped/dead
        # master can't be woken anyway, and resolving a dead UUID forces
        # resolve_session_id's tier-4 all-chat re-parse (the read-amplification
        # that starved the sweep). Live masters keep their fast tier-1 path.
        if not (sessions_mod._BASE_DIR / master_id).is_dir():
            continue
        try:
            summ = sessions_mod.summary(master_id)
            idle_s = float((summ or {}).get("last_active_age_s") or 1e9)
        except Exception:
            continue
        if idle_s <= _CHAT_MASTER_LIVE_MAX_S:
            seen.add(master_id)
            # master_name for the cross-roster unscoped window fallback (the
            # roster-scoped _discover_roster_windows misses another roster's
            # master — muther note-2 class, applied to the reconcile wake path).
            master_name = (
                (room.get("members") or {}).get(master_id) or {}
            ).get("session_name") or ""
            out.append((chat_id, master_id, master_name))
    return out


async def _reconcile_commit_ready() -> None:
    """Per-chat commit-ready backstop (muther GAP #1, F3). For each live roster
    chat, wake its master for any dual-verdict-complete tasks awaiting commit.
    Independent of the global master resolver, so it fires even when the dispatch
    flow below can't resolve a single global master.
    """
    from khimaira.monitor import chats as chats_mod

    loop = asyncio.get_event_loop()
    try:
        chat_masters = await loop.run_in_executor(None, _active_chat_masters)
    except Exception:
        _log.warning("auto-dispatch: active-chat-master scan failed", exc_info=True)
        return
    for chat_id, master_id, master_name in chat_masters:
        try:
            committable = await loop.run_in_executor(
                None, chats_mod.committable_gate_tasks, chat_id
            )
        except Exception:
            _log.warning(
                "auto-dispatch: committable scan failed for %s", chat_id, exc_info=True
            )
            continue
        if committable:
            _log.info(
                "auto-dispatch: reconcile — chat %s has %d commit-ready task(s) "
                "for master %s",
                chat_id, len(committable), master_id[:8],
            )
            await _maybe_wake_idle_master(master_id, 0, committable, master_name=master_name)


async def _emit_dispatch_proposal(
    master_id: str,
    task: dict[str, Any],
    agent: dict[str, Any],
) -> None:
    """Post a notice to master proposing the (task, agent) assignment."""
    task_id = task["task_id"]
    body_preview = task.get("body", "")[:120]
    risk = task["risk"]

    if risk == "assigned_no_begin":
        assignee_id = task.get("assignee_id") or "unknown"
        text = (
            f"⏱ auto-dispatch: task {task_id} is assigned ({assignee_id[:8]}) "
            f"but BEGIN not fired.\n"
            f"Task: {body_preview!r}\n"
            f"Action: fire BEGIN (chat_task_signal_start) or reassign."
        )
    else:
        agent_name = agent.get("name") or (agent.get("session_id") or "?")[:8]
        agent_role = agent.get("role") or "unknown"
        idle_s = agent.get("idle_s", 0)
        text = (
            f"⏱ auto-dispatch: task {task_id} has no assignee.\n"
            f"Task: {body_preview!r}\n"
            f"Candidate: {agent_name} ({agent_role}, idle {idle_s:.0f}s)\n"
            f"Action: assign + fire BEGIN, or pick a different agent."
        )

    try:
        from khimaira.monitor import sessions as sessions_mod

        loop = asyncio.get_event_loop()
        await loop.run_in_executor(
            None,
            lambda: sessions_mod.post_notice(
                master_id,
                text,
                from_session_id="auto-dispatch-daemon",
                fire_desktop_notify=False,
            ),
        )

        _PENDING_PROPOSALS[task_id] = {
            "ts": time.time(),
            "agent_id": agent.get("session_id"),
            "chat_id": task["chat_id"],
            "risk": risk,
            "escalated": False,
        }
        _log.info(
            "auto-dispatch: proposal sent to master %s for task %s (risk=%s)",
            master_id[:8],
            task_id,
            risk,
        )
    except Exception as exc:
        _log.warning(
            "auto-dispatch: proposal failed for task %s: %s",
            task_id,
            exc,
        )


# ---------------------------------------------------------------------------
# Rail #5 — TTL fallback
# ---------------------------------------------------------------------------

async def _handle_ttl_expiry(
    master_id: str,
    task: dict[str, Any],
    proposal: dict[str, Any],
) -> None:
    """Handle an expired proposal.

    assigned_no_begin: auto-fire BEGIN using master's session ID (low-risk —
        task already has an assignee; only the go-signal is missing).
    unassigned: re-escalate with a desktop notification (high-risk — master
        must choose the assignee; daemon cannot decide for it).
    """
    task_id = task["task_id"]
    chat_id = task["chat_id"]
    risk = task["risk"]

    if risk == "assigned_no_begin":
        try:
            from khimaira.monitor import chats as chats_mod

            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: chats_mod.signal_task_start(
                    chat_id,
                    task_id,
                    master_id,
                    note=(
                        f"auto-dispatch: TTL-triggered after {_PROPOSAL_TTL_S:.0f}s "
                        f"(no master response to proposal)"
                    ),
                ),
            )
            _log.info(
                "auto-dispatch: auto-BEGIN fired for task %s in %s (master=%s)",
                task_id,
                chat_id,
                master_id[:8],
            )
            _PENDING_PROPOSALS.pop(task_id, None)
        except Exception as exc:
            _log.warning(
                "auto-dispatch: auto-BEGIN failed for task %s: %s",
                task_id,
                exc,
            )
    else:
        # Unassigned — re-escalate once, then wait for next sweep cycle
        if proposal.get("escalated"):
            return
        try:
            from khimaira.monitor import sessions as sessions_mod

            body_preview = task.get("body", "")[:120]
            text = (
                f"🚨 auto-dispatch ESCALATION: task {task_id} still unassigned "
                f"after {_PROPOSAL_TTL_S:.0f}s.\n"
                f"Task: {body_preview!r}\n"
                f"Assign an agent and fire BEGIN."
            )
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(
                None,
                lambda: sessions_mod.post_notice(
                    master_id,
                    text,
                    from_session_id="auto-dispatch-daemon",
                    fire_desktop_notify=True,
                ),
            )
            proposal["escalated"] = True
            _log.info(
                "auto-dispatch: escalated unassigned task %s to master %s",
                task_id,
                master_id[:8],
            )
        except Exception as exc:
            _log.warning(
                "auto-dispatch: escalation failed for task %s: %s",
                task_id,
                exc,
            )


# ---------------------------------------------------------------------------
# Public sweep + loop
# ---------------------------------------------------------------------------

async def auto_dispatch_sweep() -> None:
    """Single sweep: detect idle agents + backlog tasks, propose or TTL-fallback."""
    if not _env_auto_dispatch_enabled():
        return

    # Per-chat commit-ready reconcile FIRST. It resolves each live chat's master
    # from that chat's own member_roles, so it fires even when the global resolver
    # below aborts on cross-roster ambiguity (the bug that silently disabled the
    # whole sweep once >1 roster had ever existed). This is the muther GAP #1 F3
    # backstop and must not be gated behind the global master resolution.
    await _reconcile_commit_ready()

    try:
        from khimaira.monitor.roster_recovery import _resolve_session_for_role

        loop = asyncio.get_event_loop()
        master_id = await loop.run_in_executor(
            None, _resolve_session_for_role, "master"
        )
        if not master_id:
            _log.debug("auto-dispatch: no global master (dispatch flow skipped)")
            return

        # Fetch backlog + available agents concurrently
        backlog_fut = loop.run_in_executor(None, _get_backlog_tasks)
        agents_fut = loop.run_in_executor(None, _get_available_agents)
        backlog, available = await asyncio.gather(backlog_fut, agents_fut)

        if not backlog:
            _log.debug("auto-dispatch: no backlog tasks, sweep done")
            _PENDING_PROPOSALS.clear()
            return

        _log.debug(
            "auto-dispatch: backlog=%d task(s), available=%d agent(s)",
            len(backlog), len(available),
        )

        # Step 1: handle TTL expirations for existing proposals
        backlog_by_id = {t["task_id"]: t for t in backlog}
        for task_id, proposal in list(_PENDING_PROPOSALS.items()):
            if task_id not in backlog_by_id:
                # Task completed, cancelled, or moved out of backlog — prune
                _PENDING_PROPOSALS.pop(task_id, None)
                continue
            if _proposal_expired(proposal):
                await _handle_ttl_expiry(master_id, backlog_by_id[task_id], proposal)

        # Step 2: rank and emit new proposals
        pairs = _rank_task_agent_pairs(backlog, available)
        for task, agent in pairs:
            await _emit_dispatch_proposal(master_id, task, agent)

        # Step 3: if dispatch backlog remains undispatched and the master is idle,
        # WAKE it to drive. (Commit-ready owed work is handled per-chat above.)
        await _maybe_wake_idle_master(master_id, len(backlog))

    except Exception as exc:
        _log.warning("auto-dispatch: sweep error: %s", exc)


async def auto_dispatch_loop() -> None:
    """Daemon watcher loop — started at server startup."""
    _log.info(
        "auto-dispatch: loop started (interval=%.0fs, ttl=%.0fs, idle_min=%.0fs)",
        _AUTO_DISPATCH_INTERVAL_S,
        _PROPOSAL_TTL_S,
        _IDLE_MIN_S,
    )
    try:
        while True:
            await asyncio.sleep(_AUTO_DISPATCH_INTERVAL_S)
            try:
                await auto_dispatch_sweep()
            except Exception as exc:
                _log.warning("auto-dispatch: loop error: %s", exc)
    except asyncio.CancelledError:
        raise  # normal on daemon shutdown
    except BaseException as exc:
        # Observability: the loop must never die silently (it has historically —
        # the whole auto_dispatch feature can be inert with only "loop started" in
        # the log). Surface any unexpected exit loudly.
        _log.error("auto-dispatch: LOOP EXITED unexpectedly via %s: %s",
                   type(exc).__name__, exc)
        raise
