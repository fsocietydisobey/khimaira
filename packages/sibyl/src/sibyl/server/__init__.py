"""Sibyl's FastMCP server — exposes meeting tools to khimaira.

Following the seance / specter / scarlet pattern. Defines an `mcp`
FastMCP instance with tools; khimaira's `register_sibling_tools`
re-exposes them under `sibyl_*` on its own MCP server. So callers
see `mcp__khimaira__sibyl_process`, `mcp__khimaira__sibyl_record_start`,
etc., via one MCP connection.

Standalone `sibyl serve` is also possible — same tools, no prefix —
but the canonical path is through khimaira.
"""

from .mcp import mcp

__all__ = ["mcp"]
