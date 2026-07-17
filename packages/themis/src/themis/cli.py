"""Standalone Themis command line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from themis.installer import InstallError, install_internal_roster


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="themis",
        description="Local role-invariant enforcement utilities.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    install = subparsers.add_parser(
        "install-internal-roster",
        help="Install standalone Claude internal-roster agents and PreToolUse hook.",
    )
    install.add_argument(
        "--claude-settings",
        type=Path,
        default=Path.home() / ".claude" / "settings.json",
    )
    install.add_argument(
        "--claude-agents-dir",
        type=Path,
        default=Path.home() / ".claude" / "agents",
    )
    install.add_argument(
        "--codex",
        action="store_true",
        help="Also merge the standalone Themis PreToolUse hook into Codex hooks.json.",
    )
    install.add_argument(
        "--codex-hooks",
        type=Path,
        default=Path.home() / ".codex" / "hooks.json",
        help="Codex hooks path used only with --codex.",
    )
    install.add_argument(
        "--uninstall",
        action="store_true",
        help="Remove package-owned agents and standalone hooks.",
    )
    install.add_argument(
        "--force",
        action="store_true",
        help="Replace conflicting package-owned agent files or symlinks.",
    )
    install.set_defaults(func=_run_install)
    return parser


def _run_install(args: argparse.Namespace) -> int:
    try:
        changes = install_internal_roster(
            claude_settings=args.claude_settings.expanduser(),
            claude_agents_dir=args.claude_agents_dir.expanduser(),
            codex_hooks=args.codex_hooks.expanduser() if args.codex else None,
            uninstall=args.uninstall,
            force=args.force,
        )
    except (InstallError, OSError) as exc:
        print(f"themis: install failed: {exc}", file=sys.stderr)
        return 3

    for change in changes:
        print(f"{change.status:9} {change.target}: {change.detail}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
