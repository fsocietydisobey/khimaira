"""Gate-complete master-wake (2026-06-11) — close the dead-SSE commit-miss.

When a verdict completes a dual-verdict gate (both critic AND verifier voted),
the daemon wakes the master to act — instead of relying on the master to see the
completion event live (its SSE doesn't survive compaction).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_chats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    from khimaira.monitor import sessions as sess
    importlib.reload(sess)
    from khimaira.monitor import chats as c
    importlib.reload(c)
    yield c
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sess)


MASTER = "11111111-0000-0000-0000-000000000001"
CRITIC = "22222222-0000-0000-0000-000000000002"
VERIFIER = "33333333-0000-0000-0000-000000000003"


def _room(c):
    room = c.create_room(
        MASTER, [CRITIC, VERIFIER], title="t", topology="hierarchical",
        member_roles={MASTER: c.ROLE_MASTER, CRITIC: c.ROLE_CRITIC, VERIFIER: c.ROLE_VERIFIER},
    )
    cid = room["meta"]["chat_id"]
    c.accept(cid, CRITIC); c.accept(cid, VERIFIER)
    task = c.create_task(cid, MASTER, "ship it", verdict_role="critic")
    c.update_task_status(cid, task["id"], MASTER, c.TASK_IN_PROGRESS)
    c.update_task_status(cid, task["id"], MASTER, c.TASK_DONE)
    return cid, task["id"]


def _capture_wake(c, monkeypatch):
    spawned = []
    import threading
    class _T:
        def __init__(self, target=None, args=(), **k): self.target, self.args = target, args
        def start(self): spawned.append(self.args)
    monkeypatch.setattr(threading, "Thread", _T)
    return spawned


def test_first_verdict_does_not_wake(isolated_chats, monkeypatch):
    c = isolated_chats
    cid, tid = _room(c)
    spawned = _capture_wake(c, monkeypatch)
    c.record_gate_verdict(cid, CRITIC, tid, "approve")  # only critic so far
    assert spawned == [], "one verdict ≠ complete gate → no master wake"


def test_completing_verdict_wakes_master(isolated_chats, monkeypatch):
    c = isolated_chats
    cid, tid = _room(c)
    c.record_gate_verdict(cid, CRITIC, tid, "approve")
    spawned = _capture_wake(c, monkeypatch)
    c.record_gate_verdict(cid, VERIFIER, tid, "ship")  # completes the gate
    assert len(spawned) == 1
    master_id, master_name, msg = spawned[0]
    assert master_id == MASTER
    # Lean rework: legacy critic+verifier dual still satisfies the gate, but the
    # wake message is now the unified "COMMIT-READY" wording (gate satisfied).
    assert "COMMIT-READY" in msg and "COMMIT" in msg


def test_non_positive_completion_wakes_with_rework_msg(isolated_chats, monkeypatch):
    c = isolated_chats
    cid, tid = _room(c)
    # Mock BEFORE the first verdict (not after, like the two tests above) —
    # "changes" alone already satisfies has_hold (crit=="changes") on this
    # already-done task, so it fires its own wake; capturing from the start
    # makes both calls visible instead of letting the first happen for real
    # and only the second land in `spawned`. See the 2026-07-14 dedup fix's
    # tests below for why this ordering now matters (it didn't when every
    # qualifying call fired unconditionally, before the outcome-based dedup).
    spawned = _capture_wake(c, monkeypatch)
    c.record_gate_verdict(cid, CRITIC, tid, "changes")
    c.record_gate_verdict(cid, VERIFIER, tid, "hold")
    assert len(spawned) == 1
    _, _, msg = spawned[0]
    assert "not" in msg.lower() and "rework" in msg.lower()


# ---------------------------------------------------------------------------
# 2026-07-14 fix (griffin-0 live incident, jeevy roster, bug #1): a stuck
# gatekeeper session re-submitted the IDENTICAL hold verdict 5+ times, and
# each one re-triggered this wake — the daemon's own _dispatch_wake_worker
# cooldown (30s) is real but doesn't help when the repeats are minutes apart
# (a normal turn cadence), so `_maybe_wake_master_on_gate_complete` now also
# dedupes on the gate's OUTCOME (committable, has_hold) — the exact pair that
# decides which message gets sent — independent of timing. These tests prove
# the class: a repeated identical vote must not re-wake, but any REAL change
# to the OUTCOME (a flip that actually unblocks/blocks the gate) must.
#
# All three tests below mock threading BEFORE the first verdict, not after —
# "changes" alone already satisfies has_hold (crit=="changes") on this
# already-done task, so the first call fires its own wake; mocking from the
# start makes every call visible to `spawned` instead of letting an early
# one happen for real. This matters now in a way it didn't for the OLD
# zero-dedup code (every qualifying call fired unconditionally regardless of
# what came before, so mock timing never affected pass/fail).
# ---------------------------------------------------------------------------


def test_repeated_identical_hold_does_not_rewake(isolated_chats, monkeypatch):
    c = isolated_chats
    cid, tid = _room(c)
    spawned = _capture_wake(c, monkeypatch)
    c.record_gate_verdict(cid, CRITIC, tid, "changes")
    c.record_gate_verdict(cid, VERIFIER, tid, "hold")  # first hold — wakes once
    assert len(spawned) == 1

    # The SAME verifier re-emits the IDENTICAL hold verdict — exactly the
    # live-incident shape (a stuck client re-posting the same tool call).
    c.record_gate_verdict(cid, VERIFIER, tid, "hold")
    c.record_gate_verdict(cid, VERIFIER, tid, "hold")
    c.record_gate_verdict(cid, VERIFIER, tid, "hold")
    assert len(spawned) == 1, "identical repeated hold must not re-wake the master"


def test_verdict_flip_after_hold_rewakes_with_new_decision(isolated_chats, monkeypatch):
    """A hold followed by a genuine reconsideration (verifier flips to ship,
    critic flips to approve) is a REAL new decision (unblocks the gate) —
    must re-wake, and with the updated (COMMIT-READY) message, not the stale
    hold one."""
    c = isolated_chats
    cid, tid = _room(c)
    spawned = _capture_wake(c, monkeypatch)
    c.record_gate_verdict(cid, CRITIC, tid, "changes")
    c.record_gate_verdict(cid, VERIFIER, tid, "hold")
    assert len(spawned) == 1
    first_msg = spawned[0][2]
    assert "not" in first_msg.lower()

    c.record_gate_verdict(cid, CRITIC, tid, "approve")
    c.record_gate_verdict(cid, VERIFIER, tid, "ship")

    assert len(spawned) == 2, "a genuine outcome change must re-wake"
    second_msg = spawned[1][2]
    assert "COMMIT-READY" in second_msg


def test_supporting_vote_change_without_outcome_change_does_not_rewake(
    isolated_chats, monkeypatch
):
    """A vote that changes SUPPORTING detail but not the OUTCOME — e.g. the
    critic revises their reasoning from "changes" to "approve" while the
    verifier is STILL holding — must not re-wake either. The gate is still
    blocked either way; nothing changed that the master needs to act on
    differently. This is the case that distinguishes an outcome-level
    fingerprint from a raw vote-tuple one (the latter would re-wake here,
    which is a milder version of the exact bug being fixed)."""
    c = isolated_chats
    cid, tid = _room(c)
    spawned = _capture_wake(c, monkeypatch)
    c.record_gate_verdict(cid, CRITIC, tid, "changes")
    c.record_gate_verdict(cid, VERIFIER, tid, "hold")
    assert len(spawned) == 1

    c.record_gate_verdict(cid, CRITIC, tid, "approve")  # supporting detail changes
    # verifier's hold is untouched — the gate is STILL blocked, same outcome.
    assert len(spawned) == 1, "a non-outcome-changing vote must not re-wake"
