"""`khimaira sessions {cleanup, list-stale}` — session registry maintenance.

  list-stale   Read-only: show sessions older than N hours
  cleanup      Delete stale sessions (with confirmation or --yes)
"""

from __future__ import annotations

import argparse


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    sessions = subparsers.add_parser(
        "sessions",
        help="Session registry maintenance (list-stale, cleanup).",
    )
    sub = sessions.add_subparsers(dest="sessions_cmd", required=True)

    # list-stale
    p_list = sub.add_parser("list-stale", help="Show stale sessions without deleting.")
    p_list.add_argument(
        "--older-than",
        type=float,
        default=48.0,
        metavar="HOURS",
        help="Age threshold in hours (default: 48).",
    )
    p_list.set_defaults(func=_run_list_stale)

    # cleanup
    p_cleanup = sub.add_parser("cleanup", help="Delete stale sessions.")
    p_cleanup.add_argument(
        "--older-than",
        type=float,
        default=48.0,
        metavar="HOURS",
        help="Age threshold in hours (default: 48).",
    )
    p_cleanup.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview what would be deleted without deleting.",
    )
    p_cleanup.add_argument(
        "--yes",
        action="store_true",
        help="Skip confirmation prompt.",
    )
    p_cleanup.add_argument(
        "--include-with-decisions",
        action="store_true",
        help="Also delete sessions that have logged decisions (archives first).",
    )
    p_cleanup.set_defaults(func=_run_cleanup)


def _find_stale_sessions(older_than_hours: float, include_with_decisions: bool) -> list[dict]:
    from khimaira.monitor import sessions as sess_mod

    threshold_s = older_than_hours * 3600
    all_sessions = sess_mod.list_sessions(use_cache=False)
    stale = []
    for s in all_sessions:
        age_s = s.get("last_active_age_s") or 0
        if age_s < threshold_s:
            continue
        if s.get("decision_count", 0) > 0 and not include_with_decisions:
            continue
        stale.append(s)
    return stale


def _print_table(sessions: list[dict]) -> None:
    import time

    if not sessions:
        print("  (none)")
        return
    fmt = "  {:<36}  {:<20}  {:<14}  {:>9}  {:>10}"
    print(fmt.format("session_id", "name", "last_active", "decisions", "file_touches"))
    print("  " + "-" * 96)
    for s in sessions:
        age_s = s.get("last_active_age_s") or 0
        age_str = f"{age_s / 3600:.1f}h ago"
        print(
            fmt.format(
                s["session_id"][:36],
                (s.get("name") or "")[:20],
                age_str,
                s.get("decision_count", 0),
                s.get("file_touch_count", 0),
            )
        )


def _run_list_stale(args: argparse.Namespace) -> int:
    stale = _find_stale_sessions(args.older_than, include_with_decisions=True)
    print(f"Stale sessions (older than {args.older_than:.0f}h): {len(stale)}")
    _print_table(stale)
    return 0


def _run_cleanup(args: argparse.Namespace) -> int:
    from khimaira.monitor import sessions as sess_mod

    stale = _find_stale_sessions(args.older_than, args.include_with_decisions)

    if not stale:
        print(f"No stale sessions found (older than {args.older_than:.0f}h).")
        return 0

    print(f"Stale sessions to delete ({len(stale)}):")
    _print_table(stale)

    if args.dry_run:
        print(f"\nDry run — would delete {len(stale)} session(s).")
        return 0

    if not args.yes:
        try:
            answer = input(f"\nDelete these {len(stale)} sessions? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\nAborted.")
            return 1
        if answer not in ("y", "yes"):
            print("Aborted.")
            return 0

    deleted = 0
    had_decisions = 0
    archived_paths: list[str] = []

    for s in stale:
        sid = s["session_id"]
        result = sess_mod.delete_session(sid, force=args.include_with_decisions)
        if result.get("deleted"):
            deleted += 1
            if result.get("had_decisions"):
                had_decisions += 1
            if result.get("archived_to"):
                archived_paths.append(result["archived_to"])
        else:
            err = result.get("error", "unknown")
            print(f"  ⚠️  {sid}: {err}")

    print(
        f"\nDeleted {deleted} session(s)"
        + (f" ({had_decisions} had decisions, archived)" if had_decisions else "")
        + "."
    )
    if archived_paths:
        print(f"Archive directory: {archived_paths[0].rsplit('/', 1)[0]}")

    return 0


def run(args: argparse.Namespace) -> int:
    return args.func(args)
