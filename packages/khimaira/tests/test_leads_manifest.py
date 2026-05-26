"""Tests for khimaira.leads.manifest — parse + validate central leads.toml."""

from __future__ import annotations

from pathlib import Path

import pytest

from khimaira.leads.manifest import load_manifest


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


def _write_toml(leads_dir: Path, name: str, content: str) -> None:
    (leads_dir / f"{name}.toml").write_text(content)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_valid_manifest(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "testproject",
        f"""
[project]
name = "testproject"
root_path = "{tmp_path}"
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
    manifest = load_manifest("testproject")

    assert manifest.project_name == "testproject"
    assert manifest.prefix == ""
    assert manifest.root_path == tmp_path
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
    assert data.model == "sonnet"
    assert data.effort == "medium"


def test_missing_manifest(central_leads: Path):
    with pytest.raises(FileNotFoundError, match="No central manifest for 'nonexistent'"):
        load_manifest("nonexistent")


def test_missing_project_name(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "testproject",
        f"""
[project]
root_path = "{tmp_path}"
roles_dir = "roles"

[leads.backend]
paths = ["packages/backend/**"]
""",
    )
    with pytest.raises(ValueError, match=r"\[project\]\.name is required"):
        load_manifest("testproject")


def test_missing_root_path(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "testproject",
        """
[project]
name = "testproject"

[leads.backend]
paths = ["packages/backend/**"]
""",
    )
    with pytest.raises(ValueError, match=r"\[project\]\.root_path is required"):
        load_manifest("testproject")


def test_relative_root_path_rejected(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "testproject",
        """
[project]
name = "testproject"
root_path = "relative/path"

[leads.backend]
paths = ["packages/backend/**"]
""",
    )
    with pytest.raises(ValueError, match="must be absolute"):
        load_manifest("testproject")


def test_empty_leads_section(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "testproject",
        f"""
[project]
name = "testproject"
root_path = "{tmp_path}"
""",
    )
    with pytest.raises(ValueError, match="at least one"):
        load_manifest("testproject")


def test_missing_paths_for_lead(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "testproject",
        f"""
[project]
name = "testproject"
root_path = "{tmp_path}"

[leads.backend]
model = "sonnet"
""",
    )
    with pytest.raises(ValueError, match=r"\[leads\.backend\]\.paths is required"):
        load_manifest("testproject")


def test_default_dirs_when_not_specified(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "myproject",
        f"""
[project]
name = "myproject"
root_path = "{tmp_path}"

[leads.frontend]
paths = ["src/frontend/**"]
""",
    )
    manifest = load_manifest("myproject")
    assert manifest.roles_dir == tmp_path / "roles"
    assert manifest.themis_dir == tmp_path / "themis/rules"
    assert manifest.knowledge_dir == tmp_path / "docs/domain"


def test_prefix_field_parsed(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "jeevy",
        f"""
[project]
name = "jeevy"
prefix = "jp"
root_path = "{tmp_path}"

[leads.backend]
paths = ["apps/jeevy/src/**"]
""",
    )
    manifest = load_manifest("jeevy")
    assert manifest.prefix == "jp"


def test_prefix_empty_by_default(tmp_path: Path, central_leads: Path):
    _write_toml(
        central_leads,
        "khimaira",
        f"""
[project]
name = "khimaira"
root_path = "{tmp_path}"

[leads.backend]
paths = ["packages/khimaira/**"]
""",
    )
    manifest = load_manifest("khimaira")
    assert manifest.prefix == ""


def test_root_path_resolves_dirs(tmp_path: Path, central_leads: Path):
    """roles_dir + themis_dir + knowledge_dir are joined with root_path."""
    _write_toml(
        central_leads,
        "testproject",
        f"""
[project]
name = "testproject"
root_path = "{tmp_path}"
roles_dir = "custom/roles"
themis_dir = "custom/themis"
knowledge_dir = "custom/knowledge"

[leads.backend]
paths = ["packages/**"]
""",
    )
    manifest = load_manifest("testproject")
    assert manifest.roles_dir == tmp_path / "custom/roles"
    assert manifest.themis_dir == tmp_path / "custom/themis"
    assert manifest.knowledge_dir == tmp_path / "custom/knowledge"
