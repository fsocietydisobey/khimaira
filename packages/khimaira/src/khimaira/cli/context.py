"""`khimaira context {refresh,show,gaps}` — manage `.khimaira/context.yaml` (#66)."""
from __future__ import annotations

import argparse
from pathlib import Path

_CONTEXT_YAML = ".khimaira/context.yaml"


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "context",
        help="manage .khimaira/context.yaml (dynamic context injection)",
        description="Regenerate / inspect the per-project .khimaira/context.yaml cache.",
    )
    parser.add_argument(
        "action",
        choices=["refresh", "show", "gaps"],
        help="refresh (regen AUTO, preserve MANUAL) | show | gaps (features w/o CLAUDE.md)",
    )
    parser.add_argument("--path", default=".", help="project root (default: cwd)")
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    root = Path(args.path).expanduser().resolve()
    if args.action == "refresh":
        from scarlet.generator.context_yaml import refresh_context_yaml

        print(f"✓ context.yaml refreshed: {refresh_context_yaml(root)}")
        return 0
    if args.action == "show":
        p = root / _CONTEXT_YAML
        if p.is_file():
            print(p.read_text(encoding="utf-8"), end="")
        else:
            print("no context.yaml — run `khimaira context refresh`")
        return 0
    if args.action == "gaps":
        from scarlet.generator.context_yaml import features_without_claude_md

        gaps = features_without_claude_md(root)
        print("\n".join(gaps) if gaps else "no gaps — every feature has a CLAUDE.md")
        return 0
    return 1
