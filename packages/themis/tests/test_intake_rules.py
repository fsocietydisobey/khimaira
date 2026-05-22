"""Tests for intake-role Themis invariants (IN-INTAKE-1, IN-INTAKE-2, IN-INTAKE-3).

Verifies the structural enforcement layer that blocks intake from editing code:
  - IN-INTAKE-1 fires on Edit/Write/MultiEdit/NotebookEdit for intake sessions
  - IN-INTAKE-1 does NOT fire for other roles (rule is intake-scoped only)
  - Non-editing tools are not blocked by IN-INTAKE-1

Context: jp-intake-1 violated the prose-level NO_IMPLEMENTATION rule on both
2026-05-21 and 2026-05-22 despite the brief. These tests verify the Themis
enforcement layer that makes the rule structural rather than aspirational.
"""

from __future__ import annotations

import pytest

from themis.data import Severity
from themis.engine import evaluate


class TestININTAKE1NoFileEdit:
    """IN-INTAKE-1 (NO_FILE_EDIT): intake cannot call file-editing tools."""

    def test_blocks_edit_for_intake(self):
        """Edit for an intake session triggers IN-INTAKE-1 at block severity."""
        result = evaluate("intake", "Edit", {})
        assert result.ok is False
        assert result.violation.rule_id == "IN-INTAKE-1"
        assert result.violation.severity == Severity.BLOCK

    def test_blocks_write_for_intake(self):
        """Write for an intake session triggers IN-INTAKE-1 at block severity."""
        result = evaluate("intake", "Write", {})
        assert result.ok is False
        assert result.violation.rule_id == "IN-INTAKE-1"
        assert result.violation.severity == Severity.BLOCK

    def test_blocks_multiedit_for_intake(self):
        """MultiEdit for an intake session triggers IN-INTAKE-1 at block severity."""
        result = evaluate("intake", "MultiEdit", {})
        assert result.ok is False
        assert result.violation.rule_id == "IN-INTAKE-1"
        assert result.violation.severity == Severity.BLOCK

    def test_does_not_fire_for_master(self):
        """IN-INTAKE-1 is intake-scoped — master calling Edit does NOT trigger it."""
        result = evaluate("master", "Edit", {})
        # master.yaml has no rule blocking Edit — master can edit freely
        assert result.ok is True

    def test_does_not_fire_for_agent(self):
        """IN-INTAKE-1 is intake-scoped — agent calling Edit does NOT trigger it."""
        result = evaluate("agent", "Edit", {})
        assert result.ok is True

    def test_allows_non_edit_tools_for_intake(self):
        """Intake calling chat_send_to is NOT blocked by IN-INTAKE-1."""
        result = evaluate("intake", "mcp__khimaira-chat__chat_send_to", {})
        assert result.ok is True

    def test_allows_read_for_intake(self):
        """Intake reading a file is NOT blocked (Read != Edit)."""
        result = evaluate("intake", "Read", {})
        assert result.ok is True

    def test_violation_message_mentions_dispatch(self):
        """The block message tells intake HOW to dispatch instead of editing."""
        result = evaluate("intake", "Edit", {})
        assert result.ok is False
        # Message should guide toward the correct action
        assert "agent" in result.violation.message.lower() or "assign" in result.violation.message.lower() or "dispatch" in result.violation.message.lower()
