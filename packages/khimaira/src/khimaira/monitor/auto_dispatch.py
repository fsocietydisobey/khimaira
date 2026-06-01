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

    try:
        from khimaira.monitor.roster_recovery import _resolve_session_for_role

        loop = asyncio.get_event_loop()
        master_id = await loop.run_in_executor(
            None, _resolve_session_for_role, "master"
        )
        if not master_id:
            _log.debug("auto-dispatch: no master session found, skipping sweep")
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
            len(backlog),
            len(available),
        )

        # Step 1: handle TTL expirations for existing proposals
        backlog_by_id = {t["task_id"]: t for t in backlog}
        for task_id, proposal in list(_PENDING_PROPOSALS.items()):
            if task_id not in backlog_by_id:
                # Task completed, cancelled, or moved out of backlog — prune
                _PENDING_PROPOSALS.pop(task_id, None)
                continue
            if _proposal_expired(proposal):
                await _handle_ttl_expiry(
                    master_id, backlog_by_id[task_id], proposal
                )

        # Step 2: rank and emit new proposals
        pairs = _rank_task_agent_pairs(backlog, available)
        for task, agent in pairs:
            await _emit_dispatch_proposal(master_id, task, agent)

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
    while True:
        await asyncio.sleep(_AUTO_DISPATCH_INTERVAL_S)
        try:
            await auto_dispatch_sweep()
        except Exception as exc:
            _log.warning("auto-dispatch: loop error: %s", exc)
