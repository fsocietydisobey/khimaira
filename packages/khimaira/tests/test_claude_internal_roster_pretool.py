"""Subprocess tests for Claude's local-Themis internal-roster hook."""

from __future__ import annotations

import builtins
import json
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from khimaira.hooks import claude_internal_roster_pretool as hook

_HOOK = Path(hook.__file__).resolve()


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
        "cwd": str(Path(__file__).resolve().parents[3]),
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


def _assert_allowed(payload: object, diagnostic: str | None = None) -> None:
    completed = _run(payload)
    assert completed.returncode == 0, completed.stderr.decode()
    if diagnostic:
        assert diagnostic in completed.stderr.decode()


def _assert_denied(payload: object, *expected: str) -> None:
    completed = _run(payload)
    assert completed.returncode == 2, completed.stderr.decode()
    stderr = completed.stderr.decode()
    for fragment in expected:
        assert fragment in stderr


@pytest.mark.parametrize("tool_name", ["Edit", "Write", "NotebookEdit"])
def test_main_thread_is_outside_roster_policy(tool_name: str) -> None:
    _assert_allowed(_payload(None, tool_name, {"file_path": "/repo/source.py"}))


def test_unrelated_subagent_is_outside_roster_policy() -> None:
    _assert_allowed(_payload("Explore", "Edit", {"file_path": "/repo/source.py"}))


def test_unknown_reserved_agent_type_is_denied() -> None:
    _assert_denied(
        _payload("khimaira-internal-agnet", "Read", {"file_path": "/repo/a.py"}),
        "UNKNOWN_ROSTER_ROLE",
        "unknown reserved roster agent_type",
    )


@pytest.mark.parametrize("tool_name", ["Edit", "Write", "MultiEdit", "NotebookEdit"])
def test_consultant_edit_uses_real_catalog_rule(tool_name: str) -> None:
    _assert_denied(
        _payload("khimaira-internal-consultant", tool_name, {"file_path": "/repo/test.py"}),
        "IN-CONSULTANT-1",
        "NO_FILE_EDIT",
    )


def test_gatekeeper_may_edit_tests_but_not_production() -> None:
    _assert_allowed(
        _payload(
            "khimaira-internal-gatekeeper",
            "Edit",
            {"file_path": "/repo/tests/test_feature.py"},
        )
    )
    _assert_denied(
        _payload(
            "khimaira-internal-gatekeeper",
            "Edit",
            {"file_path": "/repo/src/feature.py"},
        ),
        "IN-GATEKEEPER-1",
        "NO_NONTEST_FILE_EDIT",
    )


@pytest.mark.parametrize(
    ("agent_type", "rule_id"),
    [
        ("khimaira-internal-consultant", "IN-CONSULTANT-3"),
        ("khimaira-internal-gatekeeper", "IN-GATEKEEPER-3"),
    ],
)
@pytest.mark.parametrize("tool_name", ["Task", "Agent"])
def test_agent_alias_hits_task_nested_agent_rules(
    agent_type: str, rule_id: str, tool_name: str
) -> None:
    _assert_denied(
        _payload(agent_type, tool_name, {"subagent_type": "Explore"}),
        rule_id,
        "NO_STANDALONE_AGENTS",
    )


def test_agent_task_behavior_now_follows_catalog() -> None:
    """The agent catalog has no Task invariant; remove the handwritten block."""
    _assert_allowed(_payload("khimaira-internal-agent", "Agent", {"subagent_type": "Explore"}))


@pytest.mark.parametrize(
    "command",
    [
        "git diff --stat && rg TODO packages",
        "pytest -q packages/khimaira/tests/test_example.py",
        "git log -1 2>/dev/null",
    ],
)
def test_advisory_roles_allow_read_only_bash(command: str) -> None:
    _assert_allowed(_payload("khimaira-internal-gatekeeper", "Bash", {"command": command}))


@pytest.mark.parametrize(
    ("command", "rule_id"),
    [
        ("git push origin main", "IN-UNIVERSAL-1"),
        ("sudo git reset --hard HEAD", "IN-UNIVERSAL-1"),
        ("rm -rf build", "IN-CONSULTANT-2"),
        ("git status > status.txt", "IN-CONSULTANT-2"),
    ],
)
def test_universal_and_role_bash_matchers_replace_old_parser(command: str, rule_id: str) -> None:
    _assert_denied(
        _payload("khimaira-internal-consultant", "Bash", {"command": command}),
        rule_id,
    )


@pytest.mark.parametrize("command", ["rg 'git commit' packages", "git status > /tmp/status.txt"])
def test_catalog_regex_false_positives_are_visible_deltas(command: str) -> None:
    """The shared catalog owns matching, including its current conservative regexes."""
    _assert_denied(
        _payload("khimaira-internal-gatekeeper", "Bash", {"command": command}),
        "IN-GATEKEEPER-2",
    )


@pytest.mark.parametrize(
    "command",
    [
        "git add source.py",
        "git worktree add /tmp/review HEAD",
        "sed -i 's/a/b/' source.py",
        "printf content | tee source.py",
        "git status >/absolute/status.txt",
    ],
)
@pytest.mark.parametrize(
    "agent_type",
    ["khimaira-internal-consultant", "khimaira-internal-gatekeeper"],
)
def test_advisory_mutation_gaps_are_explicit_catalog_authority_deltas(
    agent_type: str,
    command: str,
) -> None:
    """Document weaker real-YAML coverage after deleting the broader local parser.

    These are role-boundary violations by prose, but no current universal,
    consultant, or gatekeeper matcher covers them. The standalone adapter must
    expose that catalog gap instead of silently preserving a private second
    policy engine. Tightening them belongs in the shared Themis YAML catalog.
    """
    _assert_allowed(_payload(agent_type, "Bash", {"command": command}))


def test_implementer_can_edit_and_run_read_only_git() -> None:
    _assert_allowed(_payload("khimaira-internal-agent", "Edit", {"file_path": "/repo/source.py"}))
    _assert_allowed(_payload("khimaira-internal-agent", "Bash", {"command": "git status"}))


def test_agent_commit_blocks_via_local_gate_sentinel() -> None:
    _assert_denied(
        _payload(
            "khimaira-internal-agent",
            "Bash",
            {"command": "printf message | git commit -F -"},
        ),
        "IN-AGENT-6",
        "GATE_BEFORE_COMMIT",
    )


def test_agent_no_verify_and_universal_git_rules_use_catalog() -> None:
    _assert_denied(
        _payload(
            "khimaira-internal-agent",
            "Bash",
            {"command": "pytest --no-verify"},
        ),
        "IN-AGENT-2",
        "NO_NO_VERIFY",
    )
    _assert_denied(
        _payload("khimaira-internal-agent", "Bash", {"command": "git push origin main"}),
        "IN-UNIVERSAL-1",
        "NO_STATE_CHANGING_GIT",
    )


@pytest.mark.parametrize("command", ["git add source.py", "git -C /repo commit -m done"])
def test_commands_outside_catalog_regexes_are_allowed(command: str) -> None:
    """Document intentional deltas from the deleted handwritten shell parser."""
    _assert_allowed(_payload("khimaira-internal-agent", "Bash", {"command": command}))


def test_condition_local_agent_warning_is_evaluated_but_allowed() -> None:
    _assert_allowed(
        _payload(
            "khimaira-internal-agent",
            "mcp__khimaira-chat__chat_task_update",
            {"new_status": "done", "note": "implementation complete"},
        ),
        "IN-AGENT-4",
    )


def test_daemon_state_conditions_remain_inactive_locally() -> None:
    completed = _run(
        _payload(
            "khimaira-internal-agent",
            "Edit",
            {"file_path": "/repo/source.py", "new_string": "safe"},
        )
    )
    assert completed.returncode == 0, completed.stderr.decode()
    assert b"IN-AGENT-5" not in completed.stderr
    assert b"IN-AGENT-7" not in completed.stderr


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


def test_local_themis_evaluation_error_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    payload = _payload("khimaira-internal-agent", "Edit", {"file_path": "/repo/a.py"})
    with patch(
        "khimaira.hooks.local_themis.evaluate_local",
        side_effect=RuntimeError("catalog unavailable"),
    ):
        assert hook.evaluate(payload) is None
    assert "fail-open" in capsys.readouterr().err


def test_local_themis_import_error_fails_open(capsys: pytest.CaptureFixture[str]) -> None:
    payload = _payload("khimaira-internal-agent", "Edit", {"file_path": "/repo/a.py"})
    real_import = builtins.__import__

    def failing_import(name: str, *args: object, **kwargs: object) -> object:
        if name == "themis.data":
            raise ImportError("themis unavailable")
        return real_import(name, *args, **kwargs)

    with patch("builtins.__import__", side_effect=failing_import):
        assert hook.evaluate(payload) is None
    assert "fail-open" in capsys.readouterr().err


def test_unexpected_main_exception_fails_open(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    def fail_main() -> int:
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(hook, "main", fail_main)

    assert hook._run_main_fail_open() == 0
    assert "adapter exploded" in capsys.readouterr().err


def test_malformed_json_shape_fails_open() -> None:
    _assert_allowed(["not", "an", "object"])
