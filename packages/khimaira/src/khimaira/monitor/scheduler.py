"""Daemon-side persistent scheduler.

ScheduleWakeup lives in the Claude Code harness and is process-bound to the
calling agent. When the window closes, pending wakeups die. This module
adds an equivalent primitive that lives in the long-running
`khimaira-monitor` daemon — survives agent restarts, persistent across
sessions, observable via REST.

Storage: append-only JSONL at ~/.local/state/khimaira/scheduled_tasks.jsonl.
Replay-on-boot folds the stream into the current in-memory schedule keyed
by task id. Compaction is opt-in (>1MB threshold or explicit maintenance
call) — daemon boot does NOT compact, to keep restart fast.

Race model: at-least-once. Worker writes status=firing BEFORE invoke,
status=fired AFTER. On daemon SIGKILL mid-fire, replay detects any
task stuck in `firing` for >60s and re-fires (status → scheduled).
**Tasks MUST be idempotent** — that's the scheduler's documented contract.

Invoke mechanism (Phase A): append a `kind=scheduled-task` note to the
target session's inbox.jsonl. The session's UserPromptSubmit hook
surfaces it on the next user prompt. Target session must be alive at
fire time; otherwise the task fails per retry policy and eventually
hits expires_at → status=expired.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from khimaira.log import get_logger
from khimaira.monitor import sessions as sessions_mod

log = get_logger("monitor.scheduler")

_COMPACT_THRESHOLD_BYTES = 1 * 1024 * 1024  # 1MB
_FIRING_STUCK_SECONDS = 60  # if status=firing and ts > 60s ago on replay, re-fire
_WORKER_TICK_SECONDS = 5
_DEFAULT_EXPIRES_HOURS = 24 * 7  # 7d TTL

# Status enum — string constants so JSONL stays human-readable.
SCHEDULED = "scheduled"
FIRING = "firing"
FIRED = "fired"
FAILED = "failed"
PENDING_RETRY = "pending_retry"
CANCELLED = "cancelled"
EXPIRED = "expired"

_TERMINAL_STATUSES = frozenset({FIRED, CANCELLED, EXPIRED})
_CANCELLABLE_STATUSES = frozenset({SCHEDULED, PENDING_RETRY})


# ---------------------------------------------------------------------------
# Storage primitives
# ---------------------------------------------------------------------------


def _state_path() -> Path:
    """Resolve state file path lazily so tests can rebind XDG_STATE_HOME."""
    xdg = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    return xdg / "khimaira" / "scheduled_tasks.jsonl"


def _ensure_dir() -> None:
    _state_path().parent.mkdir(parents=True, exist_ok=True)


def _now() -> datetime:
    return datetime.now(UTC)


def _now_iso() -> str:
    return _now().isoformat()


def _parse_iso(ts: str) -> datetime:
    # fromisoformat handles trailing 'Z' from Python 3.11+; normalize defensively
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    return datetime.fromisoformat(ts)


def _append(record: dict[str, Any]) -> None:
    _ensure_dir()
    sessions_mod._append_jsonl(_state_path(), record)


def _read_all() -> list[dict[str, Any]]:
    return sessions_mod._read_jsonl(_state_path())


def replay() -> dict[str, dict[str, Any]]:
    """Fold the JSONL stream into the current in-memory task map.

    Each line is a full record (not a delta) — the latest entry for a
    given id wins. Stuck `firing` entries (older than 60s) are reset to
    `scheduled` so the worker re-fires.
    """
    by_id: dict[str, dict[str, Any]] = {}
    for rec in _read_all():
        tid = rec.get("id")
        if not tid:
            continue
        by_id[tid] = rec

    now = _now()
    for tid, rec in by_id.items():
        if rec.get("status") == FIRING:
            attempts = rec.get("attempts") or []
            last_ts = attempts[-1].get("ts") if attempts else rec.get("created_at")
            try:
                age = (now - _parse_iso(last_ts)).total_seconds()
            except (TypeError, ValueError):
                age = _FIRING_STUCK_SECONDS + 1
            if age > _FIRING_STUCK_SECONDS:
                rec["status"] = SCHEDULED
                rec.setdefault("attempts", []).append(
                    {
                        "ts": _now_iso(),
                        "outcome": "stuck_recovery",
                        "detail": f"firing>{_FIRING_STUCK_SECONDS}s on replay; re-scheduled",
                    }
                )
                _append(rec)
                log.info("scheduler: recovered stuck firing task %s", tid)
    return by_id


def compact_if_needed(force: bool = False) -> bool:
    """Rewrite the state file with only non-terminal entries (latest per id).

    Skipped at daemon boot — caller must invoke explicitly (CLI command
    or worker-triggered when size > threshold).
    Returns True if a rewrite happened.
    """
    path = _state_path()
    if not path.exists():
        return False
    if not force and path.stat().st_size < _COMPACT_THRESHOLD_BYTES:
        return False
    by_id = replay()
    keep = [r for r in by_id.values() if r.get("status") not in _TERMINAL_STATUSES]
    tmp = path.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        import json

        for rec in keep:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    tmp.replace(path)
    log.info("scheduler: compacted state file — kept %d non-terminal tasks", len(keep))
    return True


# ---------------------------------------------------------------------------
# Public API — create / list / get / cancel
# ---------------------------------------------------------------------------


def create(
    target_session: str,
    fire_at_utc: str,
    prompt: str,
    *,
    retry_policy: dict | None = None,
    expires_in_hours: float = _DEFAULT_EXPIRES_HOURS,
) -> dict[str, Any]:
    """Create a new scheduled task.

    Resolves `target_session` (name or UUID) to its current id at
    schedule-time and stores both name + id. If the target session is
    renamed between schedule and fire, the fire dispatches to the
    stored id (which may be stale — documented v1 limitation).

    Raises ValueError on unknown target session.
    """
    target_id = sessions_mod.resolve_session_id(target_session)
    # Find a friendly name if one is registered (for observability).
    target_name = target_session
    try:
        # If target_session was a UUID, look up the registered name (if any).
        sd = sessions_mod._session_dir(target_id)
        name_file = sd / "name.txt"
        if name_file.exists():
            target_name = name_file.read_text(encoding="utf-8").strip() or target_session
    except Exception:
        pass

    # Validate fire_at_utc parses.
    _parse_iso(fire_at_utc)

    now = _now()
    record = {
        "id": "task-" + uuid.uuid4().hex[:12],
        "target_session_name": target_name,
        "target_session_id": target_id,
        "fire_at_utc": fire_at_utc,
        "prompt": prompt,
        "retry_policy": retry_policy or {"max_attempts": 1, "retry_after_seconds": 300},
        "status": SCHEDULED,
        "created_at": _now_iso(),
        "expires_at": (now + timedelta(hours=expires_in_hours)).isoformat(),
        "attempts": [],
    }
    _append(record)
    log.info(
        "scheduler: task %s scheduled for %s targeting %s",
        record["id"],
        fire_at_utc,
        target_id,
    )
    return record


def list_tasks(
    *, status_filter: list[str] | None = None, target_filter: str | None = None
) -> list[dict[str, Any]]:
    """Return current state of all known tasks, newest first."""
    by_id = replay()
    items = list(by_id.values())
    if status_filter:
        wanted = set(status_filter)
        items = [r for r in items if r.get("status") in wanted]
    if target_filter:
        try:
            target_id = sessions_mod.resolve_session_id(target_filter)
        except ValueError:
            target_id = target_filter
        items = [
            r
            for r in items
            if r.get("target_session_id") == target_id
            or r.get("target_session_name") == target_filter
        ]
    items.sort(key=lambda r: r.get("fire_at_utc", ""), reverse=False)
    return items


def get(task_id: str) -> dict[str, Any] | None:
    return replay().get(task_id)


def cancel(task_id: str) -> dict[str, Any]:
    """Cancel a scheduled or pending-retry task.

    Raises:
        ValueError — unknown task id (→ 404 at API layer).
        RuntimeError — task is in `firing` (→ 409 at API layer).
    Terminal-status tasks (fired/cancelled/expired) → idempotent no-op,
    returns the existing record unchanged.
    """
    rec = replay().get(task_id)
    if rec is None:
        raise ValueError(
            f"No scheduled task with id={task_id!r}. Use list_scheduled_tasks() to see active ids."
        )
    status = rec.get("status")
    if status == FIRING:
        raise RuntimeError(
            f"Task {task_id} is currently firing; cancellation is racy. "
            f"Stop the daemon (`systemctl --user stop khimaira-monitor`) "
            f"to forcibly halt mid-fire."
        )
    if status in _TERMINAL_STATUSES:
        return rec
    rec["status"] = CANCELLED
    rec.setdefault("attempts", []).append(
        {"ts": _now_iso(), "outcome": "cancelled", "detail": "via cancel()"}
    )
    _append(rec)
    log.info("scheduler: cancelled task %s", task_id)
    return rec


# ---------------------------------------------------------------------------
# Worker — fires tasks at their fire_at_utc
# ---------------------------------------------------------------------------


def _invoke_inbox(record: dict[str, Any]) -> dict[str, Any]:
    """Deliver the scheduled prompt as an inbox note to the target session.

    Returns the note record. Raises FileNotFoundError if the target
    session dir doesn't exist (treated as "session no longer alive").
    """
    target_id = record["target_session_id"]
    sd = sessions_mod._session_dir(target_id)
    # _session_dir creates the dir lazily; we want to fail if there's no
    # evidence of the session ever existing. Look for canonical markers.
    # If no marker, the session was never registered — fail.
    canonical = sd / "status.json"
    decisions = sd / "decisions.jsonl"
    touched = sd / "files_touched.jsonl"
    if not (canonical.exists() or decisions.exists() or touched.exists()):
        raise FileNotFoundError(
            f"Target session {target_id} has no state markers — likely never alive or fully GC'd."
        )
    note = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "kind": "scheduled-task",
        "task_id": record["id"],
        "prompt": record["prompt"],
        "from_session_id": "khimaira-scheduler",
        "read": False,
        "surface_count": 0,
    }
    sessions_mod._append_jsonl(sd / "inbox.jsonl", note)
    return note


def _fire(record: dict[str, Any]) -> dict[str, Any]:
    """Mark firing → attempt invoke → mark fired/failed/pending_retry.

    Pure function over the record dict; caller handles persistence.
    """
    record["status"] = FIRING
    record.setdefault("attempts", []).append(
        {"ts": _now_iso(), "outcome": "firing", "detail": "worker started invoke"}
    )
    _append(record)

    try:
        _invoke_inbox(record)
        record["status"] = FIRED
        record.setdefault("attempts", []).append(
            {"ts": _now_iso(), "outcome": "fired", "detail": "inbox note delivered"}
        )
        _append(record)
        log.info("scheduler: fired task %s → %s", record["id"], record["target_session_id"])
        return record
    except Exception as exc:
        attempts = record.get("attempts") or []
        # Count only the (failed|fired) outcomes against max_attempts.
        # firing/stuck_recovery/cancelled don't consume an attempt.
        used_attempts = sum(
            1 for a in attempts if a.get("outcome") in ("fired", "error", "timeout")
        )
        policy = record.get("retry_policy") or {}
        max_attempts = int(policy.get("max_attempts", 1))
        used_attempts += 1  # this failed attempt
        attempt_entry = {"ts": _now_iso(), "outcome": "error", "detail": str(exc)}
        record["attempts"].append(attempt_entry)

        if used_attempts < max_attempts:
            retry_after = int(policy.get("retry_after_seconds", 300))
            next_fire = _now() + timedelta(seconds=retry_after)
            record["fire_at_utc"] = next_fire.isoformat()
            record["status"] = PENDING_RETRY
            _append(record)
            log.info(
                "scheduler: task %s failed (%d/%d) — retry at %s",
                record["id"],
                used_attempts,
                max_attempts,
                record["fire_at_utc"],
            )
        else:
            record["status"] = FAILED
            _append(record)
            log.warning(
                "scheduler: task %s failed after %d attempt(s) — %s",
                record["id"],
                used_attempts,
                exc,
            )
        return record


def tick(now: datetime | None = None) -> list[dict[str, Any]]:
    """Single worker pass. Fires every task whose fire_at_utc has passed.

    Also marks tasks expired when expires_at has passed.
    Returns the list of records whose status changed during this tick.
    Exposed for testability — call directly with a fake clock.
    """
    by_id = replay()
    now = now or _now()
    fired: list[dict[str, Any]] = []
    for rec in by_id.values():
        status = rec.get("status")
        if status in _TERMINAL_STATUSES or status == FIRING:
            continue
        # TTL expiry takes precedence over fire.
        expires_at = rec.get("expires_at")
        if expires_at and _parse_iso(expires_at) < now:
            rec["status"] = EXPIRED
            rec.setdefault("attempts", []).append(
                {"ts": _now_iso(), "outcome": "expired", "detail": "expires_at passed"}
            )
            _append(rec)
            fired.append(rec)
            continue
        if status not in (SCHEDULED, PENDING_RETRY):
            continue
        try:
            fire_at = _parse_iso(rec["fire_at_utc"])
        except (KeyError, ValueError):
            continue
        if fire_at <= now:
            _fire(rec)
            fired.append(rec)
    return fired


async def scheduler_loop(stop_event: asyncio.Event | None = None) -> None:
    """Long-running coroutine — calls tick() every _WORKER_TICK_SECONDS."""
    log.info("scheduler: worker loop starting (tick=%ss)", _WORKER_TICK_SECONDS)
    while True:
        if stop_event is not None and stop_event.is_set():
            log.info("scheduler: worker loop exiting on stop_event")
            return
        try:
            await asyncio.to_thread(tick)
        except Exception as exc:
            log.warning("scheduler: tick failed — %s", exc)
        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=_WORKER_TICK_SECONDS)
                return
            await asyncio.sleep(_WORKER_TICK_SECONDS)
        except TimeoutError:
            continue
