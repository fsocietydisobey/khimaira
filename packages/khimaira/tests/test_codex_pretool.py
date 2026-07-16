"""Focused tests for Codex's local-Themis PreToolUse adapter."""

from __future__ import annotations

import ast
import builtins
import inspect
import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from khimaira.hooks import codex_pretool, local_themis
from themis.data import EvalResult, Severity, ViolationDetail


def _rollout(home: Path, agent_id: str, agent_path: str) -> None:
    sessions_dir = home / ".codex" / "sessions" / "2026" / "07" / "16"
    sessions_dir.mkdir(parents=True)
    record = {"type": "session_meta", "payload": {"agent_path": agent_path}}
    (sessions_dir / f"rollout-test-{agent_id}.jsonl").write_text(
        json.dumps(record) + "\n", encoding="utf-8"
    )


def _violation(severity: Severity) -> EvalResult:
    return EvalResult(
        ok=False,
        role="master",
        violation=ViolationDetail(
            rule_id="TEST-RULE",
            name="TEST_RULE",
            severity=severity,
            message="test diagnostic",
        ),
    )


@pytest.mark.parametrize(
    ("agent_path", "expected"),
    [
        ("/root/agent_1", "agent"),
        ("/root/agent_22", "agent"),
        ("/root/consultant", "consultant"),
        ("gatekeeper", "gatekeeper"),
        ("/root/", None),
    ],
)
def test_derive_role_from_rollout_agent_path(agent_path: str, expected: str | None):
    assert codex_pretool._derive_role_from_agent_path(agent_path) == expected


def test_resolve_agent_role_reads_rollout_session_meta(tmp_path, monkeypatch):
    _rollout(tmp_path, "agent-id-123", "/root/agent_2")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert codex_pretool._resolve_agent_role("agent-id-123") == "agent"


def test_top_level_call_is_master_and_subagent_uses_rollout_role(tmp_path, monkeypatch):
    seen_roles: list[str] = []

    def fake_evaluate_local(role, tool_name, tool_input, cwd):
        seen_roles.append(role)
        return EvalResult(ok=True, role=role)

    monkeypatch.setattr(local_themis, "evaluate_local", fake_evaluate_local)
    assert codex_pretool.evaluate({"tool_name": "Read", "tool_input": {}}) is None

    _rollout(tmp_path, "subagent-456", "/root/gatekeeper")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert (
        codex_pretool.evaluate(
            {
                "agent_id": "subagent-456",
                "tool_name": "Read",
                "tool_input": {},
            }
        )
        is None
    )
    assert seen_roles == ["master", "gatekeeper"]


@pytest.mark.parametrize(
    ("role", "tool_name", "tool_input", "normalized_name", "has_gate_error"),
    [
        ("consultant", "Agent", {}, "Task", False),
        ("agent", "Bash", {"command": "git commit -m done"}, "Bash", True),
        ("agent", "Bash", {"command": "  git commit -m done"}, "Bash", True),
        (
            "agent",
            "Bash",
            {"command": "pytest -q && git commit -m done"},
            "Bash",
            True,
        ),
        (
            "agent",
            "Bash",
            {"command": "printf message | git commit -F -"},
            "Bash",
            True,
        ),
        (
            "agent",
            "Bash",
            {"command": "pytest -q\ngit commit -m done"},
            "Bash",
            True,
        ),
        ("agent", "Bash", {"command": "FOO=1 git commit -m done"}, "Bash", True),
        ("agent", "Bash", {"command": "command git commit -m done"}, "Bash", True),
        ("agent", "Bash", {"command": "env FOO=1 git commit -m done"}, "Bash", True),
        ("agent", "Bash", {"command": "sudo git commit -m done"}, "Bash", True),
        ("agent", "Bash", {"command": "bash -c 'git commit -m done'"}, "Bash", True),
        ("agent", "Bash", {"command": "rg 'git commit' packages"}, "Bash", False),
        ("agent", "Bash", {"command": "rg '; git commit' packages"}, "Bash", False),
        ("agent", "Bash", {"command": "git commit-tree HEAD^{tree}"}, "Bash", False),
        ("master", "Bash", {"command": "git commit -m done"}, "Bash", False),
        (
            "master",
            "mcp__khimaira-chat__chat_task_update",
            {"new_status": "approved"},
            "mcp__khimaira-chat__chat_task_update",
            True,
        ),
        (
            "master",
            "mcp__khimaira-chat__chat_task_update",
            {"new_status": "done"},
            "mcp__khimaira-chat__chat_task_update",
            False,
        ),
    ],
)
def test_local_adapter_builds_truthful_condition_payload(
    role,
    tool_name,
    tool_input,
    normalized_name,
    has_gate_error,
    monkeypatch,
):
    captured: dict = {}
    sentinel_rules = object()

    monkeypatch.setattr("themis.data.find_app_rules_dir", lambda cwd: Path(cwd))
    monkeypatch.setattr(
        "themis.data.load_rules",
        lambda loaded_role, app_rules_dir: sentinel_rules,
    )

    def fake_evaluate(
        evaluated_role,
        evaluated_tool_name,
        evaluated_tool_input,
        conditions_payload,
        rule_set,
    ):
        captured.update(
            role=evaluated_role,
            tool_name=evaluated_tool_name,
            tool_input=evaluated_tool_input,
            conditions=conditions_payload,
            rule_set=rule_set,
        )
        return EvalResult(ok=True, role=evaluated_role)

    monkeypatch.setattr("themis.engine.evaluate", fake_evaluate)

    local_themis.evaluate_local(role, tool_name, tool_input, "/repo")

    assert captured["role"] == role
    assert captured["tool_name"] == normalized_name
    assert captured["tool_input"] is tool_input
    assert captured["conditions"]["tool_name"] == normalized_name
    assert captured["conditions"]["tool_input"] is tool_input
    assert (captured["conditions"].get("gate_verdicts") == "error") is has_gate_error
    assert captured["rule_set"] is sentinel_rules


def test_packaged_catalog_rule_preserves_id_message_and_block_severity(tmp_path):
    outcome = local_themis.evaluate_local(
        "consultant", "Edit", {"file_path": "/repo/source.py"}, str(tmp_path)
    )

    assert outcome.ok is False
    assert outcome.violation is not None
    assert outcome.violation.rule_id == "IN-CONSULTANT-1"
    assert outcome.violation.severity is Severity.BLOCK
    assert "consultant cannot call Edit" in outcome.violation.message


@pytest.mark.parametrize(
    "command",
    [
        "printf message | git commit -F -",
        "FOO=1 git commit -m done",
        "command git commit -m done",
        "env FOO=1 git commit -m done",
        "sudo git commit -m done",
        "bash -c 'git commit -m done'",
    ],
)
def test_agent_commit_uses_local_error_sentinel_to_close_catalog_gate(tmp_path, command):
    outcome = local_themis.evaluate_local("agent", "Bash", {"command": command}, str(tmp_path))

    assert outcome.ok is False
    assert outcome.violation is not None
    assert outcome.violation.rule_id == "IN-AGENT-6"
    assert outcome.violation.severity is Severity.BLOCK
    assert "commit gate is not satisfied" in outcome.violation.message


@pytest.mark.parametrize(
    "command",
    ["rg '; git commit' packages", "git commit-tree HEAD^{tree}"],
)
def test_catalog_commit_matcher_false_positives_do_not_fabricate_gate_error(tmp_path, command):
    outcome = local_themis.evaluate_local("agent", "Bash", {"command": command}, str(tmp_path))

    assert outcome.ok is True
    assert outcome.violation is None


def test_master_approval_uses_local_error_sentinel_to_close_catalog_gate(tmp_path):
    outcome = local_themis.evaluate_local(
        "master",
        "mcp__khimaira-chat__chat_task_update",
        {"task_id": "task-123", "new_status": "approved"},
        str(tmp_path),
    )

    assert outcome.ok is False
    assert outcome.violation is not None
    assert outcome.violation.rule_id == "IN-MASTER-9"
    assert outcome.violation.severity is Severity.BLOCK
    assert "task's commit gate is not satisfied" in outcome.violation.message


def test_confirmed_agent_alias_reaches_packaged_task_rule(tmp_path):
    outcome = local_themis.evaluate_local("consultant", "Agent", {}, str(tmp_path))

    assert outcome.ok is False
    assert outcome.violation is not None
    assert outcome.violation.rule_id == "IN-CONSULTANT-3"


def test_project_app_rules_are_loaded_with_packaged_rules(tmp_path):
    (tmp_path / ".git").mkdir()
    rules_dir = tmp_path / ".claude" / "themis"
    rules_dir.mkdir(parents=True)
    (rules_dir / "consultant.yaml").write_text(
        textwrap.dedent(
            """\
            role: consultant
            invariants:
              - id: APP-CODEX-1
                name: NO_SECRET_READ
                severity: block
                matchers:
                  - tool: Read
                message: "APP-CODEX-1 blocks project reads"
            """
        ),
        encoding="utf-8",
    )

    outcome = local_themis.evaluate_local("consultant", "Read", {}, str(tmp_path))

    assert outcome.ok is False
    assert outcome.violation is not None
    assert outcome.violation.rule_id == "APP-CODEX-1"
    assert outcome.violation.message == "APP-CODEX-1 blocks project reads"


@pytest.mark.parametrize("severity", [Severity.WARN, Severity.AUDIT])
def test_codex_hook_surfaces_nonblocking_severity_diagnostically(severity, monkeypatch, capsys):
    monkeypatch.setattr(local_themis, "evaluate_local", lambda *args: _violation(severity))

    assert codex_pretool.evaluate({"tool_name": "Bash", "tool_input": {}}) is None
    diagnostic = capsys.readouterr().err
    assert severity.value in diagnostic
    assert "TEST-RULE" in diagnostic
    assert "test diagnostic" in diagnostic


def test_codex_hook_blocks_only_block_severity(monkeypatch):
    monkeypatch.setattr(
        local_themis,
        "evaluate_local",
        lambda *args: _violation(Severity.BLOCK),
    )

    assert codex_pretool.evaluate({"tool_name": "Bash", "tool_input": {}}) == (
        "TEST-RULE",
        "test diagnostic",
    )


def test_unknown_subagent_role_fails_open(tmp_path, monkeypatch, capsys):
    _rollout(tmp_path, "unknown-123", "/root/not-a-real-role")
    monkeypatch.setenv("HOME", str(tmp_path))

    assert (
        codex_pretool.evaluate({"agent_id": "unknown-123", "tool_name": "Read", "tool_input": {}})
        is None
    )
    assert "fail-open" in capsys.readouterr().err


def test_themis_import_failure_fails_open(monkeypatch, capsys):
    real_import = builtins.__import__

    def failing_import(name, *args, **kwargs):
        if name == "themis.data":
            raise ImportError("themis unavailable")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", failing_import)

    assert codex_pretool.evaluate({"tool_name": "Read", "tool_input": {}}) is None
    assert "themis unavailable" in capsys.readouterr().err


def test_local_evaluation_exception_fails_open(monkeypatch, capsys):
    def fail_evaluation(*args):
        raise RuntimeError("rules exploded")

    monkeypatch.setattr(local_themis, "evaluate_local", fail_evaluation)

    assert codex_pretool.evaluate({"tool_name": "Read", "tool_input": {}}) is None
    assert "rules exploded" in capsys.readouterr().err


def test_unexpected_main_exception_fails_open(monkeypatch, capsys):
    def fail_main():
        raise RuntimeError("adapter exploded")

    monkeypatch.setattr(codex_pretool, "main", fail_main)

    assert codex_pretool._run_main_fail_open() == 0
    assert "adapter exploded" in capsys.readouterr().err


def test_codex_hook_has_no_network_virtual_session_or_cache_dependency():
    tree = ast.parse(inspect.getsource(codex_pretool))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    assert imported_roots.isdisjoint({"urllib", "fcntl", "uuid", "requests", "httpx"})
    for removed_symbol in (
        "DAEMON",
        "_http",
        "_ensure_virtual_session",
        "_ensure_roster_chat",
        "_load_virtual_cache",
        "_load_roster_cache",
    ):
        assert not hasattr(codex_pretool, removed_symbol)


def test_subprocess_block_output_uses_codex_hook_protocol(tmp_path):
    _rollout(tmp_path, "consultant-123", "/root/consultant")
    payload = {
        "agent_id": "consultant-123",
        "tool_name": "Edit",
        "tool_input": {"file_path": "/repo/source.py"},
        "cwd": str(tmp_path),
    }

    completed = subprocess.run(
        [sys.executable, "-m", "khimaira.hooks.codex_pretool"],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=False,
        env={**__import__("os").environ, "HOME": str(tmp_path)},
        timeout=5,
    )

    assert completed.returncode == 0
    decision = json.loads(completed.stdout)
    assert decision["decision"] == "block"
    assert "IN-CONSULTANT-1" in decision["reason"]
