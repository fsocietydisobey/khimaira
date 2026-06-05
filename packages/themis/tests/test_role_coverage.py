"""Role-boundary-violation bug class invariant tests.

Asserts every non-executor role has Themis rules covering all executor
tools (Edit/Write/MultiEdit/NotebookEdit/Task) and Bash. New non-executor
roles added without these rules fail this test with a clear message.

Verifier is excluded by design — Mode B verification requires Edit/Write
on test files. See verifier.yaml header for the deferred Phase 3
path-allowlist approach.

See ~/.claude/rules/personal/bug-class-enumeration.md for the discipline
this test enforces.
"""

from __future__ import annotations

import pytest

from themis.data import Severity, load_rules

_NON_EXECUTOR_ROLES = [
    # intake is executor-level (master-equivalent write access, 2026-06-05)
    "tracker",
    "observer",
    "architect",
    "analyst",
    "critic",
]

_EXECUTOR_TOOLS = [
    "Edit",
    "Write",
    "MultiEdit",
    "NotebookEdit",
    "Task",
]


@pytest.mark.parametrize("role", _NON_EXECUTOR_ROLES)
@pytest.mark.parametrize("tool", _EXECUTOR_TOOLS)
def test_non_executor_role_blocks_executor_tool(role, tool):
    """Every non-executor role must have a severity=block invariant matching every executor tool.

    Guards the role-boundary-violation bug class: non-executor roles
    (intake, tracker, observer, architect, analyst, critic) must not
    perform executor operations.
    """
    rules = load_rules(role)
    blockers = [
        inv
        for inv in rules.invariants
        if inv.severity == Severity.BLOCK
        and any(m.tool == tool for m in inv.matchers)
    ]
    assert blockers, (
        f"Role '{role}' must have at least one severity=block invariant "
        f"matching tool '{tool}'. This guards the role-boundary-violation "
        f"bug class. See ~/.claude/rules/personal/bug-class-enumeration.md."
    )


@pytest.mark.parametrize("role", _NON_EXECUTOR_ROLES)
def test_non_executor_role_blocks_bash(role):
    """Every non-executor role must block Bash.

    For tracker: full block (no tool_input_field — IN-TRACKER-2 NO_BASH).
    For others: mutating-pattern block (tool_input_field — NO_BASH_MUTATING).
    Both forms satisfy this test.
    """
    rules = load_rules(role)
    bash_blockers = [
        inv
        for inv in rules.invariants
        if inv.severity == Severity.BLOCK
        and any(m.tool == "Bash" for m in inv.matchers)
    ]
    assert bash_blockers, (
        f"Role '{role}' must have at least one severity=block invariant "
        f"matching tool 'Bash'. Models: tracker uses NO_BASH (full block); "
        f"others use NO_BASH_MUTATING (regex on mutating commands)."
    )
