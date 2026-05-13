"""`khimaira heal` — self-reflect, detect drift, apply fixes.

The "is anything broken? then fix it" command. Closes the loop between
`khimaira doctor` (read-only diagnostic) and `khimaira bootstrap`
(apply changes from a profile): heal runs the checks AND the fixes
in one shot, scoped to khimaira's own setup health.

Heal scope:
  - Profile drift (symlinks missing/wrong, MCP servers unregistered,
    hooks pointing at legacy paths, etc.) → `khimaira bootstrap` to apply
  - Daemon down → `khimaira monitor start`
  - Supervisor not installed → recommends `install-service` (only
    applies with --aggressive so we don't write systemd units on a
    user who didn't ask)
  - Stale hook command form → already covered by bootstrap

NOT in heal scope (intentional):
  - User's project code, dev server, dependencies
  - Anything that requires the user's judgment (which model to use,
    which repo to clone, etc.)

Idempotent — re-running on a healthy machine is a no-op.

Invocation: `khimaira heal [--profile <path>] [--aggressive] [--dry-run]`
Or from inside Claude Code: `/heal`.
"""

from __future__ import annotations

import argparse
import sys

from khimaira.log import get_logger

log = get_logger("cli.heal")


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "heal",
        help="Auto-detect khimaira setup drift and apply fixes (doctor + bootstrap in one).",
        description=(
            "Runs the same checks as `khimaira doctor`, then applies the "
            "fixes that doctor would surface as drift. Idempotent — safe "
            "on a healthy machine (no-op). Scoped to khimaira's own setup; "
            "doesn't touch user project state."
        ),
    )
    p.add_argument(
        "--profile",
        default=None,
        help="Path or http(s) URL to a khimaira profile YAML. Same resolution as bootstrap.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the planned fixes without applying anything (alias for `khimaira doctor`).",
    )
    p.add_argument(
        "--aggressive",
        action="store_true",
        help=(
            "Also apply fixes with side effects beyond khimaira state: "
            "install supervisor units, install MCP servers when claude "
            "CLI is absent, etc. Default is conservative — won't change "
            "things the user might want to manage manually."
        ),
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="Pass --force through to bootstrap (re-register MCPs, overwrite stale supervisor units).",
    )
    p.set_defaults(func=_run_heal)


def _run_heal(args: argparse.Namespace) -> int:
    """Three-phase heal:

      Phase 1: introspect — same checks as doctor (cheap, local).
      Phase 2: apply khimaira-setup fixes (bootstrap + hooks reload).
      Phase 3: apply daemon/supervisor fixes (only with --aggressive).

    Each phase reports what it did. Total result is a count of fixes
    applied + a list of items the user still needs to handle (e.g.
    install Claude Code).
    """
    print("khimaira heal")
    print("=" * 60)

    # ---- Phase 1 — load profile + diagnose ----
    try:
        from khimaira.bootstrap import ProfileError, load_profile
        from khimaira.bootstrap.runner import check_bootstrap, run_bootstrap
    except ImportError as e:
        print(f"❌ khimaira.bootstrap import failed: {e}", file=sys.stderr)
        return 2

    try:
        profile, source = load_profile(args.profile)
    except ProfileError as e:
        print(f"❌ profile failed to load: {e}", file=sys.stderr)
        return 2

    print(f"\nProfile: {profile.name}  (from {source})")
    drift_report = check_bootstrap(profile)
    drift_count = drift_report.summary.get("created", 0) + drift_report.summary.get(
        "updated", 0
    )
    failed_count = drift_report.summary.get("failed", 0)

    daemon_down = not _daemon_up()

    if drift_count == 0 and failed_count == 0 and not daemon_down:
        print("\n✅ Nothing to heal — everything matches profile + daemon up.")
        return 0

    # ---- Phase 2 — show plan ----
    print("\nDiagnosed:")
    if drift_count:
        print(f"  • {drift_count} profile-drift item(s)")
        for r in drift_report.results:
            if r.status in ("created", "updated"):
                label = "would-create" if r.status == "created" else "would-update"
                print(f"      [{label}] {r.op}  {r.target}")
    if failed_count:
        print(f"  • {failed_count} unrecoverable drift (needs manual intervention)")
        for r in drift_report.results:
            if r.status == "failed":
                print(f"      ✗ {r.op}  {r.target}  — {r.detail}")
    if daemon_down:
        print("  • khimaira-monitor daemon down")

    if args.dry_run:
        print("\n--dry-run: no fixes applied.")
        return 1 if drift_count or failed_count or daemon_down else 0

    # ---- Phase 3 — apply ----
    print("\nApplying fixes:")
    fixed = 0

    if drift_count or failed_count:
        print(f"  → running `khimaira bootstrap`{' --force' if args.force else ''}…")
        boot_report = run_bootstrap(profile, force=args.force)
        applied = boot_report.summary.get("created", 0) + boot_report.summary.get(
            "updated", 0
        )
        boot_failed = boot_report.summary.get("failed", 0)
        if boot_failed:
            print(f"    ⚠ {boot_failed} operation(s) failed during bootstrap:")
            for r in boot_report.results:
                if r.status == "failed":
                    print(f"        ✗ {r.op}  {r.target}  — {r.detail}")
        else:
            print(f"    ✓ {applied} fix(es) applied")
        fixed += applied

    if daemon_down:
        if args.aggressive:
            print("  → starting khimaira-monitor daemon…")
            if _start_daemon():
                print("    ✓ daemon started")
                fixed += 1
            else:
                print("    ⚠ daemon start failed — check `khimaira monitor status`")
        else:
            print("  → daemon down (skipping; use --aggressive to start automatically)")

    print(f"\n{'✅' if fixed else '⚠'} heal complete — {fixed} fix(es) applied.")
    if not args.aggressive and daemon_down:
        print(
            "   Daemon is still down. Run `khimaira heal --aggressive` "
            "or `khimaira monitor start` to recover."
        )
    return 0


def _daemon_up() -> bool:
    """Quick liveness check on the monitor daemon's loopback port."""
    import urllib.error
    import urllib.request

    try:
        with urllib.request.urlopen(
            "http://127.0.0.1:8740/api/heartbeats/stats", timeout=1.5
        ) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        return False


def _start_daemon() -> bool:
    """Best-effort daemon start. Returns True on apparent success."""
    import argparse as ap

    try:
        from khimaira.monitor.cli import _cmd_start
    except ImportError:
        return False

    # Mimic the argparse Namespace _cmd_start expects.
    args = ap.Namespace(foreground=False, no_browser=True)
    rc = _cmd_start(args)
    return rc == 0
