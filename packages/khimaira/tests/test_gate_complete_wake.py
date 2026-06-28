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
    c.record_gate_verdict(cid, CRITIC, tid, "changes")
    spawned = _capture_wake(c, monkeypatch)
    c.record_gate_verdict(cid, VERIFIER, tid, "hold")
    assert len(spawned) == 1
    _, _, msg = spawned[0]
    assert "not" in msg.lower() and "rework" in msg.lower()
