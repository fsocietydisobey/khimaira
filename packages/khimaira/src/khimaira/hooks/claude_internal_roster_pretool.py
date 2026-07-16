"""Claude Code PreToolUse adapter for the project-local internal roster.

Role attribution remains Claude-specific. Policy is evaluated by the shared
local-Themis adapter so this hook and the Codex hook consume the same packaged
catalog without depending on the khimaira daemon.
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any

ROSTER_PREFIX = "khimaira-internal-"
ROLE_BY_AGENT_TYPE = {
    "khimaira-internal-consultant": "consultant",
    "khimaira-internal-gatekeeper": "gatekeeper",
    "khimaira-internal-agent": "agent",
}


def _diagnostic(message: str) -> None:
    print(f"[claude-internal-roster] {message}", file=sys.stderr)


def _deny(role: str, rule: str, message: str) -> int:
    print(
        f"Claude internal roster {rule}: {role} denied — {message}",
        file=sys.stderr,
    )
    return 2


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
    """Return ``(role, rule_id, message)`` only for blocking decisions."""
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
    if tool_name == "Bash":
        command = tool_input.get("command")
        if not isinstance(command, str) or not command.strip():
            return (role, "MALFORMED_BASH", "Bash command is missing or invalid")

    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()

    try:
        from themis.data import Severity

        from khimaira.hooks.local_themis import evaluate_local

        outcome = evaluate_local(role, tool_name, tool_input, cwd)
    except Exception as exc:  # noqa: BLE001 — standalone hook must fail open
        _diagnostic(
            f"local Themis import/load/evaluation failed for {role}/{tool_name}; fail-open: {exc}"
        )
        return None

    if outcome.ok:
        return None
    violation = outcome.violation
    if violation is None:
        _diagnostic(
            f"local Themis returned ok=False without a violation for {role}/{tool_name}; fail-open"
        )
        return None
    if violation.severity is Severity.BLOCK:
        return (role, violation.rule_id, violation.message)

    severity = getattr(violation.severity, "value", str(violation.severity))
    _diagnostic(
        f"local Themis {severity} {violation.rule_id} ({violation.name}): {violation.message}"
    )
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


def _run_main_fail_open() -> int:
    """Keep an unexpected adapter bug from blocking Claude Code itself."""
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 — outermost guardrail boundary
        _diagnostic(f"unhandled hook exception; fail-open: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(_run_main_fail_open())
