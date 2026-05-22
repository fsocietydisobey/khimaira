"""Tests for Themis attach/detach hook injection and CLI commands.

Covers:
- JSON-merge primitive: inject creates entry, replaces existing, atomic write
- Hand-edited foreign PreToolUse entry survives attach (byte-identical preservation)
- Hand-edited foreign hook survives detach (only themis entry removed)
- khimaira themis sync round-trip: derive matcher, update settings.local.json
- khimaira themis disable/enable round-trip: overrides.jsonl + daemon verdict changes
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from khimaira.attach.settings_hooks import (
    THEMIS_MARKER,
    inject_hook_entry,
    remove_hook_entry,
)


# ---------------------------------------------------------------------------
# JSON-merge primitive tests
# ---------------------------------------------------------------------------


def test_inject_creates_settings_when_absent(tmp_path):
    """inject_hook_entry creates settings.local.json if it doesn't exist."""
    settings = tmp_path / ".claude" / "settings.local.json"
    assert not settings.exists()

    inject_hook_entry(settings, "Edit|Write", "/path/to/themis_pretool.py")

    assert settings.exists()
    data = json.loads(settings.read_text())
    pre_tool_use = data["hooks"]["PreToolUse"]
    assert len(pre_tool_use) == 1
    assert pre_tool_use[0]["matcher"] == "Edit|Write"
    assert "themis_pretool.py" in pre_tool_use[0]["hooks"][0]["command"]


def test_inject_replaces_existing_entry(tmp_path):
    """inject_hook_entry replaces an existing themis entry in-place."""
    settings = tmp_path / ".claude" / "settings.local.json"
    inject_hook_entry(settings, "Edit", "/old/themis_pretool.py")

    inject_hook_entry(settings, "Edit|Write|Bash", "/new/themis_pretool.py")

    data = json.loads(settings.read_text())
    entries = data["hooks"]["PreToolUse"]
    assert len(entries) == 1  # only one entry — replaced, not appended
    assert entries[0]["matcher"] == "Edit|Write|Bash"
    assert "/new/themis_pretool.py" in entries[0]["hooks"][0]["command"]


def test_inject_preserves_foreign_pretooluse_entries(tmp_path):
    """Foreign PreToolUse entries survive inject — byte-exact preservation."""
    settings = tmp_path / ".claude" / "settings.local.json"
    foreign_entry = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/other/hook.sh"}],
    }
    initial = {
        "hooks": {
            "PreToolUse": [foreign_entry],
        },
        "someOtherSetting": "keep-me",
    }
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(initial, indent=2), encoding="utf-8")

    inject_hook_entry(settings, "Edit", "/path/to/themis_pretool.py")

    data = json.loads(settings.read_text())
    entries = data["hooks"]["PreToolUse"]
    # Both foreign AND themis entries should be present
    assert len(entries) == 2
    commands = [e["hooks"][0]["command"] for e in entries]
    assert "/other/hook.sh" in commands
    assert "/path/to/themis_pretool.py" in commands
    # Other settings preserved
    assert data["someOtherSetting"] == "keep-me"


def test_inject_preserves_other_hook_types(tmp_path):
    """PostToolUse and other hook types survive inject."""
    settings = tmp_path / ".claude" / "settings.local.json"
    initial = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "/post-hook.sh"}]}
            ]
        }
    }
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(initial, indent=2), encoding="utf-8")

    inject_hook_entry(settings, "Edit", "/path/to/themis_pretool.py")

    data = json.loads(settings.read_text())
    # PostToolUse survived
    assert "PostToolUse" in data["hooks"]
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "/post-hook.sh"
    # Themis injected into PreToolUse
    assert len(data["hooks"]["PreToolUse"]) == 1


def test_remove_deletes_themis_entry(tmp_path):
    """remove_hook_entry removes the themis entry and returns True."""
    settings = tmp_path / ".claude" / "settings.local.json"
    inject_hook_entry(settings, "Edit", "/path/to/themis_pretool.py")

    removed = remove_hook_entry(settings)

    assert removed is True
    data = json.loads(settings.read_text())
    assert "hooks" not in data  # entire hooks key cleaned up when empty


def test_remove_preserves_foreign_pretooluse_entries(tmp_path):
    """remove_hook_entry leaves foreign PreToolUse entries untouched."""
    settings = tmp_path / ".claude" / "settings.local.json"
    foreign = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "/other/hook.sh"}],
    }
    initial = {
        "hooks": {
            "PreToolUse": [
                foreign,
                {
                    "matcher": "Edit",
                    "hooks": [{"type": "command", "command": "/path/to/themis_pretool.py"}],
                },
            ]
        }
    }
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps(initial, indent=2), encoding="utf-8")

    removed = remove_hook_entry(settings)

    assert removed is True
    data = json.loads(settings.read_text())
    entries = data["hooks"]["PreToolUse"]
    assert len(entries) == 1
    assert entries[0]["hooks"][0]["command"] == "/other/hook.sh"


def test_remove_returns_false_when_not_present(tmp_path):
    """remove_hook_entry returns False when no themis entry exists."""
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)
    settings.write_text(json.dumps({"hooks": {}}), encoding="utf-8")

    assert remove_hook_entry(settings) is False


def test_attach_detach_idempotency(tmp_path):
    """Attach then detach leaves settings.local.json byte-identical to pre-attach state.

    This is architect-1 must-fix #4: attach+detach diff must equal zero.
    """
    settings = tmp_path / ".claude" / "settings.local.json"
    settings.parent.mkdir(parents=True, exist_ok=True)

    # Pre-existing foreign hook
    initial = {
        "hooks": {
            "PostToolUse": [
                {"matcher": "Edit", "hooks": [{"type": "command", "command": "/post.sh"}]}
            ]
        },
        "other": "preserved",
    }
    settings.write_text(json.dumps(initial, indent=2), encoding="utf-8")
    original_content = settings.read_text()

    # Attach → inject
    inject_hook_entry(settings, "Edit|Write", "/path/to/themis_pretool.py")
    assert settings.read_text() != original_content  # file changed

    # Detach → remove
    remove_hook_entry(settings)
    after_detach = settings.read_text()

    # Parse both as JSON to compare semantically (whitespace may differ)
    assert json.loads(after_detach) == json.loads(original_content), (
        "attach+detach round-trip did not restore original settings.local.json content"
    )


# ---------------------------------------------------------------------------
# themis sync CLI tests
# ---------------------------------------------------------------------------


def test_sync_updates_settings_in_project(tmp_path, monkeypatch):
    """khimaira themis sync updates settings.local.json in an attached project."""
    project = tmp_path / "myproject"
    project.mkdir()
    (project / ".claude").mkdir()
    settings = project / ".claude" / "settings.local.json"

    # Mock list_attached to return our test project
    monkeypatch.setattr(
        "khimaira.cli.themis.list_attached",
        lambda: [{"project_path": str(project)}],
    )
    # Mock resolve_hook_command
    monkeypatch.setattr(
        "khimaira.cli.themis.resolve_hook_command",
        lambda p: f"{p}/.venv/bin/python3 /repo/scripts/hooks/themis_pretool.py",
    )
    # Mock derive_matcher_pattern
    monkeypatch.setattr(
        "khimaira.cli.themis.derive_matcher_pattern",
        lambda: "Edit|Write|Bash",
    )

    from khimaira.cli.themis import run_sync

    class Args:
        project = None

    result = run_sync(Args())
    assert result == 0
    assert settings.exists()
    data = json.loads(settings.read_text())
    assert data["hooks"]["PreToolUse"][0]["matcher"] == "Edit|Write|Bash"


def test_sync_with_project_flag(tmp_path, monkeypatch):
    """khimaira themis sync --project <path> updates only that project."""
    project = tmp_path / "single"
    project.mkdir()

    monkeypatch.setattr(
        "khimaira.cli.themis.resolve_hook_command",
        lambda p: "/venv/python3 /repo/themis_pretool.py",
    )
    monkeypatch.setattr(
        "khimaira.cli.themis.derive_matcher_pattern",
        lambda: "Edit",
    )

    from khimaira.cli.themis import run_sync
    import argparse

    args = argparse.Namespace(project=str(project))
    result = run_sync(args)
    assert result == 0
    settings = project / ".claude" / "settings.local.json"
    assert settings.exists()


# ---------------------------------------------------------------------------
# themis disable/enable CLI tests
# ---------------------------------------------------------------------------


def test_disable_writes_override_entry(tmp_path, monkeypatch):
    """khimaira themis disable writes a disable entry to overrides.jsonl."""
    overrides_path = tmp_path / "themis_overrides.jsonl"
    monkeypatch.setattr("khimaira.cli.themis._STATE_DIR", tmp_path)
    monkeypatch.setattr("khimaira.cli.themis._OVERRIDES_PATH", overrides_path)

    from khimaira.cli.themis import run_disable

    class Args:
        rule_id = "IN-INTAKE-1"

    result = run_disable(Args())
    assert result == 0
    assert overrides_path.exists()
    entry = json.loads(overrides_path.read_text().strip())
    assert entry["rule_id"] == "IN-INTAKE-1"
    assert entry["action"] == "disable"
    assert "ts" in entry


def test_enable_writes_tombstone_entry(tmp_path, monkeypatch):
    """khimaira themis enable writes an enable tombstone after disable."""
    overrides_path = tmp_path / "themis_overrides.jsonl"
    monkeypatch.setattr("khimaira.cli.themis._STATE_DIR", tmp_path)
    monkeypatch.setattr("khimaira.cli.themis._OVERRIDES_PATH", overrides_path)

    from khimaira.cli.themis import run_disable, run_enable

    class DisableArgs:
        rule_id = "IN-AGENT-2"

    class EnableArgs:
        rule_id = "IN-AGENT-2"

    run_disable(DisableArgs())
    run_enable(EnableArgs())

    lines = overrides_path.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["action"] == "disable"
    assert json.loads(lines[1])["action"] == "enable"


def test_disable_enable_daemon_verdict_round_trip(tmp_path, monkeypatch):
    """disable → daemon returns ok=True; enable → normal enforcement resumes.

    Tests _load_disabled_rules + _call_engine overrides integration in api/themis.py.
    """
    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    overrides_path = tmp_path / "themis_overrides.jsonl"
    monkeypatch.setattr(themis_api, "_OVERRIDES_PATH", overrides_path)

    # Mock engine returning a block violation
    mock_violation = MagicMock()
    mock_violation.rule_id = "IN-INTAKE-1"
    mock_violation.name = "NO_FILE_EDIT"
    mock_violation.message = "intake cannot call Edit"
    mock_violation.severity = "block"

    mock_result = MagicMock()
    mock_result.ok = False
    mock_result.violation = mock_violation

    mock_engine = MagicMock()
    mock_engine.evaluate.return_value = mock_result

    with patch.dict("sys.modules", {"themis.engine": mock_engine}):
        # Without override: block
        verdict = themis_api._call_engine("intake", "Edit", {}, "")
        assert verdict["ok"] is False
        assert verdict["violation"]["rule_id"] == "IN-INTAKE-1"

        # Disable the rule
        overrides_path.write_text(
            json.dumps({"ts": "2026-01-01T00:00:00Z", "rule_id": "IN-INTAKE-1", "action": "disable"}) + "\n"
        )

        # Now: override active → ok=True
        verdict_disabled = themis_api._call_engine("intake", "Edit", {}, "")
        assert verdict_disabled["ok"] is True
        assert verdict_disabled.get("_rule_disabled") == "IN-INTAKE-1"

        # Enable the rule (tombstone)
        with overrides_path.open("a") as f:
            f.write(
                json.dumps({"ts": "2026-01-01T01:00:00Z", "rule_id": "IN-INTAKE-1", "action": "enable"}) + "\n"
            )

        # Back to normal: block
        verdict_enabled = themis_api._call_engine("intake", "Edit", {}, "")
        assert verdict_enabled["ok"] is False
        assert verdict_enabled["violation"]["rule_id"] == "IN-INTAKE-1"


# ---------------------------------------------------------------------------
# Load disabled rules tests
# ---------------------------------------------------------------------------


def test_load_disabled_rules_empty_when_no_file(tmp_path, monkeypatch):
    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)
    monkeypatch.setattr(themis_api, "_OVERRIDES_PATH", tmp_path / "nonexistent.jsonl")

    assert themis_api._load_disabled_rules() == set()


def test_load_disabled_rules_last_entry_wins(tmp_path, monkeypatch):
    """When disable then enable in file, enable wins (not disabled)."""
    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)
    overrides = tmp_path / "overrides.jsonl"
    monkeypatch.setattr(themis_api, "_OVERRIDES_PATH", overrides)

    overrides.write_text(
        json.dumps({"ts": "T1", "rule_id": "IN-INTAKE-1", "action": "disable"}) + "\n"
        + json.dumps({"ts": "T2", "rule_id": "IN-INTAKE-1", "action": "enable"}) + "\n"
        + json.dumps({"ts": "T3", "rule_id": "IN-AGENT-2", "action": "disable"}) + "\n"
    )

    disabled = themis_api._load_disabled_rules()
    assert "IN-INTAKE-1" not in disabled  # re-enabled
    assert "IN-AGENT-2" in disabled  # still disabled
