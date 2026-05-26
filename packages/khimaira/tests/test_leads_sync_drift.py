"""Tests for leads sync — drift detection + manual-block preservation.

test_khimaira_leads_manifest_matches_generated:
    Skipped until Phase C creates .khimaira/leads.toml for the khimaira project.

test_manual_block_preservation:
    Generate a role doc, inject a manual block, regenerate, assert block survives.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from khimaira.leads.manifest import load_manifest
from khimaira.leads.sync import check_drift, sync_leads


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_manifest(tmp_path: Path) -> Path:
    """Write a minimal valid manifest + directory structure."""
    (tmp_path / ".khimaira").mkdir(exist_ok=True)
    (tmp_path / ".khimaira" / "leads.toml").write_text(
        """
[project]
name = "testproject"
roles_dir = "roles"
themis_dir = "themis/rules"
knowledge_dir = "docs/domain"

[leads.backend]
paths = ["packages/backend/**"]
model = "sonnet"
effort = "medium"
"""
    )
    (tmp_path / "roles").mkdir(exist_ok=True)
    (tmp_path / "themis" / "rules").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "domain").mkdir(parents=True, exist_ok=True)
    return tmp_path


# ---------------------------------------------------------------------------
# Phase C placeholder (xfail until leads.toml exists)
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    not (Path(__file__).parents[5] / ".khimaira" / "leads.toml").exists(),
    reason="Phase C: .khimaira/leads.toml does not exist yet for khimaira project",
    strict=False,
)
def test_khimaira_leads_manifest_matches_generated():
    """Run leads sync --check against khimaira's own manifest.

    Passes once Phase C creates .khimaira/leads.toml and the generated files
    match the committed role docs + Themis YAML (fidelity proof).
    """
    project_root = Path(__file__).parents[5]  # packages/khimaira/src/khimaira → repo root × 5
    has_drift, diff_lines = check_drift(project_root)
    if has_drift:
        pytest.fail("Drift detected:\n" + "".join(diff_lines))


# ---------------------------------------------------------------------------
# Manual-block preservation
# ---------------------------------------------------------------------------


def test_manual_block_preserved_across_regen(tmp_path):
    """Generate a role doc, inject a manual block, regenerate, assert block survives."""
    _minimal_manifest(tmp_path)

    # First sync: generates role doc with default <!-- BEGIN MANUAL --> blocks
    sync_leads(tmp_path)
    role_path = tmp_path / "roles" / "backend-lead.md"
    assert role_path.exists()
    first_content = role_path.read_text()

    # Verify the template's manual blocks are present
    assert "<!-- BEGIN MANUAL -->" in first_content
    assert "<!-- END MANUAL -->" in first_content

    # Inject custom content into the first manual block
    custom_content = "<!-- BEGIN MANUAL -->\n- Custom authority pattern here\n<!-- END MANUAL -->"
    modified = first_content.replace(
        first_content[
            first_content.index("<!-- BEGIN MANUAL -->") : first_content.index("<!-- END MANUAL -->")
            + len("<!-- END MANUAL -->")
        ],
        custom_content,
        1,
    )
    role_path.write_text(modified)

    # Second sync: should preserve the custom manual block
    sync_leads(tmp_path)
    second_content = role_path.read_text()

    assert "- Custom authority pattern here" in second_content, (
        "Manual block content was NOT preserved across regeneration"
    )


def test_sync_creates_role_doc_and_themis(tmp_path):
    _minimal_manifest(tmp_path)
    summary = sync_leads(tmp_path)

    role_path = tmp_path / "roles" / "backend-lead.md"
    themis_path = tmp_path / "themis" / "rules" / "backend-lead.yaml"

    assert role_path.exists(), "Role doc not created"
    assert themis_path.exists(), "Themis YAML not created"

    role_content = role_path.read_text()
    assert "backend" in role_content
    assert "testproject" in role_content

    themis_content = themis_path.read_text()
    assert "backend-lead" in themis_content
    assert "IN-BACKEND-LEAD-1" in themis_content
    assert "IN-BACKEND-LEAD-2" in themis_content
    assert "packages/backend/" in themis_content  # allow-list fragment


def test_sync_seeds_knowledge_doc(tmp_path):
    _minimal_manifest(tmp_path)
    sync_leads(tmp_path)

    knowledge_path = tmp_path / "docs" / "domain" / "backend-knowledge.md"
    assert knowledge_path.exists(), "Knowledge doc was not seeded"


def test_sync_does_not_clobber_existing_knowledge(tmp_path):
    _minimal_manifest(tmp_path)
    knowledge_path = tmp_path / "docs" / "domain" / "backend-knowledge.md"
    knowledge_path.write_text("existing content")

    sync_leads(tmp_path)

    assert knowledge_path.read_text() == "existing content", (
        "sync clobbered existing knowledge doc"
    )


def test_check_drift_detects_missing_file(tmp_path):
    _minimal_manifest(tmp_path)
    # Don't sync first — files are missing
    has_drift, diff_lines = check_drift(tmp_path)
    assert has_drift
    assert any("MISSING" in line for line in diff_lines)


def test_check_drift_clean_after_sync(tmp_path):
    _minimal_manifest(tmp_path)
    sync_leads(tmp_path)

    has_drift, diff_lines = check_drift(tmp_path)
    assert not has_drift, f"Unexpected drift after clean sync:\n{''.join(diff_lines)}"


def test_themis_yaml_has_allow_regex_with_dir_prefix(tmp_path):
    """Themis YAML must use dir/ prefix for dir/** paths (not dir/**  literally)."""
    _minimal_manifest(tmp_path)
    sync_leads(tmp_path)

    themis_content = (tmp_path / "themis" / "rules" / "backend-lead.yaml").read_text()
    # The glob "packages/backend/**" must become "packages/backend/" in the regex
    assert "packages/backend/" in themis_content
    assert "packages/backend/**" not in themis_content


def test_role_doc_contains_paths_list(tmp_path):
    """Role doc must list the manifest paths in ## Domain scope."""
    _minimal_manifest(tmp_path)
    sync_leads(tmp_path)

    role_content = (tmp_path / "roles" / "backend-lead.md").read_text()
    assert "packages/backend/**" in role_content


def test_prefix_produces_prefixed_lead_name(tmp_path):
    """With prefix='jp', domain='backend' → jp-backend-lead files."""
    (tmp_path / ".khimaira").mkdir()
    (tmp_path / ".khimaira" / "leads.toml").write_text(
        """
[project]
name = "jeevy"
prefix = "jp"
roles_dir = "roles"
themis_dir = "themis/rules"
knowledge_dir = "docs/domain"

[leads.backend]
paths = ["apps/jeevy/src/**"]
"""
    )
    (tmp_path / "roles").mkdir()
    (tmp_path / "themis" / "rules").mkdir(parents=True)
    (tmp_path / "docs" / "domain").mkdir(parents=True)

    sync_leads(tmp_path)

    # Role doc filename must include jp- prefix
    assert (tmp_path / "roles" / "jp-backend-lead.md").exists()
    # Themis YAML filename must include jp- prefix
    assert (tmp_path / "themis" / "rules" / "jp-backend-lead.yaml").exists()
    # Knowledge doc uses BARE domain (no prefix)
    assert (tmp_path / "docs" / "domain" / "backend-knowledge.md").exists()
    assert not (tmp_path / "docs" / "domain" / "jp-backend-knowledge.md").exists()

    # Themis rule IDs must be IN-JP-BACKEND-LEAD-1/2
    themis_content = (tmp_path / "themis" / "rules" / "jp-backend-lead.yaml").read_text()
    assert "IN-JP-BACKEND-LEAD-1" in themis_content
    assert "IN-JP-BACKEND-LEAD-2" in themis_content


def test_no_prefix_produces_unprefixed_lead_name(tmp_path):
    """Without prefix, domain='backend' → backend-lead (khimaira convention)."""
    _minimal_manifest(tmp_path)
    sync_leads(tmp_path)

    assert (tmp_path / "roles" / "backend-lead.md").exists()
    assert (tmp_path / "themis" / "rules" / "backend-lead.yaml").exists()

    themis_content = (tmp_path / "themis" / "rules" / "backend-lead.yaml").read_text()
    assert "IN-BACKEND-LEAD-1" in themis_content
    assert "IN-JP-BACKEND-LEAD-1" not in themis_content
