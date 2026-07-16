"""Subprocess tests for the standalone Claude internal-roster PreToolUse hook."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

_HOOK = (
    Path(__file__).parents[1] / "src" / "khimaira" / "hooks" / "claude_internal_roster_pretool.py"
)


def _payload(
    agent_type: str | None,
    tool_name: str,
    tool_input: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "session_id": "session-123",
        "hook_event_name": "PreToolUse",
        "tool_name": tool_name,
        "tool_input": tool_input or {},
    }
    if agent_type is not None:
        payload["agent_id"] = "agent-123"
        payload["agent_type"] = agent_type
    return payload


def _run(payload: object) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        [sys.executable, str(_HOOK)],
        input=json.dumps(payload).encode(),
        capture_output=True,
        check=False,
        timeout=5,
    )


def _assert_allowed(payload: object) -> None:
    completed = _run(payload)
    assert completed.returncode == 0, completed.stderr.decode()


def _assert_denied(payload: object, expected: str) -> None:
    completed = _run(payload)
    assert completed.returncode == 2
    assert expected in completed.stderr.decode()


@pytest.mark.parametrize("tool_name", ["Edit", "Write", "NotebookEdit"])
def test_main_thread_is_outside_roster_policy(tool_name: str) -> None:
    _assert_allowed(_payload(None, tool_name, {"file_path": "/repo/source.py"}))


def test_unrelated_subagent_is_outside_roster_policy() -> None:
    _assert_allowed(_payload("Explore", "Edit", {"file_path": "/repo/source.py"}))


def test_unknown_reserved_agent_type_is_denied() -> None:
    _assert_denied(
        _payload("khimaira-internal-agnet", "Read", {"file_path": "/repo/a.py"}),
        "UNKNOWN_ROSTER_ROLE",
    )


@pytest.mark.parametrize(
    "agent_type",
    ["khimaira-internal-consultant", "khimaira-internal-gatekeeper"],
)
@pytest.mark.parametrize("tool_name", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_advisory_roles_cannot_edit(agent_type: str, tool_name: str) -> None:
    _assert_denied(
        _payload(agent_type, tool_name, {"file_path": "/repo/test_feature.py"}),
        "NO_FILE_EDIT",
    )


@pytest.mark.parametrize(
    "agent_type",
    [
        "khimaira-internal-consultant",
        "khimaira-internal-gatekeeper",
        "khimaira-internal-agent",
    ],
)
@pytest.mark.parametrize("tool_name", ["Agent", "Task"])
def test_roster_members_cannot_spawn_nested_agents(agent_type: str, tool_name: str) -> None:
    _assert_denied(
        _payload(agent_type, tool_name, {"subagent_type": "Explore"}),
        "NO_NESTED_AGENTS",
    )


@pytest.mark.parametrize(
    "command",
    [
        "git diff --stat && rg TODO packages",
        "pytest -q packages/khimaira/tests/test_example.py",
        "rg 'git commit' packages",
        "git status > /tmp/status.txt",
        "git log -1 2>/dev/null",
    ],
)
def test_advisory_roles_allow_read_only_bash(command: str) -> None:
    _assert_allowed(_payload("khimaira-internal-gatekeeper", "Bash", {"command": command}))


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        ("git commit -m done", "NO_BASH_MUTATING"),
        ("sudo git push origin main", "NO_BASH_MUTATING"),
        ("rm -rf build", "NO_BASH_MUTATING"),
        ("sed -i 's/a/b/' source.py", "NO_BASH_MUTATING"),
        ("git status > status.txt", "NO_BASH_MUTATING"),
        ("bash -c 'git reset --hard HEAD'", "NO_BASH_MUTATING"),
    ],
)
def test_advisory_roles_block_mutating_bash(command: str, expected: str) -> None:
    _assert_denied(
        _payload("khimaira-internal-consultant", "Bash", {"command": command}),
        expected,
    )


def test_implementer_can_edit_and_run_read_only_git() -> None:
    _assert_allowed(_payload("khimaira-internal-agent", "Edit", {"file_path": "/repo/source.py"}))
    _assert_allowed(_payload("khimaira-internal-agent", "Bash", {"command": "git status"}))


@pytest.mark.parametrize(
    "command",
    [
        "git commit -m done",
        "git -C /repo commit -m done",
        "env TOKEN=x git push origin main",
        "sudo -u build git add source.py",
        "bash -c 'git checkout -b feature'",
        "bash -lc 'git commit -m done'",
        "git commit --no-verify -m done",
    ],
)
def test_implementer_cannot_mutate_git_state(command: str) -> None:
    _assert_denied(
        _payload("khimaira-internal-agent", "Bash", {"command": command}),
        "NO_",
    )


def test_malformed_roster_bash_payload_is_denied() -> None:
    _assert_denied(
        _payload("khimaira-internal-agent", "Bash", {"command": 42}),
        "MALFORMED_BASH",
    )


def test_agent_id_without_agent_type_fails_open_with_diagnostic() -> None:
    payload = _payload(None, "Edit", {"file_path": "/repo/source.py"})
    payload["agent_id"] = "agent-123"
    completed = _run(payload)
    assert completed.returncode == 0
    assert b"agent_id but no string agent_type" in completed.stderr


def test_malformed_json_shape_fails_open() -> None:
    _assert_allowed(["not", "an", "object"])
