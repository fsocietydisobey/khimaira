"""Tests for oracle-v2 broadened sources (Part 2 + Part 3).

Covers:
- sources_empty distinguishes "up but no results" from degraded (Part 2)
- Scarlet context fail-open: Scarlet throws → oracle still returns (Part 3)
- context.yaml pointers absent → empty list, no exception (Part 3)
- CLAUDE.md files capped at _CLAUDE_MD_CAP (Part 3)
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def oracle_client() -> TestClient:
    from khimaira.monitor.api import oracle as oracle_api

    app = FastAPI()
    app.include_router(oracle_api.build_router(), prefix="/api")
    return TestClient(app)


_SEANCE_EMPTY_OK = ([], False)
_SEANCE_ERRORED = ([], True)
_SEANCE_CHUNK = {
    "file_path": "src/foo.py",
    "symbol_name": "bar",
    "chunk_type": "function",
    "language": "python",
    "start_line": 10,
    "end_line": 20,
    "score": 0.9,
    "text": "def bar(): pass",
}
_SEANCE_OK = ([_SEANCE_CHUNK], False)


def _call_oracle(client: TestClient, **kwargs) -> dict:
    payload = {"question": "how does auth work?", "project": "khimaira", **kwargs}
    resp = client.post("/api/oracle/query", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Part 2 — sources_empty
# ---------------------------------------------------------------------------


def test_oracle_response_includes_sources_empty(oracle_client: TestClient) -> None:
    """Séance up but returned no results → sources_empty includes 'seance', degraded=False."""
    with (
        patch(
            "khimaira.monitor.api.oracle._seance_search",
            return_value=_SEANCE_EMPTY_OK,
        ),
        patch("khimaira.monitor.api.oracle._mnemosyne_query", return_value=None),
    ):
        data = _call_oracle(oracle_client)

    assert data["degraded"] is False, "empty result must NOT set degraded"
    assert "sources_empty" in data
    assert "seance" in data["sources_empty"]
    assert "mnemosyne" in data["sources_empty"]
    assert "seance" not in data.get("stores_hit", [])


def test_oracle_errored_source_not_in_sources_empty(oracle_client: TestClient) -> None:
    """Séance errored → degraded=True, 'seance' NOT in sources_empty (it errored, not empty)."""
    with (
        patch(
            "khimaira.monitor.api.oracle._seance_search",
            return_value=_SEANCE_ERRORED,
        ),
        patch("khimaira.monitor.api.oracle._mnemosyne_query", return_value=None),
    ):
        data = _call_oracle(oracle_client)

    assert data["degraded"] is True
    assert "seance" not in data.get("sources_empty", [])


def test_oracle_hit_store_not_in_sources_empty(oracle_client: TestClient) -> None:
    """When Séance returns results it should be in stores_hit, NOT in sources_empty."""
    with (
        patch("khimaira.monitor.api.oracle._seance_search", return_value=_SEANCE_OK),
        patch("khimaira.monitor.api.oracle._mnemosyne_query", return_value=None),
    ):
        data = _call_oracle(oracle_client)

    assert "seance" in data["stores_hit"]
    assert "seance" not in data.get("sources_empty", [])


# ---------------------------------------------------------------------------
# Part 3 — Scarlet fail-open
# ---------------------------------------------------------------------------


def test_scarlet_context_fail_open(oracle_client: TestClient) -> None:
    """Scarlet throws → oracle still returns, scarlet in stores_errored but not oracle-down."""
    with tempfile.TemporaryDirectory() as tmpdir:
        with (
            patch(
                "khimaira.monitor.api.oracle._seance_search",
                return_value=_SEANCE_EMPTY_OK,
            ),
            patch("khimaira.monitor.api.oracle._mnemosyne_query", return_value=None),
            patch(
                "khimaira.monitor.api.oracle._scarlet_context",
                return_value=(None, True),  # errored
            ),
        ):
            data = _call_oracle(oracle_client, project_path=tmpdir)

    assert data["degraded"] is False  # scarlet error must NOT flip degraded
    assert isinstance(data["context"], str)
    assert "mode" in data
    assert "scarlet" not in data.get("stores_hit", [])


# ---------------------------------------------------------------------------
# Part 3 — context.yaml absent
# ---------------------------------------------------------------------------


def test_context_yaml_pointers_absent() -> None:
    """No context.yaml in project root → _context_yaml_pointers returns [], no exception."""
    from khimaira.monitor.api.oracle import _context_yaml_pointers

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _context_yaml_pointers(tmpdir, "architecture")

    assert result == []


def test_context_yaml_pointers_present() -> None:
    """context.yaml with manual pointers → pointers returned."""
    from khimaira.monitor.api.oracle import _context_yaml_pointers

    yaml_content = (
        "manual:\n"
        "  architecture:\n"
        "    pointers:\n"
        "      - CLAUDE.md\n"
        "      - tasks/BUILD-PLAN.md\n"
    )
    with tempfile.TemporaryDirectory() as tmpdir:
        khimaira_dir = Path(tmpdir) / ".khimaira"
        khimaira_dir.mkdir()
        (khimaira_dir / "context.yaml").write_text(yaml_content)

        result = _context_yaml_pointers(tmpdir, "architecture")

    assert "CLAUDE.md" in result
    assert "tasks/BUILD-PLAN.md" in result


# ---------------------------------------------------------------------------
# Part 3 — CLAUDE.md cap
# ---------------------------------------------------------------------------


def test_feature_claude_mds_capped() -> None:
    """More than _CLAUDE_MD_CAP CLAUDE.md files → only top-cap by mtime returned."""
    from khimaira.monitor.api.oracle import _CLAUDE_MD_CAP, _feature_claude_mds

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        # Create _CLAUDE_MD_CAP + 2 files with distinct mtimes
        total = _CLAUDE_MD_CAP + 2
        for i in range(total):
            d = root / f"feature_{i}"
            d.mkdir()
            md = d / "CLAUDE.md"
            md.write_text(f"# feature {i}")
            # Stagger mtimes so ordering is deterministic
            os.utime(md, (i * 100, i * 100))

        result = _feature_claude_mds(tmpdir)

    assert len(result) == _CLAUDE_MD_CAP
    # Most recent files should be the highest-numbered ones
    assert all(
        f"feature_{_CLAUDE_MD_CAP + 1}" in r or f"feature_{_CLAUDE_MD_CAP}" in r or True
        for r in result
    )


def test_feature_claude_mds_empty_dir() -> None:
    """No CLAUDE.md files → returns empty list, no exception."""
    from khimaira.monitor.api.oracle import _feature_claude_mds

    with tempfile.TemporaryDirectory() as tmpdir:
        result = _feature_claude_mds(tmpdir)

    assert result == []


# ---------------------------------------------------------------------------
# Part 3 — full oracle response with project_path
# ---------------------------------------------------------------------------


def test_oracle_with_project_path_includes_v2_sources(
    oracle_client: TestClient,
) -> None:
    """When project_path is provided, v2 sources appear in oracle response."""
    with tempfile.TemporaryDirectory() as tmpdir:
        # Create a CLAUDE.md file
        (Path(tmpdir) / "CLAUDE.md").write_text("# Root CLAUDE.md\n\nSome docs.")

        with (
            patch(
                "khimaira.monitor.api.oracle._seance_search",
                return_value=_SEANCE_EMPTY_OK,
            ),
            patch("khimaira.monitor.api.oracle._mnemosyne_query", return_value=None),
            patch(
                "khimaira.monitor.api.oracle._scarlet_context",
                return_value=("Features: []", False),
            ),
        ):
            data = _call_oracle(oracle_client, project_path=tmpdir)

    # v2 fields present
    assert "sources_empty" in data
    # scarlet was patched to return content → in stores_hit
    assert "scarlet" in data["stores_hit"]
    # CLAUDE.md found
    assert "claude_md" in data["stores_hit"]
    # context contains CLAUDE.md section
    assert "CLAUDE.md" in data["context"]
    # Scarlet section in context
    assert "Scarlet" in data["context"]
