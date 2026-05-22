"""Tests for themis.data — YAML loader and data model."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest
import yaml

from themis.data import (
    VALID_ROLES,
    Invariant,
    Matcher,
    RuleSet,
    Severity,
    ToolInputMatcher,
    ViolationRecord,
    load_all_rules,
    load_rules,
)


class TestSeverityRank:
    def test_block_beats_warn(self):
        assert Severity.BLOCK.rank > Severity.WARN.rank

    def test_warn_beats_audit(self):
        assert Severity.WARN.rank > Severity.AUDIT.rank

    def test_block_beats_audit(self):
        assert Severity.BLOCK.rank > Severity.AUDIT.rank


class TestMatcher:
    def test_exact_tool_match(self):
        m = Matcher(tool="Edit")
        assert m.matches("Edit", {})

    def test_tool_mismatch(self):
        m = Matcher(tool="Edit")
        assert not m.matches("Write", {})

    def test_tool_input_field_match(self):
        m = Matcher(tool="Bash", tool_input_field=ToolInputMatcher(field="command", pattern="--no-verify"))
        assert m.matches("Bash", {"command": "git commit --no-verify"})

    def test_tool_input_field_no_match(self):
        m = Matcher(tool="Bash", tool_input_field=ToolInputMatcher(field="command", pattern="--no-verify"))
        assert not m.matches("Bash", {"command": "git commit -m 'ok'"})

    def test_tool_input_field_missing_field(self):
        m = Matcher(tool="Edit", tool_input_field=ToolInputMatcher(field="new_string", pattern="secret"))
        assert not m.matches("Edit", {})

    def test_tool_input_field_requires_tool_match(self):
        m = Matcher(tool="Edit", tool_input_field=ToolInputMatcher(field="command", pattern="x"))
        # Tool name is wrong — should not match even if field would
        assert not m.matches("Bash", {"command": "x"})


class TestInvariant:
    def test_tool_matches_or_combined(self):
        inv = Invariant(
            id="TEST-1",
            name="MULTI",
            severity=Severity.BLOCK,
            matchers=[Matcher(tool="Edit"), Matcher(tool="Write")],
            message="blocked",
        )
        assert inv.tool_matches("Edit", {})
        assert inv.tool_matches("Write", {})
        assert not inv.tool_matches("Read", {})


class TestRuleSet:
    def test_all_tool_names(self):
        rs = RuleSet(
            role="intake",
            invariants=[
                Invariant(
                    id="T-1",
                    name="A",
                    severity=Severity.BLOCK,
                    matchers=[Matcher(tool="Edit"), Matcher(tool="Write")],
                    message="",
                ),
                Invariant(
                    id="T-2",
                    name="B",
                    severity=Severity.BLOCK,
                    matchers=[Matcher(tool="Edit"), Matcher(tool="Task")],
                    message="",
                ),
            ],
        )
        names = rs.all_tool_names
        assert "Edit" in names
        assert "Write" in names
        assert "Task" in names
        # No duplicates
        assert len(names) == len(set(names))


class TestLoadRules:
    def test_load_all_bundled_rules(self):
        for role in VALID_ROLES:
            rs = load_rules(role)
            assert rs.role == role
            assert isinstance(rs.invariants, list)

    def test_unknown_role_raises(self):
        with pytest.raises(ValueError, match="Unknown role"):
            load_rules("supervillain")

    def test_all_bundled_rules_have_required_fields(self):
        for role in VALID_ROLES:
            rs = load_rules(role)
            for inv in rs.invariants:
                assert inv.id
                assert inv.name
                assert isinstance(inv.severity, Severity)
                assert inv.matchers
                assert inv.message

    def test_load_all_rules_returns_all_roles(self):
        all_rules = load_all_rules()
        assert set(all_rules.keys()) == VALID_ROLES

    def test_verifier_has_no_invariants(self):
        rs = load_rules("verifier")
        assert rs.invariants == []

    def test_intake_has_three_invariants(self):
        rs = load_rules("intake")
        assert len(rs.invariants) == 3

    def test_intake_in_intake_1_severity_is_block(self):
        rs = load_rules("intake")
        inv = next(i for i in rs.invariants if i.id == "IN-INTAKE-1")
        assert inv.severity == Severity.BLOCK

    def test_master_in_master_1_has_conditions(self):
        rs = load_rules("master")
        inv = next(i for i in rs.invariants if i.id == "IN-MASTER-1")
        assert "chat_my_chats_not_called_this_turn" in inv.conditions

    def test_master_in_master_3_has_idle_agents_condition(self):
        rs = load_rules("master")
        inv = next(i for i in rs.invariants if i.id == "IN-MASTER-3")
        assert "idle_agents_exist" in inv.conditions

    def test_agent_in_agent_1_has_tool_input_matcher(self):
        rs = load_rules("agent")
        inv = next(i for i in rs.invariants if i.id == "IN-AGENT-1")
        # At least one matcher has a tool_input_field
        assert any(m.tool_input_field is not None for m in inv.matchers)

    def test_agent_in_agent_2_bash_no_verify(self):
        rs = load_rules("agent")
        inv = next(i for i in rs.invariants if i.id == "IN-AGENT-2")
        bash_matchers = [m for m in inv.matchers if m.tool == "Bash"]
        assert bash_matchers
        assert bash_matchers[0].tool_input_field is not None
        assert "--no-verify" in bash_matchers[0].tool_input_field.pattern

    def test_malformed_yaml_raises(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("themis.data._RULES_DIR", tmp_path)
        (tmp_path / "intake.yaml").write_text("- not: a mapping")
        with pytest.raises(ValueError, match="must be a YAML mapping"):
            load_rules("intake")

    def test_missing_required_field_raises(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr("themis.data._RULES_DIR", tmp_path)
        (tmp_path / "intake.yaml").write_text(
            textwrap.dedent("""
            role: intake
            invariants:
              - id: T-1
                name: MISSING_FIELDS
                severity: block
                # matchers and message are missing
            """)
        )
        with pytest.raises(ValueError, match="missing required fields"):
            load_rules("intake")


class TestViolationRecord:
    def test_round_trip(self):
        rec = ViolationRecord(
            ts="2026-05-21T17:00:00+00:00",
            session_id="abc",
            session_name="agent-1",
            role="agent",
            rule_id="IN-AGENT-2",
            tool_name="Bash",
            tool_use_id="toolu_123",
            tool_input_summary='{"command": "git commit --no-verify"}',
            decision="blocked",
            cwd="/home/user/project",
        )
        d = rec.to_dict()
        restored = ViolationRecord.from_dict(d)
        assert restored.session_id == rec.session_id
        assert restored.rule_id == rec.rule_id
        assert restored.decision == rec.decision
