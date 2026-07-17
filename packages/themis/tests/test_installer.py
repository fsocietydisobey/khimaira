"""Standalone internal-roster packaging and installer tests."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest
from themis import installer
from themis.cli import main

_REPO_ROOT = Path(__file__).resolve().parents[3]
_AGENTS = (
    "khimaira-internal-consultant.md",
    "khimaira-internal-gatekeeper.md",
    "khimaira-internal-agent.md",
)


def test_packaged_agents_match_project_definitions() -> None:
    for filename in _AGENTS:
        packaged = installer._asset_text(filename)
        project_copy = (_REPO_ROOT / ".claude" / "agents" / filename).read_text(encoding="utf-8")
        assert packaged == project_copy, f"packaged agent drift: {filename}"


def test_install_is_idempotent_and_preserves_unrelated_claude_hooks(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "home" / ".claude" / "settings.json"
    agents = tmp_path / "home" / ".claude" / "agents"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps(
            {
                "theme": "dark",
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "Read",
                            "hooks": [{"type": "command", "command": "other-hook"}],
                        }
                    ],
                    "Stop": [{"hooks": [{"type": "command", "command": "stop"}]}],
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    first = installer.install_internal_roster(
        claude_settings=settings,
        claude_agents_dir=agents,
    )
    first_settings = settings.read_bytes()
    first_agents = {path.name: path.read_bytes() for path in agents.iterdir()}
    first_mtime = settings.stat().st_mtime_ns
    second = installer.install_internal_roster(
        claude_settings=settings,
        claude_agents_dir=agents,
    )

    assert any(change.status == "updated" for change in first)
    assert all(change.status == "unchanged" for change in second)
    assert settings.read_bytes() == first_settings
    assert settings.stat().st_mtime_ns == first_mtime
    assert {path.name: path.read_bytes() for path in agents.iterdir()} == first_agents
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["theme"] == "dark"
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "stop"
    commands = [
        command["command"] for entry in data["hooks"]["PreToolUse"] for command in entry["hooks"]
    ]
    assert "other-hook" in commands
    assert sum(installer.CLAUDE_HOOK_MODULE in command for command in commands) == 1


def test_integrated_khimaira_hook_dedupes_namespace_without_rewriting_it(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    agents = tmp_path / "agents"
    integrated = "/venv/python -m khimaira.hooks.claude_internal_roster_pretool"
    settings.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": integrated,
                                    "_khimaira_hook": "claude_internal_roster_pretool",
                                }
                            ]
                        },
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": (
                                        "/old/python -m themis.hooks.claude_internal_roster_pretool"
                                    ),
                                    "_themis_hook": installer.CLAUDE_HOOK_MODULE,
                                }
                            ]
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    installer.install_internal_roster(
        claude_settings=settings,
        claude_agents_dir=agents,
    )
    data = json.loads(settings.read_text(encoding="utf-8"))
    commands = [
        command["command"] for entry in data["hooks"]["PreToolUse"] for command in entry["hooks"]
    ]
    assert commands == [integrated]


def test_codex_hook_merge_preserves_unrelated_events_and_quotes_python_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = tmp_path / "claude.json"
    hooks = tmp_path / "codex hooks.json"
    agents = tmp_path / "agents"
    hooks.write_text(
        json.dumps(
            {
                "version": 1,
                "hooks": {
                    "Stop": [{"matcher": "*", "hooks": [{"type": "command", "command": "stop"}]}]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(installer.sys, "executable", "/Applications/My Python/bin/python")

    installer.install_internal_roster(
        claude_settings=settings,
        claude_agents_dir=agents,
        codex_hooks=hooks,
    )
    data = json.loads(hooks.read_text(encoding="utf-8"))
    assert data["version"] == 1
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "stop"
    entry = data["hooks"]["PreToolUse"][0]
    assert entry["matcher"] == "*"
    command_hook = entry["hooks"][0]
    assert command_hook["command"] == (
        "'/Applications/My Python/bin/python' -m themis.hooks.codex_pretool"
    )
    assert set(command_hook) == {"type", "command", "statusMessage"}


def test_integrated_codex_hook_dedupes_namespace(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    hooks = tmp_path / "hooks.json"
    agents = tmp_path / "agents"
    integrated = "/venv/python -m khimaira.hooks.codex_pretool"
    hooks.write_text(
        json.dumps(
            {
                "hooks": {
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [{"type": "command", "command": integrated}],
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    before = hooks.read_bytes()

    changes = installer.install_internal_roster(
        claude_settings=settings,
        claude_agents_dir=agents,
        codex_hooks=hooks,
    )

    assert hooks.read_bytes() == before
    codex_change = next(change for change in changes if change.target == hooks)
    assert codex_change.status == "unchanged"


@pytest.mark.parametrize("invalid_path", ["claude", "codex"])
def test_invalid_json_prevents_every_write(tmp_path: Path, invalid_path: str) -> None:
    settings = tmp_path / "settings.json"
    hooks = tmp_path / "hooks.json"
    agents = tmp_path / "agents"
    settings.write_text("{bad" if invalid_path == "claude" else "{}", encoding="utf-8")
    hooks.write_text("{bad" if invalid_path == "codex" else "{}", encoding="utf-8")
    settings_before = settings.read_bytes()
    hooks_before = hooks.read_bytes()

    with pytest.raises(installer.InstallError):
        installer.install_internal_roster(
            claude_settings=settings,
            claude_agents_dir=agents,
            codex_hooks=hooks,
        )

    assert settings.read_bytes() == settings_before
    assert hooks.read_bytes() == hooks_before
    assert not agents.exists()


def test_nested_malformed_codex_hooks_preflight_prevents_partial_install(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    hooks = tmp_path / "hooks.json"
    agents = tmp_path / "agents"
    settings.write_text('{"theme": "keep"}\n', encoding="utf-8")
    hooks.write_text('{"hooks": {"PreToolUse": {}}}\n', encoding="utf-8")
    settings_before = settings.read_bytes()
    hooks_before = hooks.read_bytes()

    with pytest.raises(installer.InstallError, match="PreToolUse"):
        installer.install_internal_roster(
            claude_settings=settings,
            claude_agents_dir=agents,
            codex_hooks=hooks,
        )

    assert settings.read_bytes() == settings_before
    assert hooks.read_bytes() == hooks_before
    assert not agents.exists()


def test_agent_conflict_refuses_before_any_install_write(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    agents = tmp_path / "agents"
    agents.mkdir()
    conflict = agents / "khimaira-internal-agent.md"
    conflict.write_text("local customization\n", encoding="utf-8")
    settings.write_text('{"theme": "keep"}\n', encoding="utf-8")
    settings_before = settings.read_bytes()

    with pytest.raises(installer.InstallError, match="--force"):
        installer.install_internal_roster(
            claude_settings=settings,
            claude_agents_dir=agents,
        )

    assert conflict.read_text(encoding="utf-8") == "local customization\n"
    assert settings.read_bytes() == settings_before
    assert sorted(path.name for path in agents.iterdir()) == [conflict.name]


def test_binary_agent_conflict_is_clean_install_error(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    agents = tmp_path / "agents"
    agents.mkdir()
    conflict = agents / "khimaira-internal-agent.md"
    conflict.write_bytes(b"\xff\xfe\x00")

    with pytest.raises(installer.InstallError, match="--force"):
        installer.install_internal_roster(
            claude_settings=settings,
            claude_agents_dir=agents,
        )
    assert conflict.read_bytes() == b"\xff\xfe\x00"


def test_differing_symlink_requires_force_and_force_replaces_link(tmp_path: Path) -> None:
    settings = tmp_path / "settings.json"
    agents = tmp_path / "agents"
    source = tmp_path / "dotfiles" / "agent.md"
    source.parent.mkdir()
    source.write_text("dotfiles customization\n", encoding="utf-8")
    agents.mkdir()
    destination = agents / "khimaira-internal-agent.md"
    destination.symlink_to(source)

    with pytest.raises(installer.InstallError, match="--force"):
        installer.install_internal_roster(
            claude_settings=settings,
            claude_agents_dir=agents,
        )
    assert destination.is_symlink()
    assert source.read_text(encoding="utf-8") == "dotfiles customization\n"

    installer.install_internal_roster(
        claude_settings=settings,
        claude_agents_dir=agents,
        force=True,
    )
    assert not destination.is_symlink()
    assert destination.read_text(encoding="utf-8") == installer._asset_text(
        "khimaira-internal-agent.md"
    )
    assert source.read_text(encoding="utf-8") == "dotfiles customization\n"


def test_uninstall_removes_only_owned_content_and_preserves_modified_agent(
    tmp_path: Path,
) -> None:
    settings = tmp_path / "settings.json"
    hooks = tmp_path / "hooks.json"
    agents = tmp_path / "agents"
    installer.install_internal_roster(
        claude_settings=settings,
        claude_agents_dir=agents,
        codex_hooks=hooks,
    )
    modified = agents / "khimaira-internal-agent.md"
    modified.write_text("user customization\n", encoding="utf-8")
    settings_data = json.loads(settings.read_text(encoding="utf-8"))
    settings_data["hooks"]["Stop"] = [{"hooks": [{"type": "command", "command": "foreign"}]}]
    settings.write_text(json.dumps(settings_data), encoding="utf-8")

    changes = installer.install_internal_roster(
        claude_settings=settings,
        claude_agents_dir=agents,
        codex_hooks=hooks,
        uninstall=True,
    )

    assert modified.read_text(encoding="utf-8") == "user customization\n"
    assert sum(change.status == "removed" for change in changes) == 4
    assert sum(change.status == "skipped" for change in changes) == 1
    data = json.loads(settings.read_text(encoding="utf-8"))
    assert data["hooks"]["Stop"][0]["hooks"][0]["command"] == "foreign"
    assert "PreToolUse" not in data["hooks"]
    assert "PreToolUse" not in json.loads(hooks.read_text()).get("hooks", {})


def test_cli_uses_only_explicit_scratch_paths(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = tmp_path / "settings.json"
    agents = tmp_path / "agents"
    hooks = tmp_path / "hooks.json"
    rc = main(
        [
            "install-internal-roster",
            "--claude-settings",
            str(settings),
            "--claude-agents-dir",
            str(agents),
            "--codex",
            "--codex-hooks",
            str(hooks),
        ]
    )
    assert rc == 0
    assert settings.is_file()
    assert hooks.is_file()
    assert "themis.hooks" in capsys.readouterr().out


def test_dependency_split_and_explicit_wheel_assets() -> None:
    themis_pyproject = tomllib.loads(
        (_REPO_ROOT / "packages" / "themis" / "pyproject.toml").read_text(encoding="utf-8")
    )
    khimaira_pyproject = tomllib.loads(
        (_REPO_ROOT / "packages" / "khimaira" / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert themis_pyproject["project"]["dependencies"] == ["pyyaml>=6.0"]
    assert themis_pyproject["project"]["optional-dependencies"]["server"] == ["mcp[cli]>=1.0"]
    artifacts = themis_pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["artifacts"]
    assert "src/themis/rules/*.yaml" in artifacts
    assert "src/themis/assets/**/*.md" in artifacts
    assert "khimaira-themis[server]" in khimaira_pyproject["project"]["dependencies"]


def test_script_entrypoint_imports_and_has_help(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["--help"])
    assert exit_info.value.code == 0
    assert "install-internal-roster" in capsys.readouterr().out
