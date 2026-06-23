"""Tests for Guard-7 — task-delivery watchdog (#32).

Covers the SPEC acceptance set: the cogitate-vs-dark split (the key new signal),
healthy no-fire, done-committable no-fire, verdict-owed fire, wind-down suppression,
per-(task,signal) debounce, and the surfacing contract (NOTICE/chat, NOT kitty inject).
"""

from __future__ import annotations

import datetime

from unittest.mock import AsyncMock

from khimaira.monitor import guard7

NOW = 1_000_000.0  # fixed test clock


def _iso(age_s: float) -> str:
    return datetime.datetime.fromtimestamp(
        NOW - age_s, datetime.timezone.utc
    ).isoformat()


def _gate(status="in_progress", task_age=12 * 60, assignee="agent-1", **kw):
    g = {
        "task_id": "task-1",
        "chat_id": "chat-1",
        "status": status,
        "assignee_id": assignee,
        "assignee_role": "agent",
        "last_state_change_ts": _iso(task_age),
        "last_event_ts": _iso(task_age),
        "has_verdict": False,
        "begin_fired": True,
        "preview": "do the thing",
    }
    g.update(kw)
    return g


# ---------------------------------------------------------------------------
# Pure classifier — the dark-vs-cogitate seam (SPEC's central acceptance case)
# ---------------------------------------------------------------------------


def test_classify_cogitate_when_stalled_but_assignee_active():
    # task stale 12min (>10min stall) + assignee active 1min (<15min inactive)
    assert guard7._classify_signal(_gate(), NOW, 60) == guard7.SIG_COGITATE


def test_classify_dark_when_stalled_and_assignee_idle():
    # same stalled task, assignee idle 20min (>15min inactive) → dark, not cogitate
    assert guard7._classify_signal(_gate(), NOW, 20 * 60) == guard7.SIG_DARK


def test_classify_healthy_when_task_advancing():
    # last_state_change 1min ago → advancing → no signal
    assert guard7._classify_signal(_gate(task_age=60), NOW, 60) is None


def test_classify_none_without_liveness_signal():
    # no assignee liveness → fail-safe, don't guess
    assert guard7._classify_signal(_gate(), NOW, None) is None


def test_classify_none_for_done_status():
    # done is handled by the verdict path, not the dark/cogitate classifier
    assert guard7._classify_signal(_gate(status="done"), NOW, 60) is None


def test_classify_none_when_untimeable():
    g = _gate()
    g["last_state_change_ts"] = "not-a-timestamp"
    g["last_event_ts"] = ""
    assert guard7._classify_signal(g, NOW, 60) is None


# ---------------------------------------------------------------------------
# Orchestration — _guard7_check_once wiring + surfacing
# ---------------------------------------------------------------------------


def _wire(
    monkeypatch,
    gates,
    *,
    idle,
    wind_down=False,
    committable=None,
    target="master-1",
    created_age=30 * 60,
    done_age=20 * 60,
):
    """Patch guard7's lazily-imported deps; return (notices, chat_posts) captured."""
    from khimaira.monitor import guard5, sessions, chats

    guard7._GUARD7_SEEN.clear()
    monkeypatch.setattr(guard7, "_ENABLED", True)
    monkeypatch.setattr(guard7.time, "time", lambda: NOW)

    # per-task timeline (created_ts / done_ts) the abandonment + done-recency checks use
    times = {"task-1": {"created_ts": _iso(created_age), "done_ts": _iso(done_age)}}
    monkeypatch.setattr(guard7, "_chat_task_times", lambda cid, cache: times)

    monkeypatch.setattr(guard5, "_scan_blocking_gates", lambda: gates)
    monkeypatch.setattr(guard5, "is_wind_down", lambda: wind_down)
    monkeypatch.setattr(guard5, "_resolve_escalation_target", lambda g, rows: target)

    monkeypatch.setattr(sessions, "list_sessions", lambda use_cache=True: [])
    monkeypatch.setattr(sessions, "summary", lambda sid: {"last_active_age_s": idle})
    notices: list[dict] = []
    monkeypatch.setattr(sessions, "post_notice", lambda **kw: notices.append(kw) or {})

    posts: list[tuple] = []

    async def _fake_post(chat_id, body):
        posts.append((chat_id, body))

    monkeypatch.setattr(chats, "_post_synthetic_message", _fake_post)
    monkeypatch.setattr(chats, "committable_gate_tasks", lambda cid: list(committable or []))
    return notices, posts


async def test_cogitate_nudges_the_assignee_via_notice(monkeypatch):
    notices, posts = _wire(monkeypatch, [_gate()], idle=60)
    await guard7._guard7_check_once()
    assert len(notices) == 1
    assert notices[0]["target_session_id"] == "agent-1"  # the assignee, NOT master
    assert notices[0]["from_session_id"] == "khimaira-daemon"
    assert "hasn't advanced" in notices[0]["text"]
    assert len(posts) == 1  # deliberate: also a synthetic chat post


async def test_dark_escalates_to_resolved_target(monkeypatch):
    notices, _ = _wire(monkeypatch, [_gate()], idle=20 * 60, target="master-1")
    await guard7._guard7_check_once()
    assert len(notices) == 1
    assert notices[0]["target_session_id"] == "master-1"  # peer/master/coordinator
    assert "dark" in notices[0]["text"].lower()


async def test_healthy_task_does_not_fire(monkeypatch):
    notices, posts = _wire(monkeypatch, [_gate(task_age=60)], idle=60)
    await guard7._guard7_check_once()
    assert notices == [] and posts == []


async def test_done_with_both_verdicts_does_not_fire(monkeypatch):
    g = _gate(status="done", task_age=20 * 60)
    notices, _ = _wire(monkeypatch, [g], idle=60, committable=["task-1"])
    await guard7._guard7_check_once()
    assert notices == []  # committable → master owns the commit, not Guard-7


async def test_done_verdict_owed_fires(monkeypatch):
    g = _gate(status="done", task_age=20 * 60)
    notices, _ = _wire(monkeypatch, [g], idle=60, committable=[])
    await guard7._guard7_check_once()
    assert len(notices) == 1
    assert "verdict" in notices[0]["text"].lower()


async def test_wind_down_suppresses(monkeypatch):
    notices, posts = _wire(monkeypatch, [_gate()], idle=60, wind_down=True)
    await guard7._guard7_check_once()
    assert notices == [] and posts == []


async def test_debounce_escalates_once_per_cooldown(monkeypatch):
    notices, _ = _wire(monkeypatch, [_gate()], idle=60)
    await guard7._guard7_check_once()
    await guard7._guard7_check_once()  # same stalled task, within cooldown
    assert len(notices) == 1  # second sweep debounced


async def test_verdict_owed_skips_when_no_reachable_target(monkeypatch):
    """Dead chat (no reachable reviewer/master) → no surface (the 43-burst fix)."""
    g = _gate(status="done", task_age=20 * 60)
    notices, posts = _wire(monkeypatch, [g], idle=60, committable=[], target=None)
    await guard7._guard7_check_once()
    assert notices == [] and posts == []


async def test_verdict_owed_skips_when_abandoned_old(monkeypatch):
    """done_ts LONGER ago than VERDICT_MAX_AGE → abandoned, not owed (no resurrection).
    Keyed on done_ts: last_state_change is recent here, but done happened 10h ago."""
    g = _gate(status="done", task_age=60)  # recent last_state_change (a re-touch)
    notices, _ = _wire(
        monkeypatch, [g], idle=60, committable=[], target="master-1", done_age=10 * 3600
    )
    await guard7._guard7_check_once()
    assert notices == []


async def test_cogitate_skips_when_task_abandoned(monkeypatch):
    """in_progress task open for DAYS (created_ts old) → abandoned, not a live cogitate."""
    notices, _ = _wire(monkeypatch, [_gate()], idle=60, created_age=93 * 3600)
    await guard7._guard7_check_once()
    assert notices == []


async def test_dark_skips_when_no_reachable_target(monkeypatch):
    """Dark assignee but nobody reachable to escalate to → don't post into the void."""
    notices, posts = _wire(monkeypatch, [_gate()], idle=20 * 60, target=None)
    await guard7._guard7_check_once()
    assert notices == [] and posts == []


async def test_never_uses_kitty_injection(monkeypatch):
    """Surfacing is NOTICE/chat only — never the blind window-injection path."""
    from khimaira.monitor import auto_dispatch

    monkeypatch.setattr(
        auto_dispatch,
        "_maybe_wake_idle_master",
        AsyncMock(side_effect=AssertionError("guard7 must not kitty-wake")),
    )
    notices, _ = _wire(monkeypatch, [_gate()], idle=20 * 60)
    await guard7._guard7_check_once()  # must not raise
    assert len(notices) == 1  # surfaced deliberately instead
