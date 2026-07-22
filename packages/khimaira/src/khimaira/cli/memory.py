"""Manual Claude native-memory maintenance commands."""

from __future__ import annotations

import argparse
import json


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    memory = subparsers.add_parser(
        "memory",
        help="Maintain and search Claude Code native memory indexes.",
    )
    sub = memory.add_subparsers(dest="memory_cmd", required=True)
    refresh = sub.add_parser(
        "refresh",
        help="Prune both known MEMORY.md files and reindex changed content.",
    )
    refresh.add_argument(
        "--max-bytes",
        type=int,
        default=12000,
        help="Maximum live MEMORY.md size per project (default: 12000).",
    )
    refresh.add_argument(
        "--force-reindex",
        action="store_true",
        help="Rebuild Qdrant even when the content fingerprint is unchanged.",
    )
    refresh.set_defaults(func=run_refresh)


def run_refresh(args: argparse.Namespace) -> int:
    from khimaira.claude_memory_retrieval import refresh_configured_memories

    result = refresh_configured_memories(
        max_bytes=args.max_bytes,
        force_reindex=args.force_reindex,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 1 if result.get("reindex", {}).get("status") == "error" else 0
