"""Shared in-process Themis adapter for coding-tool hooks.

Hook-specific code owns role attribution and output formatting. This module owns
the common policy boundary: normalize only confirmed cross-tool aliases, discover
project-local rule overlays, build condition input from the real tool call, and
evaluate the packaged Themis catalog without a daemon or network dependency.

Imports are intentionally lazy so each hook can catch import failures at its
outer fail-open boundary. This function likewise does not catch rule-loading or
evaluation errors; callers decide and diagnose their fail-open behavior.
"""

from __future__ import annotations

import re
import shlex
from pathlib import PurePath
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from themis.data import EvalResult


_TOOL_ALIASES = {
    # Claude Code's current subagent tool is documented as Agent while the
    # packaged Themis catalog uses its historical Task name.
    "Agent": "Task",
}
_TASK_UPDATE_TOOL = "mcp__khimaira-chat__chat_task_update"
_SHELL_PUNCTUATION = frozenset(";&|()\n")
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_SHELLS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})


def evaluate_local(
    role: str,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str,
) -> EvalResult:
    """Evaluate one proposed tool call against local core and app rules.

    The condition payload always contains the normalized tool name and the
    original tool input. ``gate_verdicts='error'`` is added only where the local
    adapter knows the catalog's verdict gate applies but cannot truthfully query
    verdict state without the daemon:

    - an ``agent`` directly invoking ``git commit`` through ``Bash``;
    - a ``master`` moving a task to ``approved``.

    Treating those two gated operations as an enrichment error makes them loud
    and recoverable instead of pretending that review verdicts were observed.
    All other calls omit ``gate_verdicts`` so condition evaluation retains its
    documented fail-open behavior for missing enrichment.
    """
    from themis.data import find_app_rules_dir, load_rules
    from themis.engine import evaluate

    normalized_tool_name = _TOOL_ALIASES.get(tool_name, tool_name)
    conditions_payload: dict[str, Any] = {
        "tool_name": normalized_tool_name,
        "tool_input": tool_input,
    }
    if _requires_gate_verdicts_error(role, normalized_tool_name, tool_input):
        conditions_payload["gate_verdicts"] = "error"

    app_rules_dir = find_app_rules_dir(cwd)
    rule_set = load_rules(role, app_rules_dir)
    return evaluate(
        role,
        normalized_tool_name,
        tool_input,
        conditions_payload,
        rule_set=rule_set,
    )


def _requires_gate_verdicts_error(
    role: str,
    tool_name: str,
    tool_input: dict[str, Any],
) -> bool:
    if role == "agent" and tool_name == "Bash":
        command = tool_input.get("command")
        return isinstance(command, str) and _direct_git_commit(command)
    if role == "master" and tool_name == _TASK_UPDATE_TOOL:
        return tool_input.get("new_status") == "approved"
    return False


def _direct_git_commit(command: str, *, depth: int = 0) -> bool:
    """Recognize an actual direct ``git commit`` shell segment.

    The catalog's matcher is intentionally broad and also matches quoted search
    text and ``git commit-tree``. Those are matcher candidates, but they are not
    truthful evidence that the standalone hook failed to obtain a gate verdict.
    Quote-aware tokenization keeps the injected ``gate_verdicts='error'`` fact
    limited to exact command positions while still covering pipes, background
    commands, parentheses, and newline-separated commands.
    """
    if depth > 4:
        return False
    try:
        lexer = shlex.shlex(
            command,
            posix=True,
            punctuation_chars=";&|()\n",
        )
        # Newlines separate shell commands, so retain them as punctuation
        # instead of allowing shlex's default whitespace handling to erase the
        # boundary.
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        tokens = list(lexer)
    except ValueError:
        return False

    segment: list[str] = []
    for token in [*tokens, ";"]:
        if token and all(character in _SHELL_PUNCTUATION for character in token):
            if _invokes_git_commit(_unwrap_command(segment), depth=depth):
                return True
            segment = []
        else:
            segment.append(token)
    return False


def _skip_options(
    words: list[str],
    index: int,
    options_with_values: frozenset[str],
) -> int:
    while index < len(words) and words[index].startswith("-"):
        option = words[index]
        index += 1
        if option in options_with_values and index < len(words):
            index += 1
    return index


def _unwrap_command(words: list[str]) -> list[str]:
    """Remove shell-transparent prefixes without interpreting policy."""
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


def _invokes_git_commit(invocation: list[str], *, depth: int) -> bool:
    if not invocation:
        return False
    executable = PurePath(invocation[0]).name
    if executable == "git":
        return len(invocation) >= 2 and invocation[1] == "commit"
    if executable in _SHELLS:
        for index, word in enumerate(invocation[1:], start=1):
            if word.startswith("-") and "c" in word[1:] and index + 1 < len(invocation):
                return _direct_git_commit(invocation[index + 1], depth=depth + 1)
    return False
