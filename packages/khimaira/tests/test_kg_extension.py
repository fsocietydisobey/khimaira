"""Tests for the KG extension: kg_scopes, kg_schema dangling, _kg_default_project.

Contract: KG-extension contract v1 (scratchpad/KG-EXTENSION-CONTRACT-v1.md).
Mocks at the `_get` boundary (same pattern as test_kg_mcp_tools.py) so these
tests run without a live daemon or adapter.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from khimaira.server import monitor_tools as mt


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# kg_scopes — happy path
# ---------------------------------------------------------------------------


def test_kg_scopes_happy_headline():
    """Headline shows count + richest scope (first entry, richest by nodes)."""
    payload = {
        "data": {
            "scopes": [
                {"scope": "shop:10", "nodes": 1234, "edges": 5678, "label": "Acme Auto"},
                {"scope": "shop:3", "nodes": 87, "edges": 120, "label": None},
            ]
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_scopes("backend"))
    assert "2 scopes" in out
    assert "richest = shop:10 (1234 nodes)" in out


def test_kg_scopes_happy_per_scope_lines():
    """Each scope renders as 'scope · N nodes · N edges [· label]'."""
    payload = {
        "data": {
            "scopes": [
                {"scope": "shop:10", "nodes": 1234, "edges": 5678, "label": "Acme Auto"},
                {"scope": "shop:3", "nodes": 87, "edges": 120, "label": None},
            ]
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_scopes("backend"))
    assert "  shop:10 · 1234 nodes · 5678 edges · Acme Auto" in out
    # null label omitted — no literal "None" in output
    assert "  shop:3 · 87 nodes · 120 edges" in out
    assert "None" not in out


def test_kg_scopes_single_scope_grammatically_correct():
    """'1 scope' not '1 scopes'."""
    payload = {
        "data": {
            "scopes": [
                {"scope": "shop:1", "nodes": 5, "edges": 3, "label": "Solo"},
            ]
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_scopes("backend"))
    assert "1 scope ·" in out
    assert "1 scopes" not in out


# ---------------------------------------------------------------------------
# kg_scopes — empty + error
# ---------------------------------------------------------------------------


def test_kg_scopes_empty():
    """Empty scopes list → 📭 message, no crash."""
    with patch.object(mt, "_get", return_value={"data": {"scopes": []}}):
        out = _run(mt.kg_scopes("backend"))
    assert "📭" in out
    assert "no scopes with KG data" in out
    assert "`backend`" in out


def test_kg_scopes_error_passthrough():
    """Daemon error string passes through (no masking)."""
    with patch.object(mt, "_get", return_value="khimaira-monitor → HTTP 404: no adapter"):
        out = _run(mt.kg_scopes("nope"))
    assert "HTTP 404" in out


def test_kg_scopes_request_path():
    """Tool hits /api/graph/{project}/scopes (no scope/since params)."""
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"scopes": []}}

    with patch.object(mt, "_get", side_effect=fake_get):
        _run(mt.kg_scopes("backend"))
    assert captured["path"] == "/api/graph/backend/scopes"


# ---------------------------------------------------------------------------
# kg_schema — dangling render
# ---------------------------------------------------------------------------


def test_kg_schema_dangling_shown_when_nonzero():
    """A triple with dangling > 0 appends '  ⚠ N dangling'."""
    payload = {
        "data": {
            "nodeTypes": ["job", "task"],
            "linkTypes": ["belongs-to"],
            "triples": [
                {
                    "fromType": "task",
                    "linkType": "belongs-to",
                    "toType": "job",
                    "count": 10,
                    "dangling": 3,
                },
                {
                    "fromType": "job",
                    "linkType": "contains",
                    "toType": "task",
                    "count": 5,
                    "dangling": 0,
                },
            ],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_schema("backend", "shop:10"))
    assert "task -[belongs-to]-> job  × 10  ⚠ 3 dangling" in out
    # dangling=0 must NOT produce a warning
    job_line = next(ln for ln in out.splitlines() if "job -[contains]" in ln)
    assert "⚠" not in job_line
    assert "dangling" not in job_line


def test_kg_schema_dangling_absent_is_back_compat():
    """Adapters that omit 'dangling' render identically to the old format."""
    payload = {
        "data": {
            "nodeTypes": ["job"],
            "linkTypes": ["belongs-to"],
            "triples": [
                {"fromType": "task", "linkType": "belongs-to", "toType": "job", "count": 10},
            ],
        }
    }
    with patch.object(mt, "_get", return_value=payload):
        out = _run(mt.kg_schema("backend", "shop:10"))
    assert "task -[belongs-to]-> job  × 10" in out
    assert "dangling" not in out
    assert "⚠" not in out


# ---------------------------------------------------------------------------
# _kg_default_project — the auto-resolve helper
# ---------------------------------------------------------------------------


def test_kg_default_project_explicit_passthrough():
    """Non-empty project arg is returned as-is, no registry lookup."""
    project, err = mt._kg_default_project("my-project")
    assert project == "my-project"
    assert err is None


def test_kg_default_project_single_adapter_auto_resolves():
    """Exactly one registered KG adapter → returns its label."""
    fake_projects = [
        {
            "label": "backend",
            "kg_adapter": {"url": "http://somewhere/internal/kg/graph"},
            "project_path": "/app/backend",
        },
        {
            "label": "myapp",
            "project_path": "/app/myapp",
            # no kg_adapter — not counted
        },
    ]
    with patch("khimaira.attach.registry.list_attached", return_value=fake_projects):
        project, err = mt._kg_default_project("")
    assert project == "backend"
    assert err is None


def test_kg_default_project_multiple_adapters_error():
    """Multiple registered KG adapters → helpful error, no guess."""
    fake_projects = [
        {"label": "backend", "kg_adapter": {"url": "..."}, "project_path": "/a"},
        {"label": "staging", "kg_adapter": {"url": "..."}, "project_path": "/b"},
    ]
    with patch("khimaira.attach.registry.list_attached", return_value=fake_projects):
        project, err = mt._kg_default_project("")
    assert project is None
    assert "multiple KG projects" in err
    assert "backend" in err
    assert "staging" in err
    assert "pass project=" in err


def test_kg_default_project_no_adapters_error():
    """No projects have a kg_adapter → error (not a crash)."""
    fake_projects = [
        {"label": "myapp", "project_path": "/app/myapp"},
    ]
    with patch("khimaira.attach.registry.list_attached", return_value=fake_projects):
        project, err = mt._kg_default_project("")
    assert project is None
    assert "no KG adapter registered" in err


def test_kg_default_project_empty_registry():
    """Empty registry → same error as no-adapters."""
    with patch("khimaira.attach.registry.list_attached", return_value=[]):
        project, err = mt._kg_default_project("")
    assert project is None
    assert "no KG adapter registered" in err


# ---------------------------------------------------------------------------
# project= auto-resolve integration: kg_scopes + kg_health pass-through
# ---------------------------------------------------------------------------


def test_kg_scopes_auto_resolves_single_adapter():
    """When project is omitted and exactly one adapter exists, kg_scopes resolves it."""
    fake_projects = [
        {"label": "backend", "kg_adapter": {"url": "..."}, "project_path": "/app"},
    ]
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"scopes": [{"scope": "shop:1", "nodes": 5, "edges": 2, "label": None}]}}

    with (
        patch("khimaira.attach.registry.list_attached", return_value=fake_projects),
        patch.object(mt, "_get", side_effect=fake_get),
    ):
        out = _run(mt.kg_scopes(""))  # no project
    assert "/api/graph/backend/scopes" in captured["path"]
    assert "1 scope" in out


def test_kg_graph_auto_resolves_single_adapter():
    """kg_graph with project='' auto-resolves when one adapter is registered."""
    fake_projects = [
        {"label": "backend", "kg_adapter": {"url": "..."}, "project_path": "/app"},
    ]
    captured = {}

    def fake_get(path, **kw):
        captured["path"] = path
        return {"data": {"nodes": [], "edges": []}}

    with (
        patch("khimaira.attach.registry.list_attached", return_value=fake_projects),
        patch.object(mt, "_get", side_effect=fake_get),
    ):
        _run(mt.kg_graph(""))  # no project
    assert "/api/graph/backend" in captured["path"]


def test_kg_tool_returns_error_on_no_adapter():
    """kg_graph with project='' and no adapters returns ❌ error string."""
    with (
        patch("khimaira.attach.registry.list_attached", return_value=[]),
        patch.object(mt, "_get") as mock_get,
    ):
        out = _run(mt.kg_graph(""))
    mock_get.assert_not_called()  # never hits daemon
    assert "❌" in out
    assert "no KG adapter registered" in out
