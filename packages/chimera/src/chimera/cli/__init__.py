"""chimera CLI entry point — `chimera <subcommand> ...`.

Subcommands (each in its own module):
  task   — context-resolved auto-routed dispatch
  route  — classify-only, no dispatch (debugging / dry-run)
  doctor — environment diagnostic
  init   — first-time setup [SCAFFOLDED, not yet implemented]
  dev    — runtime manager [SCAFFOLDED, not yet implemented]
  install — configure MCP for terminal CLIs [SCAFFOLDED]
  monitor — observability daemon control [SCAFFOLDED — migrate from legacy]

Each subcommand module exports `add_subparser(subparsers)` to register
itself here, and a `run(args) -> int` function called by argparse dispatch.
"""

from __future__ import annotations

import argparse
import sys

from chimera import __version__

from . import doctor, monitor, route, task


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="chimera",
        description=(
            "chimera — multi-model AI orchestration for terminal AI CLIs. "
            "Auto-routes dev tasks to the cheapest competent model across "
            "Claude Code, Codex CLI, Gemini CLI, Ollama, llm. "
            "No API keys required."
        ),
    )
    parser.add_argument("--version", action="version", version=f"chimera {__version__}")

    subparsers = parser.add_subparsers(dest="command", required=True)
    task.add_subparser(subparsers)
    route.add_subparser(subparsers)
    doctor.add_subparser(subparsers)
    monitor.add_subparser(subparsers)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
