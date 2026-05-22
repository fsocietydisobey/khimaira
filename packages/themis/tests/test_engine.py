"""Tests for themis.engine — evaluate() two-pass severity logic."""

from __future__ import annotations

import pytest

from themis.data import (
    EvalResult,
    Invariant,
    Matcher,
    RuleSet,
    Severity,
    ToolInputMatcher,
)
from themis.engine import evaluate, _conditions_pass, _select_winner


# ---------------------------------------------------------------------------
# Fixtures — synthetic rule sets for fast, YAML-free unit tests
# ---------------------------------------------------------------------------

def _make_rule_set(role: str, invariants: list[Invariant]) -> RuleSet:
    return RuleSet(role=role, invariants=invariants)


def _block_inv(id: str, tool: str) -> Invariant:
    return Invariant(
        id=id, name=f"BLOCK_{id}", severity=Severity.BLOCK,
        matchers=[Matcher(tool=tool)], message=f"blocked by {id}",
    )


def _warn_inv(id: str, tool: str) -> Invariant:
    return Invariant(
        id=id, name=f"WARN_{id}", severity=Severity.WARN,
        matchers=[Matcher(tool=tool)], message=f"warned by {id}",
    )


def _audit_inv(id: str, tool: str) -> Invariant:
    return Invariant(
        id=id, name=f"AUDIT_{id}", severity=Severity.AUDIT,
        matchers=[Matcher(tool=tool)], message=f"audited by {id}",
    )


# ---------------------------------------------------------------------------
# Basic pass/block
# ---------------------------------------------------------------------------

class TestEvaluateBasic:
    def test_no_match_returns_ok(self):
        rs = _make_rule_set("intake", [_block_inv("IN-1", "Edit")])
        result = evaluate("intake", "Read", {}, rule_set=rs)
        assert result.ok is True

    def test_match_returns_violation(self):
        rs = _make_rule_set("intake", [_block_inv("IN-1", "Edit")])
        result = evaluate("intake", "Edit", {}, rule_set=rs)
        assert result.ok is False
        assert result.violation is not None
        assert result.violation.rule_id == "IN-1"

    def test_result_includes_role(self):
        rs = _make_rule_set("intake", [_block_inv("IN-1", "Edit")])
        result = evaluate("intake", "Edit", {}, rule_set=rs)
        assert result.role == "intake"

    def test_ok_result_includes_role(self):
        rs = _make_rule_set("intake", [])
        result = evaluate("intake", "Edit", {}, rule_set=rs)
        assert result.role == "intake"
        assert result.ok is True


# ---------------------------------------------------------------------------
# Severity precedence — the non-negotiable test per analyst-1 ctx-734a8fc7
# ---------------------------------------------------------------------------

class TestEngineSeverityPrecedence:
    """audit + block both matching → block wins, regardless of id-order."""

    def test_block_beats_audit_low_block_id(self):
        # audit has a "lower" id (AA-) than block (ZZ-): block must still win
        rs = _make_rule_set(
            "test",
            [
                _audit_inv("AA-AUDIT", "Edit"),
                _block_inv("ZZ-BLOCK", "Edit"),
            ],
        )
        result = evaluate("test", "Edit", {}, rule_set=rs)
        assert result.ok is False
        assert result.violation is not None
        assert result.violation.severity == Severity.BLOCK
        assert result.violation.rule_id == "ZZ-BLOCK"

    def test_block_beats_audit_high_block_id(self):
        # block has a "higher" id than audit: block still wins (just confirming)
        rs = _make_rule_set(
            "test",
            [
                _block_inv("AA-BLOCK", "Edit"),
                _audit_inv("ZZ-AUDIT", "Edit"),
            ],
        )
        result = evaluate("test", "Edit", {}, rule_set=rs)
        assert result.violation.severity == Severity.BLOCK

    def test_block_beats_warn(self):
        rs = _make_rule_set(
            "test",
            [
                _warn_inv("WARN-1", "Bash"),
                _block_inv("BLOCK-1", "Bash"),
            ],
        )
        result = evaluate("test", "Bash", {}, rule_set=rs)
        assert result.violation.severity == Severity.BLOCK

    def test_warn_beats_audit(self):
        rs = _make_rule_set(
            "test",
            [
                _audit_inv("AUDIT-1", "Task"),
                _warn_inv("WARN-1", "Task"),
            ],
        )
        result = evaluate("test", "Task", {}, rule_set=rs)
        assert result.violation.severity == Severity.WARN

    def test_id_tiebreak_within_same_severity(self):
        # Two block invariants match — lexicographically larger id wins the tiebreak
        rs = _make_rule_set(
            "test",
            [
                _block_inv("IN-B", "Edit"),
                _block_inv("IN-A", "Edit"),
            ],
        )
        result = evaluate("test", "Edit", {}, rule_set=rs)
        # max("IN-B", "IN-A") == "IN-B"
        assert result.violation.rule_id == "IN-B"


# ---------------------------------------------------------------------------
# Conditions AND-combining
# ---------------------------------------------------------------------------

class TestConditionEvaluation:
    def test_unconditional_rule_always_fires(self):
        rs = _make_rule_set("master", [_block_inv("IN-1", "Task")])
        result = evaluate("master", "Task", {}, conditions_payload={}, rule_set=rs)
        assert result.ok is False

    def test_condition_not_met_rule_does_not_fire(self):
        inv = Invariant(
            id="IN-MASTER-3",
            name="NO_STANDALONE_WHEN_IDLE",
            severity=Severity.BLOCK,
            matchers=[Matcher(tool="Task")],
            message="blocked",
            conditions=["idle_agents_exist"],
        )
        rs = _make_rule_set("master", [inv])
        # idle_agents absent from payload → condition False → rule does not fire
        result = evaluate("master", "Task", {}, conditions_payload={}, rule_set=rs)
        assert result.ok is True

    def test_condition_met_rule_fires(self):
        inv = Invariant(
            id="IN-MASTER-3",
            name="NO_STANDALONE_WHEN_IDLE",
            severity=Severity.BLOCK,
            matchers=[Matcher(tool="Task")],
            message="blocked",
            conditions=["idle_agents_exist"],
        )
        rs = _make_rule_set("master", [inv])
        payload = {"idle_agents": [{"session_id": "x", "name": "agent-1"}]}
        result = evaluate("master", "Task", {}, conditions_payload=payload, rule_set=rs)
        assert result.ok is False

    def test_all_conditions_must_pass(self):
        inv = Invariant(
            id="IN-1",
            name="MULTI_COND",
            severity=Severity.BLOCK,
            matchers=[Matcher(tool="Bash")],
            message="blocked",
            conditions=["idle_agents_exist", "chat_my_chats_not_called_this_turn"],
        )
        rs = _make_rule_set("master", [inv])
        # Only one condition met — both must pass
        payload = {"idle_agents": [{"session_id": "x"}]}
        result = evaluate("master", "Bash", {}, conditions_payload=payload, rule_set=rs)
        assert result.ok is True

    def test_unknown_condition_treats_as_false(self):
        inv = Invariant(
            id="IN-1",
            name="UNKNOWN_COND",
            severity=Severity.BLOCK,
            matchers=[Matcher(tool="Edit")],
            message="blocked",
            conditions=["nonexistent_condition_xyz"],
        )
        rs = _make_rule_set("test", [inv])
        result = evaluate("test", "Edit", {}, conditions_payload={}, rule_set=rs)
        assert result.ok is True

    def test_none_conditions_payload_treated_as_empty(self):
        inv = Invariant(
            id="IN-1",
            name="COND",
            severity=Severity.BLOCK,
            matchers=[Matcher(tool="Task")],
            message="blocked",
            conditions=["idle_agents_exist"],
        )
        rs = _make_rule_set("master", [inv])
        result = evaluate("master", "Task", {}, conditions_payload=None, rule_set=rs)
        assert result.ok is True


# ---------------------------------------------------------------------------
# tool_input_field matcher
# ---------------------------------------------------------------------------

class TestToolInputFieldMatcher:
    def test_bash_no_verify_blocked(self):
        rs = RuleSet(
            role="agent",
            invariants=[
                Invariant(
                    id="IN-AGENT-2",
                    name="NO_NO_VERIFY",
                    severity=Severity.BLOCK,
                    matchers=[
                        Matcher(
                            tool="Bash",
                            tool_input_field=ToolInputMatcher(
                                field="command", pattern=r"--no-verify\b"
                            ),
                        )
                    ],
                    message="blocked",
                )
            ],
        )
        result = evaluate("agent", "Bash", {"command": "git commit --no-verify"}, rule_set=rs)
        assert result.ok is False

    def test_bash_clean_command_allowed(self):
        rs = RuleSet(
            role="agent",
            invariants=[
                Invariant(
                    id="IN-AGENT-2",
                    name="NO_NO_VERIFY",
                    severity=Severity.BLOCK,
                    matchers=[
                        Matcher(
                            tool="Bash",
                            tool_input_field=ToolInputMatcher(
                                field="command", pattern=r"--no-verify\b"
                            ),
                        )
                    ],
                    message="blocked",
                )
            ],
        )
        result = evaluate("agent", "Bash", {"command": "git commit -m 'clean'"}, rule_set=rs)
        assert result.ok is True

    def test_or_combined_matchers(self):
        """If tool matches but tool_input_field doesn't, other matchers are tried."""
        rs = RuleSet(
            role="test",
            invariants=[
                Invariant(
                    id="IN-1",
                    name="MULTI",
                    severity=Severity.BLOCK,
                    matchers=[
                        Matcher(tool="Write"),  # plain tool match — no input check
                        Matcher(
                            tool="Edit",
                            tool_input_field=ToolInputMatcher(field="new_string", pattern="secret"),
                        ),
                    ],
                    message="blocked",
                )
            ],
        )
        # Write matches via first matcher
        assert evaluate("test", "Write", {}, rule_set=rs).ok is False
        # Edit matches if new_string contains secret
        assert evaluate("test", "Edit", {"new_string": "secret key"}, rule_set=rs).ok is False
        # Edit without the pattern does not match
        assert evaluate("test", "Edit", {"new_string": "normal content"}, rule_set=rs).ok is True


# ---------------------------------------------------------------------------
# Bundled YAML rules smoke-tests
# ---------------------------------------------------------------------------

class TestBundledRulesEngine:
    def test_intake_edit_blocked(self):
        result = evaluate("intake", "Edit", {})
        assert result.ok is False
        assert result.violation.rule_id == "IN-INTAKE-1"
        assert result.violation.severity == Severity.BLOCK

    def test_intake_read_allowed(self):
        result = evaluate("intake", "Read", {})
        assert result.ok is True

    def test_observer_edit_blocked(self):
        result = evaluate("observer", "Write", {})
        assert result.ok is False
        assert result.violation.rule_id == "IN-OBSERVER-1"

    def test_verifier_edit_allowed(self):
        result = evaluate("verifier", "Edit", {})
        assert result.ok is True

    def test_agent_no_verify_blocked(self):
        result = evaluate("agent", "Bash", {"command": "git commit --no-verify -m x"})
        assert result.ok is False
        assert result.violation.rule_id == "IN-AGENT-2"

    def test_agent_clean_bash_allowed(self):
        result = evaluate("agent", "Bash", {"command": "pytest -x"})
        assert result.ok is True

    def test_master_no_api_dispatch(self):
        result = evaluate("master", "mcp__khimaira__auto", {})
        assert result.ok is False
        assert result.violation.rule_id == "IN-MASTER-2"

    def test_architect_edit_blocked(self):
        result = evaluate("architect", "Edit", {})
        assert result.ok is False
        assert result.violation.rule_id == "IN-ARCHITECT-1"
