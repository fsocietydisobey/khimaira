"""Tests for session_delete + khimaira sessions CLI (cleanup/list-stale).

All tests use the `isolated_state` fixture from conftest.py to avoid
touching the real ~/.local/state/khimaira/ tree.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# session_delete unit tests
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _disable_alive_delete_guard(monkeypatch):
    """delete_session's alive-guard (root fix for chat-orphaning) refuses to delete a
    recently-active session. These unit tests create-then-delete fresh sessions, which
    the guard correctly treats as 'alive' — disable it here so they exercise the rest of
    delete_session. test_delete_session_refuses_active re-enables it + verifies it works."""
    monkeypatch.setenv("KHIMAIRA_ALIVE_DELETE_GUARD_S", "0")


def test_delete_session_refuses_active(isolated_state, monkeypatch):
    """Alive-guard: a currently-active session is refused (not deleted, not chat-left).

    Root fix for the orphaning bug — deleting a live session (e.g. a roster cleanup
    resolving a name to the LIVE same-named session) left it from its chats → state=left,
    which cannot self-rejoin. force=True must NOT override (active roster sessions carry
    decisions, so a force=True delete would otherwise bypass the guard)."""
    monkeypatch.setenv("KHIMAIRA_ALIVE_DELETE_GUARD_S", "900")
    sid = "active-session-guard"
    isolated_state.set_status(sid, "orchestrating", "")
    session_dir = isolated_state._session_dir(sid)

    result = isolated_state.delete_session(sid, force=True)

    assert "error" in result and result.get("active") is True
    assert "ACTIVE" in result["error"]
    assert session_dir.exists()  # untouched — not deleted, chats not left


def test_delete_session_without_decisions(isolated_state):
    """Happy path: session with no decisions is deleted; files removed."""
    sid = "delete-test-1"
    isolated_state.set_status(sid, "idle", "")
    isolated_state.log_touch(sid, "foo.py", "setup")

    session_dir = isolated_state._session_dir(sid)
    assert session_dir.exists()

    result = isolated_state.delete_session(sid)

    assert result["deleted"] is True
    assert result["session_id"] == sid
    assert result["had_decisions"] is False
    assert result["archived_to"] is None
    assert not session_dir.exists()


def test_delete_session_with_decisions_refuses_without_force(isolated_state):
    """structured error returned when session has decisions and force=False."""
    sid = "delete-test-2"
    isolated_state.log_decision(sid, "use postgres", "acid")
    session_dir = isolated_state._session_dir(sid)

    result = isolated_state.delete_session(sid, force=False)

    assert "error" in result
    assert "decision" in result["error"]
    assert result.get("decision_count") == 1
    # Files must be untouched
    assert session_dir.exists()


def test_delete_session_with_decisions_force_archives(isolated_state, tmp_path):
    """force=True archives decisions to _archive/ before deletion."""
    sid = "delete-test-3"
    isolated_state.log_decision(sid, "chose redis", "speed")
    isolated_state.log_decision(sid, "chose postgres", "acid")
    session_dir = isolated_state._session_dir(sid)

    result = isolated_state.delete_session(sid, force=True)

    assert result["deleted"] is True
    assert result["had_decisions"] is True
    assert result["archived_to"] is not None
    archive_path = Path(result["archived_to"])
    assert archive_path.exists()
    import json
    data = json.loads(archive_path.read_text())
    assert data["session_id"] == sid
    assert len(data["decisions"]) == 2
    # Session dir must be gone
    assert not session_dir.exists()


def test_delete_session_unknown_id(isolated_state):
    """Structured error for unknown session id."""
    result = isolated_state.delete_session("definitely-not-a-real-session-id")
    assert "error" in result
    assert "not found" in result["error"]


def test_delete_session_idempotent(isolated_state):
    """Calling delete on an already-deleted session returns error, not exception."""
    sid = "delete-test-5"
    isolated_state.set_status(sid, "idle", "")
    # Delete once
    isolated_state.delete_session(sid)
    # Delete again — must return structured error, not raise
    result = isolated_state.delete_session(sid)
    assert "error" in result
    assert "not found" in result["error"]


def test_delete_session_removes_chat_membership(isolated_state, monkeypatch):
    """After delete, the session is marked LEFT in its chats."""
    import importlib
    from khimaira.monitor import chats as chats_mod
    importlib.reload(chats_mod)

    # Use UUID-format IDs so chats._resolve_or_uuid trusts them verbatim
    sid_master = "00000000-0000-0000-0000-000000000001"
    sid_member = "00000000-0000-0000-0000-000000000002"

    # Create sessions so isolated_state knows them
    isolated_state.set_status(sid_master, "active", "")
    isolated_state.set_status(sid_member, "active", "")

    # Create a chat with two members
    chat = chats_mod.create_room(
        creator_session_id=sid_master,
        member_session_ids=[sid_master, sid_member],
        title="test-del",
    )
    chat_id = chat["meta"]["chat_id"]

    # Accept the non-master member (creator is auto-accepted)
    chats_mod.accept(chat_id, sid_member)

    # Delete the non-master member
    result = isolated_state.delete_session(sid_member)

    assert result["deleted"] is True
    assert chat_id in result["chats_left"]

    # Verify membership is now LEFT in the chat
    room = chats_mod.load_room(chat_id)
    assert room["members"][sid_member]["state"] == chats_mod.LEFT


def test_delete_session_self_guard(isolated_state, monkeypatch):
    """Cannot delete the current session (self-delete guard)."""
    sid = "self-session-id"
    isolated_state.set_status(sid, "active", "")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", sid)
    # Must reload after monkeypatching
    import importlib
    importlib.reload(isolated_state)

    result = isolated_state.delete_session(sid)
    assert "error" in result
    assert "current session" in result["error"]


# ---------------------------------------------------------------------------
# CLI tests — sessions cleanup / list-stale
# ---------------------------------------------------------------------------


def _make_old_session(isolated_state, sid: str, age_hours: float, decisions: int = 0) -> None:
    """Helper: create a session with a backdated mtime."""
    isolated_state.set_status(sid, "idle", "")
    if decisions:
        for i in range(decisions):
            isolated_state.log_decision(sid, f"decision {i}", "")
    # Backdate files to simulate age
    session_dir = isolated_state._session_dir(sid)
    old_time = time.time() - age_hours * 3600
    for f in session_dir.iterdir():
        import os
        os.utime(f, (old_time, old_time))


def test_cleanup_dry_run(isolated_state, capsys):
    """--dry-run prints a preview but does NOT delete."""
    _make_old_session(isolated_state, "old-1", age_hours=72)
    _make_old_session(isolated_state, "old-2", age_hours=96)
    session_dir_1 = isolated_state._session_dir("old-1")
    session_dir_2 = isolated_state._session_dir("old-2")

    import argparse
    from khimaira.cli.sessions import _run_cleanup

    args = argparse.Namespace(
        older_than=48.0,
        dry_run=True,
        yes=False,
        include_with_decisions=False,
    )
    # Patch find-stale to use isolated_state's list_sessions
    with patch("khimaira.cli.sessions._find_stale_sessions") as mock_find:
        sessions = isolated_state.list_sessions(use_cache=False)
        stale = [s for s in sessions if (s.get("last_active_age_s") or 0) > 48 * 3600]
        mock_find.return_value = stale
        rc = _run_cleanup(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "Dry run" in captured.out
    # Sessions must NOT be deleted
    assert session_dir_1.exists()
    assert session_dir_2.exists()


def test_cleanup_with_yes_no_prompt(isolated_state, capsys):
    """--yes skips prompt and deletes all matching sessions."""
    _make_old_session(isolated_state, "old-yes-1", age_hours=72)
    session_dir = isolated_state._session_dir("old-yes-1")

    import argparse
    from khimaira.cli.sessions import _run_cleanup

    args = argparse.Namespace(
        older_than=48.0,
        dry_run=False,
        yes=True,
        include_with_decisions=False,
    )
    with patch("khimaira.cli.sessions._find_stale_sessions") as mock_find:
        sessions = isolated_state.list_sessions(use_cache=False)
        stale = [s for s in sessions if (s.get("last_active_age_s") or 0) > 48 * 3600]
        mock_find.return_value = stale
        with patch("khimaira.monitor.sessions.delete_session", wraps=isolated_state.delete_session):
            rc = _run_cleanup(args)

    assert rc == 0
    captured = capsys.readouterr()
    assert "Deleted" in captured.out
    assert not session_dir.exists()


def test_list_stale_only_lists_no_delete(isolated_state, capsys):
    """list-stale prints sessions but never mutates state."""
    _make_old_session(isolated_state, "old-list-1", age_hours=60)
    session_dir = isolated_state._session_dir("old-list-1")

    import argparse
    from khimaira.cli.sessions import _run_list_stale

    args = argparse.Namespace(older_than=48.0)
    with patch("khimaira.cli.sessions._find_stale_sessions") as mock_find:
        sessions = isolated_state.list_sessions(use_cache=False)
        stale = [s for s in sessions if (s.get("last_active_age_s") or 0) > 48 * 3600]
        mock_find.return_value = stale
        rc = _run_list_stale(args)

    assert rc == 0
    # Session must NOT be deleted
    assert session_dir.exists()
