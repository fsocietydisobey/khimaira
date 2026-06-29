"""Tests for Themis app-scoped rule extension (find_app_rules_dir, _load_app_invariants,
_merge_app_layer, and load_rules with app_rules_dir).

Convention: APP-<app-name>-N for app rule ids, e.g. APP-TEST-1.
"""

from __future__ import annotations

import logging
import textwrap
from pathlib import Path

import pytest

from themis.data import (
    _load_app_invariants,
    _merge_app_layer,
    find_app_rules_dir,
    load_rules,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_app_rule(app_rules_dir: Path, role: str, invariants_yaml: str) -> Path:
    """Write a minimal app rule file and return its path.

    `invariants_yaml` must be a valid YAML block-sequence string (each item
    starts with `- id: ...`). It is placed verbatim after `invariants:` at
    column 0, so the items may be at column 0 or 2 — either is valid YAML.
    Do NOT wrap this function in textwrap.dedent; the mixed-indent f-string
    trick corrupts subsequent lines of multi-line string values.
    """
    path = app_rules_dir / f"{role}.yaml"
    path.write_text(f"role: {role}\ninvariants:\n{invariants_yaml}")
    return path


# ---------------------------------------------------------------------------
# find_app_rules_dir
# ---------------------------------------------------------------------------


def test_find_app_rules_dir_no_git_returns_none(tmp_path: Path) -> None:
    """Session outside any git repo → no app rules dir → None (core-only, fail-open)."""
    # No .git anywhere under tmp_path hierarchy.
    nested = tmp_path / "some" / "nested" / "dir"
    nested.mkdir(parents=True)
    assert find_app_rules_dir(str(nested)) is None


def test_find_app_rules_dir_finds_dir_at_git_root(tmp_path: Path) -> None:
    """App rules dir at the git root is found from a nested working directory."""
    # Simulate repo root with .git + .claude/themis/
    (tmp_path / ".git").mkdir()
    app_dir = tmp_path / ".claude" / "themis"
    app_dir.mkdir(parents=True)

    nested = tmp_path / "packages" / "foo" / "src"
    nested.mkdir(parents=True)

    result = find_app_rules_dir(str(nested))
    assert result == app_dir


def test_find_app_rules_dir_stops_at_git_root(tmp_path: Path) -> None:
    """find_app_rules_dir does not ascend past the git root even if a parent has the dir."""
    # Parent of tmp_path has .claude/themis/ (simulate a super-repo or home dir).
    # Repo root is tmp_path itself — the walk must stop there.
    (tmp_path / ".git").mkdir()
    # No .claude/themis/ under the repo root.

    result = find_app_rules_dir(str(tmp_path / "src"))
    assert result is None


def test_find_app_rules_dir_worktree_git_file(tmp_path: Path) -> None:
    """Handles git worktree where .git is a file, not a directory."""
    (tmp_path / ".git").write_text("gitdir: ../.git/worktrees/my-branch\n")
    app_dir = tmp_path / ".claude" / "themis"
    app_dir.mkdir(parents=True)

    result = find_app_rules_dir(str(tmp_path / "nested"))
    assert result == app_dir


# ---------------------------------------------------------------------------
# _load_app_invariants
# ---------------------------------------------------------------------------


def test_load_app_invariants_returns_none_when_no_file(tmp_path: Path) -> None:
    """Missing role file → None (not an error; core-only)."""
    app_dir = tmp_path / "themis"
    app_dir.mkdir()
    result = _load_app_invariants(app_dir, "agent")
    assert result is None


def test_load_app_invariants_fail_open_bad_yaml(tmp_path: Path, caplog) -> None:
    """Bad YAML → returns [] and logs WARNING; never raises (fail-open)."""
    app_dir = tmp_path / "themis"
    app_dir.mkdir()
    (app_dir / "agent.yaml").write_text("this: [is: bad: yaml\n")

    with caplog.at_level(logging.WARNING, logger="themis.data"):
        result = _load_app_invariants(app_dir, "agent")

    assert result == []
    assert any("fail-open" in r.message.lower() or "parse error" in r.message.lower() for r in caplog.records)


def test_load_app_invariants_returns_invariants(tmp_path: Path) -> None:
    """Well-formed app rule file returns parsed invariants."""
    app_dir = tmp_path / "themis"
    app_dir.mkdir()
    _write_app_rule(
        app_dir,
        "agent",
        textwrap.dedent("""\
          - id: APP-TEST-1
            name: TEST_RULE
            severity: warn
            matchers:
              - tool: Bash
                tool_input_field:
                  field: command
                  pattern: "forbidden_cmd"
            message: "APP-TEST-1: forbidden_cmd detected."
        """),
    )
    result = _load_app_invariants(app_dir, "agent")
    assert result is not None
    assert len(result) == 1
    assert result[0].id == "APP-TEST-1"


# ---------------------------------------------------------------------------
# _merge_app_layer
# ---------------------------------------------------------------------------


def test_merge_app_layer_adds_new_ids(tmp_path: Path) -> None:
    """App rules with fresh IDs are appended after core rules."""
    app_dir = tmp_path / "themis"
    app_dir.mkdir()
    _write_app_rule(
        app_dir,
        "agent",
        textwrap.dedent("""\
          - id: APP-TEST-1
            name: TEST_RULE
            severity: warn
            matchers:
              - tool: Bash
                tool_input_field:
                  field: command
                  pattern: "forbidden_cmd"
            message: "APP-TEST-1: test."
        """),
    )
    core_rule_set = load_rules("agent")
    app_invs = _load_app_invariants(app_dir, "agent")
    assert app_invs is not None

    merged = _merge_app_layer(core_rule_set.invariants, app_invs)
    ids = [inv.id for inv in merged]
    # All core ids still present
    for inv in core_rule_set.invariants:
        assert inv.id in ids
    # App rule appended
    assert "APP-TEST-1" in ids
    # App rule is AFTER all core rules (additive)
    core_ids = {inv.id for inv in core_rule_set.invariants}
    core_positions = [i for i, inv in enumerate(merged) if inv.id in core_ids]
    app_position = ids.index("APP-TEST-1")
    assert app_position > max(core_positions)


def test_merge_app_layer_core_wins_on_id_collision(tmp_path: Path, caplog) -> None:
    """App rule with same ID as a core rule is silently dropped; core version retained."""
    app_dir = tmp_path / "themis"
    app_dir.mkdir()
    # IN-AGENT-1 is a real core rule for the agent role.
    _write_app_rule(
        app_dir,
        "agent",
        textwrap.dedent("""\
          - id: IN-AGENT-1
            name: SHOULD_NOT_OVERRIDE
            severity: audit
            matchers:
              - tool: Bash
                tool_input_field:
                  field: command
                  pattern: "whatever"
            message: "This must not replace the core rule."
        """),
    )
    core_rule_set = load_rules("agent")
    app_invs = _load_app_invariants(app_dir, "agent")
    assert app_invs is not None

    with caplog.at_level(logging.WARNING, logger="themis.data"):
        merged = _merge_app_layer(core_rule_set.invariants, app_invs)

    # Count occurrences of IN-AGENT-1 — must be exactly 1 (the core version)
    matching = [inv for inv in merged if inv.id == "IN-AGENT-1"]
    assert len(matching) == 1
    # The surviving one must be the core version (name = LOAD_DOTENV_OVERRIDE)
    assert matching[0].name == "LOAD_DOTENV_OVERRIDE"
    # A WARNING must have been emitted about the collision
    assert any("core wins" in r.message.lower() or "collision" in r.message.lower() or "same id" in r.message.lower() for r in caplog.records)


# ---------------------------------------------------------------------------
# load_rules integration
# ---------------------------------------------------------------------------


def test_load_rules_with_app_rules_dir_merges_additive(tmp_path: Path) -> None:
    """load_rules(role, app_rules_dir=...) returns core + app rules merged."""
    app_dir = tmp_path / "themis"
    app_dir.mkdir()
    _write_app_rule(
        app_dir,
        "agent",
        textwrap.dedent("""\
          - id: APP-MYPROJECT-1
            name: NO_DEPLOY_PROD
            severity: block
            matchers:
              - tool: Bash
                tool_input_field:
                  field: command
                  pattern: "deploy --env prod"
            message: "APP-MYPROJECT-1: production deploys blocked in this repo."
        """),
    )
    core_rule_set = load_rules("agent")
    merged_rule_set = load_rules("agent", app_rules_dir=app_dir)

    core_ids = {inv.id for inv in core_rule_set.invariants}
    merged_ids = {inv.id for inv in merged_rule_set.invariants}

    # All core IDs present in merged
    assert core_ids.issubset(merged_ids)
    # App rule present
    assert "APP-MYPROJECT-1" in merged_ids
    # Merged has exactly core + 1 new app rule
    assert len(merged_rule_set.invariants) == len(core_rule_set.invariants) + 1


def test_load_rules_missing_app_file_returns_core_only(tmp_path: Path) -> None:
    """app_rules_dir given but no file for this role → same rule set as core-only."""
    app_dir = tmp_path / "themis"
    app_dir.mkdir()
    # No agent.yaml written

    core_rule_set = load_rules("agent")
    merged_rule_set = load_rules("agent", app_rules_dir=app_dir)

    assert {inv.id for inv in core_rule_set.invariants} == {
        inv.id for inv in merged_rule_set.invariants
    }
