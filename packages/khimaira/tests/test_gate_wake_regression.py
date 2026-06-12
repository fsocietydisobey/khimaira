"""Gate-complete master-wake regression (muther GAP #1, 2026-06-11).

Audit found: 3 dual-verdict-complete tasks stranded uncommitted; the daemon log
had 12 verdict records but ZERO `chats: wake →` lines and no clue why (every
suppression path returned silently). Root causes were a class, not an instance:
  F2 — per-target cooldown collapsed distinct-task completion bursts
  F3 — no level-triggered backstop; the dispatch sweep early-returned before the
       master-wake whenever there was no DISPATCH backlog (commit-ready ≠ backlog)
  F4 — the worker matched the master window by fragile exact session_name, not role

These tests guard the detection (F3) + the worker's key/role behavior (F2/F4).
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def chats():
    from khimaira.monitor import chats as mod
    importlib.reload(mod)
    mod._last_dispatch_wake.clear()
    return mod


def _task(tid, status):
    return {"kind": "task", "id": tid, "status": status}


def _update(tid, status):
    return {"kind": "task_update", "task_id": tid, "status": status}


def _verdict(tid, verdict):
    return {"kind": "task_verdict", "task_id": tid, "verdict": verdict}


def _room(*messages):
    return {"messages": list(messages), "members": {}, "meta": {}}


# --------------------------------------------------------------------------- F3
# committable = done + critic-approve + verifier-ship, not yet master-acted.

def test_done_approve_ship_is_committable(chats):
    room = _room(_task("t1", "done"), _verdict("t1", "approve"), _verdict("t1", "ship"))
    assert chats._committable_task_ids(room) == ["t1"]


def test_approved_status_excluded(chats):
    # master already committed → status moved to approved → not owed
    room = _room(
        _task("t1", "done"), _verdict("t1", "approve"), _verdict("t1", "ship"),
        _update("t1", "approved"),
    )
    assert chats._committable_task_ids(room) == []


def test_hold_not_committable(chats):
    room = _room(_task("t1", "done"), _verdict("t1", "approve"), _verdict("t1", "hold"))
    assert chats._committable_task_ids(room) == []


def test_changes_not_committable(chats):
    room = _room(_task("t1", "done"), _verdict("t1", "changes"), _verdict("t1", "ship"))
    assert chats._committable_task_ids(room) == []


def test_single_verdict_not_committable(chats):
    room = _room(_task("t1", "done"), _verdict("t1", "ship"))  # verifier only
    assert chats._committable_task_ids(room) == []


def test_in_progress_not_committable(chats):
    room = _room(_task("t1", "in_progress"), _verdict("t1", "approve"), _verdict("t1", "ship"))
    assert chats._committable_task_ids(room) == []


def test_refiled_verdicts_still_single_committable(chats):
    # the exact incident: verdicts cleared by compaction then RE-filed (dupes).
    # last-wins scan must still yield the task once, not miss it.
    room = _room(
        _task("t1", "done"),
        _verdict("t1", "approve"), _verdict("t1", "ship"),   # round 1
        _verdict("t1", "approve"), _verdict("t1", "ship"),   # re-filed
    )
    assert chats._committable_task_ids(room) == ["t1"]


def test_changes_then_reapprove_becomes_committable(chats):
    # critic said changes, then re-reviewed to approve; verifier ship → committable
    room = _room(
        _task("t1", "done"),
        _verdict("t1", "changes"), _verdict("t1", "ship"),
        _verdict("t1", "approve"),   # last critic verdict wins
    )
    assert chats._committable_task_ids(room) == ["t1"]


def test_multiple_tasks_filtered(chats):
    room = _room(
        _task("t1", "done"), _verdict("t1", "approve"), _verdict("t1", "ship"),
        _task("t2", "done"), _verdict("t2", "approve"), _verdict("t2", "hold"),
        _task("t3", "done"), _verdict("t3", "approve"), _verdict("t3", "ship"),
    )
    assert sorted(chats._committable_task_ids(room)) == ["t1", "t3"]


# ----------------------------------------------------------------------- F2/F4
# worker: role-hint window match + cooldown keyed on cooldown_key.

@pytest.fixture
def patched_worker(chats, monkeypatch):
    """Patch the worker's externals so it runs to the inject step deterministically."""
    from khimaira.monitor import roster_recovery as rr

    # master idle long enough to pass the idle gate
    monkeypatch.setattr(
        chats.sessions_mod, "summary",
        lambda sid: {"last_active_age_s": 9999},
    )
    monkeypatch.setattr(
        rr, "_discover_roster_windows",
        lambda: [
            {"window_id": 1, "role": "master", "raw_name": "muther-0"},
            {"window_id": 2, "role": "agent", "raw_name": "muther"},
        ],
    )
    monkeypatch.setattr(rr, "_get_screen", lambda wid: "idle prompt")
    monkeypatch.setattr(rr, "_is_busy", lambda screen: False)
    injected: list[int] = []
    monkeypatch.setattr(
        rr, "_inject_text_and_submit",
        lambda wid, text, title="": injected.append(wid) or True,
    )
    return chats, injected


def test_role_hint_beats_name_match(patched_worker):
    # target_name "muther" name-matches the AGENT window (id 2); role_hint must
    # steer to the master window (id 1). This is the F4 fix.
    chats, injected = patched_worker
    chats._dispatch_wake_worker("master-uuid", "muther", "msg", role_hint="master")
    assert injected == [1], "role_hint should select the master window, not the name twin"


def test_name_fallback_when_no_role(patched_worker):
    chats, injected = patched_worker
    chats._dispatch_wake_worker("agent-uuid", "muther", "msg")
    assert injected == [2], "no role_hint → exact name match (agent window)"


def test_distinct_cooldown_keys_both_fire(patched_worker):
    # F2: two DISTINCT tasks completing for the same master each wake.
    chats, injected = patched_worker
    chats._dispatch_wake_worker("m", "muther", "msg", cooldown_key="m:taskA", role_hint="master")
    chats._dispatch_wake_worker("m", "muther", "msg", cooldown_key="m:taskB", role_hint="master")
    assert injected == [1, 1], "distinct task keys must not collapse"


def test_same_cooldown_key_suppressed(patched_worker):
    # re-filed verdict for the SAME task within cooldown → one wake, not two.
    chats, injected = patched_worker
    chats._dispatch_wake_worker("m", "muther", "msg", cooldown_key="m:taskA", role_hint="master")
    chats._dispatch_wake_worker("m", "muther", "msg", cooldown_key="m:taskA", role_hint="master")
    assert injected == [1], "same key within cooldown is deduped"


def test_no_window_logs_reason(patched_worker, caplog):
    # F1: a missing window must LOG (the silent return was the diagnosis-killer).
    chats, injected = patched_worker
    from khimaira.monitor import roster_recovery as rr
    import logging
    # no window matches this name or role
    with caplog.at_level(logging.INFO):
        chats._dispatch_wake_worker("x", "nonexistent", "msg", role_hint="nope")
    assert injected == []
    assert any("wake skipped" in r.message or "no window" in r.message for r in caplog.records)
