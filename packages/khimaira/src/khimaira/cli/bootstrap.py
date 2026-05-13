"""`khimaira bootstrap` / `khimaira sync` — profile-driven setup.

Bootstrap is first-run on a fresh machine: clone dotfiles, apply
symlinks, clone sibling repos, install MCP servers, install supervisor,
build SPA. Sync is the ongoing flow: pull the dotfiles repo, re-apply
the manifest. Both are idempotent.

The profile is YAML the dev maintains in their own git repo (typically
alongside dotfiles). See khimaira/bootstrap/schema.py for the grammar
and khimaira/bootstrap/default_profile.yaml for the khimaira-only baseline.
"""

from __future__ import annotations

import argparse
import sys

from khimaira.bootstrap import dump_profile_json, load_profile, ProfileError
from khimaira.bootstrap.runner import RunReport, check_bootstrap, run_bootstrap, run_sync

# Status → terminal-friendly glyph. Keep narrow (1 char + space) so output
# columns align cleanly when the user pipes through `column` or similar.
_GLYPH = {
    "created": "✨",
    "updated": "🔄",
    "unchanged": "·",
    "skipped": "—",
    "failed": "✗",
}


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p_boot = subparsers.add_parser(
        "bootstrap",
        help="Set up a fresh machine from a profile manifest (dotfiles + repos + MCP servers).",
        description=(
            "Reads a profile YAML and applies it: clones dotfiles, "
            "creates symlinks, clones sibling MCP-server repos, runs "
            "their install commands, registers each with Claude Code, "
            "optionally installs the supervisor + builds the SPA. "
            "Idempotent — safe to re-run.\n\n"
            "Profile resolution order: --profile arg, then "
            "KHIMAIRA_PROFILE env, then ~/.config/khimaira/profile.yaml, "
            "then the built-in default (khimaira-only)."
        ),
    )
    p_boot.add_argument(
        "--profile",
        help="Path or http(s) URL of the profile YAML.",
    )
    p_boot.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the resolved profile + planned operations, do nothing.",
    )
    p_boot.add_argument(
        "--check",
        action="store_true",
        help=(
            "Read-only drift report — show what bootstrap WOULD do "
            "without applying any changes. Different from --dry-run "
            "(which prints the parsed profile): --check inspects "
            "local state for each operation and reports current vs "
            "would-create / would-update. Safe on any machine."
        ),
    )
    p_boot.add_argument(
        "--force",
        action="store_true",
        help="Wipe non-git dirs that block a clone; re-register MCP servers that already exist.",
    )
    p_boot.set_defaults(func=_run_bootstrap)

    p_sync = subparsers.add_parser(
        "sync",
        help="Pull dotfiles + re-apply the profile manifest (ongoing cross-machine sync).",
        description=(
            "After the initial `khimaira bootstrap`, run `khimaira sync` "
            "to pick up profile changes (new symlinks, new MCP servers, "
            "etc.). Pulls the dotfiles repo, re-applies the manifest. "
            "Idempotent — only changes what's actually drifted."
        ),
    )
    p_sync.add_argument(
        "--profile",
        help="Path or http(s) URL of the profile YAML (same resolution as bootstrap).",
    )
    p_sync.add_argument(
        "--force",
        action="store_true",
        help="Re-register MCP servers even if Claude Code already lists them.",
    )
    p_sync.set_defaults(func=_run_sync)


def _load(args: argparse.Namespace) -> int | None:
    """Resolve + load the profile. Returns None on success (caller continues
    with self.profile); returns an exit code on failure."""
    try:
        profile, source_desc = load_profile(args.profile)
    except ProfileError as e:
        print(f"khimaira bootstrap: {e}", file=sys.stderr)
        return 2
    args.profile_obj = profile
    args.profile_source = source_desc
    return None


def _print_header(args: argparse.Namespace, action: str) -> None:
    print(
        f"khimaira {action}: profile = {args.profile_obj.name} (from {args.profile_source})"
    )
    if args.profile_obj.description:
        # Print the first line of the description for context.
        first_line = args.profile_obj.description.split("\n")[0].strip()
        if first_line:
            print(f"  {first_line}")


def _print_report(report: RunReport, *, action: str = "apply") -> None:
    """Render every op result + a summary tail. Failed ops bubble up first
    so the user spots them without scrolling. Unchanged ops are quiet
    by default; included so the user can see "yes, I noticed, no work
    needed."

    `action="check"` swaps `created`/`updated` → `would-create`/
    `would-update` in the summary tail and detail prefix so the user
    isn't misled into thinking anything actually changed.
    """
    failed = [r for r in report.results if r.status == "failed"]
    other = [r for r in report.results if r.status != "failed"]

    # In check mode, an unchanged status means "drift-free" — surface
    # with a calmer glyph than the apply-mode `·` to make the report
    # more legible as a diff at a glance.
    label_for = {
        "created": "would-create" if action == "check" else "created",
        "updated": "would-update" if action == "check" else "updated",
        "unchanged": "current" if action == "check" else "unchanged",
        "skipped": "skipped",
        "failed": "failed",
    }

    for r in failed:
        glyph = _GLYPH.get(r.status, "?")
        print(
            f"  {glyph}  {r.op:<16}  {r.target}  [{label_for['failed']}]",
            file=sys.stderr,
        )
        if r.detail:
            print(f"      ↳ {r.detail}", file=sys.stderr)

    for r in other:
        glyph = _GLYPH.get(r.status, "?")
        label = label_for.get(r.status, r.status)
        line = f"  {glyph}  {r.op:<16}  {r.target}"
        if action == "check":
            # Always show the would-* tag in check mode — that's
            # the load-bearing info.
            line += f"  [{label}]"
        if r.status != "unchanged" and r.detail:
            line += f"  — {r.detail}"
        print(line)

    summary = report.summary
    parts = []
    for status in ("created", "updated", "unchanged", "skipped", "failed"):
        if status in summary:
            parts.append(f"{summary[status]} {label_for[status]}")
    print(f"\n{', '.join(parts) if parts else 'no operations'}")


def _run_bootstrap(args: argparse.Namespace) -> int:
    rc = _load(args)
    if rc is not None:
        return rc

    if args.dry_run:
        # --dry-run is purely descriptive — what profile was loaded,
        # not what state the machine is in. Use --check for the drift
        # report against actual state.
        _print_header(args, "bootstrap")
        print("\n--dry-run: resolved profile (no operations executed):")
        print(dump_profile_json(args.profile_obj))
        return 0

    if args.check:
        _print_header(args, "bootstrap --check")
        print("\nread-only drift report — nothing applied:")
        report = check_bootstrap(args.profile_obj)
        _print_report(report, action="check")
        # In check mode, "failures" are unrecoverable drift (e.g. non-git
        # dir blocking a clone). Surface as exit 1 so CI can gate on it.
        # `created`/`updated` are NOT failures here — they're informational
        # ("you'd change N things if you ran bootstrap now").
        return 1 if report.had_failures else 0

    _print_header(args, "bootstrap")
    report = run_bootstrap(args.profile_obj, force=args.force)
    _print_report(report)
    return 1 if report.had_failures else 0


def _run_sync(args: argparse.Namespace) -> int:
    rc = _load(args)
    if rc is not None:
        return rc

    _print_header(args, "sync")

    report = run_sync(args.profile_obj, force=args.force)
    _print_report(report)
    return 1 if report.had_failures else 0
