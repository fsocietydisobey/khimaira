"""Tests for `khimaira.server.sibling_tools` — the Phase 0 unification.

Verifies that:
  - All three sibling FastMCP packages (seance, specter, scarlet) can
    be re-registered on a fresh FastMCP without exceptions.
  - Re-registered tool names carry the source prefix.
  - A broken sibling import doesn't poison the others.
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP


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
    from khimaira.server.sibling_tools import register_sibling_tools

    fresh_mcp = FastMCP("test-khimaira")
    register_sibling_tools(fresh_mcp)

    names = [t.name for t in fresh_mcp._tool_manager.list_tools()]
    for name in names:
        assert name.startswith(("seance_", "specter_", "scarlet_")), (
            f"tool {name!r} lacks a source prefix"
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
