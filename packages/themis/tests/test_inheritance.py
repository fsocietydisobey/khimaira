"""Tests for Themis rule inheritance (load_rules extends-chain + LOCKED enforcement).

Tests map to analyst's live-enforcement spec (T1-T10) where noted. The in-process
tests (T4/T5/T8/T9/T10) are covered here. T1/T2/T3/T6/T7 require the live daemon
(T1: role×Edit→block, T2: lead×ls→pass, T3: agent×Edit→pass, T6: new-id-allow→still-block,
T7: cache-propagation trivially passes since load_rules is uncached); those are authored
separately in the live-enforcement harness for analyst's sign-off.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest
import yaml

from themis.data import (
    VALID_ROLES,
    Invariant,
    RuleSet,
    Severity,
    list_rules,
    load_rules,
    load_rules_flat,
)


# ---------------------------------------------------------------------------
# Helpers: inject temporary yaml files into the rules dir
# ---------------------------------------------------------------------------


@pytest.fixture
def rules_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Redirect the Themis rules dir to a tmp dir for isolation."""
    import themis.data as data_mod

    monkeypatch.setattr(data_mod, "_RULES_DIR", tmp_path)
    # Reload VALID_ROLES to pick up the new dir (it's a module-level frozenset).
    new_valid = frozenset(
        p.stem for p in tmp_path.glob("*.yaml") if not p.stem.endswith(".base")
    )
    monkeypatch.setattr(data_mod, "VALID_ROLES", new_valid)
    return tmp_path


def write_yaml(rules_dir: Path, name: str, doc: dict) -> None:
    (rules_dir / f"{name}.yaml").write_text(yaml.dump(doc))


# ---------------------------------------------------------------------------
# VALID_ROLES excludes *.base.yaml
# ---------------------------------------------------------------------------


def test_valid_roles_excludes_base_files(rules_dir: Path, monkeypatch):
    """VALID_ROLES must not contain *.base stems."""
    import themis.data as data_mod

    write_yaml(rules_dir, "my-role", {"role": "my-role", "invariants": []})
    write_yaml(rules_dir, "my-role.base", {"role": "my-role.base", "invariants": []})
    new_valid = frozenset(
        p.stem for p in rules_dir.glob("*.yaml") if not p.stem.endswith(".base")
    )
    monkeypatch.setattr(data_mod, "VALID_ROLES", new_valid)
    assert "my-role" in data_mod.VALID_ROLES
    assert "my-role.base" not in data_mod.VALID_ROLES


def test_load_rules_rejects_base_role(rules_dir: Path):
    """load_rules raises FileNotFoundError for a *.base name (fail-closed, T9)."""
    write_yaml(rules_dir, "analyst.base", {"role": "analyst.base", "invariants": []})
    with pytest.raises(FileNotFoundError, match="base file.*not an assignable role"):
        load_rules("analyst.base")


# ---------------------------------------------------------------------------
# Flat load: no extends → same as before
# ---------------------------------------------------------------------------


def test_flat_load_no_extends(rules_dir: Path):
    """load_rules without extends behaves identically to the old flat loader."""
    write_yaml(
        rules_dir,
        "agent",
        {
            "role": "agent",
            "invariants": [
                {
                    "id": "IN-AGENT-1",
                    "name": "NO_GIT_PUSH",
                    "severity": "block",
                    "matchers": [{"tool": "Bash"}],
                    "message": "no push",
                }
            ],
        },
    )
    rs = load_rules("agent")
    assert rs.role == "agent"
    assert len(rs.invariants) == 1
    assert rs.invariants[0].id == "IN-AGENT-1"
    assert rs.invariants[0].source_layer == "agent"


# ---------------------------------------------------------------------------
# Extends-chain resolution: instance → base → universal
# ---------------------------------------------------------------------------


def test_extends_chain_merges_base_invariants(rules_dir: Path):
    """Instance rules + base rules are all present in the merged set."""
    write_yaml(
        rules_dir,
        "backend-lead.base",
        {
            "role": "backend-lead.base",
            "invariants": [
                {
                    "id": "IN-LEAD-BASE-1",
                    "name": "NO_DIRECT_CODING",
                    "severity": "block",
                    "locked": True,
                    "matchers": [{"tool": "Edit"}, {"tool": "Write"}],
                    "message": "no coding",
                }
            ],
        },
    )
    write_yaml(
        rules_dir,
        "backend-lead",
        {
            "role": "backend-lead",
            "extends": "backend-lead.base",
            "invariants": [
                {
                    "id": "IN-BACKEND-LEAD-2",
                    "name": "NO_STANDALONE_AGENTS",
                    "severity": "block",
                    "matchers": [{"tool": "Task"}],
                    "message": "no task",
                }
            ],
        },
    )
    rs = load_rules("backend-lead")
    ids = {inv.id for inv in rs.invariants}
    assert "IN-LEAD-BASE-1" in ids
    assert "IN-BACKEND-LEAD-2" in ids


def test_extends_chain_universal_base_included(rules_dir: Path):
    """universal.base rules propagate into all extending roles."""
    write_yaml(
        rules_dir,
        "universal.base",
        {
            "role": "universal.base",
            "invariants": [
                {
                    "id": "IN-UNIVERSAL-1",
                    "name": "NO_STANDALONE_AGENTS",
                    "severity": "block",
                    "locked": True,
                    "matchers": [{"tool": "Task"}],
                    "message": "no task",
                }
            ],
        },
    )
    write_yaml(
        rules_dir,
        "backend-lead.base",
        {
            "role": "backend-lead.base",
            "extends": "universal.base",
            "invariants": [],
        },
    )
    write_yaml(
        rules_dir,
        "backend-lead",
        {
            "role": "backend-lead",
            "extends": "backend-lead.base",
            "invariants": [],
        },
    )
    rs = load_rules("backend-lead")
    assert any(inv.id == "IN-UNIVERSAL-1" for inv in rs.invariants)


def test_instance_overrides_base_default_rule(rules_dir: Path):
    """Instance may override a base-DEFAULT (locked=False) rule."""
    write_yaml(
        rules_dir,
        "my-role.base",
        {
            "role": "my-role.base",
            "invariants": [
                {
                    "id": "IN-BASE-WARN",
                    "name": "WARN_BASH",
                    "severity": "warn",
                    "locked": False,
                    "matchers": [{"tool": "Bash"}],
                    "message": "warn",
                }
            ],
        },
    )
    write_yaml(
        rules_dir,
        "my-role",
        {
            "role": "my-role",
            "extends": "my-role.base",
            "invariants": [
                {
                    "id": "IN-BASE-WARN",  # same id → override
                    "name": "BLOCK_BASH",
                    "severity": "block",
                    "matchers": [{"tool": "Bash"}],
                    "message": "blocked",
                }
            ],
        },
    )
    rs = load_rules("my-role")
    # Only one invariant with that id
    matching = [inv for inv in rs.invariants if inv.id == "IN-BASE-WARN"]
    assert len(matching) == 1
    assert matching[0].severity == Severity.BLOCK
    assert matching[0].source_layer == "my-role"


# ---------------------------------------------------------------------------
# LOCKED enforcement: override/disable attempts silently ignored (T4/T5)
# ---------------------------------------------------------------------------


def test_locked_rule_cannot_be_overridden_by_instance(rules_dir: Path):
    """T4: instance attempting to override a locked rule is silently ignored."""
    write_yaml(
        rules_dir,
        "lead.base",
        {
            "role": "lead.base",
            "invariants": [
                {
                    "id": "IN-LEAD-1",
                    "name": "NO_DIRECT_CODING",
                    "severity": "block",
                    "locked": True,
                    "matchers": [{"tool": "Edit"}],
                    "message": "no edit",
                }
            ],
        },
    )
    write_yaml(
        rules_dir,
        "my-lead",
        {
            "role": "my-lead",
            "extends": "lead.base",
            "invariants": [
                {
                    "id": "IN-LEAD-1",  # same locked id — attempt override
                    "name": "ALLOW_EDIT",
                    "severity": "warn",  # trying to weaken to warn
                    "matchers": [{"tool": "Edit"}],
                    "message": "allowed",
                }
            ],
        },
    )
    rs = load_rules("my-lead")
    matching = [inv for inv in rs.invariants if inv.id == "IN-LEAD-1"]
    assert len(matching) == 1
    # The locked base version is kept, not the instance's weaker override
    assert matching[0].severity == Severity.BLOCK
    assert matching[0].name == "NO_DIRECT_CODING"
    assert matching[0].locked is True
    assert matching[0].source_layer == "lead.base"


def test_locked_rule_cannot_be_disabled_by_instance(rules_dir: Path, caplog):
    """T5: instance `disable: [locked-id]` is silently ignored + warning logged."""
    import logging

    write_yaml(
        rules_dir,
        "lead.base",
        {
            "role": "lead.base",
            "invariants": [
                {
                    "id": "IN-LEAD-1",
                    "name": "NO_DIRECT_CODING",
                    "severity": "block",
                    "locked": True,
                    "matchers": [{"tool": "Edit"}],
                    "message": "no edit",
                }
            ],
        },
    )
    write_yaml(
        rules_dir,
        "my-lead",
        {
            "role": "my-lead",
            "extends": "lead.base",
            "disable": ["IN-LEAD-1"],  # attempt to disable locked rule
            "invariants": [],
        },
    )
    with caplog.at_level(logging.WARNING):
        rs = load_rules("my-lead")

    # Locked rule must still be present
    assert any(inv.id == "IN-LEAD-1" for inv in rs.invariants)
    # Warning must have been logged
    assert any("disable" in rec.message.lower() or "locked" in rec.message.lower()
               for rec in caplog.records)


def test_default_rule_can_be_disabled_by_instance(rules_dir: Path):
    """Base-DEFAULT (locked=False) rules CAN be disabled by instances."""
    write_yaml(
        rules_dir,
        "role.base",
        {
            "role": "role.base",
            "invariants": [
                {
                    "id": "IN-DEFAULT-1",
                    "name": "WARN_CONTENTION",
                    "severity": "warn",
                    "locked": False,
                    "matchers": [{"tool": "Edit"}],
                    "message": "warn",
                }
            ],
        },
    )
    write_yaml(
        rules_dir,
        "my-role",
        {
            "role": "my-role",
            "extends": "role.base",
            "disable": ["IN-DEFAULT-1"],
            "invariants": [],
        },
    )
    rs = load_rules("my-role")
    assert not any(inv.id == "IN-DEFAULT-1" for inv in rs.invariants)


# ---------------------------------------------------------------------------
# CYCLE detection (T10)
# ---------------------------------------------------------------------------


def test_cycle_in_extends_chain_raises(rules_dir: Path):
    """T10: extends-cycle raises ValueError loudly."""
    write_yaml(
        rules_dir, "a", {"role": "a", "extends": "b", "invariants": []}
    )
    write_yaml(
        rules_dir, "b", {"role": "b", "extends": "a", "invariants": []}
    )
    with pytest.raises(ValueError, match="Cycle"):
        load_rules("a")


# ---------------------------------------------------------------------------
# list_rules returns source-layer annotations (D3)
# ---------------------------------------------------------------------------


def test_list_rules_includes_source_layer(rules_dir: Path):
    """list_rules annotates each rule with its source layer."""
    write_yaml(
        rules_dir,
        "role.base",
        {
            "role": "role.base",
            "invariants": [
                {
                    "id": "IN-BASE-1",
                    "name": "BASE_RULE",
                    "severity": "block",
                    "locked": True,
                    "matchers": [{"tool": "Edit"}],
                    "message": "base",
                }
            ],
        },
    )
    write_yaml(
        rules_dir,
        "my-role",
        {
            "role": "my-role",
            "extends": "role.base",
            "invariants": [
                {
                    "id": "IN-INSTANCE-1",
                    "name": "INSTANCE_RULE",
                    "severity": "warn",
                    "matchers": [{"tool": "Bash"}],
                    "message": "instance",
                }
            ],
        },
    )
    entries = list_rules("my-role")
    source_layers = {e["id"]: e["source_layer"] for e in entries}
    assert source_layers["IN-BASE-1"] == "role.base"
    assert source_layers["IN-INSTANCE-1"] == "my-role"


# ---------------------------------------------------------------------------
# Engine block-semantics: no ALLOW severity → "new-id weakening" closed by construction
# ---------------------------------------------------------------------------


def test_allow_severity_does_not_exist():
    """T6 prerequisite: there is no ALLOW severity in the engine.

    The 'add a new-id ALLOW to weaken a locked BLOCK' attack is closed by
    construction — Severity('allow') raises ValueError, so no instance can
    parse a permissive rule. This pins the closed-by-construction invariant.
    """
    with pytest.raises(ValueError):
        Severity("allow")


# ---------------------------------------------------------------------------
# Migration-equivalence snapshot (T8): flat load == merged load for roles with no extends
# ---------------------------------------------------------------------------


def test_migration_equivalence_flat_roles(rules_dir: Path, monkeypatch):
    """T8: for roles without extends, load_rules == load_rules_flat (zero behavior change)."""
    import themis.data as data_mod

    # Create a flat role (no extends) with two invariants
    write_yaml(
        rules_dir,
        "analyst",
        {
            "role": "analyst",
            "invariants": [
                {
                    "id": "IN-ANALYST-1",
                    "name": "NO_FILE_EDIT",
                    "severity": "block",
                    "matchers": [{"tool": "Edit"}],
                    "message": "no edit",
                },
                {
                    "id": "IN-ANALYST-2",
                    "name": "NO_BASH_MUTATING",
                    "severity": "block",
                    "matchers": [{"tool": "Bash"}],
                    "message": "no mutate",
                },
            ],
        },
    )
    monkeypatch.setattr(
        data_mod, "VALID_ROLES", frozenset(["analyst"])
    )

    rs_new = load_rules("analyst")
    rs_flat = load_rules_flat("analyst")

    new_ids = {inv.id for inv in rs_new.invariants}
    flat_ids = {inv.id for inv in rs_flat.invariants}
    assert new_ids == flat_ids, "Merged load must match flat load for roles without extends"

    new_by_id = {inv.id: inv for inv in rs_new.invariants}
    flat_by_id = {inv.id: inv for inv in rs_flat.invariants}
    for inv_id in flat_ids:
        assert new_by_id[inv_id].severity == flat_by_id[inv_id].severity
        assert new_by_id[inv_id].name == flat_by_id[inv_id].name


# ---------------------------------------------------------------------------
# T8 REAL-RULES migration-equivalence: exact per-role delta
# (uses actual rules dir after the restructure — not the temp fixture)
# ---------------------------------------------------------------------------


def test_t8_non_lead_roles_equal_pre_post_migration():
    """T8 (real rules): each non-lead role's OWN (per-file) invariants are
    unchanged by the extends migration.

    As of 2026-06-16 every role (lead or not) additionally inherits
    universal.base — the loader auto-prepends it (previously dormant). So the
    merged load == the role's flat per-file invariants PLUS the universal
    invariants, with each role's own invariants unchanged (severity included).
    A role MAY override a universal id (master downgrades IN-UNIVERSAL-1 to warn)
    — that override lives in the role's own file, so it's covered by the
    own-invariants check. Catches accidental per-role drift while acknowledging
    the universal layer.
    """
    NON_LEAD_ROLES = [
        "agent", "analyst", "architect", "critic", "intake",
        "master", "member", "observer", "tracker", "verifier",
    ]
    universal_ids = {inv.id for inv in load_rules_flat("universal.base").invariants}
    for role in NON_LEAD_ROLES:
        rs_new = load_rules(role)
        rs_flat = load_rules_flat(role)  # role's own file only, no universal
        new_ids = {inv.id for inv in rs_new.invariants}
        flat_ids = {inv.id for inv in rs_flat.invariants}
        # merged set == role's own invariants ∪ the universal invariants
        assert new_ids == flat_ids | universal_ids, (
            f"Non-lead role {role!r}: merged ids {new_ids} != own {flat_ids} "
            f"∪ universal {universal_ids}"
        )
        new_by_id = {inv.id: inv for inv in rs_new.invariants}
        flat_by_id = {inv.id: inv for inv in rs_flat.invariants}
        # each role's OWN invariants (incl. any override of a universal id) are
        # byte-for-byte severity-stable; universal-only ids aren't in flat_ids.
        for inv_id in flat_ids:
            assert new_by_id[inv_id].severity == flat_by_id[inv_id].severity, (
                f"Role {role!r}, rule {inv_id!r}: severity changed"
            )


def test_t8_lead_roles_gain_base_locked_rules():
    """T8 (real rules): the 6 lead roles GAIN NO_DIRECT_CODING + NO_GIT_COMMIT (locked).

    Per-role exact delta (analyst sharpening): assert each lead's merged set contains
    both locked base rules. This is the INTENDED change — the Joseph fix.
    """
    LEAD_ROLES = [
        "backend-lead", "jp-backend-lead",
        "data-lead", "jp-data-lead",
        "frontend-lead", "jp-frontend-lead",
    ]
    for role in LEAD_ROLES:
        rs = load_rules(role)
        ids = {inv.id: inv for inv in rs.invariants}

        # Each lead must gain a locked NO_DIRECT_CODING rule from the base
        no_direct = [inv for inv in rs.invariants if inv.name == "NO_DIRECT_CODING"]
        assert no_direct, f"Lead role {role!r} missing NO_DIRECT_CODING rule"
        assert any(inv.locked for inv in no_direct), (
            f"Lead role {role!r}: NO_DIRECT_CODING must be locked=True"
        )
        assert no_direct[0].severity == Severity.BLOCK, (
            f"Lead role {role!r}: NO_DIRECT_CODING must be severity=block"
        )

        # Each lead must gain a locked NO_GIT_COMMIT rule from the base
        no_commit = [inv for inv in rs.invariants if inv.name == "NO_GIT_COMMIT"]
        assert no_commit, f"Lead role {role!r} missing NO_GIT_COMMIT rule"
        assert any(inv.locked for inv in no_commit), (
            f"Lead role {role!r}: NO_GIT_COMMIT must be locked=True"
        )


def test_t8_no_incidental_drift_on_non_leads():
    """T8 (real rules): no non-lead role accidentally gained lead-base rules."""
    NON_LEAD_ROLES = [
        "agent", "analyst", "architect", "critic", "intake",
        "master", "member", "observer", "tracker", "verifier",
    ]
    for role in NON_LEAD_ROLES:
        rs = load_rules(role)
        no_direct = [inv for inv in rs.invariants if inv.name == "NO_DIRECT_CODING"]
        assert not no_direct, (
            f"Non-lead role {role!r} accidentally has NO_DIRECT_CODING — "
            "migration incidental drift"
        )
