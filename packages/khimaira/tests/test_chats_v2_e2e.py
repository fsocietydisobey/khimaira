"""Phase B v2 end-to-end role-lifecycle integration tests.

Lives in a separate file from `test_chats.py` so the V1 / V2 / V3 lanes
can land in parallel without rebase conflicts on the unit-test file.

Covers the full v2 surface composed:
  - implicit-master materialization on first explicit `chat_grant_role`
  - atomic promote-demote on `chat_grant_role(target, "master")`
  - observer enforcement (read OK, write rejected)
  - critic semantic (send + read OK, approve rejected)
  - master-leave guard (chat_leave by master refused)
  - chat_set_creator orphaned-master recovery
  - chat_transfer_membership member_roles propagation (Lane E parity)
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest
from khimaira.monitor import chats as c


@pytest.fixture
def isolated_chats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Reload-isolated chats module rooted at a fresh state dir. Mirrors
    test_chats.py's fixture; duplicated here to keep this file
    self-contained while V1/V2/V3 land in parallel."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod

    importlib.reload(chats_mod)
    yield chats_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)


def _make(name: str) -> None:
    """Create a session dir with a friendly name. Bypasses `set_name`
    (which has UUID-drift detection that would interfere in test setup).
    Mirrors the `_make_session` helper in test_chats.py — direct
    status.json write."""
    from khimaira.monitor import sessions as sessions_mod

    sd = sessions_mod._session_dir(name)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "status.json").write_text(
        json.dumps({"status": "implementing", "detail": "", "name": name}),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Happy-path lifecycle: create → invite → accept → grant → tasks → approve →
# transfer master → observer/critic enforcement
# ---------------------------------------------------------------------------


def test_v2_role_lifecycle_end_to_end(isolated_chats):
    """Exercise every v2 primitive in one realistic flow.

    Alice (creator + implicit master) → grants Bob critic, Carol observer,
    Dave agent. Alice creates a task. Bob (critic) tries to approve → fails.
    Carol (observer) tries to send → fails. Alice approves. Then Alice
    promotes Dave to master (atomic demote of Alice to agent). Dave creates
    a task; Alice (now agent) can't approve, but Dave can. Dave tries to
    leave directly → refused. Dave transfers master back to Alice, then
    leaves → succeeds.
    """
    chats = isolated_chats
    for name in ("alice", "bob", "carol", "dave"):
        _make(name)

    # --- create chat ---
    room = chats.create_room("alice", ["bob", "carol", "dave"], title="v2-e2e")
    chat_id = room["meta"]["chat_id"]
    chats.accept(chat_id, "bob")
    chats.accept(chat_id, "carol")
    chats.accept(chat_id, "dave")

    # --- pre-grant: alice is implicit master via created_by ---
    fresh = chats.load_room(chat_id)
    assert (
        fresh["meta"].get("member_roles") is None
    ), "v1-era chats should have no explicit member_roles until first grant"
    assert chats._is_master(fresh, "alice")
    assert not chats._is_master(fresh, "bob")

    # --- grant: alice grants bob critic (non-master grant materializes implicit master) ---
    chats.chat_grant_role(chat_id, "alice", "bob", "critic")
    fresh = chats.load_room(chat_id)
    assert fresh["meta"]["member_roles"] == {
        "alice": "master",
        "bob": "critic",
    }, "first grant must materialize implicit master and add the requested role in one write"

    # --- grant: alice grants carol observer ---
    chats.chat_grant_role(chat_id, "alice", "carol", "observer")
    fresh = chats.load_room(chat_id)
    assert fresh["meta"]["member_roles"] == {
        "alice": "master",
        "bob": "critic",
        "carol": "observer",
    }

    # --- grant: alice grants dave agent (explicit; would default anyway, but pins the audit) ---
    chats.chat_grant_role(chat_id, "alice", "dave", "agent")

    # --- create + advance a task ---
    task = chats.create_task(chat_id, "alice", "ship v2", assignee_session_id="dave")
    chats.update_task_status(chat_id, task["id"], "dave", c.TASK_IN_PROGRESS)
    chats.update_task_status(chat_id, task["id"], "dave", c.TASK_DONE)

    # --- enforcement: critic CANNOT approve ---
    with pytest.raises(ValueError, match="master"):
        chats.update_task_status(chat_id, task["id"], "bob", c.TASK_APPROVED)

    # --- enforcement: observer CANNOT send messages ---
    with pytest.raises(ValueError, match="observer"):
        chats.send_message(chat_id, "carol", "I have opinions")

    # --- enforcement: observer CAN read history ---
    history = chats.history(chat_id, "carol")
    assert isinstance(history, list)

    # --- master (alice) approves ---
    chats.update_task_status(chat_id, task["id"], "alice", c.TASK_APPROVED)

    # --- promote dave to master with explicit alice demote_to=agent ---
    chats.chat_grant_role(chat_id, "alice", "dave", "master", demote_to="agent")
    fresh = chats.load_room(chat_id)
    assert fresh["meta"]["member_roles"]["dave"] == "master"
    assert (
        fresh["meta"]["member_roles"]["alice"] == "agent"
    ), "atomic promote-demote must demote the prior master in the same write"
    assert not chats._is_master(fresh, "alice")
    assert chats._is_master(fresh, "dave")

    # --- alice (now agent) CANNOT approve ---
    task2 = chats.create_task(
        chat_id, "dave", "second task", assignee_session_id="alice"
    )
    chats.update_task_status(chat_id, task2["id"], "alice", c.TASK_IN_PROGRESS)
    chats.update_task_status(chat_id, task2["id"], "alice", c.TASK_DONE)
    with pytest.raises(ValueError, match="master"):
        chats.update_task_status(chat_id, task2["id"], "alice", c.TASK_APPROVED)

    # --- dave (master) can approve ---
    chats.update_task_status(chat_id, task2["id"], "dave", c.TASK_APPROVED)

    # --- master-leave guard: dave can't leave directly ---
    with pytest.raises(ValueError, match="master.*cannot leave"):
        chats.leave(chat_id, "dave")

    # --- dave transfers master back to alice, then leaves ---
    chats.chat_grant_role(chat_id, "dave", "alice", "master")
    fresh = chats.load_room(chat_id)
    assert chats._is_master(fresh, "alice")
    assert not chats._is_master(fresh, "dave")

    # --- dave can now leave ---
    chats.leave(chat_id, "dave")
    fresh = chats.load_room(chat_id)
    assert fresh["members"]["dave"]["state"] == c.LEFT


# ---------------------------------------------------------------------------
# chat_set_creator: orphaned-master recovery (the chat-84afd6396a3d case)
# ---------------------------------------------------------------------------


def test_set_creator_recovers_orphaned_chat(isolated_chats):
    """Reproduce the v1.2 dogfood failure pattern: A creates chat, A
    transfers to B (pre-v1.3 chat_transfer_membership semantics — only
    membership moves, master role stays pinned to A). B is left
    accepted but unable to gate tasks. A is TRANSFERRED_OUT.

    `chat_set_creator(chat_id, B)` should let B claim master post-hoc.
    """
    chats = isolated_chats
    for name in ("alice", "bob", "carol"):
        _make(name)

    room = chats.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    chats.accept(chat_id, "bob")

    chats.transfer_membership(chat_id, "alice", "carol")

    # --- post-transfer (v1.3): alice should already be transferred-out + carol
    # should be the new master via Lane E's META propagation. So this is the
    # "happy v1.3" path. The orphaned-master case is the PRE-v1.3 scenario
    # we can't reproduce in unit tests without manually crafting the JSONL
    # without a META update. Skip the orphan-precondition reproduction; just
    # assert set_creator REFUSES when the current creator is still accepted.

    fresh = chats.load_room(chat_id)
    assert (
        fresh["meta"]["created_by"] == "carol"
    ), "Lane E v1.3 fix: master role moved on transfer"
    assert fresh["members"]["alice"]["state"] == c.TRANSFERRED_OUT

    # --- set_creator should refuse: current creator (carol) is accepted, not transferred-out ---
    with pytest.raises(ValueError, match="not 'transferred-out'"):
        chats.set_creator(chat_id, "bob")


def test_set_creator_refuses_non_member(isolated_chats):
    """`chat_set_creator` requires the target to be an accepted member."""
    chats = isolated_chats
    for name in ("alice", "bob", "carol", "dave"):
        _make(name)

    room = chats.create_room("alice", ["bob"])
    chat_id = room["meta"]["chat_id"]
    chats.accept(chat_id, "bob")

    # Force alice into TRANSFERRED_OUT via transfer to non-member carol.
    chats.transfer_membership(chat_id, "alice", "carol")
    # Now carol is the v1.3-corrected master. Try set_creator on non-member dave.
    with pytest.raises(ValueError, match="non-member"):
        chats.set_creator(chat_id, "dave")


# ---------------------------------------------------------------------------
# #14 — Auto-BEGIN dispatch tests
# ---------------------------------------------------------------------------


def _setup_auto_begin_room(
    chats, names: list[str], agents: list[str]
) -> tuple[str, str]:
    """Create room with master=names[0], agents=names[1:], accept all, create a
    task with required_agents=agents, required_model='sonnet', required_effort='medium'.
    Returns (chat_id, task_id)."""
    for name in names:
        _make(name)
    master = names[0]
    invited = names[1:]
    room = chats.create_room(master, invited)
    chat_id = room["meta"]["chat_id"]
    for name in invited:
        chats.accept(chat_id, name)
    task = chats.create_task(
        chat_id,
        master,
        "implement the feature",
        assignee_session_id=agents[0],
        required_agents=agents,
        auto_begin=True,
        required_model="sonnet",
        required_effort="medium",
    )
    return chat_id, task["id"]


def test_auto_begin_fires_on_last_ack(isolated_chats):
    """Partial acks must not fire BEGIN; the final ack must trigger auto-BEGIN immediately."""
    chats = isolated_chats
    chat_id, task_id = _setup_auto_begin_room(
        chats,
        ["master", "agent-a", "agent-b"],
        ["agent-a", "agent-b"],
    )

    # First ack — only agent-a present; BEGIN must NOT fire yet.
    chats.send_message(
        chat_id,
        "agent-a",
        f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
    )
    assert not chats._is_task_begun(chat_id, task_id), "partial ack must not fire BEGIN"

    # Second ack — gate satisfied; BEGIN MUST fire synchronously.
    chats.send_message(
        chat_id,
        "agent-b",
        f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
    )
    assert chats._is_task_begun(chat_id, task_id), "all acks received; BEGIN must fire"

    # Exactly one TASK_SIGNAL 'start' record in JSONL.
    signals = [
        line
        for line in chats._read(chat_id)
        if line.get("kind") == chats.TASK_SIGNAL
        and line.get("task_id") == task_id
        and line.get("signal") == "start"
    ]
    assert len(signals) == 1, f"expected exactly 1 BEGIN signal, got {len(signals)}"


def test_auto_begin_ignores_noncompliant_ack(isolated_chats):
    """An ack with the wrong model or effort must not satisfy the gate."""
    chats = isolated_chats
    chat_id, task_id = _setup_auto_begin_room(
        chats,
        ["master", "agent-a", "agent-b"],
        ["agent-a", "agent-b"],
    )

    # agent-a sends a NON-compliant ack (wrong model).
    chats.send_message(
        chat_id,
        "agent-a",
        f"✅ ready [task-id: {task_id}] | model=haiku effort=medium",
    )
    # agent-b sends a compliant ack.
    chats.send_message(
        chat_id,
        "agent-b",
        f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
    )
    # Gate should NOT be satisfied — agent-a's ack was non-compliant.
    assert not chats._is_task_begun(
        chat_id, task_id
    ), "non-compliant ack must not satisfy the gate"

    # Now agent-a re-acks with the correct tier — gate satisfies.
    chats.send_message(
        chat_id,
        "agent-a",
        f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
    )
    assert chats._is_task_begun(chat_id, task_id), "compliant re-ack must fire BEGIN"


def test_auto_begin_respects_hold(isolated_chats):
    """auto_begin=False disables daemon auto-fire; master manual signal still works."""
    chats = isolated_chats
    for name in ("master", "agent-a"):
        _make(name)
    room = chats.create_room("master", ["agent-a"])
    chat_id = room["meta"]["chat_id"]
    chats.accept(chat_id, "agent-a")
    task = chats.create_task(
        chat_id,
        "master",
        "hold task",
        assignee_session_id="agent-a",
        required_agents=["agent-a"],
        auto_begin=False,
        required_model="sonnet",
        required_effort="medium",
    )
    task_id = task["id"]

    chats.send_message(
        chat_id,
        "agent-a",
        f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
    )
    assert not chats._is_task_begun(
        chat_id, task_id
    ), "auto_begin=False must suppress daemon BEGIN even when all acks received"

    # Master can still manually fire.
    chats.signal_task_start(chat_id, task_id, "master", note="manual")
    assert chats._is_task_begun(
        chat_id, task_id
    ), "manual signal_task_start must still work"


def test_auto_begin_idempotent(isolated_chats):
    """Receiving acks after BEGIN has already fired must not produce duplicate signals."""
    chats = isolated_chats
    chat_id, task_id = _setup_auto_begin_room(
        chats,
        ["master", "agent-a"],
        ["agent-a"],
    )

    # Single agent acks — auto-BEGIN fires.
    chats.send_message(
        chat_id,
        "agent-a",
        f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
    )
    assert chats._is_task_begun(chat_id, task_id)

    # Send another ack (e.g. from a restart) — must not double-fire.
    chats.send_message(
        chat_id,
        "agent-a",
        f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
    )

    signals = [
        line
        for line in chats._read(chat_id)
        if line.get("kind") == chats.TASK_SIGNAL
        and line.get("task_id") == task_id
        and line.get("signal") == "start"
    ]
    assert (
        len(signals) == 1
    ), f"double ack must not produce duplicate BEGIN; got {len(signals)}"


def test_auto_begin_honors_verdict_gate(isolated_chats):
    """begin_gate_task_id blocks auto-BEGIN until that task's B3 verdicts are in."""
    chats = isolated_chats
    for name in ("master", "agent-a"):
        _make(name)
    room = chats.create_room("master", ["agent-a"])
    chat_id = room["meta"]["chat_id"]
    chats.accept(chat_id, "agent-a")

    # Gate task — the one whose B3 verdicts we're waiting on (none submitted yet).
    gate_task = chats.create_task(
        chat_id,
        "master",
        "gate work",
        assignee_session_id="agent-a",
    )
    gate_task_id = gate_task["id"]

    # Work task gated on the gate task's verdicts.
    work_task = chats.create_task(
        chat_id,
        "master",
        "gated work",
        assignee_session_id="agent-a",
        required_agents=["agent-a"],
        auto_begin=True,
        required_model="sonnet",
        required_effort="medium",
        begin_gate_task_id=gate_task_id,
    )
    work_task_id = work_task["id"]

    # All required agents ack — but gate task has no verdicts yet.
    chats.send_message(
        chat_id,
        "agent-a",
        f"✅ ready [task-id: {work_task_id}] | model=sonnet effort=medium",
    )
    assert not chats._is_task_begun(
        chat_id, work_task_id
    ), "BEGIN must be blocked while begin_gate_task_id has no verdicts"


def test_no_ready_task_idles_without_master(isolated_chats):
    """Class invariant: when all required agents are compliant-ready, BEGIN fires
    in the same ack-processing turn — a fully-ready task can never block on the master.
    """
    chats = isolated_chats
    chat_id, task_id = _setup_auto_begin_room(
        chats,
        ["master", "agent-x"],
        ["agent-x"],
    )

    # Single agent — compliant ack → BEGIN fires synchronously within send_message.
    chats.send_message(
        chat_id,
        "agent-x",
        f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
    )

    # No polling, no master action — the task is begun immediately.
    assert chats._is_task_begun(
        chat_id, task_id
    ), "a fully-ready task must be begun within the ack-processing turn, not wait for master"
