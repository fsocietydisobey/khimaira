"""Class-invariant tests for the direct-verdict obligation in _get_session_obligations.

CONTEXT (the muther 2026-06-17 stall): roster_recovery wakes an idle session only
when _get_session_obligations returns work for it. Real rosters record review verdicts
DIRECTLY on the work-task (task_verdict → work-task), NOT via gate-task wrappers
(gate_for/gate_required), which are dormant in practice (0/475 real tasks). So a `done`
work-task with a PARTIAL verdict (e.g. critic=approve, verifier owes ship) produced NO
obligation for the owing reviewer → it was never woken → the pipeline stalled silently.

CLASS-INVARIANT: a reviewer-role MEMBER of a chat OWES a verdict iff a work-task is
`done`, already carries >=1 verdict (proof it's under review), and that member's verdict
slot is still empty. Mirrors chats._committable_task_ids — read straight off task_verdict
records, zero dependency on gate_for/gate_required/the status filter at line 759.

Fire tests: owed critic + owed verifier both surface.
Stay-quiet tests: a fully-verdicted (committable) task does NOT re-flag; an un-gated done
task with zero verdicts does NOT flag (review-initiation, not stall-recovery); a reviewer
who is not a member of the chat is not flagged (per-chat scoping).
"""

from __future__ import annotations

import json
from datetime import UTC

from khimaira.monitor import chats
from khimaira.monitor.api import chats as apichats

CRITIC_SID = "11111111-1111-1111-1111-111111111111"
VERIFIER_SID = "22222222-2222-2222-2222-222222222222"
MASTER_SID = "33333333-3333-3333-3333-333333333333"
AGENT_SID = "44444444-4444-4444-4444-444444444444"
WORK_TASK = "task-deadbeef0001"
CHAT_ID = "chat-feedface0001"


def _write_chat(lines: list[dict]) -> None:
    """Write raw JSONL events into the isolated chat store."""
    chat_dir = chats._chat_dir()
    chat_dir.mkdir(parents=True, exist_ok=True)
    path = chat_dir / f"{CHAT_ID}.jsonl"
    path.write_text("".join(json.dumps(line) + "\n" for line in lines))


def _meta() -> dict:
    return {
        "kind": chats.META,
        "member_roles": {
            CRITIC_SID: chats.ROLE_CRITIC,
            VERIFIER_SID: chats.ROLE_VERIFIER,
            MASTER_SID: "master",
        },
    }


def _done_task(ts: str | None = None) -> dict:
    """A done work-task. Stamps a RECENT done-ts by default so it passes the
    owed-verdict recency gate; pass an explicit old ts to test the stale-backlog skip.
    """
    from datetime import datetime

    return {
        "kind": chats.TASK,
        "id": WORK_TASK,
        "status": chats.TASK_DONE,
        "ts": ts or datetime.now(UTC).isoformat(),
    }


def _verdict(task_id: str, verdict: str, by: str) -> dict:
    return {
        "kind": chats.TASK_VERDICT,
        "task_id": task_id,
        "verdict": verdict,
        "by_session_id": by,
    }


def _owed(obligations: list[dict], role: str, task_id: str) -> bool:
    return any(o.get("owed_verdict") == role and o.get("task_id") == task_id for o in obligations)


# --- FIRE -----------------------------------------------------------------


def test_owed_verifier_gets_obligation(isolated_state):
    """done + critic approved + verifier ship MISSING → verifier owes ship."""
    _write_chat([_meta(), _done_task(), _verdict(WORK_TASK, "approve", CRITIC_SID)])

    obs = apichats._get_session_obligations(VERIFIER_SID)

    assert _owed(obs, chats.ROLE_VERIFIER, WORK_TASK), obs


def test_owed_critic_gets_obligation(isolated_state):
    """done + verifier shipped + critic verdict MISSING → critic owes review."""
    _write_chat([_meta(), _done_task(), _verdict(WORK_TASK, "ship", VERIFIER_SID)])

    obs = apichats._get_session_obligations(CRITIC_SID)

    assert _owed(obs, chats.ROLE_CRITIC, WORK_TASK), obs


def test_changes_verdict_counts_as_critic_present(isolated_state):
    """A 'changes' verdict means the critic ACTED — only the verifier still owes."""
    _write_chat([_meta(), _done_task(), _verdict(WORK_TASK, "changes", CRITIC_SID)])

    assert not _owed(apichats._get_session_obligations(CRITIC_SID), chats.ROLE_CRITIC, WORK_TASK)
    assert _owed(apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK)


# --- LEAN GATEKEEPER obligations (Option B — distinct-session counting) -----
# These drive the C3 drain hook + roster wake for the gatekeeper role: a gatekeeper
# MEMBER owes a ship iff the gate hasn't reached N DISTINCT gatekeeper ships and this
# session hasn't itself shipped (a shipped gatekeeper does NOT owe the 2nd — distinct
# sessions only). N=2 high-stakes/escalated else 1.

GK1_SID = "55555555-5555-5555-5555-555555555555"
GK2_SID = "66666666-6666-6666-6666-666666666666"


def _gk_meta() -> dict:
    return {
        "kind": chats.META,
        "member_roles": {
            GK1_SID: chats.ROLE_GATEKEEPER,
            GK2_SID: chats.ROLE_GATEKEEPER,
            MASTER_SID: "master",
        },
    }


def _hs_done_task() -> dict:
    t = _done_task()
    t["high_stakes"] = True
    return t


def _gk_owed(sid: str) -> bool:
    return _owed(apichats._get_session_obligations(sid), chats.ROLE_GATEKEEPER, WORK_TASK)


def test_gatekeeper_cold_start_gate_required_owes(isolated_state):
    """gate_required done task, 0 gatekeeper verdicts → the gatekeeper owes the first."""
    t = _done_task()
    t["gate_required"] = True
    _write_chat([_gk_meta(), t])
    assert _gk_owed(GK1_SID)


def test_gatekeeper_high_stakes_cold_start_owes(isolated_state):
    """high_stakes is itself a review-wanted signal → cold-start engages."""
    _write_chat([_gk_meta(), _hs_done_task()])
    assert _gk_owed(GK1_SID)


def test_gatekeeper_shipped_does_not_self_owe(isolated_state):
    """Normal task, GK1 shipped → N=1 met → GK1 does NOT still owe."""
    _write_chat([_gk_meta(), _done_task(), _verdict(WORK_TASK, "ship", GK1_SID)])
    assert not _gk_owed(GK1_SID)


def test_gatekeeper_held_does_not_self_owe(isolated_state):
    """Normal task, GK1 rendered HOLD → GK1 does NOT still owe, even though the
    gate correctly stays un-closed (HOLD != ship, N=1 not met). Regression test
    for the drain-hook repost-loop bug: a gatekeeper who already voted HOLD was
    being re-flagged as still owing a verdict on every subsequent check, causing
    the same structured HOLD to be re-posted repeatedly."""
    _write_chat([_gk_meta(), _done_task(), _verdict(WORK_TASK, "hold", GK1_SID)])
    assert not _gk_owed(GK1_SID)


def test_gatekeeper_held_gate_stays_open_for_others(isolated_state):
    """high-stakes (N=2): GK1 held → GK1 does not owe again, but a DISTINCT GK2
    still owes (the gate itself is not closed by a HOLD)."""
    _write_chat([_gk_meta(), _hs_done_task(), _verdict(WORK_TASK, "hold", GK1_SID)])
    assert not _gk_owed(GK1_SID)
    assert _gk_owed(GK2_SID)


def test_gatekeeper_committable_no_second_owe(isolated_state):
    """Normal task (N=1) with one ship is committable → no OTHER gatekeeper owes."""
    _write_chat([_gk_meta(), _done_task(), _verdict(WORK_TASK, "ship", GK1_SID)])
    assert not _gk_owed(GK2_SID)


def test_gatekeeper_high_stakes_second_distinct_owes(isolated_state):
    """high-stakes (N=2): GK1 shipped → a DISTINCT GK2 owes the 2nd ship; GK1 does
    NOT (a shipped gatekeeper doesn't owe the second — distinct-session counting)."""
    _write_chat([_gk_meta(), _hs_done_task(), _verdict(WORK_TASK, "ship", GK1_SID)])
    assert _gk_owed(GK2_SID)
    assert not _gk_owed(GK1_SID)


def test_gatekeeper_ungated_zero_verdict_stays_quiet(isolated_state):
    """Storm guard: a plain un-gated 0-verdict done task does NOT nag gatekeepers."""
    _write_chat([_gk_meta(), _done_task()])
    assert not _gk_owed(GK1_SID)


# --- #39 COLD-START (0-verdict initiation, gate_required-gated) -------------


def test_gate_required_cold_start_fires_both_reviewers(isolated_state):
    """#39: a gate_required done task with ZERO verdicts engages BOTH reviewers.

    The cold-start (review-never-started) case. gate_required is the master's
    explicit "this needs review" signal, so the opening verdict from EACH role is
    owed — closing the input-starvation hole where review-INITIATION was punted to
    "master's normal dispatch" (which, dispatched via undirected chat_send, created
    no obligation). Forms the engagement half of the class invariant; its storm-guard
    twin is test_ungated_done_task_with_no_verdicts_stays_quiet — WITHOUT
    gate_required the same 0-verdict done task stays silent. Together: engage iff gated.
    """
    task = _done_task()
    task["gate_required"] = True
    _write_chat([_meta(), task])

    assert _owed(apichats._get_session_obligations(CRITIC_SID), chats.ROLE_CRITIC, WORK_TASK)
    assert _owed(apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK)


def test_gate_required_cold_start_respects_recency(isolated_state):
    """#39 cold-start still obeys the recency gate: an ANCIENT gate_required done
    task does NOT backfill as owed — so a long-lived roster's old gated-but-
    unreviewed tasks can't storm reviewers on deploy (same guard as the partial-
    verdict path in test_stale_done_task_not_owed)."""
    task = _done_task(ts="2020-01-01T00:00:00+00:00")
    task["gate_required"] = True
    _write_chat([_meta(), task])

    assert not _owed(apichats._get_session_obligations(CRITIC_SID), chats.ROLE_CRITIC, WORK_TASK)
    assert not _owed(
        apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK
    )


# --- RECENCY (the muther long-lived-roster backlog storm) ------------------


def test_stale_done_task_not_owed(isolated_state):
    """A done task whose done-transition is older than the window → NOT owed.

    The muther over-fire: a long-lived roster accumulates done-not-approved tasks
    (audit/research that skip the gate, done days ago). Without a recency gate they
    all backfill as "owed" on deploy and storm every reviewer.
    """
    _write_chat(
        [
            _meta(),
            _done_task(ts="2020-01-01T00:00:00+00:00"),  # ancient done-transition
            _verdict(WORK_TASK, "approve", CRITIC_SID),
        ]
    )

    assert not _owed(
        apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK
    )


def test_recent_done_task_owed(isolated_state):
    """A freshly-done partial-verdict task (within the window) → owed (live stall)."""
    from datetime import datetime

    _write_chat(
        [
            _meta(),
            _done_task(ts=datetime.now(UTC).isoformat()),
            _verdict(WORK_TASK, "approve", CRITIC_SID),
        ]
    )

    assert _owed(apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK)


def test_done_task_with_no_done_ts_not_owed(isolated_state):
    """A done task with no parseable done-ts → NOT owed (can't prove recency → skip)."""
    _write_chat(
        [
            _meta(),
            {"kind": chats.TASK, "id": WORK_TASK, "status": chats.TASK_DONE},  # no ts
            _verdict(WORK_TASK, "approve", CRITIC_SID),
        ]
    )

    assert not _owed(
        apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK
    )


# --- STAY QUIET -----------------------------------------------------------


def test_committable_task_does_not_reflag(isolated_state):
    """done + approve + ship (committable) → neither reviewer is re-flagged."""
    _write_chat(
        [
            _meta(),
            _done_task(),
            _verdict(WORK_TASK, "approve", CRITIC_SID),
            _verdict(WORK_TASK, "ship", VERIFIER_SID),
        ]
    )

    assert not _owed(
        apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK
    )
    assert not _owed(apichats._get_session_obligations(CRITIC_SID), chats.ROLE_CRITIC, WORK_TASK)


def test_ungated_done_task_with_no_verdicts_stays_quiet(isolated_state):
    """done with ZERO verdicts → not yet under review; no reviewer is nagged."""
    _write_chat([_meta(), _done_task()])

    assert apichats._get_session_obligations(VERIFIER_SID) == []
    assert apichats._get_session_obligations(CRITIC_SID) == []


def test_non_member_reviewer_not_flagged(isolated_state):
    """A verifier-role session that isn't a member of this chat owes nothing here."""
    stranger = "99999999-9999-9999-9999-999999999999"
    _write_chat([_meta(), _done_task(), _verdict(WORK_TASK, "approve", CRITIC_SID)])

    assert apichats._get_session_obligations(stranger) == []


def test_pending_task_under_review_is_not_a_direct_verdict_obligation(isolated_state):
    """The direct-verdict path is `done`-only — a pending task never owes a verdict."""
    _write_chat(
        [
            _meta(),
            {"kind": chats.TASK, "id": WORK_TASK, "status": chats.TASK_PENDING},
            _verdict(WORK_TASK, "approve", CRITIC_SID),
        ]
    )

    assert not _owed(
        apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK
    )


# --- ISSUE 3 (muther 2026-06-18): gate-complete task not owed by assignee ------


def _assignee_meta() -> dict:
    m = _meta()
    m["member_roles"][AGENT_SID] = chats.ROLE_AGENT
    return m


def _named(obligations: list[dict], task_id: str) -> bool:
    """True if a NAMED-assignee obligation (not a reviewer-role one) exists."""
    return any(o.get("task_id") == task_id for o in obligations)


def test_gate_complete_inprogress_task_not_owed_by_assignee(isolated_state):
    """A task stuck at in_progress but with BOTH gate verdicts recorded is
    gate-complete — the assignee no longer owes it. Without this guard the watchdog
    false-wakes an agent whose work is finished + approved (the agent-2 recurrence)."""
    _write_chat(
        [
            _assignee_meta(),
            {
                "kind": chats.TASK,
                "id": WORK_TASK,
                "status": chats.TASK_IN_PROGRESS,
                "assignee_id": AGENT_SID,
            },
            _verdict(WORK_TASK, "approve", CRITIC_SID),
            _verdict(WORK_TASK, "ship", VERIFIER_SID),
        ]
    )
    assert not _named(apichats._get_session_obligations(AGENT_SID), WORK_TASK)


def test_inprogress_task_partial_gate_still_owed_by_assignee(isolated_state):
    """Control: an in_progress task with only ONE verdict is NOT gate-complete →
    the assignee still owes it (don't over-suppress)."""
    _write_chat(
        [
            _assignee_meta(),
            {
                "kind": chats.TASK,
                "id": WORK_TASK,
                "status": chats.TASK_IN_PROGRESS,
                "assignee_id": AGENT_SID,
            },
            _verdict(WORK_TASK, "approve", CRITIC_SID),  # critic only — gate incomplete
        ]
    )
    assert _named(apichats._get_session_obligations(AGENT_SID), WORK_TASK)
