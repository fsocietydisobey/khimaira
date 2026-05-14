"""Unit tests for khimaira.monitor.scheduler.

Covers:
  - JSONL round-trip (write → replay → assert)
  - Compaction (terminal-status drop)
  - Worker fire path with a frozen clock (advance to fire_at_utc)
  - SIGKILL recovery (status=firing entry with old ts → re-fire on replay)
  - Cancel state matrix (scheduled, pending_retry, firing, terminal)
  - Inbox delivery (real target session dir with markers)
  - TTL expiration
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def isolated_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root state at tmp_path. Reloads sessions + scheduler modules so
    their lazy state-path lookups pick up the new XDG_STATE_HOME."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import scheduler as scheduler_mod

    importlib.reload(scheduler_mod)
    yield scheduler_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(scheduler_mod)


def _make_session(sessions_mod, session_id: str) -> None:
    """Plant marker files so scheduler._invoke_inbox treats the session as alive."""
    sd = sessions_mod._session_dir(session_id)
    (sd / "status.json").write_text(
        json.dumps({"status": "implementing", "detail": ""}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# Storage round-trip
# ---------------------------------------------------------------------------


def test_create_then_replay_round_trips(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "target-1")
    fire_at = (s._now() + timedelta(hours=1)).isoformat()

    rec = s.create(target_session="target-1", fire_at_utc=fire_at, prompt="hello")
    assert rec["status"] == s.SCHEDULED
    assert rec["target_session_id"] == "target-1"
    assert rec["id"].startswith("task-")

    reloaded = s.replay()
    assert rec["id"] in reloaded
    assert reloaded[rec["id"]]["prompt"] == "hello"


def test_create_unknown_target_raises(isolated_scheduler):
    s = isolated_scheduler
    with pytest.raises(ValueError):
        s.create(
            target_session="nonexistent",
            fire_at_utc=(s._now() + timedelta(hours=1)).isoformat(),
            prompt="...",
        )


def test_list_filters_by_status_and_target(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "a-session")
    _make_session(sessions_mod, "b-session")
    fire_at = (s._now() + timedelta(hours=2)).isoformat()
    s.create(target_session="a-session", fire_at_utc=fire_at, prompt="A")
    s.create(target_session="b-session", fire_at_utc=fire_at, prompt="B")

    all_tasks = s.list_tasks()
    assert len(all_tasks) == 2

    scheduled = s.list_tasks(status_filter=[s.SCHEDULED])
    assert len(scheduled) == 2
    fired = s.list_tasks(status_filter=[s.FIRED])
    assert fired == []

    a_only = s.list_tasks(target_filter="a-session")
    assert len(a_only) == 1
    assert a_only[0]["target_session_id"] == "a-session"


# ---------------------------------------------------------------------------
# Worker fire path
# ---------------------------------------------------------------------------


def test_tick_fires_due_task(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "target-fire")
    past = (s._now() - timedelta(seconds=30)).isoformat()
    rec = s.create(target_session="target-fire", fire_at_utc=past, prompt="run me")

    s.tick()

    after = s.get(rec["id"])
    assert after["status"] == s.FIRED
    outcomes = [a["outcome"] for a in after["attempts"]]
    assert "firing" in outcomes
    assert "fired" in outcomes


def test_tick_does_not_fire_future_task(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "future-target")
    future = (s._now() + timedelta(hours=1)).isoformat()
    rec = s.create(target_session="future-target", fire_at_utc=future, prompt="not yet")
    s.tick()
    after = s.get(rec["id"])
    assert after["status"] == s.SCHEDULED


def test_fire_delivers_inbox_note(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "inbox-target")
    past = (s._now() - timedelta(seconds=30)).isoformat()
    rec = s.create(
        target_session="inbox-target",
        fire_at_utc=past,
        prompt="scheduled prompt body",
    )

    s.tick()

    inbox_path = sessions_mod._session_dir("inbox-target") / "inbox.jsonl"
    assert inbox_path.exists()
    notes = sessions_mod._read_jsonl(inbox_path)
    assert any(n.get("kind") == "scheduled-task" and n.get("task_id") == rec["id"] for n in notes)
    delivered = next(n for n in notes if n.get("task_id") == rec["id"])
    assert delivered["prompt"] == "scheduled prompt body"


def test_fire_to_dead_target_pending_retry_then_failed(isolated_scheduler):
    s = isolated_scheduler
    # Bypass create() (which requires resolve_session_id success) — we need a
    # target_session_id that has no marker files so _invoke_inbox raises
    # FileNotFoundError. Write the task record directly.
    past = (s._now() - timedelta(seconds=30)).isoformat()
    ghost = {
        "id": "task-ghost0000001",
        "target_session_name": "ghost-target",
        "target_session_id": "ghost-target",
        "fire_at_utc": past,
        "prompt": "going to /dev/null",
        "retry_policy": {"max_attempts": 2, "retry_after_seconds": 1},
        "status": s.SCHEDULED,
        "created_at": past,
        "expires_at": (s._now() + timedelta(days=7)).isoformat(),
        "attempts": [],
    }
    s._append(ghost)

    s.tick()
    mid = s.get("task-ghost0000001")
    assert mid["status"] == s.PENDING_RETRY
    # retry_after_seconds=1 — force-fire by reverting fire_at_utc to the past.
    mid["fire_at_utc"] = (s._now() - timedelta(seconds=5)).isoformat()
    s._append(mid)
    s.tick()
    final = s.get("task-ghost0000001")
    assert final["status"] == s.FAILED


# ---------------------------------------------------------------------------
# SIGKILL recovery
# ---------------------------------------------------------------------------


def test_replay_recovers_stuck_firing_task(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "stuck-target")
    # Hand-craft a stuck record (status=firing, last attempt 5 minutes ago).
    stuck_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    stuck = {
        "id": "task-stuck0000001",
        "target_session_name": "stuck-target",
        "target_session_id": "stuck-target",
        "fire_at_utc": stuck_ts,
        "prompt": "was firing when killed",
        "retry_policy": {"max_attempts": 1, "retry_after_seconds": 300},
        "status": s.FIRING,
        "created_at": stuck_ts,
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        "attempts": [{"ts": stuck_ts, "outcome": "firing", "detail": "killed mid-fire"}],
    }
    s._append(stuck)

    after = s.replay()
    assert after["task-stuck0000001"]["status"] == s.SCHEDULED
    recovery_outcomes = [a["outcome"] for a in after["task-stuck0000001"]["attempts"]]
    assert "stuck_recovery" in recovery_outcomes


def test_replay_leaves_recent_firing_alone(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "fresh-firing-target")
    fresh_ts = datetime.now(UTC).isoformat()
    fresh = {
        "id": "task-fresh000000001",
        "target_session_name": "fresh-firing-target",
        "target_session_id": "fresh-firing-target",
        "fire_at_utc": fresh_ts,
        "prompt": "actively firing right now",
        "retry_policy": {"max_attempts": 1, "retry_after_seconds": 300},
        "status": s.FIRING,
        "created_at": fresh_ts,
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        "attempts": [{"ts": fresh_ts, "outcome": "firing", "detail": "in flight"}],
    }
    s._append(fresh)

    after = s.replay()
    # Status remains firing — race recovery threshold is 60s.
    assert after["task-fresh000000001"]["status"] == s.FIRING


# ---------------------------------------------------------------------------
# Cancellation state matrix
# ---------------------------------------------------------------------------


def test_cancel_scheduled_succeeds(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "cancel-target")
    future = (s._now() + timedelta(hours=1)).isoformat()
    rec = s.create(target_session="cancel-target", fire_at_utc=future, prompt="please cancel")
    result = s.cancel(rec["id"])
    assert result["status"] == s.CANCELLED


def test_cancel_unknown_task_raises(isolated_scheduler):
    s = isolated_scheduler
    with pytest.raises(ValueError):
        s.cancel("task-doesnotexist")


def test_cancel_firing_raises_runtime_error(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "firing-target")
    fresh_ts = datetime.now(UTC).isoformat()
    firing = {
        "id": "task-firing0000001",
        "target_session_name": "firing-target",
        "target_session_id": "firing-target",
        "fire_at_utc": fresh_ts,
        "prompt": "in flight",
        "retry_policy": {"max_attempts": 1, "retry_after_seconds": 300},
        "status": s.FIRING,
        "created_at": fresh_ts,
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        "attempts": [{"ts": fresh_ts, "outcome": "firing", "detail": "running"}],
    }
    s._append(firing)
    with pytest.raises(RuntimeError):
        s.cancel("task-firing0000001")


def test_cancel_terminal_is_idempotent(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "done-target")
    past = (s._now() - timedelta(seconds=30)).isoformat()
    rec = s.create(target_session="done-target", fire_at_utc=past, prompt="done")
    s.tick()
    assert s.get(rec["id"])["status"] == s.FIRED
    # Cancelling a fired task returns the existing record, doesn't error.
    result = s.cancel(rec["id"])
    assert result["status"] == s.FIRED


# ---------------------------------------------------------------------------
# TTL expiration
# ---------------------------------------------------------------------------


def test_tick_marks_expired_task(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "expire-target")
    # Schedule fire well in the future but expires_at in the past.
    far_future = (s._now() + timedelta(hours=10)).isoformat()
    rec = s.create(
        target_session="expire-target",
        fire_at_utc=far_future,
        prompt="should never fire",
    )
    # Mutate the persisted record's expires_at to the past.
    rec["expires_at"] = (s._now() - timedelta(hours=1)).isoformat()
    s._append(rec)

    s.tick()
    after = s.get(rec["id"])
    assert after["status"] == s.EXPIRED


# ---------------------------------------------------------------------------
# Compaction
# ---------------------------------------------------------------------------


def test_compact_drops_terminal_entries(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "compact-target")
    past = (s._now() - timedelta(seconds=30)).isoformat()
    future = (s._now() + timedelta(hours=1)).isoformat()

    rec_done = s.create(target_session="compact-target", fire_at_utc=past, prompt="A")
    rec_pending = s.create(target_session="compact-target", fire_at_utc=future, prompt="B")
    s.tick()
    assert s.get(rec_done["id"])["status"] == s.FIRED

    pre_size = s._state_path().stat().st_size
    rewrote = s.compact_if_needed(force=True)
    assert rewrote is True
    post_size = s._state_path().stat().st_size
    assert post_size < pre_size

    remaining = s.replay()
    # Fired task gone after compaction (we drop terminals).
    assert rec_done["id"] not in remaining
    assert rec_pending["id"] in remaining


def test_compact_skips_below_threshold(isolated_scheduler):
    s = isolated_scheduler
    from khimaira.monitor import sessions as sessions_mod

    _make_session(sessions_mod, "small-target")
    future = (s._now() + timedelta(hours=1)).isoformat()
    s.create(target_session="small-target", fire_at_utc=future, prompt="tiny")
    # Default threshold is 1MB; we write ~200 bytes.
    assert s.compact_if_needed(force=False) is False
