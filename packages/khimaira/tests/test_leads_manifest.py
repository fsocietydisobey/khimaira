"""Tests for khimaira.leads.manifest — parse + validate leads.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from khimaira.leads.manifest import load_manifest


def _write_toml(tmp_path: Path, content: str) -> Path:
    (tmp_path / ".khimaira").mkdir(exist_ok=True)
    (tmp_path / ".khimaira" / "leads.toml").write_text(content)
    return tmp_path


def test_valid_manifest(tmp_path):
    _write_toml(
        tmp_path,
        """
[project]
name = "testproject"
roles_dir = "roles"
themis_dir = "themis/rules"
knowledge_dir = "docs/domain"

[leads.backend]
paths = ["packages/backend/**", "packages/api/**"]
model = "sonnet"
effort = "medium"

[leads.data]
paths = ["packages/data/sessions.py"]
""",
    )
    manifest = load_manifest(tmp_path)

    assert manifest.project_name == "testproject"
    assert manifest.prefix == ""
    assert manifest.roles_dir == tmp_path / "roles"
    assert manifest.themis_dir == tmp_path / "themis/rules"
    assert manifest.knowledge_dir == tmp_path / "docs/domain"
    assert set(manifest.leads.keys()) == {"backend", "data"}

    backend = manifest.leads["backend"]
    assert backend.name == "backend"
    assert backend.paths == ["packages/backend/**", "packages/api/**"]
    assert backend.model == "sonnet"
    assert backend.effort == "medium"

    data = manifest.leads["data"]
    assert data.paths == ["packages/data/sessions.py"]
    # Defaults applied
    assert data.model == "sonnet"
    assert data.effort == "medium"


def test_missing_toml(tmp_path):
    with pytest.raises(ValueError, match="No leads.toml found"):
        load_manifest(tmp_path)


def test_missing_project_name(tmp_path):
    _write_toml(
        tmp_path,
        """
[project]
roles_dir = "roles"

[leads.backend]
paths = ["packages/backend/**"]
""",
    )
    with pytest.raises(ValueError, match=r"\[project\]\.name is required"):
        load_manifest(tmp_path)


def test_empty_leads_section(tmp_path):
    _write_toml(
        tmp_path,
        """
[project]
name = "testproject"
""",
    )
    with pytest.raises(ValueError, match="at least one"):
        load_manifest(tmp_path)


def test_missing_paths_for_lead(tmp_path):
    _write_toml(
        tmp_path,
        """
[project]
name = "testproject"

[leads.backend]
model = "sonnet"
""",
    )
    with pytest.raises(ValueError, match=r"\[leads\.backend\]\.paths is required"):
        load_manifest(tmp_path)


def test_default_dirs_when_not_specified(tmp_path):
    _write_toml(
        tmp_path,
        """
[project]
name = "myproject"

[leads.frontend]
paths = ["src/frontend/**"]
""",
    )
    manifest = load_manifest(tmp_path)
    # Defaults are relative to project_root
    assert manifest.roles_dir == tmp_path / "roles"
    assert manifest.themis_dir == tmp_path / "themis/rules"
    assert manifest.knowledge_dir == tmp_path / "docs/domain"


def test_prefix_field_parsed(tmp_path):
    _write_toml(
        tmp_path,
        """
[project]
name = "jeevy"
prefix = "jp"

[leads.backend]
paths = ["apps/jeevy/src/**"]
""",
    )
    manifest = load_manifest(tmp_path)
    assert manifest.prefix == "jp"


def test_prefix_empty_by_default(tmp_path):
    _write_toml(
        tmp_path,
        """
[project]
name = "khimaira"

[leads.backend]
paths = ["packages/khimaira/**"]
""",
    )
    manifest = load_manifest(tmp_path)
    assert manifest.prefix == ""
