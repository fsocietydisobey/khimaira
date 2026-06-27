"""Guard-7 — task-delivery watchdog (idle-while-owed + cogitate-then-drop).

The daemon dispatches tasks fire-and-forget. Existing guards miss the case where an
assignee was *given a task* and then either went dark owing it, or TOOK a turn but
never advanced the task / posted the artifact ("cogitate-then-drop" — looks like it
acted). Guard-7 keys on the TASK's own clock vs the ASSIGNEE's activity and SURFACES
to a human-resolvable target deliberately (notice + chat), NOT blind window-injection
(blind injection caused over-wakes + backfill floods — that is exactly what this
replaces). SPEC: tasks/guard7-task-delivery-watchdog/SPEC.md (#32).

Three signals, per task in {pending, in_progress, done}, NOT in roster wind-down:
  1. assigned-but-dark      — task stalled > TASK_STALL  AND assignee idle > INACTIVE
  2. cogitate-then-drop     — task stalled > TASK_STALL  AND assignee idle <= INACTIVE
                              (the key NEW signal: turning, but not delivering)
  3. verdict-owed-unposted  — task done, no verdict activity > VERDICT_STALL, not yet
                              committable (a reviewer's verdict slot is empty)

Design constraints (honored): NEW FILE (no edits to manual-formatted guard5/6/
auto_dispatch); rides the proven roster_recovery.watcher_loop sweep (no new
asyncio.sleep loop — #18 freeze risk); reuses guard5's scan + target-resolution +
surfacing; env-tunable + fail-open + wind-down-suppressed + per-(task,signal) debounce.
"""

from __future__ import annotations

import datetime
import logging
import os
import time
from typing import Any

log = logging.getLogger(__name__)

# --- tunables (env, fail-open) ---------------------------------------------

_ENABLED = os.environ.get("KHIMAIRA_GUARD7", "1") == "1"
# Task not advancing (no TASK_UPDATE/SIGNAL/VERDICT) for this long → candidate.
_TASK_STALL_S = float(os.environ.get("KHIMAIRA_GUARD7_TASK_STALL_S", str(10 * 60)))
# Assignee-dark vs still-turning split (session-dir mtime age).
_INACTIVE_S = float(os.environ.get("KHIMAIRA_GUARD7_INACTIVE_S", str(15 * 60)))
# Done task with an unposted verdict for this long → reviewer owes.
_VERDICT_STALL_S = float(os.environ.get("KHIMAIRA_GUARD7_VERDICT_STALL_S", str(15 * 60)))
# UPPER bound: a task done LONGER ago than this is abandoned, not "owed" — don't
# resurrect ancient dead-roster tasks every sweep (the verdict obligation is only
# live within a recency window; without this, every old done-without-verdict task in
# every dead chat fires forever — a 43-task first-sweep burst in the live dry-run).
_VERDICT_MAX_AGE_S = float(os.environ.get("KHIMAIRA_GUARD7_VERDICT_MAX_AGE_S", str(6 * 3600)))
# Abandonment horizon for signals 1+2: a task open this long (since CREATED) is a stale
# assignment no one will finish, not a live cogitate/dark — don't nag it (the 93h-old
# "wake test" task is the case). Keyed on created_ts, not last_state_change, so an old
# task re-touched once isn't judged "young".
_ABANDON_AGE_S = float(os.environ.get("KHIMAIRA_GUARD7_ABANDON_AGE_S", str(24 * 3600)))
# Per-(chat, task, signal) debounce so a persistent stall escalates once per cooldown.
_DEBOUNCE_S = float(os.environ.get("KHIMAIRA_GUARD7_DEBOUNCE_S", str(30 * 60)))
_DEBOUNCE_TTL_S = 2 * 3600.0  # fallback expiry so the table can't grow unbounded

# (chat_id, task_id, signal) -> last-escalation epoch
_GUARD7_SEEN: dict[tuple[str, str, str], float] = {}

# Signal names (also the debounce key component + the escalation label).
SIG_DARK = "assigned-but-dark"
SIG_COGITATE = "cogitate-then-drop"
SIG_VERDICT = "verdict-owed-unposted"


# --- small helpers ----------------------------------------------------------


def _age_s(ts_str: str | None, now_ts: float) -> float | None:
    """Age in seconds of an ISO-8601 timestamp, or None if absent/unparseable.

    None is the FAIL-SAFE sentinel: a task we can't time is never escalated.
    """
    if not ts_str:
        return None
    try:
        dt = datetime.datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        return now_ts - dt.timestamp()
    except Exception:
        return None


def _assignee_idle_s(assignee_id: str | None) -> float | None:
    """Assignee's last-active age (session-dir mtime), or None if unknown.

    None = no liveness signal → fail-safe (the caller skips rather than guess).
    """
    if not assignee_id:
        return None
    try:
        from khimaira.monitor import sessions as sessions_mod

        st = sessions_mod.summary(assignee_id)
        age = (st or {}).get("last_active_age_s")
        return float(age) if age is not None else None
    except Exception:
        return None


def _chat_task_times(chat_id: str, cache: dict[str, dict[str, dict[str, str]]]) -> dict[str, dict[str, str]]:
    """Per-task {created_ts, done_ts} for a chat (folded once, cached per sweep).

    guard5's gate dict lacks these — we need created_ts for the abandonment horizon
    (signals 1+2) and done_ts (when it entered done-awaiting-verdict) for signal-3's
    recency, rather than last_state_change which any re-touch/partial-verdict advances.
    Fail-open: read errors → empty (callers fall back to last_state_change).
    """
    if chat_id in cache:
        return cache[chat_id]
    times: dict[str, dict[str, str]] = {}
    try:
        from khimaira.monitor import chats as chats_mod

        for line in chats_mod._read(chat_id):
            kind = line.get("kind")
            ts = line.get("ts", "")
            if kind == chats_mod.TASK:
                tid = line.get("id")
                if tid:
                    times[tid] = {"created_ts": ts, "done_ts": ""}
            elif kind == chats_mod.TASK_UPDATE:
                tid = line.get("task_id")
                if tid and tid in times:
                    ns = line.get("new_status") or line.get("status")
                    if ns == "done":
                        times[tid]["done_ts"] = ts
    except Exception:
        pass
    cache[chat_id] = times
    return times


def _classify_signal(
    gate: dict[str, Any], now_ts: float, assignee_idle_s: float | None
) -> str | None:
    """Pure signal classifier for the pending/in_progress dark-vs-cogitate split.

    Returns SIG_DARK, SIG_COGITATE, or None (healthy / advancing / untimeable).
    Signal-3 (verdict) is handled in the check loop (needs the committable check).
    This is the unit-test seam for the SPEC's cogitate-vs-dark acceptance cases.
    """
    if gate.get("status") not in ("pending", "in_progress"):
        return None
    task_age = _age_s(gate.get("last_state_change_ts") or gate.get("last_event_ts"), now_ts)
    if task_age is None or task_age <= _TASK_STALL_S:
        return None  # advancing (healthy) or untimeable (fail-safe)
    if assignee_idle_s is None:
        return None  # no liveness info → don't guess
    return SIG_DARK if assignee_idle_s > _INACTIVE_S else SIG_COGITATE


def _gc_debounce(now_ts: float) -> None:
    expired = [k for k, ts in _GUARD7_SEEN.items() if now_ts - ts > _DEBOUNCE_TTL_S]
    for k in expired:
        _GUARD7_SEEN.pop(k, None)


def _debounced(chat_id: str, task_id: str, signal: str, now_ts: float) -> bool:
    """True if this (task, signal) was escalated within the cooldown."""
    last = _GUARD7_SEEN.get((chat_id, task_id, signal))
    return last is not None and (now_ts - last) < _DEBOUNCE_S


def _mark(chat_id: str, task_id: str, signal: str, now_ts: float) -> None:
    _GUARD7_SEEN[(chat_id, task_id, signal)] = now_ts


def _nudge_body(gate: dict[str, Any], signal: str) -> str:
    """Task-specific surfacing text (deliberate, not a blind 'wake up')."""
    tid = gate.get("task_id", "?")
    preview = (gate.get("preview") or "").strip()
    tail = f' — "{preview}"' if preview else ""
    if signal == SIG_COGITATE:
        return (
            f"⏳ Guard-7: you hold task {tid}{tail} (in_progress) but it hasn't advanced "
            f"in over {int(_TASK_STALL_S // 60)}min though you're active — run it and post "
            "the artifact (chat_task_update), or report why you can't."
        )
    if signal == SIG_DARK:
        return (
            f"🌑 Guard-7: task {tid}{tail} is stalled and its assignee appears dark "
            f"(>{int(_INACTIVE_S // 60)}min idle). Reassign or restart the assignee."
        )
    return (
        f"⚖️ Guard-7: task {tid}{tail} is done but a verdict is unposted "
        f"(>{int(_VERDICT_STALL_S // 60)}min). The owed reviewer should post it."
    )


# --- escalation (reuse guard5's resolution + surfacing) ---------------------


async def _surface(chat_id: str, target_id: str | None, body: str) -> None:
    """Deliberate surface: a synthetic chat message + a notice to the target.

    Mirrors guard5's escalation path (chat + inbox notice). NO kitty injection —
    blind window-injection is exactly the flood-causing pattern Guard-7 replaces.
    Fail-open: a surfacing error never breaks the sweep.
    """
    try:
        from khimaira.monitor.chats import _post_synthetic_message

        await _post_synthetic_message(chat_id, body)
    except Exception as exc:
        log.warning("guard7: synthetic chat post failed (chat=%s): %s", chat_id, exc)
    if target_id:
        try:
            from khimaira.monitor import sessions as sessions_mod

            sessions_mod.post_notice(
                target_session_id=target_id,
                text=body,
                from_session_id="khimaira-daemon",
            )
        except Exception as exc:
            log.warning("guard7: notice failed (target=%s): %s", target_id, exc)


def _resolve_target(gate: dict[str, Any], session_rows: list[dict[str, Any]]) -> str | None:
    """Escalation target via guard5's resolver (same-role peer → master → coordinator)."""
    try:
        from khimaira.monitor.guard5 import _resolve_escalation_target

        return _resolve_escalation_target(gate, session_rows)
    except Exception:
        return None


# --- the sweep entrypoint (called from roster_recovery.watcher_loop) --------


async def _guard7_check_once() -> None:
    """One Guard-7 pass. Fail-open + wind-down-suppressed; rides the watcher sweep."""
    if not _ENABLED:
        return
    try:
        from khimaira.monitor.guard5 import (
            _scan_blocking_gates,
            is_wind_down,
        )
        from khimaira.monitor import sessions as sessions_mod
    except Exception as exc:
        log.warning("guard7: import failed (skipping sweep): %s", exc)
        return

    if is_wind_down():
        return

    now = time.time()
    _gc_debounce(now)

    try:
        gates = _scan_blocking_gates()
    except Exception as exc:
        log.warning("guard7: gate scan failed: %s", exc)
        return
    if not gates:
        return

    try:
        session_rows = sessions_mod.list_sessions(use_cache=True)
    except Exception:
        session_rows = []

    # committable lookups cached per chat this sweep (signal-3 dedup vs auto_dispatch).
    _committable_cache: dict[str, set[str]] = {}
    # per-task created_ts/done_ts cached per chat this sweep (abandonment + done recency).
    _times_cache: dict[str, dict[str, dict[str, str]]] = {}

    def _is_committable(chat_id: str, task_id: str) -> bool:
        if chat_id not in _committable_cache:
            try:
                from khimaira.monitor import chats as chats_mod

                _committable_cache[chat_id] = set(chats_mod.committable_gate_tasks(chat_id))
            except Exception:
                _committable_cache[chat_id] = set()
        return task_id in _committable_cache[chat_id]

    for gate in gates:
        try:
            chat_id = gate.get("chat_id", "")
            task_id = gate.get("task_id", "")
            status = gate.get("status")
            if not chat_id or not task_id:
                continue

            if status in ("pending", "in_progress"):
                idle = _assignee_idle_s(gate.get("assignee_id"))
                signal = _classify_signal(gate, now, idle)
                if signal is None:
                    continue
                created_ts = (
                    _chat_task_times(chat_id, _times_cache).get(task_id, {}).get("created_ts")
                )
                created_age = _age_s(created_ts, now)
                if created_age is not None and created_age > _ABANDON_AGE_S:
                    continue  # open for days → abandoned assignment, not a live stall
                if _debounced(chat_id, task_id, signal, now):
                    continue
                # cogitate-then-drop → nudge the ASSIGNEE (it's the one turning).
                # assigned-but-dark → escalate to a peer/master/coordinator.
                if signal == SIG_COGITATE:
                    target = gate.get("assignee_id")
                else:
                    target = _resolve_target(gate, session_rows)
                if not target:
                    continue  # no one reachable to act → don't surface into a dead chat
                await _surface(chat_id, target, _nudge_body(gate, signal))
                _mark(chat_id, task_id, signal, now)
                log.info(
                    "guard7: %s task=%s chat=%s target=%s (task_age stale, idle=%s)",
                    signal, task_id, chat_id, target, idle,
                )

            elif status == "done":
                # signal-3: done, no verdict activity for VERDICT_STALL, not committable
                # (a reviewer slot is still empty). Reuse committable_gate_tasks so we
                # don't dup auto_dispatch's commit path.
                done_ts = (
                    _chat_task_times(chat_id, _times_cache).get(task_id, {}).get("done_ts")
                )
                age = _age_s(done_ts, now)
                if age is None:  # no recorded done transition → fall back to last state change
                    age = _age_s(
                        gate.get("last_state_change_ts") or gate.get("last_event_ts"), now
                    )
                if age is None or age <= _VERDICT_STALL_S:
                    continue
                if age > _VERDICT_MAX_AGE_S:
                    continue  # done long ago = abandoned, not owed (no dead-roster resurrection)
                if _is_committable(chat_id, task_id):
                    continue  # both verdicts in — master owns the commit, not Guard-7
                if _debounced(chat_id, task_id, SIG_VERDICT, now):
                    continue
                target = _resolve_target(gate, session_rows)
                if not target:
                    continue  # dead chat, no reachable reviewer/master → skip
                await _surface(chat_id, target, _nudge_body(gate, SIG_VERDICT))
                _mark(chat_id, task_id, SIG_VERDICT, now)
                log.info(
                    "guard7: %s task=%s chat=%s target=%s",
                    SIG_VERDICT, task_id, chat_id, target,
                )
        except Exception as exc:
            log.warning("guard7: error on gate %s: %s", gate.get("task_id", "?"), exc)
            continue
