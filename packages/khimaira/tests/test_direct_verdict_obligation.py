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
from pathlib import Path

from khimaira.monitor import chats
from khimaira.monitor.api import chats as apichats

CRITIC_SID = "11111111-1111-1111-1111-111111111111"
VERIFIER_SID = "22222222-2222-2222-2222-222222222222"
MASTER_SID = "33333333-3333-3333-3333-333333333333"
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
    from datetime import datetime, timezone

    return {
        "kind": chats.TASK,
        "id": WORK_TASK,
        "status": chats.TASK_DONE,
        "ts": ts or datetime.now(timezone.utc).isoformat(),
    }


def _verdict(task_id: str, verdict: str, by: str) -> dict:
    return {
        "kind": chats.TASK_VERDICT,
        "task_id": task_id,
        "verdict": verdict,
        "by_session_id": by,
    }


def _owed(obligations: list[dict], role: str, task_id: str) -> bool:
    return any(
        o.get("owed_verdict") == role and o.get("task_id") == task_id
        for o in obligations
    )


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

    assert not _owed(
        apichats._get_session_obligations(CRITIC_SID), chats.ROLE_CRITIC, WORK_TASK
    )
    assert _owed(
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
    from datetime import datetime, timezone

    _write_chat(
        [
            _meta(),
            _done_task(ts=datetime.now(timezone.utc).isoformat()),
            _verdict(WORK_TASK, "approve", CRITIC_SID),
        ]
    )

    assert _owed(
        apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK
    )


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
    assert not _owed(
        apichats._get_session_obligations(CRITIC_SID), chats.ROLE_CRITIC, WORK_TASK
    )


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
