"""Declarative, idempotent configuration for khimaira's Codex adapter.

Only the two khimaira MCP tables and the four khimaira hook commands are
managed. Everything else in Codex's user configuration is preserved.
"""

from __future__ import annotations

import json
import os
import shlex
import stat
import sys
import tempfile
from collections.abc import MutableMapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import tomlkit
from tomlkit.items import Table


class CodexConfigError(ValueError):
    """The existing Codex config cannot be safely merged."""


@dataclass(frozen=True)
class MergeOutcome:
    """Result of inspecting or applying one Codex configuration file."""

    changed: bool
    existed: bool

    @property
    def status(self) -> Literal["created", "updated", "unchanged"]:
        if not self.changed:
            return "unchanged"
        return "updated" if self.existed else "created"


_CHAT_APPROVAL_TOOLS = (
    "chat_my_chats",
    "chat_accept",
    "chat_create_room",
    "chat_send",
    "chat_history",
)

_KHIMAIRA_APPROVAL_TOOLS = (
    "session_delete",
    "notebook_delete",
    "kill_process",
    "rewind",
    "spawn_process",
    "khimaira_configure",
    "scarlet_generate_barrel",
    "cancel_scheduled_task",
)

_HOOK_SPECS = {
    "SessionStart": (
        "codex_session_start",
        "khimaira-chat registration",
        "*",
    ),
    "UserPromptSubmit": (
        "codex_user_prompt_submit",
        "khimaira-chat delivery check",
        None,
    ),
    "Stop": ("codex_stop", "khimaira idle marker", "*"),
    "PreToolUse": ("codex_pretool", "khimaira Themis check", "*"),
}


def default_codex_config_path() -> Path:
    return Path.home() / ".codex" / "config.toml"


def default_codex_hooks_path() -> Path:
    return Path.home() / ".codex" / "hooks.json"


def _atomic_write(path: Path, content: str) -> None:
    """Write beside the destination and atomically replace it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
            temporary_path.chmod(mode)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _new_table() -> Table:
    return tomlkit.table()


def _require_table(
    parent: MutableMapping[str, Any], key: str, *, location: str
) -> MutableMapping[str, Any]:
    if key not in parent:
        parent[key] = _new_table()
    value = parent[key]
    if not isinstance(value, MutableMapping):
        raise CodexConfigError(f"{location}.{key} must be a TOML table")
    return value


def _set_value(table: MutableMapping[str, Any], key: str, value: Any) -> bool:
    if key in table and table[key] == value:
        return False
    table[key] = value
    return True


def _merge_server(
    servers: MutableMapping[str, Any],
    *,
    name: str,
    args: list[str],
    approval_tools: tuple[str, ...],
) -> bool:
    changed = name not in servers
    server = _require_table(servers, name, location="mcp_servers")
    changed |= _set_value(server, "command", "bash")
    changed |= _set_value(server, "args", args)
    changed |= _set_value(server, "default_tools_approval_mode", "auto")

    tools = _require_table(server, "tools", location=f"mcp_servers.{name}")
    for tool_name in approval_tools:
        changed |= tool_name not in tools
        tool = _require_table(
            tools,
            tool_name,
            location=f"mcp_servers.{name}.tools",
        )
        changed |= _set_value(tool, "approval_mode", "approve")
    return changed


def _khimaira_chat_root(khimaira_root: Path) -> str:
    """Render the checkout path used by the khimaira-chat launcher.

    Preserve the established portable spelling for the default checkout so
    already-configured machines remain byte-for-byte unchanged. Custom
    profile paths use their resolved absolute path instead of silently
    falling back to ``~/dev/khimaira``.
    """
    resolved_root = khimaira_root.resolve()
    default_root = (Path.home() / "dev" / "khimaira").resolve()
    if resolved_root == default_root:
        return "~/dev/khimaira"
    return shlex.quote(str(resolved_root))


def merge_codex_mcp_config(
    khimaira_root: Path,
    *,
    path: Path | None = None,
    apply: bool = True,
) -> MergeOutcome:
    """Merge the two managed MCP servers into Codex's TOML config."""
    config_path = path or default_codex_config_path()
    existed = config_path.is_file()
    original = ""
    if existed:
        try:
            original = config_path.read_text(encoding="utf-8")
            document = tomlkit.parse(original)
        except (OSError, tomlkit.exceptions.ParseError) as exc:
            raise CodexConfigError(f"{config_path} is not valid TOML: {exc}") from exc
    else:
        document = tomlkit.document()

    changed = "mcp_servers" not in document
    servers = _require_table(document, "mcp_servers", location="config")
    changed |= _merge_server(
        servers,
        name="khimaira-chat",
        args=[
            "-lc",
            f"uv --directory {_khimaira_chat_root(khimaira_root)} "
            "run khimaira-chat 2>>/tmp/khimaira-chat.log",
        ],
        approval_tools=_CHAT_APPROVAL_TOOLS,
    )
    root = shlex.quote(str(khimaira_root.resolve()))
    changed |= _merge_server(
        servers,
        name="khimaira",
        args=[
            "-lc",
            f"uv --directory {root} run python -m khimaira.cli mcp 2>>/tmp/khimaira-codex.log",
        ],
        approval_tools=_KHIMAIRA_APPROVAL_TOOLS,
    )

    if changed and apply:
        _atomic_write(config_path, tomlkit.dumps(document))
    return MergeOutcome(changed=changed, existed=existed)


def _parse_module_command(command: object) -> tuple[str, str] | None:
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) != 3 or tokens[1] != "-m":
        return None
    return tokens[0], tokens[2]


def _same_executable(left: str, right: str) -> bool:
    if left == right:
        return True
    try:
        return Path(left).resolve(strict=True) == Path(right).resolve(strict=True)
    except OSError:
        return False


def _hook_command(module_basename: str) -> str:
    return f"{shlex.quote(sys.executable)} -m khimaira.hooks.{module_basename}"


def _validate_hooks_shape(data: object, path: Path) -> dict[str, Any]:
    if not isinstance(data, dict):
        raise CodexConfigError(f"{path} root must be a JSON object")
    hooks = data.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise CodexConfigError(f"{path}: hooks must be an object")
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            raise CodexConfigError(f"{path}: hooks.{event} must be an array")
        for index, entry in enumerate(entries):
            if not isinstance(entry, dict):
                raise CodexConfigError(f"{path}: hooks.{event}[{index}] must be an object")
            commands = entry.get("hooks")
            if not isinstance(commands, list):
                raise CodexConfigError(f"{path}: hooks.{event}[{index}].hooks must be an array")
            if not all(isinstance(command, dict) for command in commands):
                raise CodexConfigError(
                    f"{path}: hooks.{event}[{index}].hooks entries must be objects"
                )
    return hooks


def _merge_hook_event(
    entries: list[dict[str, Any]],
    *,
    module_basename: str,
    status_message: str,
    matcher: str | None,
) -> bool:
    module_name = f"khimaira.hooks.{module_basename}"
    desired_command = _hook_command(module_basename)
    owned: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in entries:
        for command in entry["hooks"]:
            parsed = _parse_module_command(command.get("command"))
            if parsed is not None and parsed[1] == module_name:
                owned.append((entry, command))

    if len(owned) == 1:
        entry, command = owned[0]
        matcher_matches = (
            entry.get("matcher") == matcher if matcher is not None else "matcher" not in entry
        )
        parsed = _parse_module_command(command.get("command"))
        command_matches = (
            parsed is not None
            and _same_executable(parsed[0], sys.executable)
            and command.get("type") == "command"
            and command.get("statusMessage") == status_message
        )
        if matcher_matches and command_matches:
            return False

    # Canonicalize only commands owned by this module. Unrelated hooks and
    # event keys survive untouched; empty groups created by dedupe are removed.
    for entry in entries[:]:
        entry["hooks"] = [
            command
            for command in entry["hooks"]
            if not (
                (parsed := _parse_module_command(command.get("command")))
                and parsed[1] == module_name
            )
        ]
        if not entry["hooks"]:
            entries.remove(entry)

    group: dict[str, Any] = {
        "hooks": [
            {
                "type": "command",
                "command": desired_command,
                "statusMessage": status_message,
            }
        ]
    }
    if matcher is not None:
        group["matcher"] = matcher
        # Match the established file shape: matcher precedes hooks.
        group = {"matcher": matcher, "hooks": group["hooks"]}
    entries.append(group)
    return True


def merge_codex_hooks(
    *,
    path: Path | None = None,
    apply: bool = True,
) -> MergeOutcome:
    """Merge the four managed hook commands into Codex's hooks JSON."""
    hooks_path = path or default_codex_hooks_path()
    existed = hooks_path.is_file()
    if existed:
        try:
            data = json.loads(hooks_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CodexConfigError(f"{hooks_path} is not valid JSON: {exc}") from exc
    else:
        data = {}

    hooks = _validate_hooks_shape(data, hooks_path)
    changed = not existed
    for event, (module_basename, status_message, matcher) in _HOOK_SPECS.items():
        if event not in hooks:
            hooks[event] = []
            changed = True
        changed |= _merge_hook_event(
            hooks[event],
            module_basename=module_basename,
            status_message=status_message,
            matcher=matcher,
        )

    if changed and apply:
        _atomic_write(hooks_path, json.dumps(data, indent=2) + "\n")
    return MergeOutcome(changed=changed, existed=existed)
