"""Focused tests for the declarative Claude Code hook installer."""

from __future__ import annotations

import argparse
import copy
import json

from khimaira.bootstrap import checks as bootstrap_checks
from khimaira.cli import install_hooks


def _command(entry: dict) -> str:
    return entry["hooks"][0]["command"]


def _args(settings_path, *, uninstall: bool = False) -> argparse.Namespace:
    return argparse.Namespace(
        uninstall=uninstall,
        settings_path=str(settings_path),
        scripts_dir=None,
        dry_run=False,
    )


def test_add_registers_roster_as_independent_pretool_entry():
    themis_entry = {
        "matcher": "Edit|Write|Bash",
        "hooks": [
            {
                "type": "command",
                "command": "/venv/python /repo/scripts/hooks/themis_pretool.py",
            }
        ],
    }
    foreign_entry = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/opt/foreign-hook"}],
    }
    settings = {"hooks": {"PreToolUse": [themis_entry, foreign_entry]}}
    original = copy.deepcopy(settings)

    merged = install_hooks._add_khimaira_hooks(settings)

    assert settings == original
    pretool_entries = merged["hooks"]["PreToolUse"]
    assert pretool_entries[:2] == [themis_entry, foreign_entry]
    assert len(pretool_entries) == 3
    roster_hook = pretool_entries[-1]["hooks"][0]
    assert roster_hook == {
        "type": "command",
        "command": install_hooks._build_hook_command("claude_internal_roster_pretool"),
        install_hooks._KHIMAIRA_MARKER: (install_hooks._CLAUDE_INTERNAL_ROSTER_MARKER),
    }


def test_add_migrates_unmarked_filesystem_roster_without_duplication():
    themis_entry = {
        "hooks": [{"type": "command", "command": "/repo/scripts/hooks/themis_pretool.py"}]
    }
    legacy_roster_entry = {
        "hooks": [
            {
                "type": "command",
                "command": "/venv/python /repo/claude_internal_roster_pretool.py",
            }
        ]
    }
    settings = {"hooks": {"PreToolUse": [themis_entry, legacy_roster_entry]}}

    merged = install_hooks._add_khimaira_hooks(settings)

    pretool_entries = merged["hooks"]["PreToolUse"]
    assert pretool_entries[0] == themis_entry
    assert len(pretool_entries) == 2
    assert _command(pretool_entries[1]) == install_hooks._build_hook_command(
        "claude_internal_roster_pretool"
    )


def test_add_is_idempotent_and_does_not_duplicate_roster():
    once = install_hooks._add_khimaira_hooks({})
    twice = install_hooks._add_khimaira_hooks(once)

    assert twice == once
    roster_commands = [
        _command(entry)
        for entry in twice["hooks"]["PreToolUse"]
        if "claude_internal_roster_pretool" in _command(entry)
    ]
    assert roster_commands == [install_hooks._build_hook_command("claude_internal_roster_pretool")]


def test_uninstall_removes_roster_and_other_khimaira_hooks_only():
    themis_entry = {
        "hooks": [{"type": "command", "command": "/repo/scripts/hooks/themis_pretool.py"}]
    }
    foreign_session_entry = {"hooks": [{"type": "command", "command": "/opt/session-hook"}]}
    installed = install_hooks._add_khimaira_hooks(
        {
            "hooks": {
                "PreToolUse": [themis_entry],
                "SessionStart": [foreign_session_entry],
            }
        }
    )

    stripped = install_hooks._strip_khimaira_hooks(installed)

    assert stripped["hooks"]["PreToolUse"] == [themis_entry]
    assert stripped["hooks"]["SessionStart"] == [foreign_session_entry]
    assert "PostToolUse" not in stripped["hooks"]
    assert "UserPromptSubmit" not in stripped["hooks"]
    assert "SubagentStop" not in stripped["hooks"]


def test_run_reports_no_changes_without_rewriting(tmp_path, capsys):
    settings_path = tmp_path / ".claude" / "settings.json"
    settings_path.parent.mkdir()
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "/repo/scripts/hooks/themis_pretool.py",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    assert install_hooks.run(_args(settings_path)) == 0
    first_output = capsys.readouterr().out
    assert "PreToolUse" in first_output
    assert "claude_internal_roster_pretool" in first_output
    installed_content = settings_path.read_text(encoding="utf-8")
    backups = list(settings_path.parent.glob("settings.json.bak.*"))
    assert len(backups) == 1

    assert install_hooks.run(_args(settings_path)) == 0
    second_output = capsys.readouterr().out
    assert "no changes needed" in second_output
    assert settings_path.read_text(encoding="utf-8") == installed_content
    assert list(settings_path.parent.glob("settings.json.bak.*")) == backups


def test_bootstrap_check_requires_roster_and_all_lifecycle_hooks(tmp_path, monkeypatch):
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(json.dumps(install_hooks._add_khimaira_hooks({})), encoding="utf-8")
    monkeypatch.setattr(bootstrap_checks, "SETTINGS_PATH", settings_path, raising=False)
    monkeypatch.setattr(install_hooks, "SETTINGS_PATH", settings_path)

    current = bootstrap_checks.check_claude_hooks()

    assert current.status == "unchanged"
    assert "all 5 events" in current.detail

    without_roster = json.loads(settings_path.read_text(encoding="utf-8"))
    del without_roster["hooks"]["PreToolUse"]
    settings_path.write_text(json.dumps(without_roster), encoding="utf-8")

    missing = bootstrap_checks.check_claude_hooks()
    assert missing.status == "created"
    assert "PreToolUse" in missing.detail
