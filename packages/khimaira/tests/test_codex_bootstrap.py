"""Scratch-only tests for the declarative Codex bootstrap adapter."""

from __future__ import annotations

import json
import shlex
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import tomlkit
from khimaira.bootstrap import checks, operations
from khimaira.bootstrap.codex_config import (
    CodexConfigError,
    merge_codex_hooks,
    merge_codex_mcp_config,
)
from khimaira.bootstrap.runner import (
    _codex_khimaira_root,
    check_bootstrap,
    check_sync,
    run_bootstrap,
    run_sync,
)
from khimaira.bootstrap.schema import Profile, ProfileError, RepoSpec, _parse_dict


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "khimaira checkout"
    root.mkdir()
    return root


def test_mcp_merge_is_idempotent_and_preserves_unrelated_content(
    tmp_path: Path, checkout: Path
) -> None:
    config = tmp_path / "config.toml"
    trusted_hash_block = """# keep this comment
model = "custom"
[mcp_servers.other]
command = "other"
[mcp_servers.khimaira]
extra = "preserve-me"
[hooks.state]
[hooks.state."/tmp/hooks.json:stop:0:0"]
trusted_hash = "sha256:abc123"
"""
    config.write_text(trusted_hash_block, encoding="utf-8")

    first = merge_codex_mcp_config(checkout, path=config)
    first_content = config.read_text(encoding="utf-8")
    first_mtime = config.stat().st_mtime_ns
    second = merge_codex_mcp_config(checkout, path=config)

    assert first.status == "updated"
    assert second.status == "unchanged"
    assert config.read_text(encoding="utf-8") == first_content
    assert config.stat().st_mtime_ns == first_mtime
    assert "# keep this comment" in first_content
    assert 'trusted_hash = "sha256:abc123"' in first_content
    parsed = tomlkit.parse(first_content)
    assert parsed["model"] == "custom"
    assert parsed["mcp_servers"]["other"]["command"] == "other"
    assert parsed["mcp_servers"]["khimaira"]["extra"] == "preserve-me"
    assert parsed["mcp_servers"]["khimaira"]["command"] == "bash"
    assert str(checkout.resolve()) in parsed["mcp_servers"]["khimaira"]["args"][1]
    assert str(checkout.resolve()) in parsed["mcp_servers"]["khimaira-chat"]["args"][1]


def test_mcp_merge_repairs_drift_and_preserves_unknown_tool_keys(
    tmp_path: Path, checkout: Path
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(
        """[mcp_servers.khimaira-chat]
command = "wrong"
args = ["wrong"]
[mcp_servers.khimaira-chat.tools.chat_send]
approval_mode = "auto"
note = "keep"
""",
        encoding="utf-8",
    )

    outcome = merge_codex_mcp_config(checkout, path=config)
    parsed = tomlkit.parse(config.read_text(encoding="utf-8"))

    assert outcome.status == "updated"
    chat = parsed["mcp_servers"]["khimaira-chat"]
    assert chat["command"] == "bash"
    assert chat["tools"]["chat_send"]["approval_mode"] == "approve"
    assert chat["tools"]["chat_send"]["note"] == "keep"


@pytest.mark.parametrize(
    "content",
    ["not = [valid", 'mcp_servers = "not-a-table"\n'],
)
def test_mcp_invalid_or_incompatible_input_is_not_mutated(
    tmp_path: Path, checkout: Path, content: str
) -> None:
    config = tmp_path / "config.toml"
    config.write_text(content, encoding="utf-8")

    with pytest.raises(CodexConfigError):
        merge_codex_mcp_config(checkout, path=config)

    assert config.read_text(encoding="utf-8") == content


def test_mcp_read_only_check_does_not_create_file(tmp_path: Path, checkout: Path) -> None:
    config = tmp_path / "missing" / "config.toml"
    outcome = merge_codex_mcp_config(checkout, path=config, apply=False)
    assert outcome.status == "created"
    assert not config.exists()


def test_hooks_merge_is_idempotent_and_preserves_unrelated_entries(
    tmp_path: Path,
) -> None:
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(
        json.dumps(
            {
                "custom": {"keep": True},
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Write",
                            "hooks": [{"type": "command", "command": "other-hook"}],
                        }
                    ]
                },
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    first = merge_codex_hooks(path=hooks_path)
    first_content = hooks_path.read_text(encoding="utf-8")
    first_mtime = hooks_path.stat().st_mtime_ns
    second = merge_codex_hooks(path=hooks_path)

    assert first.status == "updated"
    assert second.status == "unchanged"
    assert hooks_path.read_text(encoding="utf-8") == first_content
    assert hooks_path.stat().st_mtime_ns == first_mtime
    data = json.loads(first_content)
    assert data["custom"] == {"keep": True}
    assert data["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "other-hook"


def test_hooks_merge_repairs_and_deduplicates_owned_commands(tmp_path: Path) -> None:
    hooks_path = tmp_path / "hooks.json"
    command = "/old/python -m khimaira.hooks.codex_stop"
    hooks_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "Stop": [
                        {"hooks": [{"type": "command", "command": command}]},
                        {
                            "matcher": "*",
                            "hooks": [
                                {"type": "command", "command": command},
                                {"type": "command", "command": "keep-me"},
                            ],
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    merge_codex_hooks(path=hooks_path)
    data = json.loads(hooks_path.read_text(encoding="utf-8"))
    stop_commands = [hook["command"] for group in data["hooks"]["Stop"] for hook in group["hooks"]]
    assert sum("khimaira.hooks.codex_stop" in command for command in stop_commands) == 1
    assert "keep-me" in stop_commands


@pytest.mark.parametrize(
    "content",
    ["{bad json", '{"hooks": {"Stop": {}}}'],
)
def test_hooks_invalid_or_incompatible_input_is_not_mutated(tmp_path: Path, content: str) -> None:
    hooks_path = tmp_path / "hooks.json"
    hooks_path.write_text(content, encoding="utf-8")
    with pytest.raises(CodexConfigError):
        merge_codex_hooks(path=hooks_path)
    assert hooks_path.read_text(encoding="utf-8") == content


def test_already_configured_machine_shape_is_byte_unchanged(tmp_path: Path) -> None:
    """Hermetic copy of the inspected live shapes, including trusted hashes."""
    root = Path.home() / "dev" / "khimaira"
    config_copy = tmp_path / "config.toml"
    hooks_copy = tmp_path / "hooks.json"
    config_copy.write_text(
        f"""model = "custom"
[mcp_servers.khimaira-chat]
command = "bash"
args = ["-lc", "uv --directory ~/dev/khimaira run khimaira-chat 2>>/tmp/khimaira-chat.log"]
default_tools_approval_mode = "auto"
[mcp_servers.khimaira-chat.tools.chat_my_chats]
approval_mode = "approve"
[mcp_servers.khimaira-chat.tools.chat_accept]
approval_mode = "approve"
[mcp_servers.khimaira-chat.tools.chat_create_room]
approval_mode = "approve"
[mcp_servers.khimaira-chat.tools.chat_send]
approval_mode = "approve"
[mcp_servers.khimaira-chat.tools.chat_history]
approval_mode = "approve"
[mcp_servers.khimaira]
command = "bash"
args = ["-lc", "uv --directory {root.resolve()} run python -m khimaira.cli mcp 2>>/tmp/khimaira-codex.log"]
default_tools_approval_mode = "auto"
[mcp_servers.khimaira.tools.session_delete]
approval_mode = "approve"
[mcp_servers.khimaira.tools.notebook_delete]
approval_mode = "approve"
[mcp_servers.khimaira.tools.kill_process]
approval_mode = "approve"
[mcp_servers.khimaira.tools.rewind]
approval_mode = "approve"
[mcp_servers.khimaira.tools.spawn_process]
approval_mode = "approve"
[mcp_servers.khimaira.tools.khimaira_configure]
approval_mode = "approve"
[mcp_servers.khimaira.tools.scarlet_generate_barrel]
approval_mode = "approve"
[mcp_servers.khimaira.tools.cancel_scheduled_task]
approval_mode = "approve"
[hooks.state]
[hooks.state."/home/user/.codex/hooks.json:stop:0:0"]
trusted_hash = "sha256:keep-this-exactly"
""",
        encoding="utf-8",
    )
    executable = shlex.quote(sys.executable)
    hooks_copy.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{executable} -m khimaira.hooks.codex_session_start",
                                    "statusMessage": "khimaira-chat registration",
                                }
                            ],
                        }
                    ],
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{executable} -m khimaira.hooks.codex_user_prompt_submit",
                                    "statusMessage": "khimaira-chat delivery check",
                                }
                            ]
                        }
                    ],
                    "Stop": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{executable} -m khimaira.hooks.codex_stop",
                                    "statusMessage": "khimaira idle marker",
                                }
                            ],
                        }
                    ],
                    "PreToolUse": [
                        {
                            "matcher": "*",
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": f"{executable} -m khimaira.hooks.codex_pretool",
                                    "statusMessage": "khimaira Themis check",
                                }
                            ],
                        }
                    ],
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    config_before = config_copy.read_bytes()
    hooks_before = hooks_copy.read_bytes()
    assert merge_codex_mcp_config(root, path=config_copy).status == "unchanged"
    assert merge_codex_hooks(path=hooks_copy).status == "unchanged"
    assert config_copy.read_bytes() == config_before
    assert hooks_copy.read_bytes() == hooks_before


def test_existing_symlink_engine_supports_all_codex_profile_pairs(
    tmp_path: Path,
) -> None:
    dotfiles = tmp_path / "dotfiles"
    sources = (
        "codex/AGENTS.md",
        "codex/skills/khimaira-ask",
        "codex/skills/khimaira-tell",
        "codex/skills/khimaira-notes",
    )
    for source in sources:
        source_path = dotfiles / source
        if source.endswith(".md"):
            source_path.parent.mkdir(parents=True, exist_ok=True)
            source_path.write_text("rules", encoding="utf-8")
        else:
            source_path.mkdir(parents=True)
            (source_path / "SKILL.md").write_text("skill", encoding="utf-8")

    for source in sources:
        destination = tmp_path / "home" / ".codex" / Path(source).relative_to("codex")
        outcome = operations.apply_symlink(
            operations.SymlinkEntry(src=source, dest=str(destination)), dotfiles
        )
        assert outcome.status == "created"
        assert destination.is_symlink()


def test_schema_flag_is_strict_and_round_trips() -> None:
    assert _parse_dict({"install_codex_adapter": True}).to_dict()["install_codex_adapter"] is True
    assert _parse_dict({}).install_codex_adapter is False
    with pytest.raises(ProfileError, match="must be a boolean"):
        _parse_dict({"install_codex_adapter": "yes"})


def test_codex_root_uses_declared_future_checkout(tmp_path: Path) -> None:
    future_checkout = tmp_path / "not-cloned-yet"
    profile = Profile(
        repos=[
            RepoSpec(
                name="khimaira",
                url="unused",
                path=str(future_checkout),
            )
        ]
    )

    assert _codex_khimaira_root(profile) == future_checkout.resolve()


def test_operation_and_check_wrappers_keep_files_separate(tmp_path: Path, checkout: Path) -> None:
    config = tmp_path / "config.toml"
    hooks = tmp_path / "hooks.json"
    assert operations.install_codex_mcp_config(checkout, config_path=config).status in {
        "created",
        "updated",
    }
    assert operations.install_codex_hooks(hooks_path=hooks).status in {
        "created",
        "updated",
    }
    assert checks.check_codex_mcp_config(checkout, config_path=config).status == "unchanged"
    assert checks.check_codex_hooks(hooks_path=hooks).status == "unchanged"


def test_bootstrap_and_checks_wire_codex_adapter() -> None:
    profile = Profile(
        install_codex_adapter=True,
        repos=[RepoSpec(name="khimaira", url="unused", path="/tmp/khimaira")],
    )
    unchanged = operations.OpResult("test", "test", "unchanged")
    with (
        patch("khimaira.bootstrap.operations.ensure_repo", return_value=unchanged),
        patch("khimaira.bootstrap.operations.run_install", return_value=unchanged),
        patch("khimaira.bootstrap.runner._install_codex_adapter") as install,
        patch("khimaira.bootstrap.checks.check_repo", return_value=unchanged),
        patch("khimaira.bootstrap.runner._check_codex_adapter") as check,
    ):
        run_bootstrap(profile)
        check_bootstrap(profile)
    install.assert_called_once()
    check.assert_called_once()


@pytest.mark.parametrize("mode", ["editable", "site-packages"])
def test_both_sync_modes_wire_codex_adapter(mode: str) -> None:
    profile = Profile(install_codex_adapter=True)
    unchanged = operations.OpResult("test", "test", "unchanged")
    with (
        patch("khimaira.bootstrap.runner.install_mode.detect_install_mode", return_value=mode),
        patch("khimaira.bootstrap.runner._install_codex_adapter") as install,
        patch("khimaira.bootstrap.runner._check_codex_adapter") as check,
        patch(
            "khimaira.bootstrap.operations.check_and_upgrade_khimaira",
            return_value=unchanged,
        ),
        patch("khimaira.bootstrap.operations.reconcile_mcp_drift", return_value=[]),
        patch("khimaira.bootstrap.operations.check_monitor_freshness", return_value=unchanged),
        patch("khimaira.bootstrap.operations.log_sync_event"),
        patch("khimaira.bootstrap.runner._khimaira_repo_root", return_value=None),
        patch("khimaira.bootstrap.runner.install_mode.check_pypi_version", return_value=None),
    ):
        run_sync(profile)
        check_sync(profile)
    install.assert_called_once()
    check.assert_called_once()
