"""Unified MCP surface — re-register seance/specter/scarlet tools under khimaira.

NORTH_STAR Phase 0: one MCP server, all tools. Each sibling package
keeps its own FastMCP instance (so `seance serve` / `specter serve`
/ `scarlet serve` continue to work for backward compat). This module
imports those instances, walks each tool registry, and re-registers
under khimaira's MCP with a source prefix to prevent name collisions
and keep origin visible.

After this runs, hosts that connect to khimaira see:

  mcp__khimaira__seance_semantic_search    (from seance.server)
  mcp__khimaira__specter_take_screenshot   (from specter.server)
  mcp__khimaira__scarlet_analyze_project   (from scarlet.server)
  mcp__khimaira__auto / session_* / ...    (khimaira's own)

Tool function bodies are unchanged — they execute through the same
`fn` reference, using the same module-level state from the sibling
module. Config loading and resource setup happen in the sibling
module's import.
"""

from __future__ import annotations

import importlib
import logging

log = logging.getLogger(__name__)

SIBLING_PACKAGES: tuple[str, ...] = ("seance", "specter", "scarlet")


def register_sibling_tools(khimaira_mcp) -> int:
    """Re-register all sibling FastMCP tools on khimaira_mcp with prefixed names.

    Failures are logged + skipped — one broken sibling does not
    prevent the other two from registering. Returns the total count
    of tools successfully re-registered.
    """
    total = 0
    for name in SIBLING_PACKAGES:
        try:
            mod = importlib.import_module(f"{name}.server")
        except Exception as exc:  # noqa: BLE001 — never block khimaira boot
            log.warning("sibling_tools: failed to import %s.server: %s", name, exc)
            continue

        sibling_mcp = getattr(mod, "mcp", None)
        if sibling_mcp is None:
            log.warning("sibling_tools: %s.server has no `mcp` attribute", name)
            continue

        try:
            tools = sibling_mcp._tool_manager.list_tools()
        except Exception as exc:  # noqa: BLE001
            log.warning("sibling_tools: failed to list %s tools: %s", name, exc)
            continue

        prefix = f"{name}_"
        for tool in tools:
            new_name = f"{prefix}{tool.name}"
            try:
                khimaira_mcp._tool_manager.add_tool(
                    tool.fn,
                    name=new_name,
                    description=tool.description or "",
                )
                total += 1
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "sibling_tools: failed to re-register %s.%s as %s: %s",
                    name,
                    tool.name,
                    new_name,
                    exc,
                )
    return total
