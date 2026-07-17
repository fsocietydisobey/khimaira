"""Idempotent local installer for Themis's internal coding-tool roster.

This module only manages package-owned agent definitions and hook entries. It
does not configure a daemon, dotfiles repository, MCP server, or chat surface.
All public functions accept explicit paths so tests and automation never need to
touch live user configuration.
"""

from __future__ import annotations

import copy
import json
import os
import shlex
import stat
import sys
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any, Literal

CLAUDE_HOOK_MODULE = "themis.hooks.claude_internal_roster_pretool"
CODEX_HOOK_MODULE = "themis.hooks.codex_pretool"
_CLAUDE_EQUIVALENT_MODULES = frozenset(
    {CLAUDE_HOOK_MODULE, "khimaira.hooks.claude_internal_roster_pretool"}
)
_CODEX_EQUIVALENT_MODULES = frozenset({CODEX_HOOK_MODULE, "khimaira.hooks.codex_pretool"})
_THEMIS_MARKER = "_themis_hook"
_AGENT_FILENAMES = (
    "khimaira-internal-consultant.md",
    "khimaira-internal-gatekeeper.md",
    "khimaira-internal-agent.md",
)

ChangeStatus = Literal["created", "updated", "unchanged", "removed", "skipped"]


class InstallError(ValueError):
    """Existing configuration cannot be safely merged."""


@dataclass(frozen=True)
class FileChange:
    target: Path
    status: ChangeStatus
    detail: str


def _module_command(module: str) -> str:
    return f"{shlex.quote(sys.executable)} -m {module}"


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        if path.exists():
            temporary_path.chmod(stat.S_IMODE(path.stat().st_mode))
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)


def _read_json_object(path: Path) -> tuple[dict[str, Any], bool]:
    if not path.is_file():
        return {}, False
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise InstallError(f"{path} root must be a JSON object")
    return value, True


def _validate_hook_file(path: Path) -> None:
    value, _ = _read_json_object(path)
    candidate = copy.deepcopy(value)
    if "hooks" not in candidate:
        candidate["hooks"] = {}
    hooks = candidate["hooks"]
    if not isinstance(hooks, dict):
        raise InstallError(f"{path}: hooks must be an object")
    _target_event(hooks, "PreToolUse", path=path)


def _command_module(command: object) -> str | None:
    if not isinstance(command, str):
        return None
    try:
        tokens = shlex.split(command)
    except ValueError:
        return None
    if len(tokens) != 3 or tokens[1] != "-m":
        return None
    return tokens[2]


def _hook_module(hook: object) -> str | None:
    if not isinstance(hook, dict):
        return None
    return _command_module(hook.get("command"))


def _target_event(hooks: dict[str, Any], event: str, *, path: Path) -> list[Any]:
    if event not in hooks:
        hooks[event] = []
    entries = hooks[event]
    if not isinstance(entries, list):
        raise InstallError(f"{path}: hooks.{event} must be an array")
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise InstallError(f"{path}: hooks.{event}[{index}] must be an object")
        commands = entry.get("hooks")
        if not isinstance(commands, list) or not all(
            isinstance(command, dict) for command in commands
        ):
            raise InstallError(f"{path}: hooks.{event}[{index}].hooks must be an array of objects")
    return entries


def _all_modules(entries: Iterable[dict[str, Any]]) -> set[str]:
    return {
        module
        for entry in entries
        for command in entry["hooks"]
        if (module := _hook_module(command)) is not None
    }


def _strip_owned_entries(entries: list[Any], owned_module: str) -> bool:
    changed = False
    for entry in entries[:]:
        original_hooks = entry["hooks"]
        kept_hooks = [
            command
            for command in original_hooks
            if _hook_module(command) != owned_module and command.get(_THEMIS_MARKER) != owned_module
        ]
        if len(kept_hooks) != len(original_hooks):
            entry["hooks"] = kept_hooks
            changed = True
        if not entry["hooks"]:
            entries.remove(entry)
    return changed


def _is_canonical_entry(
    entries: list[Any],
    *,
    module: str,
    matcher: str | None,
    status_message: str | None,
    include_marker: bool,
) -> bool:
    found: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for entry in entries:
        for command in entry["hooks"]:
            if _hook_module(command) == module:
                found.append((entry, command))
    if len(found) != 1:
        return False
    entry, command = found[0]
    matcher_matches = (
        entry.get("matcher") == matcher if matcher is not None else "matcher" not in entry
    )
    expected_command = _module_command(module)
    return (
        matcher_matches
        and command.get("type") == "command"
        and command.get("command") == expected_command
        and (
            command.get(_THEMIS_MARKER) == module
            if include_marker
            else _THEMIS_MARKER not in command
        )
        and (status_message is None or command.get("statusMessage") == status_message)
    )


def _upsert_namespaced_hook(
    entries: list[Any],
    *,
    owned_module: str,
    equivalent_modules: frozenset[str],
    matcher: str | None,
    status_message: str | None,
    include_marker: bool,
) -> bool:
    modules = _all_modules(entries)
    foreign_equivalents = (equivalent_modules - {owned_module}) & modules
    if foreign_equivalents:
        # The integrated khimaira hook already governs this namespace. Remove
        # only our duplicate, if present; never rewrite khimaira's entry.
        return _strip_owned_entries(entries, owned_module)

    if _is_canonical_entry(
        entries,
        module=owned_module,
        matcher=matcher,
        status_message=status_message,
        include_marker=include_marker,
    ):
        return False

    _strip_owned_entries(entries, owned_module)
    hook: dict[str, Any] = {
        "type": "command",
        "command": _module_command(owned_module),
    }
    if include_marker:
        hook[_THEMIS_MARKER] = owned_module
    if status_message is not None:
        hook["statusMessage"] = status_message
    entry: dict[str, Any] = {"hooks": [hook]}
    if matcher is not None:
        entry = {"matcher": matcher, "hooks": [hook]}
    entries.append(entry)
    return True


def _merge_hook_file(
    path: Path,
    *,
    module: str,
    equivalent_modules: frozenset[str],
    matcher: str | None,
    status_message: str | None,
    include_marker: bool,
    uninstall: bool,
) -> FileChange:
    original, existed = _read_json_object(path)
    merged = copy.deepcopy(original)
    if "hooks" not in merged:
        merged["hooks"] = {}
    hooks = merged["hooks"]
    if not isinstance(hooks, dict):
        raise InstallError(f"{path}: hooks must be an object")
    entries = _target_event(hooks, "PreToolUse", path=path)

    changed = (
        _strip_owned_entries(entries, module)
        if uninstall
        else _upsert_namespaced_hook(
            entries,
            owned_module=module,
            equivalent_modules=equivalent_modules,
            matcher=matcher,
            status_message=status_message,
            include_marker=include_marker,
        )
    )
    if not entries:
        hooks.pop("PreToolUse", None)
    if not hooks:
        merged.pop("hooks", None)

    if not changed:
        return FileChange(path, "unchanged", "managed hook already matches")
    _atomic_write(path, json.dumps(merged, indent=2) + "\n")
    if uninstall:
        return FileChange(path, "removed", f"removed {module}")
    return FileChange(
        path,
        "updated" if existed else "created",
        f"merged {module}",
    )


def _asset_text(filename: str) -> str:
    asset = resources.files("themis").joinpath("assets", "claude_agents", filename)
    return asset.read_text(encoding="utf-8")


def _matches_agent_asset(destination: Path, expected: str) -> bool:
    if not destination.is_file():
        return False
    try:
        return destination.read_text(encoding="utf-8") == expected
    except (OSError, UnicodeError):
        return False


def _agent_conflicts(agents_dir: Path) -> list[Path]:
    conflicts: list[Path] = []
    for filename in _AGENT_FILENAMES:
        destination = agents_dir / filename
        if not destination.exists() and not destination.is_symlink():
            continue
        if _matches_agent_asset(destination, _asset_text(filename)):
            continue
        conflicts.append(destination)
    return conflicts


def _install_agent_assets(agents_dir: Path, *, uninstall: bool, force: bool) -> list[FileChange]:
    changes: list[FileChange] = []
    for filename in _AGENT_FILENAMES:
        destination = agents_dir / filename
        expected = _asset_text(filename)
        if uninstall:
            if not destination.exists():
                changes.append(FileChange(destination, "unchanged", "already absent"))
            elif _matches_agent_asset(destination, expected):
                destination.unlink()
                changes.append(FileChange(destination, "removed", "removed packaged agent"))
            else:
                changes.append(
                    FileChange(
                        destination,
                        "skipped",
                        "content differs from packaged agent; preserved",
                    )
                )
            continue

        if _matches_agent_asset(destination, expected):
            changes.append(FileChange(destination, "unchanged", "agent already matches"))
            continue
        if destination.is_dir() and not destination.is_symlink():
            raise InstallError(f"{destination} is a directory; refusing to replace it")
        existed = destination.exists() or destination.is_symlink()
        if existed and not force:
            raise InstallError(
                f"{destination} differs from the packaged agent; use --force to replace it"
            )
        _atomic_write(destination, expected)
        changes.append(
            FileChange(
                destination,
                "updated" if existed else "created",
                "installed packaged Claude agent",
            )
        )
    return changes


def install_internal_roster(
    *,
    claude_settings: Path,
    claude_agents_dir: Path,
    codex_hooks: Path | None = None,
    uninstall: bool = False,
    force: bool = False,
) -> list[FileChange]:
    """Install or remove the standalone roster using only local files."""
    # Validate every requested JSON file before the first write so an invalid
    # optional Codex config cannot leave a half-installed Claude setup.
    _validate_hook_file(claude_settings)
    if codex_hooks is not None:
        _validate_hook_file(codex_hooks)
    if not uninstall:
        conflicts = _agent_conflicts(claude_agents_dir)
        directories = [path for path in conflicts if path.is_dir() and not path.is_symlink()]
        if directories:
            raise InstallError(f"{directories[0]} is a directory; refusing to replace it")
        if conflicts and not force:
            rendered = ", ".join(str(path) for path in conflicts)
            raise InstallError(
                f"agent definitions differ from packaged content: {rendered}; "
                "use --force to replace them"
            )

    changes = _install_agent_assets(
        claude_agents_dir,
        uninstall=uninstall,
        force=force,
    )
    changes.append(
        _merge_hook_file(
            claude_settings,
            module=CLAUDE_HOOK_MODULE,
            equivalent_modules=_CLAUDE_EQUIVALENT_MODULES,
            matcher=None,
            status_message=None,
            include_marker=True,
            uninstall=uninstall,
        )
    )
    if codex_hooks is not None:
        changes.append(
            _merge_hook_file(
                codex_hooks,
                module=CODEX_HOOK_MODULE,
                equivalent_modules=_CODEX_EQUIVALENT_MODULES,
                matcher="*",
                status_message="Themis internal-roster check",
                include_marker=False,
                uninstall=uninstall,
            )
        )
    return changes
