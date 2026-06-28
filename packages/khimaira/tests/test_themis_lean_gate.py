"""Lean gate ↔ Themis enrichment — the `committable` single-source path (2026-06-28).

Covers the daemon-side fix that teaches the Themis commit gate to read the lean
gatekeeper-N-distinct-ship gate, not just the legacy critic-approve + verifier-ship
dual. Two layers:

1. chats._round_aware_gate_entry → chats._is_committable — the enrichment core that
   get_gate_verdicts[/_by_task] now use to emit `committable`. Crucially ROUND-AWARE:
   stale prior-round verdicts (recorded before the last done→reopen) must NOT satisfy
   the current gate (Guard-5 Part A). This is exactly why enrichment uses
   _round_aware_gate_entry and NOT _gate_tally (which has no round-reset).
2. themis.conditions.gate_verdicts_incomplete — keys off the `committable` bool;
   the None/absent/error tri-state is preserved; a payload predating the field falls
   back to the legacy critic+verifier pair.

Pure-core tests on hand-built room dicts + payloads — no disk, fully deterministic.
"""

from __future__ import annotations

from khimaira.monitor import chats as c
from themis.conditions import gate_verdicts_incomplete

GK1, GK2, GK3 = "gk-1", "gk-2", "gk-3"
CRIT, VER, AGENT = "crit-1", "ver-1", "agent-1"

ROLES = {
    GK1: c.ROLE_GATEKEEPER,
    GK2: c.ROLE_GATEKEEPER,
    GK3: c.ROLE_GATEKEEPER,
    CRIT: c.ROLE_CRITIC,
    VER: c.ROLE_VERIFIER,
    AGENT: c.ROLE_AGENT,
}

# Lexically-ordered ISO timestamps (string comparison is what _last_round_reset_ts uses).
T0 = "2026-01-01T00:00:00Z"
T1 = "2026-01-01T00:01:00Z"
T2 = "2026-01-01T00:02:00Z"
T3 = "2026-01-01T00:03:00Z"
T4 = "2026-01-01T00:04:00Z"
T5 = "2026-01-01T00:05:00Z"


def _room(messages: list[dict], roles: dict[str, str] | None = None) -> dict:
    return {"meta": {"member_roles": roles or ROLES}, "messages": messages}


def _task(tid="t", status=None, high_stakes=False, ts=T0) -> dict:
    return {
        "kind": c.TASK,
        "id": tid,
        "status": status or c.TASK_DONE,
        "high_stakes": high_stakes,
        "ts": ts,
    }


def _upd(tid, status, ts) -> dict:
    return {"kind": c.TASK_UPDATE, "task_id": tid, "status": status, "ts": ts}


def _v(tid, sid, verdict="ship", escalate=False, ts=T1) -> dict:
    return {
        "kind": c.TASK_VERDICT,
        "task_id": tid,
        "verdict": verdict,
        "by_session_id": sid,
        "escalate": escalate,
        "ts": ts,
    }


def _committable(messages, roles=None, tid="t") -> bool:
    """The enrichment decision: round-aware entry → _is_committable (single source)."""
    return c._is_committable(c._round_aware_gate_entry(_room(messages, roles), tid))


# --- enrichment core: lean gatekeeper-ship gate -----------------------------


def test_lean_one_distinct_ship_committable():
    assert _committable([_task(), _v("t", GK1, "ship")])


def test_lean_zero_ships_not_committable():
    assert not _committable([_task()])


def test_lean_high_stakes_one_ship_not_committable():
    assert not _committable([_task(high_stakes=True), _v("t", GK1, "ship")])


def test_lean_high_stakes_two_distinct_ships_committable():
    assert _committable(
        [_task(high_stakes=True), _v("t", GK1, "ship", ts=T1), _v("t", GK2, "ship", ts=T2)]
    )


def test_lean_same_session_twice_is_not_two_verdicts():
    """LOAD-BEARING independence property: one gatekeeper session shipping twice is
    ONE vote, not two — must NOT satisfy an N=2 gate (mirrors the pure-core test,
    proven here through the round-aware enrichment path the Themis gate reads)."""
    assert not _committable(
        [_task(high_stakes=True), _v("t", GK1, "ship", ts=T1), _v("t", GK1, "ship", ts=T2)]
    )


def test_lean_escalate_bumps_to_n2():
    assert not _committable([_task(), _v("t", GK1, "ship", escalate=True)])
    assert _committable(
        [_task(), _v("t", GK1, "ship", escalate=True, ts=T1), _v("t", GK2, "ship", ts=T2)]
    )


def test_lean_outstanding_hold_blocks_even_with_enough_ships():
    assert not _committable(
        [
            _task(high_stakes=True),
            _v("t", GK1, "ship", ts=T1),
            _v("t", GK2, "ship", ts=T2),
            _v("t", GK3, "hold", ts=T3),
        ]
    )


def test_non_gatekeeper_ship_is_not_a_gate_vote():
    """A `ship` from a non-gatekeeper session must not count toward the distinct-ship
    quorum (gk_latest is role-filtered)."""
    assert not _committable(
        [_task(high_stakes=True), _v("t", GK1, "ship", ts=T1), _v("t", AGENT, "ship", ts=T2)]
    )


def test_in_progress_task_not_committable():
    assert not _committable([_task(status=c.TASK_IN_PROGRESS), _v("t", GK1, "ship")])


# --- legacy dual still works through the same path ---------------------------


def test_legacy_critic_approve_plus_verifier_ship_committable():
    assert _committable([_task(), _v("t", CRIT, "approve", ts=T1), _v("t", VER, "ship", ts=T2)])


def test_legacy_changes_not_committable():
    assert not _committable([_task(), _v("t", CRIT, "changes", ts=T1), _v("t", VER, "ship", ts=T2)])


# --- ROUND-RESET: the reason enrichment uses _round_aware_gate_entry ---------


def _reopened_no_new_ship() -> list[dict]:
    """Round-1 ship, then sent back (done→changes_requested→in_progress→done) with
    NO fresh ship in round 2."""
    return [
        _task(status=c.TASK_DONE, ts=T0),
        _v("t", GK1, "ship", ts=T1),  # round-1 ship
        _upd("t", "changes_requested", ts=T2),  # reopen → resets the verdict round
        _upd("t", c.TASK_IN_PROGRESS, ts=T3),
        _upd("t", c.TASK_DONE, ts=T4),  # round-2 done, no new ship
    ]


def test_stale_prior_round_ship_excluded():
    msgs = _reopened_no_new_ship()
    assert not _committable(msgs)  # round-1 ship is stale → gate not satisfied
    # a fresh round-2 ship DOES close the gate
    assert _committable(msgs + [_v("t", GK1, "ship", ts=T5)])


def test_gate_tally_would_wrongly_pass_documenting_why_round_aware():
    """Contrast guard: the non-round-aware _gate_tally counts the stale round-1 ship
    (committable=True), which is exactly the bypass we must avoid. The round-aware
    enrichment path correctly excludes it. This pins WHY we don't route enrichment
    through _gate_tally."""
    msgs = _reopened_no_new_ship()
    assert "t" in c._committable_task_ids(_room(msgs))  # _gate_tally: stale ship counted
    assert not _committable(msgs)  # round-aware: correctly excluded


# --- themis condition: gate_verdicts_incomplete keys off `committable` -------


def test_condition_committable_true_allows():
    assert gate_verdicts_incomplete({"gate_verdicts": {"committable": True}}) is False


def test_condition_committable_false_blocks():
    assert gate_verdicts_incomplete({"gate_verdicts": {"committable": False}}) is True


def test_condition_tristate_preserved():
    assert gate_verdicts_incomplete({}) is False  # no key → enrichment didn't run → fail-open
    assert gate_verdicts_incomplete({"gate_verdicts": None}) is False  # no active task
    assert gate_verdicts_incomplete({"gate_verdicts": "absent"}) is True  # task, no verdicts
    assert gate_verdicts_incomplete({"gate_verdicts": "error"}) is True  # fail closed


def test_condition_committable_subsumes_legacy_fields():
    """When `committable` is present it is authoritative — even if the legacy
    critic_approved/verifier_shipped pair would say otherwise."""
    # committable True but legacy pair incomplete (a lean gatekeeper-only gate) → allow
    assert (
        gate_verdicts_incomplete(
            {
                "gate_verdicts": {
                    "committable": True,
                    "critic_approved": False,
                    "verifier_shipped": True,
                }
            }
        )
        is False
    )


def test_condition_legacy_fallback_when_no_committable_key():
    """A payload predating the committable field falls back to the legacy pair."""
    assert (
        gate_verdicts_incomplete(
            {"gate_verdicts": {"critic_approved": True, "verifier_shipped": True}}
        )
        is False
    )
    assert (
        gate_verdicts_incomplete(
            {"gate_verdicts": {"critic_approved": True, "verifier_shipped": False}}
        )
        is True
    )
