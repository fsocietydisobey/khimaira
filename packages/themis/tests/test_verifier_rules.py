"""Tests for verifier-role Themis invariants (IN-VERIFIER-1, IN-VERIFIER-2).

IN-VERIFIER-1 (NO_NONTEST_FILE_EDIT): verifier may only edit test files.
  Production path → block. Test path (test_*, conftest, /tests/, *.spec, *.test) → allow.

IN-VERIFIER-2 (NO_BASH_MUTATING): verifier cannot commit, push, or run destructive ops.
  npm install / pip install → allow (Mode B package restoration). git commit → block.

Context: verifier.yaml was empty by design (Phase 1 deferred). Phase 3 path-allowlist
is now live — closes the gap where verifier could edit/commit any file unchecked.
"""

from __future__ import annotations

import pytest

from themis.data import Severity
from themis.engine import evaluate


# ---------------------------------------------------------------------------
# IN-VERIFIER-1 — path-allowlist (non-test paths blocked, test paths allowed)
# ---------------------------------------------------------------------------

class TestINVERIFIER1NonTestFileEdit:
    """IN-VERIFIER-1 (NO_NONTEST_FILE_EDIT): blocks edits to non-test files."""

    def test_blocks_edit_to_source_file(self):
        """Edit to a production source file triggers IN-VERIFIER-1 at block severity."""
        result = evaluate("verifier", "Edit", {"file_path": "/project/src/foo.py"})
        assert result.ok is False
        assert result.violation.rule_id == "IN-VERIFIER-1"
        assert result.violation.severity == Severity.BLOCK

    def test_blocks_write_to_source_file(self):
        """Write to a production source file triggers IN-VERIFIER-1."""
        result = evaluate("verifier", "Write", {"file_path": "/project/src/bar.ts"})
        assert result.ok is False
        assert result.violation.rule_id == "IN-VERIFIER-1"

    def test_blocks_multiedit_to_source_file(self):
        """MultiEdit to a production source file triggers IN-VERIFIER-1."""
        result = evaluate("verifier", "MultiEdit", {"file_path": "/project/lib/utils.py"})
        assert result.ok is False
        assert result.violation.rule_id == "IN-VERIFIER-1"

    def test_allows_edit_to_test_file_prefix(self):
        """Edit to a test_*.py file is allowed (Mode B: fix failing tests)."""
        result = evaluate("verifier", "Edit", {"file_path": "/project/tests/test_foo.py"})
        assert result.ok is True

    def test_allows_edit_to_suffix_test_file(self):
        """Edit to a *_test.py file is allowed."""
        result = evaluate("verifier", "Edit", {"file_path": "/project/tests/foo_test.py"})
        assert result.ok is True

    def test_allows_edit_in_tests_directory(self):
        """Edit to a file inside /tests/ directory is allowed."""
        result = evaluate("verifier", "Edit", {"file_path": "/project/tests/fixtures/data.json"})
        assert result.ok is True

    def test_allows_edit_to_conftest(self):
        """Edit to conftest.py is allowed (test infrastructure)."""
        result = evaluate("verifier", "Edit", {"file_path": "/project/conftest.py"})
        assert result.ok is True

    def test_allows_edit_to_spec_file(self):
        """Edit to a *.spec.ts file is allowed (frontend test)."""
        result = evaluate("verifier", "Edit", {"file_path": "/project/src/foo.spec.ts"})
        assert result.ok is True

    def test_allows_edit_to_test_dot_file(self):
        """Edit to a *.test.js file is allowed."""
        result = evaluate("verifier", "Edit", {"file_path": "/project/src/foo.test.js"})
        assert result.ok is True

    def test_allows_edit_in_test_directory_singular(self):
        """Edit to a file inside /test/ (singular) directory is allowed."""
        result = evaluate("verifier", "Edit", {"file_path": "/project/test/unit/helpers.py"})
        assert result.ok is True


# ---------------------------------------------------------------------------
# IN-VERIFIER-2 — NO_BASH_MUTATING
# ---------------------------------------------------------------------------

class TestINVERIFIER2BashMutating:
    """IN-VERIFIER-2 (NO_BASH_MUTATING): verifier cannot commit, push, or run
    destructive ops. Mode B package managers (npm install, pip install) are allowed."""

    def test_blocks_git_commit(self):
        """git commit triggers IN-VERIFIER-2 — verifier delivers verdicts, not commits."""
        result = evaluate("verifier", "Bash", {"command": 'git commit -m "fix test"'})
        assert result.ok is False
        assert result.violation.rule_id == "IN-VERIFIER-2"
        assert result.violation.severity == Severity.BLOCK

    def test_blocks_git_push(self):
        """git push triggers IN-VERIFIER-2."""
        result = evaluate("verifier", "Bash", {"command": "git push origin main"})
        assert result.ok is False
        assert result.violation.rule_id == "IN-VERIFIER-2"

    def test_blocks_rm(self):
        """rm triggers IN-VERIFIER-2."""
        result = evaluate("verifier", "Bash", {"command": "rm -rf build/"})
        assert result.ok is False
        assert result.violation.rule_id == "IN-VERIFIER-2"

    def test_allows_npm_install(self):
        """npm install is allowed — Mode B may need to restore deps before running tests."""
        result = evaluate("verifier", "Bash", {"command": "npm install"})
        assert result.ok is True

    def test_allows_pip_install(self):
        """pip install is allowed — Mode B may need deps."""
        result = evaluate("verifier", "Bash", {"command": "pip install -r requirements.txt"})
        assert result.ok is True

    def test_allows_pytest(self):
        """Running pytest is allowed and expected."""
        result = evaluate("verifier", "Bash", {"command": "pytest packages/khimaira/tests/ -q"})
        assert result.ok is True

    def test_allows_git_status(self):
        """git status (read-only) is allowed."""
        result = evaluate("verifier", "Bash", {"command": "git status"})
        assert result.ok is True
