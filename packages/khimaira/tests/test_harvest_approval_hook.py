"""Unit tests for khimaira.hooks.harvest_approval (PostToolUse hook).

Tests verify:
  - project:domain resolution path (assignee_name → domain, cwd → project)
  - distill is called with the curated text shape (decisions + done-report)
  - non-approved status → no distill call (noop)
  - missing assignee → no distill call (noop)
  - empty decisions + empty done_note → no distill call
  - fail-open: daemon HTTP errors don't raise

The Haiku distiller (mnemosyne HTTP) is MOCKED throughout — never called.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

import khimaira.hooks.harvest_approval as hook_mod

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stdin_payload(
    tool_name: str = "mcp__khimaira-chat__chat_task_update",
    new_status: str = "approved",
    task_id: str = "task-abc123",
    chat_id: str = "chat-xyz456",
    session_id: str = "aaaabbbb-cccc-dddd-eeee-ffffffffffff",
    cwd: str = "/home/user/dev/khimaira",
) -> str:
    return json.dumps(
        {
            "session_id": session_id,
            "tool_name": tool_name,
            "cwd": cwd,
            "tool_input": {
                "session_id": session_id,
                "chat_id": chat_id,
                "task_id": task_id,
                "new_status": new_status,
            },
        }
    )


def _write_chat_jsonl(
    path: Path,
    task_id: str,
    assignee_id: str,
    done_note: str | None,
    assignee_name: str = "agent-1",
) -> None:
    records = [
        {
            "kind": "task",
            "id": task_id,
            "assignee_id": assignee_id,
            "assignee_name": assignee_name,
            "body": "Do some backend work",
        },
        {
            "kind": "task_update",
            "task_id": task_id,
            "status": "done",
            "note": done_note,
        },
        {
            "kind": "task_update",
            "task_id": task_id,
            "status": "approved",
            "note": None,
        },
    ]
    with path.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec) + "\n")


def _fake_session_state(decisions: list[dict] | None = None) -> dict:
    return {
        "session_id": "assignee-session-uuid",
        "name": "agent-1",
        "recent_decisions": decisions or [],
    }


# ---------------------------------------------------------------------------
# Tests: project:domain resolution + distill call shape
# ---------------------------------------------------------------------------


def test_harvest_builds_correct_project_domain(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hook resolves project:domain from assignee name + cwd, calls distill."""
    chat_id = "chat-xyz456"
    task_id = "task-abc123"
    assignee_id = "assignee-0000-1111-2222-3333333333"
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()
    # assignee_name contains "backend-lead" → detect_domain resolves "backend"
    _write_chat_jsonl(
        chats_dir / f"{chat_id}.jsonl",
        task_id,
        assignee_id,
        "Backend work done.",
        assignee_name="backend-lead-1",
    )

    monkeypatch.setattr(hook_mod, "_CHATS_DIR", chats_dir)
    monkeypatch.setattr(
        hook_mod,
        "_get_session_state",
        lambda sid: _fake_session_state(
            [{"text": "Use Postgres", "why": "reliability"}]
        ),
    )
    monkeypatch.setattr(hook_mod, "detect_project", lambda cwd: "khimaira")

    captured: list[tuple] = []

    def _fake_distill(domain: str, transcript: str, slug: str, **_kw):
        captured.append((domain, transcript, slug))

    monkeypatch.setattr(hook_mod, "_mnemosyne_distill", _fake_distill)
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(_stdin_payload(cwd="/home/user/dev/khimaira"))
    )

    result = hook_mod.main()

    assert result == 0
    assert len(captured) == 1
    domain, text, slug = captured[0]
    assert domain == "khimaira:backend"  # project:domain qualified key
    assert slug == f"harvest-{task_id}"


def test_harvest_curated_text_contains_decisions_and_done_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Curated text passed to distill includes decisions + done-report."""
    chat_id = "chat-xyz456"
    task_id = "task-abc123"
    assignee_id = "assignee-0000-1111-2222-3333333333"
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()
    _write_chat_jsonl(
        chats_dir / f"{chat_id}.jsonl",
        task_id,
        assignee_id,
        "Implemented the hook. Tests pass.",
    )

    monkeypatch.setattr(hook_mod, "_CHATS_DIR", chats_dir)
    monkeypatch.setattr(
        hook_mod,
        "_get_session_state",
        lambda sid: _fake_session_state(
            [
                {"text": "Use stdlib urllib", "why": "no third-party deps in hooks"},
                {"text": "Fail-open on daemon errors", "why": "hooks must not block"},
            ]
        ),
    )
    monkeypatch.setattr(hook_mod, "detect_project", lambda cwd: "khimaira")

    captured_text: list[str] = []

    def _fake_distill(domain: str, transcript: str, slug: str, **_kw):
        captured_text.append(transcript)

    monkeypatch.setattr(hook_mod, "_mnemosyne_distill", _fake_distill)
    monkeypatch.setattr("sys.stdin", io.StringIO(_stdin_payload()))

    hook_mod.main()

    assert captured_text, "distill was not called"
    text = captured_text[0]
    assert "Use stdlib urllib" in text
    assert "no third-party deps in hooks" in text
    assert "Implemented the hook. Tests pass." in text


# ---------------------------------------------------------------------------
# Tests: noop paths
# ---------------------------------------------------------------------------


def test_harvest_noop_on_non_approved_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook does nothing when new_status is not 'approved'."""
    payload = _stdin_payload(new_status="done")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))

    with patch.object(hook_mod, "_mnemosyne_distill") as mock_distill:
        result = hook_mod.main()

    assert result == 0
    mock_distill.assert_not_called()


def test_harvest_noop_on_wrong_tool_name(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hook does nothing for tool calls that are not chat_task_update."""
    payload = _stdin_payload(tool_name="mcp__khimaira__session_log_decision")
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))

    with patch.object(hook_mod, "_mnemosyne_distill") as mock_distill:
        result = hook_mod.main()

    assert result == 0
    mock_distill.assert_not_called()


def test_harvest_noop_when_no_assignee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hook exits cleanly when the task has no assignee."""
    chat_id = "chat-xyz456"
    task_id = "task-abc123"
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()

    # Task with no assignee_id
    chat_path = chats_dir / f"{chat_id}.jsonl"
    chat_path.write_text(
        json.dumps({"kind": "task", "id": task_id, "assignee_id": None, "body": "x"})
        + "\n"
    )

    monkeypatch.setattr(hook_mod, "_CHATS_DIR", chats_dir)
    monkeypatch.setattr("sys.stdin", io.StringIO(_stdin_payload()))

    with patch.object(hook_mod, "_mnemosyne_distill") as mock_distill:
        result = hook_mod.main()

    assert result == 0
    mock_distill.assert_not_called()


def test_harvest_noop_when_no_decisions_and_no_done_note(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Hook does not call distill when assignee has no decisions and task has no done note."""
    chat_id = "chat-xyz456"
    task_id = "task-abc123"
    assignee_id = "assignee-0000-1111-2222-3333333333"
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()

    # Task with no done note
    chat_path = chats_dir / f"{chat_id}.jsonl"
    chat_path.write_text(
        json.dumps(
            {
                "kind": "task",
                "id": task_id,
                "assignee_id": assignee_id,
                "assignee_name": "agent-1",
                "body": "x",
            }
        )
        + "\n"
    )

    monkeypatch.setattr(hook_mod, "_CHATS_DIR", chats_dir)
    monkeypatch.setattr(
        hook_mod,
        "_get_session_state",
        lambda sid: _fake_session_state([]),  # empty decisions
    )
    monkeypatch.setattr("sys.stdin", io.StringIO(_stdin_payload()))

    with patch.object(hook_mod, "_mnemosyne_distill") as mock_distill:
        result = hook_mod.main()

    assert result == 0
    mock_distill.assert_not_called()


# ---------------------------------------------------------------------------
# Tests: fail-open
# ---------------------------------------------------------------------------


def test_harvest_fail_open_on_daemon_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Daemon HTTP failure does not raise — hook falls back gracefully."""
    chat_id = "chat-xyz456"
    task_id = "task-abc123"
    assignee_id = "assignee-0000-1111-2222-3333333333"
    chats_dir = tmp_path / "chats"
    chats_dir.mkdir()
    _write_chat_jsonl(chats_dir / f"{chat_id}.jsonl", task_id, assignee_id, "Done.")

    monkeypatch.setattr(hook_mod, "_CHATS_DIR", chats_dir)
    # Simulate daemon being down
    monkeypatch.setattr(hook_mod, "_get_session_state", lambda sid: None)
    monkeypatch.setattr(hook_mod, "detect_project", lambda cwd: "khimaira")

    captured: list[tuple] = []

    def _fake_distill(domain: str, transcript: str, slug: str, **_kw):
        captured.append((domain, transcript, slug))

    monkeypatch.setattr(hook_mod, "_mnemosyne_distill", _fake_distill)
    monkeypatch.setattr("sys.stdin", io.StringIO(_stdin_payload()))

    result = hook_mod.main()

    # Still distills with done_note even if daemon is down (no decisions)
    assert result == 0
    assert len(captured) == 1
    _, text, _ = captured[0]
    assert "Done." in text


def test_harvest_fail_open_on_empty_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty stdin payload → exit 0 silently."""
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    with patch.object(hook_mod, "_mnemosyne_distill") as mock_distill:
        result = hook_mod.main()

    assert result == 0
    mock_distill.assert_not_called()
