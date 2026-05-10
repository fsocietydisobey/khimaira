"""Unit tests for chimera.monitor.sessions — direct function calls.

Tests target the bugs we shipped + caught in one day on 2026-05-09:
  - post_notice with unknown session name → ValueError (then 404 at API layer)
  - log_question with target_session_id → resolved at write time
  - post_handoff + consume_handoffs → cwd-match + read tracking
  - ack_notes → archive move
  - search_archive → substring matching

If these regress, /inbox / handoffs / cross-session ack break in user-
facing ways. Run with `uv run pytest packages/chimera/tests/`.
"""

from __future__ import annotations

import pytest


def test_log_decision_round_trip(isolated_state):
    """Sanity check fixture works + happy path."""
    sid = "test-session-1"
    rec = isolated_state.log_decision(sid, "use Postgres", "ACID")
    assert rec["text"] == "use Postgres"
    assert rec["why"] == "ACID"
    state = isolated_state.state(sid)
    assert state["decision_count"] == 1
    assert state["recent_decisions"][0]["id"] == rec["id"]


def test_log_question_targeted(isolated_state):
    """Targeted question stamps target_session_id (resolved-or-literal)."""
    sid = "asker"
    # Target doesn't exist as a name yet — should fall back to literal
    q = isolated_state.log_question(
        sid, "approach a or b?", target_session_id="not-yet-named",
    )
    assert q["target_session_id"] == "not-yet-named"


def test_post_notice_unknown_target_raises(isolated_state):
    """Regression for 2026-05-09 bug: post_notice 500'd on bad name.

    The function should raise ValueError; the API layer wraps this into
    a 404. This test asserts the underlying contract.
    """
    with pytest.raises(ValueError, match="No session named or id'd"):
        isolated_state.post_notice(
            "definitely-not-a-real-session",
            text="this should fail",
            from_session_id="me",
        )


def test_post_notice_lands_in_inbox(isolated_state):
    """Notice posts → inbox, kind=notice, surface_count=0, unread."""
    target = "target-session"
    # Materialize the session by logging a decision
    isolated_state.log_decision(target, "init", "")

    note = isolated_state.post_notice(
        target, text="FYI thing happened", from_session_id="me",
    )
    assert note["kind"] == "notice"
    assert note["read"] is False
    assert note["surface_count"] == 0

    pending = isolated_state.pending_notes(target, mark_read=False)
    assert len(pending) == 1
    assert pending[0]["text"] == "FYI thing happened"


def test_pending_notes_marks_read_and_archives(isolated_state):
    """Drained notes → archive.jsonl, removed from inbox.jsonl."""
    target = "target-2"
    isolated_state.log_decision(target, "init", "")
    isolated_state.post_notice(target, text="msg 1", from_session_id="me")
    isolated_state.post_notice(target, text="msg 2", from_session_id="me")

    drained = isolated_state.pending_notes(target, mark_read=True)
    assert len(drained) == 2
    # Inbox now empty
    assert isolated_state.pending_notes(target, mark_read=False) == []
    # Archived
    archived = isolated_state.search_archive(target)
    assert len(archived) == 2
    assert {n["text"] for n in archived} == {"msg 1", "msg 2"}


def test_search_archive_substring(isolated_state):
    """search_archive matches body substring case-insensitively."""
    target = "target-3"
    isolated_state.log_decision(target, "init", "")
    isolated_state.post_notice(target, text="Roboflow concurrency check", from_session_id="me")
    isolated_state.post_notice(target, text="something unrelated", from_session_id="me")
    isolated_state.pending_notes(target, mark_read=True)

    hits = isolated_state.search_archive(target, query="roboflow")
    assert len(hits) == 1
    assert "Roboflow" in hits[0]["text"]


def test_post_handoff_cwd_inferred_from_files(isolated_state, tmp_path):
    """When scope_cwd not given, infer from from_session's file_touched dir."""
    asker = "asker"
    project = tmp_path / "myproject"
    project.mkdir()
    isolated_state.log_touch(asker, str(project / "main.py"), "edited")

    h = isolated_state.post_handoff(asker, "pickup notes", scope_cwd=None)
    assert h["scope_cwd"] == str(project)


def test_consume_handoffs_cwd_match_and_child(isolated_state, tmp_path):
    """Handoffs match exact scope_cwd OR any cwd under it. Read tracking."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    sub = project / "subdir"
    sub.mkdir()

    isolated_state.post_handoff(
        asker, text="hand off", scope_cwd=str(project), expires_in_hours=24,
    )

    # Exact match
    matched = isolated_state.consume_handoffs("future-session-1", str(project))
    assert len(matched) == 1
    # Same session re-consumes → empty (read tracking)
    matched_again = isolated_state.consume_handoffs("future-session-1", str(project))
    assert matched_again == []
    # Different session, child cwd → still matches
    matched_child = isolated_state.consume_handoffs("future-session-2", str(sub))
    assert len(matched_child) == 1


def test_consume_handoffs_unrelated_cwd_no_match(isolated_state, tmp_path):
    """Handoffs with non-matching scope_cwd are not surfaced."""
    asker = "asker"
    project = tmp_path / "project-a"
    project.mkdir()
    other = tmp_path / "project-b"
    other.mkdir()

    isolated_state.post_handoff(
        asker, text="for project-a", scope_cwd=str(project), expires_in_hours=24,
    )
    matched = isolated_state.consume_handoffs("session", str(other))
    assert matched == []


def test_consume_handoffs_expired_dropped(isolated_state, tmp_path, monkeypatch):
    """Expired handoffs are not surfaced AND dropped on next consume."""
    import time as time_mod

    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()

    # Negative TTL → expires_at is in the past
    isolated_state.post_handoff(
        asker, text="stale", scope_cwd=str(project), expires_in_hours=-1.0,
    )
    matched = isolated_state.consume_handoffs("session", str(project))
    assert matched == []
    # File should now be empty (or no longer contain the stale entry)
    if isolated_state._HANDOFFS_PATH.exists():
        contents = isolated_state._HANDOFFS_PATH.read_text()
        assert "stale" not in contents
