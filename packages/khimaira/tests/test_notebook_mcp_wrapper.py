"""Regression coverage for the `@mcp.tool()` notebook_list WRAPPER
(khimaira.server.mcp.notebook_list) — distinct from test_notebook_mcp_tools.py,
which tests the underlying khimaira.server.notebook_tools implementation.

Bug this guards against (2026-07-04): notebook_tools.notebook_list already
accepted `kind`, but the registered MCP tool wrapper in mcp.py dropped it on
the floor (only forwarded project/tab) — so no MCP caller could actually
filter by kind, even though the implementation fully supported it. A
parameter-drop in a thin delegation wrapper isn't caught by testing the impl
module alone, since the impl was already correct.
"""

from __future__ import annotations

import asyncio
from unittest.mock import patch

from khimaira.server import mcp as mcp_mod


def _run(coro):
    return asyncio.run(coro)


def test_notebook_list_wrapper_forwards_kind_to_impl():
    async def fake_notebook_list(project="", tab="", kind=""):
        return f"project={project!r} tab={tab!r} kind={kind!r}"

    with patch.object(mcp_mod._notebook_tools, "notebook_list", fake_notebook_list):
        out = _run(mcp_mod.notebook_list(kind="study_guide"))

    assert "kind='study_guide'" in out


def test_notebook_list_wrapper_forwards_all_three_filters_together():
    async def fake_notebook_list(project="", tab="", kind=""):
        return f"project={project!r} tab={tab!r} kind={kind!r}"

    with patch.object(mcp_mod._notebook_tools, "notebook_list", fake_notebook_list):
        out = _run(mcp_mod.notebook_list(project="khimaira", tab="t1", kind="note"))

    assert "project='khimaira'" in out
    assert "tab='t1'" in out
    assert "kind='note'" in out


def test_notebook_list_wrapper_kind_defaults_to_empty():
    async def fake_notebook_list(project="", tab="", kind=""):
        return f"kind={kind!r}"

    with patch.object(mcp_mod._notebook_tools, "notebook_list", fake_notebook_list):
        out = _run(mcp_mod.notebook_list())

    assert "kind=''" in out
