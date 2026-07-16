"""Codex PreToolUse adapter for local Themis policy evaluation.

Codex role attribution is intentionally small and local:

- a top-level tool call (no ``agent_id``) is the ``master`` role;
- a spawned agent's role comes from the ``agent_path`` in its rollout
  ``session_meta`` record; a trailing numeric seat suffix is removed, so
  ``/root/agent_1`` and ``/root/agent_2`` both resolve to ``agent``.

Policy evaluation is in-process through :mod:`khimaira.hooks.local_themis`.
There is no daemon, chat provisioning, virtual session, HTTP, or persistent
cache dependency. Attribution, import, rule-loading, and evaluation failures
all fail open with a diagnostic because Themis is a guardrail rather than a
security boundary.
"""

from __future__ import annotations

import glob
import json
import os
import re
import sys
from pathlib import Path
from typing import Any


def _diagnostic(message: str) -> None:
    print(f"[codex-themis] {message}", file=sys.stderr)


def _block(message: str) -> None:
    print(json.dumps({"decision": "block", "reason": message}))


def _derive_role_from_agent_path(agent_path: str) -> str | None:
    name = agent_path.rsplit("/", 1)[-1]
    base = re.sub(r"_\d+$", "", name)
    return base or None


def _resolve_agent_role(agent_id: str) -> str | None:
    """Resolve a Codex subagent id through its rollout session metadata."""
    sessions_dir = Path(os.path.expanduser("~/.codex/sessions"))
    pattern = str(sessions_dir / "**" / f"rollout-*-{glob.escape(agent_id)}.jsonl")
    matches = glob.glob(pattern, recursive=True)
    if not matches:
        return None
    try:
        with open(matches[0], encoding="utf-8") as rollout_file:
            first_line = rollout_file.readline()
        record = json.loads(first_line)
        agent_path = (record.get("payload") or {}).get("agent_path")
        if not isinstance(agent_path, str) or not agent_path:
            return None
        return _derive_role_from_agent_path(agent_path)
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        return None


def evaluate(payload: dict[str, Any]) -> tuple[str, str] | None:
    """Return ``(rule_id, message)`` only for a blocking decision."""
    agent_id = payload.get("agent_id")
    if agent_id is None:
        role = "master"
    elif isinstance(agent_id, str) and agent_id:
        role = _resolve_agent_role(agent_id)
        if role is None:
            _diagnostic(f"could not resolve role for agent_id={agent_id[:12]}; fail-open")
            return None
    else:
        _diagnostic("agent_id is not a non-empty string; fail-open")
        return None

    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name:
        _diagnostic(f"missing or invalid tool_name for role={role}; fail-open")
        return None
    tool_input = payload.get("tool_input", {})
    if not isinstance(tool_input, dict):
        _diagnostic(f"tool_input is not an object for role={role}; fail-open")
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        cwd = os.getcwd()

    try:
        from themis.data import Severity

        from khimaira.hooks.local_themis import evaluate_local

        outcome = evaluate_local(role, tool_name, tool_input, cwd)
    except Exception as exc:  # noqa: BLE001 - standalone hook must fail open
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
        return (violation.rule_id, violation.message)

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
    if denial is not None:
        rule_id, message = denial
        _block(f"Themis {rule_id}: {message}")
    return 0


def _run_main_fail_open() -> int:
    """Keep an unexpected adapter bug from blocking Codex itself."""
    try:
        return main()
    except Exception as exc:  # noqa: BLE001 — outermost guardrail boundary
        _diagnostic(f"unhandled hook exception; fail-open: {exc}")
        return 0


if __name__ == "__main__":
    raise SystemExit(_run_main_fail_open())
