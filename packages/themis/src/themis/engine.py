"""Themis rule engine.

Public API:
  evaluate(role, tool_name, tool_input, conditions_payload) -> EvalResult

Algorithm (two-pass, per spec §"Matcher shapes"):
  1. Collect every invariant whose matchers fire AND whose conditions all pass.
  2. Select the highest-severity match; ties within the same severity break by
     id (lexicographic). block > warn > audit.

This guarantees a block invariant always wins over a warn/audit match on the
same call — id-order accidents can never silently degrade enforcement.
"""

from __future__ import annotations

from typing import Any

from themis.conditions import evaluate_condition
from themis.data import (
    EvalResult,
    Invariant,
    RuleSet,
    ViolationDetail,
    load_rules,
)


def evaluate(
    role: str,
    tool_name: str,
    tool_input: dict[str, Any],
    conditions_payload: dict[str, Any] | None = None,
    rule_set: RuleSet | None = None,
) -> EvalResult:
    """Evaluate all invariants for `role` against a proposed tool call.

    Args:
        role: The session's role (e.g. "intake", "master").
        tool_name: The tool being called (e.g. "Edit", "mcp__khimaira__auto").
        tool_input: The tool's input dict (may be partial / empty).
        conditions_payload: Runtime state for condition evaluation
            (e.g. {"idle_agents": [...], "turn_start_ts": "..."}).
            If None, conditions that require runtime state evaluate to False
            (fail-closed: missing payload means the condition does NOT fire).
        rule_set: Override for testing. If None, loads from YAML.

    Returns:
        EvalResult with ok=True if no violation, or ok=False with ViolationDetail.
    """
    if conditions_payload is None:
        conditions_payload = {}

    if rule_set is None:
        rule_set = load_rules(role)

    matched: list[Invariant] = []
    for inv in rule_set.invariants:
        if not inv.tool_matches(tool_name, tool_input):
            continue
        if _conditions_pass(inv.conditions, conditions_payload):
            matched.append(inv)

    if not matched:
        return EvalResult(ok=True, role=role)

    winner = _select_winner(matched)
    return EvalResult(
        ok=False,
        role=role,
        violation=ViolationDetail(
            rule_id=winner.id,
            name=winner.name,
            severity=winner.severity,
            message=winner.message.format(tool_name=tool_name),
        ),
    )


def _conditions_pass(condition_names: list[str], payload: dict[str, Any]) -> bool:
    """Return True only when ALL conditions evaluate to True (AND-only gate).

    Phase 1 conditions: no OR/NOT support by design — see spec §"Condition shapes".
    Unknown condition name evaluates to False (fail-closed: unknown condition means
    the rule does NOT fire so we don't accidentally block on a typo).
    """
    return all(evaluate_condition(name, payload) for name in condition_names)


def _select_winner(candidates: list[Invariant]) -> Invariant:
    """Pick the highest-severity invariant; break ties lexicographically by id."""
    return max(candidates, key=lambda inv: (inv.severity.rank, inv.id))
