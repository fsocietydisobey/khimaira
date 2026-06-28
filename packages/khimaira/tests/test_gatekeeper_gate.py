"""Lean gatekeeper commit-gate — class-invariant tests (Option B, 2026-06-28).

The gate's load-bearing property is INDEPENDENCE expressed as a COUNT: a task is
committable when >= N DISTINCT gatekeeper-role sessions have a latest `ship`
verdict (no outstanding gatekeeper `hold`), N=2 for high-stakes (explicit flag OR
gatekeeper self-escalation) else 1. The single most important assertion:
**the same session voting twice is NOT two independent verdicts** — that is the
property the old two-role (critic≠verifier) dual gate gave for free and that a
single gatekeeper role would silently lose without distinct-session counting.

These test the pure gate core (`_gate_tally` / `_is_committable` via
`_committable_task_ids`) on hand-built room dicts — no disk, fully deterministic.
The gatekeeper OWED-verdict obligation (distinct-session counting that drives the
C3 drain hook + roster wake) is covered in test_direct_verdict_obligation.py.
"""

from __future__ import annotations

import pytest
from khimaira.monitor import chats as c

GK1 = "gk-sess-1"
GK2 = "gk-sess-2"
GK3 = "gk-sess-3"
MASTER = "master-sess"

_GK_ROLES = {
    GK1: c.ROLE_GATEKEEPER,
    GK2: c.ROLE_GATEKEEPER,
    GK3: c.ROLE_GATEKEEPER,
    MASTER: c.ROLE_MASTER,
}


def _room(messages: list[dict], roles: dict[str, str] | None = None) -> dict:
    return {"meta": {"member_roles": roles or _GK_ROLES}, "messages": messages}


def _task(tid: str = "t", status: str | None = None, high_stakes: bool = False) -> dict:
    return {
        "kind": c.TASK,
        "id": tid,
        "status": status or c.TASK_DONE,
        "high_stakes": high_stakes,
    }


def _v(tid: str, sid: str, verdict: str = "ship", escalate: bool = False) -> dict:
    return {
        "kind": c.TASK_VERDICT,
        "task_id": tid,
        "verdict": verdict,
        "by_session_id": sid,
        "escalate": escalate,
    }


def _committable(messages, roles=None) -> bool:
    return "t" in c._committable_task_ids(_room(messages, roles))


# --- N=1 (normal) -----------------------------------------------------------


def test_normal_one_distinct_ship_is_committable():
    assert _committable([_task(), _v("t", GK1, "ship")])


def test_normal_zero_ships_not_committable():
    assert not _committable([_task()])


def test_normal_hold_only_not_committable():
    assert not _committable([_task(), _v("t", GK1, "hold")])


# --- N=2 (high-stakes) ------------------------------------------------------


def test_high_stakes_one_ship_not_committable():
    assert not _committable([_task(high_stakes=True), _v("t", GK1, "ship")])


def test_high_stakes_two_distinct_ships_committable():
    assert _committable([_task(high_stakes=True), _v("t", GK1, "ship"), _v("t", GK2, "ship")])


def test_high_stakes_same_session_twice_is_not_two_verdicts():
    """LOAD-BEARING: one gatekeeper session shipping twice is ONE independent
    verdict, not two — must NOT satisfy an N=2 gate. This is the independence
    property the dual gate used to guarantee structurally."""
    assert not _committable([_task(high_stakes=True), _v("t", GK1, "ship"), _v("t", GK1, "ship")])


# --- self-escalation bumps N to 2 ------------------------------------------


def test_escalate_bumps_normal_task_to_n2():
    # one ship that self-escalates → N becomes 2 → not committable on its own
    assert not _committable([_task(), _v("t", GK1, "ship", escalate=True)])
    # a second DISTINCT ship satisfies the escalated N=2
    assert _committable([_task(), _v("t", GK1, "ship", escalate=True), _v("t", GK2, "ship")])


# --- hold blocks; latest-per-session wins ----------------------------------


def test_outstanding_hold_blocks_even_with_enough_ships():
    assert not _committable(
        [_task(high_stakes=True), _v("t", GK1, "ship"), _v("t", GK2, "ship"), _v("t", GK3, "hold")]
    )


def test_latest_verdict_per_session_wins():
    # ship then hold (same session) → latest hold → blocked
    assert not _committable([_task(), _v("t", GK1, "ship"), _v("t", GK1, "hold")])
    # hold then ship (same session) → latest ship → committable (N=1)
    assert _committable([_task(), _v("t", GK1, "hold"), _v("t", GK1, "ship")])


# --- role + status scoping --------------------------------------------------


def test_non_gatekeeper_ship_does_not_count():
    roles = {GK1: c.ROLE_AGENT, MASTER: c.ROLE_MASTER}
    assert not _committable([_task(), _v("t", GK1, "ship")], roles)


def test_not_done_task_never_committable():
    assert not _committable([_task(status=c.TASK_IN_PROGRESS), _v("t", GK1, "ship")])


# --- legacy backward-compat -------------------------------------------------


def test_legacy_critic_approve_plus_verifier_ship_still_committable():
    roles = {"crit": c.ROLE_CRITIC, "ver": c.ROLE_VERIFIER, MASTER: c.ROLE_MASTER}
    assert _committable([_task(), _v("t", "crit", "approve"), _v("t", "ver", "ship")], roles)


def test_legacy_critic_only_not_committable():
    roles = {"crit": c.ROLE_CRITIC, "ver": c.ROLE_VERIFIER, MASTER: c.ROLE_MASTER}
    assert not _committable([_task(), _v("t", "crit", "approve")], roles)


# --- effective-N helper directly -------------------------------------------


@pytest.mark.parametrize(
    "high_stakes, escalated, expected_n",
    [(False, False, 1), (True, False, 2), (False, True, 2), (True, True, 2)],
)
def test_effective_gate_n(high_stakes, escalated, expected_n):
    entry = {"high_stakes": high_stakes, "escalated": escalated}
    assert c._effective_gate_n(entry) == expected_n
