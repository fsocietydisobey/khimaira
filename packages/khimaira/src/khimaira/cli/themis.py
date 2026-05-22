"""`khimaira themis` subcommand — Themis rule-enforcement management.

Subcommands:
  sync              Re-derive matcher pattern from rule YAMLs; update all attached projects
  disable <rule_id> Fast-rollback: flip rule to audit-only (no block)
  enable  <rule_id> Re-activate a disabled rule
"""

from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

from khimaira.attach.registry import list_attached
from khimaira.attach.settings_hooks import (
    derive_matcher_pattern,
    inject_hook_entry,
    resolve_hook_command,
)

_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
)
_OVERRIDES_PATH = _STATE_DIR / "themis_overrides.jsonl"


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "themis",
        help="Themis rule-enforcement management (sync, disable, enable).",
    )
    sub = p.add_subparsers(dest="themis_cmd", required=True)

    # --- sync ---
    p_sync = sub.add_parser(
        "sync",
        help="Re-derive matcher pattern from rule YAMLs and update attached projects.",
        description=(
            "Scans packages/themis/src/themis/rules/*.yaml for all tool: fields, "
            "unions them into a pipe-separated PreToolUse matcher pattern, and "
            "updates settings.local.json in every attached project. Run after "
            "editing rule YAML files."
        ),
    )
    p_sync.add_argument(
        "--project",
        default=None,
        metavar="PATH",
        help="Update a single project instead of all attached projects.",
    )
    p_sync.set_defaults(func=run_sync)

    # --- disable ---
    p_disable = sub.add_parser(
        "disable",
        help="Fast-rollback: flip a rule to audit-only (logged, not blocked).",
        description=(
            "Appends a disable entry to ~/.local/state/khimaira/themis_overrides.jsonl. "
            "The daemon consults this file on every /api/themis/check call and forces "
            "severity=audit for the named rule_id. Immediate effect — no daemon restart needed. "
            "Use `khimaira themis enable <rule_id>` to revert."
        ),
    )
    p_disable.add_argument("rule_id", help="Rule ID to disable (e.g., IN-INTAKE-1).")
    p_disable.set_defaults(func=run_disable)

    # --- enable ---
    p_enable = sub.add_parser(
        "enable",
        help="Re-activate a previously disabled rule.",
        description=(
            "Appends an enable tombstone to ~/.local/state/khimaira/themis_overrides.jsonl. "
            "The daemon sees the latest entry for the rule_id and reverts to normal enforcement."
        ),
    )
    p_enable.add_argument("rule_id", help="Rule ID to re-enable (e.g., IN-INTAKE-1).")
    p_enable.set_defaults(func=run_enable)

    p.set_defaults(func=_dispatch)


def _dispatch(args: argparse.Namespace) -> int:
    """Top-level dispatch — argparse calls the sub-subcommand's func directly."""
    return args.func(args)


def run_sync(args: argparse.Namespace) -> int:
    """Re-derive matcher and update settings.local.json in all (or one) attached project(s)."""
    matcher = derive_matcher_pattern()
    print(f"Derived matcher: {matcher}")

    if args.project:
        projects = [{"project_path": str(Path(args.project).expanduser().resolve())}]
    else:
        projects = list_attached()

    if not projects:
        print("No attached projects. Run `khimaira attach <path>` first.")
        return 0

    updated = 0
    for entry in projects:
        project = Path(entry.get("project_path", ""))
        if not project.exists():
            print(f"  ⚠️  skipping {project} (not found)")
            continue
        settings_path = project / ".claude" / "settings.local.json"
        try:
            command = resolve_hook_command(project)
            inject_hook_entry(settings_path, matcher, command)
            print(f"  ✅ updated {project.name} ({settings_path})")
            updated += 1
        except Exception as exc:
            print(f"  ⚠️  {project.name}: {exc}")

    print(f"\nSync complete — {updated}/{len(projects)} project(s) updated.")
    return 0


def _append_override(rule_id: str, action: str) -> None:
    """Append a disable/enable record to the overrides JSONL file."""
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "rule_id": rule_id,
        "action": action,
    }
    with _OVERRIDES_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def run_disable(args: argparse.Namespace) -> int:
    """Flip a rule to audit-only by appending a disable record to overrides.jsonl."""
    rule_id = args.rule_id
    _append_override(rule_id, "disable")
    print(
        f"✅ {rule_id} disabled (severity forced to audit).\n"
        f"   Overrides file: {_OVERRIDES_PATH}\n"
        f"   Daemon picks up the change immediately — no restart needed.\n"
        f"   Re-activate with: khimaira themis enable {rule_id}"
    )
    return 0


def run_enable(args: argparse.Namespace) -> int:
    """Re-activate a rule by appending an enable tombstone to overrides.jsonl."""
    rule_id = args.rule_id
    _append_override(rule_id, "enable")
    print(
        f"✅ {rule_id} re-enabled.\n"
        f"   Overrides file: {_OVERRIDES_PATH}\n"
        f"   Normal enforcement active — no daemon restart needed."
    )
    return 0
