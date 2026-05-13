"""`khimaira mcp` — start the MCP server over stdio.

This is what terminal AI shells (Claude Code, Codex CLI, Gemini CLI) call.
Update `.claude.json` to point at:

    uv --directory /home/_3ntropy/dev/khimaira run khimaira mcp

The legacy invocation `uv run khimaira` (no subcommand) defaulted to MCP;
the new layered CLI requires the explicit `mcp` subcommand for clarity.
"""

from __future__ import annotations

import argparse
import sys


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "mcp",
        help="Start the khimaira MCP server over stdio (for AI CLI integration).",
        description=(
            "Launches the FastMCP server. Reads protocol messages on stdin, "
            "writes on stdout, logs on stderr. Designed to be invoked by an "
            "AI CLI (Claude Code, Codex CLI, Gemini CLI) via its MCP config."
        ),
    )
    p.set_defaults(func=run)


def run(_args: argparse.Namespace) -> int:
    # Lazy import — the FastMCP server pulls heavy deps (LangGraph, langchain)
    # that we don't want loaded for `khimaira doctor` or `khimaira task`.
    from khimaira.server.mcp import mcp

    # FastMCP's stdio runner takes over stdin/stdout. Logs go to stderr.
    print("[khimaira mcp] starting MCP server over stdio", file=sys.stderr)
    mcp.run()
    return 0
