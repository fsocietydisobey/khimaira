"""Role guardrails for Claude Code's project-local internal roster.

This command hook is intentionally standalone. It does not import, call, or modify the
live ``scripts/hooks/themis_pretool.py`` hook. Claude Code supplies the custom agent's
frontmatter ``name`` as ``agent_type`` on PreToolUse payloads; exact roster names select
the policy below, while main-thread and unrelated-agent calls receive no decision.

The Bash checks are conservative text guardrails, not an operating-system sandbox.
"""

from __future__ import annotations

import json
import re
import shlex
import sys
from collections.abc import Iterable
from pathlib import PurePath
from typing import Any

ROSTER_PREFIX = "khimaira-internal-"
ROLE_BY_AGENT_TYPE = {
    "khimaira-internal-consultant": "consultant",
    "khimaira-internal-gatekeeper": "gatekeeper",
    "khimaira-internal-agent": "agent",
}

_EDIT_TOOLS = frozenset({"Edit", "Write", "MultiEdit", "NotebookEdit"})
_SUBAGENT_TOOLS = frozenset({"Agent", "Task"})
_READ_ONLY_ROLES = frozenset({"consultant", "gatekeeper"})

_MUTATING_GIT_SUBCOMMANDS = frozenset(
    {
        "add",
        "am",
        "apply",
        "bisect",
        "branch",
        "checkout",
        "cherry-pick",
        "clean",
        "clone",
        "commit",
        "config",
        "fetch",
        "filter-branch",
        "gc",
        "init",
        "lfs",
        "maintenance",
        "merge",
        "merge-file",
        "mv",
        "notes",
        "prune",
        "pull",
        "push",
        "rebase",
        "reflog",
        "remote",
        "repack",
        "replace",
        "reset",
        "restore",
        "revert",
        "rm",
        "sparse-checkout",
        "stash",
        "submodule",
        "switch",
        "tag",
        "update-index",
        "update-ref",
        "worktree",
    }
)
_FILESYSTEM_MUTATORS = frozenset(
    {
        "chmod",
        "chown",
        "cp",
        "dd",
        "install",
        "ln",
        "mkdir",
        "mv",
        "patch",
        "rm",
        "rmdir",
        "rsync",
        "scp",
        "touch",
        "truncate",
        "unlink",
    }
)
_SHELLS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
_CONTROL_CHARS = frozenset(";&|()")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_REDIRECTION_RE = re.compile(r"(?<!<)(?:\d*)>{1,2}\s*(?P<target>&\d+|[^\s;&|]+)")


class CommandParseError(ValueError):
    """Raised when a Bash command cannot be conservatively tokenized."""


def _diagnostic(message: str) -> None:
    print(f"[claude-internal-roster] {message}", file=sys.stderr)


def _deny(role: str, rule: str, message: str) -> int:
    print(
        f"Claude internal roster {rule}: {role} denied — {message}",
        file=sys.stderr,
    )
    return 2


def _is_control_token(token: str) -> bool:
    return bool(token) and all(character in _CONTROL_CHARS for character in token)


def _command_segments(command: str) -> list[list[str]]:
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError as exc:
        raise CommandParseError(str(exc)) from exc

    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if _is_control_token(token):
            if current:
                segments.append(current)
                current = []
            continue
        current.append(token)
    if current:
        segments.append(current)
    return segments


def _skip_options(words: list[str], index: int, options_with_values: frozenset[str]) -> int:
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        index += 1
        if option in options_with_values and index < len(words):
            index += 1
    return index


def _unwrap_command(words: list[str]) -> list[str]:
    index = 0
    while index < len(words) and _ASSIGNMENT_RE.match(words[index]):
        index += 1

    while index < len(words):
        executable = PurePath(words[index]).name
        if executable == "env":
            index = _skip_options(words, index + 1, frozenset({"-u", "--unset"}))
            while index < len(words) and _ASSIGNMENT_RE.match(words[index]):
                index += 1
            continue
        if executable == "sudo":
            index = _skip_options(
                words,
                index + 1,
                frozenset(
                    {
                        "-C",
                        "-D",
                        "-g",
                        "-h",
                        "-p",
                        "-R",
                        "-r",
                        "-T",
                        "-t",
                        "-U",
                        "-u",
                    }
                ),
            )
            continue
        if executable in {"builtin", "command", "exec", "nohup"}:
            index = _skip_options(words, index + 1, frozenset())
            continue
        break
    return words[index:]


def _iter_invocations(command: str, *, depth: int = 0) -> Iterable[list[str]]:
    if depth > 4:
        raise CommandParseError("nested shell depth exceeds four")

    for segment in _command_segments(command):
        invocation = _unwrap_command(segment)
        if not invocation:
            continue
        yield invocation

        executable = PurePath(invocation[0]).name
        if executable in _SHELLS:
            for index, word in enumerate(invocation[1:], start=1):
                if word.startswith("-") and "c" in word[1:] and index + 1 < len(invocation):
                    yield from _iter_invocations(invocation[index + 1], depth=depth + 1)
                    break
        elif executable == "xargs":
            nested = _skip_options(
                invocation,
                1,
                frozenset({"-a", "-d", "-E", "-I", "-L", "-n", "-P", "-s"}),
            )
            if nested < len(invocation):
                yield _unwrap_command(invocation[nested:])


def _git_subcommand(invocation: list[str]) -> str | None:
    if not invocation or PurePath(invocation[0]).name != "git":
        return None

    index = 1
    options_with_values = {
        "-C",
        "-c",
        "--exec-path",
        "--git-dir",
        "--namespace",
        "--super-prefix",
        "--work-tree",
    }
    while index < len(invocation):
        word = invocation[index]
        if word == "--":
            index += 1
            break
        if not word.startswith("-"):
            break
        index += 1
        if word in options_with_values and index < len(invocation):
            index += 1

    if index >= len(invocation):
        return None
    return invocation[index].lower()


def _git_mutation(command: str) -> str | None:
    for invocation in _iter_invocations(command):
        subcommand = _git_subcommand(invocation)
        if subcommand in _MUTATING_GIT_SUBCOMMANDS:
            return subcommand
    return None


def _redirection_outside_tmp(command: str) -> str | None:
    for match in _REDIRECTION_RE.finditer(command):
        target = match.group("target").strip("'\"")
        if target.startswith("&"):
            continue
        if target == "/dev/null" or target == "/dev/stderr":
            continue
        if target == "/tmp" or target.startswith("/tmp/"):
            continue
        return target
    return None


def _read_only_bash_violation(command: str) -> str | None:
    git_subcommand = _git_mutation(command)
    if git_subcommand:
        return f"git {git_subcommand} mutates git state"

    for invocation in _iter_invocations(command):
        executable = PurePath(invocation[0]).name
        if executable in _FILESYSTEM_MUTATORS:
            return f"{executable} is a filesystem-mutating command"
        if executable == "sed" and any(
            word == "-i" or word.startswith("-i") for word in invocation[1:]
        ):
            return "sed -i mutates files"
        if executable == "perl" and any(
            word.startswith("-") and "i" in word for word in invocation[1:]
        ):
            return "perl -i mutates files"
        if executable == "tee":
            targets = [word for word in invocation[1:] if not word.startswith("-")]
            if any(
                target != "/dev/null" and target != "/tmp" and not target.startswith("/tmp/")
                for target in targets
            ):
                return "tee writes outside /tmp"

    redirection_target = _redirection_outside_tmp(command)
    if redirection_target:
        return f"output redirection writes outside /tmp ({redirection_target})"
    return None


def _bash_denial(role: str, command: str) -> tuple[str, str] | None:
    try:
        if role in _READ_ONLY_ROLES:
            reason = _read_only_bash_violation(command)
            if reason:
                return ("NO_BASH_MUTATING", reason)
            return None

        if "--no-verify" in command:
            return ("NO_NO_VERIFY", "pre-commit hooks may not be bypassed")
        git_subcommand = _git_mutation(command)
        if git_subcommand:
            return (
                "NO_GIT_STATE_MUTATION",
                f"implementers may not run git {git_subcommand}; the master owns git state",
            )
    except CommandParseError as exc:
        if role in _READ_ONLY_ROLES or re.search(r"\bgit\b", command):
            return (
                "UNPARSEABLE_BASH",
                f"cannot establish that this command is role-safe: {exc}",
            )
    return None


def _governed_role(payload: dict[str, Any]) -> tuple[str | None, str | None]:
    agent_type = payload.get("agent_type")
    if not isinstance(agent_type, str):
        if payload.get("agent_id"):
            _diagnostic("subagent payload has agent_id but no string agent_type; fail-open")
        return (None, None)
    role = ROLE_BY_AGENT_TYPE.get(agent_type)
    if role:
        return (role, None)
    if agent_type.startswith(ROSTER_PREFIX):
        return (None, f"unknown reserved roster agent_type {agent_type!r}")
    return (None, None)


def evaluate(payload: dict[str, Any]) -> tuple[str, str, str] | None:
    """Return ``(role, rule, reason)`` when the proposed call must be denied."""

    role, attribution_error = _governed_role(payload)
    if attribution_error:
        return ("unknown", "UNKNOWN_ROSTER_ROLE", attribution_error)
    if role is None:
        return None

    if payload.get("hook_event_name") not in {None, "PreToolUse"}:
        return None

    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_name, str) or not tool_name:
        return (role, "MALFORMED_PAYLOAD", "tool_name is missing or invalid")
    if not isinstance(tool_input, dict):
        return (role, "MALFORMED_PAYLOAD", "tool_input is not an object")

    if tool_name in _SUBAGENT_TOOLS:
        return (
            role,
            "NO_NESTED_AGENTS",
            f"{role} may not spawn another subagent via {tool_name}",
        )
    if role in _READ_ONLY_ROLES and tool_name in _EDIT_TOOLS:
        return (
            role,
            "NO_FILE_EDIT",
            f"{role} is advisory/review-only and may not call {tool_name}",
        )
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return (role, "MALFORMED_BASH", "Bash command is missing or invalid")
        denial = _bash_denial(role, command)
        if denial:
            rule, reason = denial
            return (role, rule, reason)
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError) as exc:
        _diagnostic(f"invalid stdin JSON; fail-open: {exc}")
        return 0
    if not isinstance(payload, dict):
        _diagnostic("stdin JSON is not an object; fail-open")
        return 0

    denial = evaluate(payload)
    if denial is None:
        return 0
    role, rule, reason = denial
    return _deny(role, rule, reason)


if __name__ == "__main__":
    raise SystemExit(main())
