"""Tests for leads sync — drift detection + manual-block preservation.

test_khimaira_leads_manifest_matches_generated:
    xfail until the central manifest ~/.local/share/khimaira/leads/khimaira.toml exists.
    Activates (runs as real test) once Phase C writes that file.

test_manual_block_preservation:
    Generate a role doc, inject a manual block, regenerate, assert block survives.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from khimaira.leads.manifest import load_manifest
from khimaira.leads.sync import check_drift, sync_leads


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def central_leads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect central leads dir to tmp_path for test isolation."""
    leads_dir = tmp_path / "xdg" / "khimaira" / "leads"
    leads_dir.mkdir(parents=True)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    return leads_dir


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_manifest(tmp_path: Path, central_leads: Path) -> str:
    """Write a minimal valid central manifest + project directory structure."""
    (central_leads / "testproject.toml").write_text(
        f"""
[project]
name = "testproject"
root_path = "{tmp_path}"
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
    return "testproject"


# ---------------------------------------------------------------------------
# Phase C integration — khimaira's own central manifest
# ---------------------------------------------------------------------------


def _khimaira_central_manifest_path() -> Path:
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(xdg) / "khimaira" / "leads" / "khimaira.toml"


@pytest.mark.xfail(
    not _khimaira_central_manifest_path().exists(),
    reason="Phase C: central manifest ~/.local/share/khimaira/leads/khimaira.toml does not exist yet",
    strict=False,
)
def test_khimaira_leads_manifest_matches_generated():
    """Run leads sync --check against khimaira's own central manifest.

    Passes once Phase C writes the central manifest and the generated files
    match the committed role docs + Themis YAML (fidelity proof).
    """
    has_drift, diff_lines = check_drift("khimaira")
    if has_drift:
        pytest.fail("Drift detected:\n" + "".join(diff_lines))


# ---------------------------------------------------------------------------
# Manual-block preservation
# ---------------------------------------------------------------------------


def test_manual_block_preserved_across_regen(tmp_path: Path, central_leads: Path):
    """Generate a role doc, inject a manual block, regenerate, assert block survives."""
    project_name = _minimal_manifest(tmp_path, central_leads)

    # First sync: generates role doc with default <!-- BEGIN MANUAL --> blocks
    sync_leads(project_name)
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
    sync_leads(project_name)
    second_content = role_path.read_text()

    assert "- Custom authority pattern here" in second_content, (
        "Manual block content was NOT preserved across regeneration"
    )


def test_sync_creates_role_doc_and_themis(tmp_path: Path, central_leads: Path):
    project_name = _minimal_manifest(tmp_path, central_leads)
    summary = sync_leads(project_name)

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


def test_sync_seeds_knowledge_doc(tmp_path: Path, central_leads: Path):
    project_name = _minimal_manifest(tmp_path, central_leads)
    sync_leads(project_name)

    knowledge_path = tmp_path / "docs" / "domain" / "backend-knowledge.md"
    assert knowledge_path.exists(), "Knowledge doc was not seeded"


def test_sync_does_not_clobber_existing_knowledge(tmp_path: Path, central_leads: Path):
    project_name = _minimal_manifest(tmp_path, central_leads)
    knowledge_path = tmp_path / "docs" / "domain" / "backend-knowledge.md"
    knowledge_path.write_text("existing content")

    sync_leads(project_name)

    assert knowledge_path.read_text() == "existing content", (
        "sync clobbered existing knowledge doc"
    )


def test_check_drift_detects_missing_file(tmp_path: Path, central_leads: Path):
    project_name = _minimal_manifest(tmp_path, central_leads)
    # Don't sync first — files are missing
    has_drift, diff_lines = check_drift(project_name)
    assert has_drift
    assert any("MISSING" in line for line in diff_lines)


def test_check_drift_clean_after_sync(tmp_path: Path, central_leads: Path):
    project_name = _minimal_manifest(tmp_path, central_leads)
    sync_leads(project_name)

    has_drift, diff_lines = check_drift(project_name)
    assert not has_drift, f"Unexpected drift after clean sync:\n{''.join(diff_lines)}"


def test_themis_yaml_has_allow_regex_with_dir_prefix(tmp_path: Path, central_leads: Path):
    """Themis YAML must use dir/ prefix for dir/** paths (not dir/**  literally)."""
    project_name = _minimal_manifest(tmp_path, central_leads)
    sync_leads(project_name)

    themis_content = (tmp_path / "themis" / "rules" / "backend-lead.yaml").read_text()
    # The glob "packages/backend/**" must become "packages/backend/" in the regex
    assert "packages/backend/" in themis_content
    assert "packages/backend/**" not in themis_content


def test_role_doc_contains_paths_list(tmp_path: Path, central_leads: Path):
    """Role doc must list the manifest paths in ## Domain scope."""
    project_name = _minimal_manifest(tmp_path, central_leads)
    sync_leads(project_name)

    role_content = (tmp_path / "roles" / "backend-lead.md").read_text()
    assert "packages/backend/**" in role_content


def test_prefix_produces_prefixed_lead_name(tmp_path: Path, central_leads: Path):
    """With prefix='jp', domain='backend' → jp-backend-lead files."""
    (central_leads / "jeevy.toml").write_text(
        f"""
[project]
name = "jeevy"
prefix = "jp"
root_path = "{tmp_path}"
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

    sync_leads("jeevy")

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


def test_no_prefix_produces_unprefixed_lead_name(tmp_path: Path, central_leads: Path):
    """Without prefix, domain='backend' → backend-lead (khimaira convention)."""
    project_name = _minimal_manifest(tmp_path, central_leads)
    sync_leads(project_name)

    assert (tmp_path / "roles" / "backend-lead.md").exists()
    assert (tmp_path / "themis" / "rules" / "backend-lead.yaml").exists()

    themis_content = (tmp_path / "themis" / "rules" / "backend-lead.yaml").read_text()
    assert "IN-BACKEND-LEAD-1" in themis_content
    assert "IN-JP-BACKEND-LEAD-1" not in themis_content


# ---------------------------------------------------------------------------
# propose_only — per-project write-block gate
# ---------------------------------------------------------------------------


def _propose_only_manifest(tmp_path: Path, central_leads: Path, propose_only: bool) -> str:
    """Write a manifest with configurable propose_only flag."""
    flag = "true" if propose_only else "false"
    (central_leads / "testproject.toml").write_text(
        f"""
[project]
name = "testproject"
prefix = "jp"
root_path = "{tmp_path}"
roles_dir = "roles"
themis_dir = "themis/rules"
knowledge_dir = "docs/domain"
propose_only = {flag}

[leads.backend]
paths = ["packages/backend/**"]
model = "sonnet"
effort = "medium"
"""
    )
    (tmp_path / "roles").mkdir(exist_ok=True)
    (tmp_path / "themis" / "rules").mkdir(parents=True, exist_ok=True)
    (tmp_path / "docs" / "domain").mkdir(parents=True, exist_ok=True)
    return "testproject"


def test_propose_only_true_emits_block_rule(tmp_path: Path, central_leads: Path):
    """propose_only=true → Themis YAML contains NO_FILE_EDIT_PROPOSE_ONLY block rule."""
    project_name = _propose_only_manifest(tmp_path, central_leads, propose_only=True)
    sync_leads(project_name)

    themis_content = (tmp_path / "themis" / "rules" / "jp-backend-lead.yaml").read_text()
    assert "NO_FILE_EDIT_PROPOSE_ONLY" in themis_content, (
        "propose_only=true must emit NO_FILE_EDIT_PROPOSE_ONLY in Themis YAML"
    )
    assert "IN-JP-BACKEND-LEAD-1-PO" in themis_content, (
        "propose_only rule id must follow <rule_id>-PO convention"
    )
    assert "severity: block" in themis_content, "propose_only rule must be severity: block"
    # All 4 write tools must be in matchers (unconditional)
    for tool in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        assert f"tool: {tool}" in themis_content, (
            f"propose_only rule missing matcher for tool: {tool}"
        )


def test_propose_only_false_omits_block_rule(tmp_path: Path, central_leads: Path):
    """propose_only=false → NO_FILE_EDIT_PROPOSE_ONLY rule MUST NOT appear.

    Critical assertion: per-project isolation. A false/absent flag must leave
    other projects' leads completely unchanged.
    """
    project_name = _propose_only_manifest(tmp_path, central_leads, propose_only=False)
    sync_leads(project_name)

    themis_content = (tmp_path / "themis" / "rules" / "jp-backend-lead.yaml").read_text()
    assert "NO_FILE_EDIT_PROPOSE_ONLY" not in themis_content, (
        "propose_only=false must NOT emit NO_FILE_EDIT_PROPOSE_ONLY rule"
    )
    assert "-PO" not in themis_content, (
        "propose_only=false must NOT emit any -PO rule id"
    )


def test_propose_only_true_emits_role_doc_section(tmp_path: Path, central_leads: Path):
    """propose_only=true → role doc contains PROPOSE-ONLY section under Constraints."""
    project_name = _propose_only_manifest(tmp_path, central_leads, propose_only=True)
    sync_leads(project_name)

    role_content = (tmp_path / "roles" / "jp-backend-lead.md").read_text()
    assert "PROPOSE-ONLY" in role_content, (
        "propose_only=true must emit PROPOSE-ONLY section in role doc"
    )
    assert "NO_FILE_EDIT_PROPOSE_ONLY" in role_content, (
        "role doc must reference the Themis rule by name"
    )
    assert "OVERRIDES" in role_content, (
        "propose_only section must state it overrides the small-plans clause"
    )


def test_propose_only_false_omits_role_doc_section(tmp_path: Path, central_leads: Path):
    """propose_only=false → PROPOSE-ONLY section MUST NOT appear in role doc.

    Per-project isolation: khimaira leads must be unaffected by jeevy's flag.
    """
    project_name = _propose_only_manifest(tmp_path, central_leads, propose_only=False)
    sync_leads(project_name)

    role_content = (tmp_path / "roles" / "jp-backend-lead.md").read_text()
    assert "PROPOSE-ONLY" not in role_content, (
        "propose_only=false must NOT emit PROPOSE-ONLY section"
    )


def test_propose_only_true_no_drift_after_sync(tmp_path: Path, central_leads: Path):
    """sync then check_drift on propose_only=true project → clean (no drift)."""
    project_name = _propose_only_manifest(tmp_path, central_leads, propose_only=True)
    sync_leads(project_name)

    has_drift, diff_lines = check_drift(project_name)
    assert not has_drift, f"Unexpected drift after propose_only=true sync:\n{''.join(diff_lines)}"


def test_propose_only_false_no_drift_after_sync(tmp_path: Path, central_leads: Path):
    """sync then check_drift on propose_only=false project → clean (no drift)."""
    project_name = _propose_only_manifest(tmp_path, central_leads, propose_only=False)
    sync_leads(project_name)

    has_drift, diff_lines = check_drift(project_name)
    assert not has_drift, f"Unexpected drift after propose_only=false sync:\n{''.join(diff_lines)}"


def test_absolute_output_dirs_generate_into_separate_repo(
    tmp_path: Path, central_leads: Path
):
    """Cross-repo case: root_path=X, roles_dir/themis_dir/knowledge_dir are absolute
    paths outside X.  Generated files must land in the absolute dirs — NOT in X.

    This is the jeevy pattern: jeevy_portal is the project root, but lead docs,
    Themis YAMLs, and knowledge seeds land in the khimaira repo.
    """
    project_root = tmp_path / "project_root"
    output_root = tmp_path / "output_repo"
    for d in [
        project_root,
        output_root / "roles",
        output_root / "themis" / "rules",
        output_root / "docs" / "domain",
    ]:
        d.mkdir(parents=True, exist_ok=True)

    (central_leads / "cross.toml").write_text(
        f"""
[project]
name = "cross"
prefix = "xp"
root_path = "{project_root}"
roles_dir = "{output_root / "roles"}"
themis_dir = "{output_root / "themis" / "rules"}"
knowledge_dir = "{output_root / "docs" / "domain"}"

[leads.backend]
paths = ["src/backend/**"]
model = "sonnet"
effort = "medium"
"""
    )

    sync_leads("cross")

    # Generated files must land in output_repo, NOT in project_root
    role_path = output_root / "roles" / "xp-backend-lead.md"
    themis_path = output_root / "themis" / "rules" / "xp-backend-lead.yaml"
    knowledge_path = output_root / "docs" / "domain" / "backend-knowledge.md"

    assert role_path.exists(), "Role doc not generated into absolute output dir"
    assert themis_path.exists(), "Themis YAML not generated into absolute output dir"
    assert knowledge_path.exists(), "Knowledge seed not created in absolute output dir"

    # Nothing should be written under project_root
    project_files = list(project_root.rglob("*"))
    assert project_files == [], f"Files unexpectedly written to project_root: {project_files}"

    # Themis allow-regex must include the domain paths (project-root-relative)
    themis_content = themis_path.read_text()
    assert "src/backend/" in themis_content

    # Role doc knowledge_doc_path should be the absolute path (cross-repo fallback)
    role_content = role_path.read_text()
    assert str(output_root / "docs" / "domain" / "backend-knowledge.md") in role_content
