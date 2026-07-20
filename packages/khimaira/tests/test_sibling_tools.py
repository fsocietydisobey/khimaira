"""Tests for `khimaira.server.sibling_tools` — the Phase 0 unification.

Verifies that:
  - All three sibling FastMCP packages (seance, specter, scarlet) can
    be re-registered on a fresh FastMCP without exceptions.
  - Re-registered tool names carry the source prefix.
  - A broken sibling import doesn't poison the others.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError


def _register_fake_sibling(monkeypatch, *functions):
    import khimaira.server.sibling_tools as st

    sibling_mcp = FastMCP("fake-sibling")
    for function in functions:
        sibling_mcp.tool()(function)

    monkeypatch.setattr(st, "SIBLING_PACKAGES", ("fake",))
    monkeypatch.setattr(
        st.importlib,
        "import_module",
        lambda module_name: SimpleNamespace(mcp=sibling_mcp),
    )

    khimaira_mcp = FastMCP("test-khimaira")
    assert st.register_sibling_tools(khimaira_mcp) == len(functions)
    return khimaira_mcp


def test_register_sibling_tools_attaches_all_three_packages():
    """Smoke test — all 48 known sibling tools register cleanly."""
    from khimaira.server.sibling_tools import register_sibling_tools

    fresh_mcp = FastMCP("test-khimaira")
    count = register_sibling_tools(fresh_mcp)

    # Conservative lower bound — 48 today (seance 5 + specter 34 + scarlet 9).
    # Tightening to ==48 would couple the test to sibling tool counts and
    # fail anytime someone adds a tool to a sibling. Bound below at 40
    # gives slack while still catching catastrophic regressions.
    assert count >= 40, f"expected >=40 sibling tools, got {count}"

    names = {t.name for t in fresh_mcp._tool_manager.list_tools()}
    assert "seance_semantic_search" in names
    assert "specter_take_screenshot" in names
    assert "scarlet_analyze_project" in names


def test_register_sibling_tools_uses_source_prefix():
    """Every re-registered tool has a `<source>_` prefix."""
    from khimaira.server.sibling_tools import SIBLING_PACKAGES, register_sibling_tools

    fresh_mcp = FastMCP("test-khimaira")
    register_sibling_tools(fresh_mcp)

    expected_prefixes = tuple(f"{name}_" for name in SIBLING_PACKAGES)
    names = [t.name for t in fresh_mcp._tool_manager.list_tools()]
    for name in names:
        assert name.startswith(expected_prefixes), (
            f"tool {name!r} lacks a source prefix in {expected_prefixes}"
        )


def test_register_sibling_tools_tolerates_missing_package(monkeypatch):
    """A broken sibling import doesn't prevent the others from registering."""
    import khimaira.server.sibling_tools as st

    # Inject a fake sibling that fails on import — register_sibling_tools
    # should log + skip and continue with the real siblings.
    monkeypatch.setattr(st, "SIBLING_PACKAGES", ("does_not_exist", "seance"))

    fresh_mcp = FastMCP("test-khimaira")
    count = st.register_sibling_tools(fresh_mcp)

    # seance's tools should still register despite the bad sibling.
    assert count >= 1
    names = {t.name for t in fresh_mcp._tool_manager.list_tools()}
    assert any(n.startswith("seance_") for n in names)


def test_register_sibling_tools_idempotent_within_fresh_server():
    """Two registrations on the same MCP should be a no-op or duplicate-safe.

    FastMCP raises on duplicate tool names by default. The integration in
    `mcp.py:main()` calls register_sibling_tools exactly once at boot, so
    duplicate-calling isn't a production path — but this test documents
    that the helper itself doesn't accumulate state between calls (each
    invocation freshly lists the sibling registry).
    """
    from khimaira.server.sibling_tools import register_sibling_tools

    fresh_mcp = FastMCP("test-khimaira")
    first = register_sibling_tools(fresh_mcp)

    # Second registration: every name already exists, so FastMCP will
    # warn/raise. Our helper catches those and logs them. The count
    # returned should be 0 (every re-add fails) without raising.
    second = register_sibling_tools(fresh_mcp)
    assert first >= 40
    assert second == 0 or second == first  # duplicate-overwrite or skip


@pytest.mark.asyncio
async def test_system_exit_becomes_tool_error_and_server_remains_callable(monkeypatch):
    def fatal(value: int) -> int:
        raise SystemExit(f"bad configuration for {value}")

    def echo(value: int, prefix: str = "ok") -> str:
        return f"{prefix}:{value}"

    khimaira_mcp = _register_fake_sibling(monkeypatch, fatal, echo)

    fatal_tool = khimaira_mcp._tool_manager.get_tool("fake_fatal")
    assert fatal_tool is not None
    assert inspect.signature(fatal_tool.fn) == inspect.signature(fatal)
    assert fatal_tool.parameters["required"] == ["value"]

    with pytest.raises(ToolError, match="SystemExit") as exc_info:
        await khimaira_mcp._tool_manager.call_tool("fake_fatal", {"value": 7})

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert isinstance(exc_info.value.__cause__.__cause__, SystemExit)
    assert (
        await khimaira_mcp._tool_manager.call_tool("fake_echo", {"value": 7, "prefix": "alive"})
        == "alive:7"
    )


@pytest.mark.asyncio
async def test_async_base_exception_becomes_tool_error(monkeypatch):
    async def fatal_async(value: str) -> str:
        raise KeyboardInterrupt(value)

    khimaira_mcp = _register_fake_sibling(monkeypatch, fatal_async)

    with pytest.raises(ToolError, match="KeyboardInterrupt") as exc_info:
        await khimaira_mcp._tool_manager.call_tool("fake_fatal_async", {"value": "stop"})

    assert isinstance(exc_info.value.__cause__, RuntimeError)
    assert isinstance(exc_info.value.__cause__.__cause__, KeyboardInterrupt)


@pytest.mark.asyncio
async def test_normal_exception_propagates_unchanged_to_fastmcp(monkeypatch):
    def broken(value: int) -> int:
        raise ValueError(f"invalid {value}")

    khimaira_mcp = _register_fake_sibling(monkeypatch, broken)

    with pytest.raises(ToolError, match="invalid 3") as exc_info:
        await khimaira_mcp._tool_manager.call_tool("fake_broken", {"value": 3})

    assert isinstance(exc_info.value.__cause__, ValueError)
