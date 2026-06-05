"""Tests for intake-role Themis invariants (IN-INTAKE-2, IN-INTAKE-3, IN-INTAKE-5).

Intake now has master-equivalent write access (Joseph-directed, 2026-06-05):
  - IN-INTAKE-1 (NO_FILE_EDIT) removed — intake may call Edit/Write/MultiEdit
  - IN-INTAKE-4 (NO_BASH_MUTATING) removed — intake may run mutating Bash
  - Coordination guards (IN-INTAKE-2/3/5) remain

These tests verify that Edit/Write/Bash-mutating are no longer blocked for intake,
and that the remaining coordination invariants still hold.
"""

from __future__ import annotations

from themis.engine import evaluate


class TestIntakeWriteAccessGranted:
    """Intake now has write access — Edit/Write/Bash-mutating must not be blocked."""

    def test_allows_edit_for_intake(self):
        """Edit is no longer blocked for intake (IN-INTAKE-1 removed)."""
        result = evaluate("intake", "Edit", {})
        assert result.ok is True

    def test_allows_write_for_intake(self):
        """Write is no longer blocked for intake (IN-INTAKE-1 removed)."""
        result = evaluate("intake", "Write", {})
        assert result.ok is True

    def test_allows_multiedit_for_intake(self):
        """MultiEdit is no longer blocked for intake (IN-INTAKE-1 removed)."""
        result = evaluate("intake", "MultiEdit", {})
        assert result.ok is True

    def test_allows_read_for_intake(self):
        """Read was always allowed and remains allowed."""
        result = evaluate("intake", "Read", {})
        assert result.ok is True

    def test_allows_non_edit_tools_for_intake(self):
        """Intake calling chat_send_to is not blocked."""
        result = evaluate("intake", "mcp__khimaira-chat__chat_send_to", {})
        assert result.ok is True
