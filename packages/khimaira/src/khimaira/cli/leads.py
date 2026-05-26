"""`khimaira leads` subcommand — lead-automation infrastructure.

Subcommands:
  sync <project_name>           Generate role docs + Themis YAML + knowledge seeds
  sync --check <project_name>   Drift detection: regenerate in-memory + diff (exit 1 on drift)
"""

from __future__ import annotations

import argparse
import sys

from khimaira.leads.manifest import load_manifest
from khimaira.leads.sync import check_drift, sync_leads


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "leads",
        help="Lead-automation: generate role docs + Themis rules from the central manifest.",
    )
    sub = p.add_subparsers(dest="leads_cmd", required=True)

    # --- sync ---
    p_sync = sub.add_parser(
        "sync",
        help="Generate role docs + Themis YAML + knowledge seeds from the central manifest.",
        description=(
            "Reads ~/.local/share/khimaira/leads/<project_name>.toml and generates:\n"
            "  - <roles_dir>/<domain>-lead.md  (role doc)\n"
            "  - <themis_dir>/<domain>-lead.yaml  (Themis block rules)\n"
            "  - <knowledge_dir>/<domain>-knowledge.md  (seeded if absent)\n"
            "\n"
            "Directories are relative to root_path in the manifest.\n"
            "Manual blocks (<!-- BEGIN MANUAL -->...<!-- END MANUAL -->) in\n"
            "existing role docs are preserved across regeneration.\n"
            "\n"
            "Use --check for drift detection without writing files."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_sync.add_argument(
        "project_name",
        metavar="PROJECT_NAME",
        help=(
            "Name of the project (looks up "
            "~/.local/share/khimaira/leads/<project_name>.toml)."
        ),
    )
    p_sync.add_argument(
        "--check",
        action="store_true",
        default=False,
        help=(
            "Regenerate in-memory, diff against on-disk files, and "
            "exit 1 if any drift is detected. No files are written."
        ),
    )
    p_sync.set_defaults(func=_run_sync)


def _run_sync(args: argparse.Namespace) -> int:
    project_name = args.project_name

    if args.check:
        print(f"khimaira leads sync --check {project_name}")
        try:
            has_drift, diff_lines = check_drift(project_name)
        except (FileNotFoundError, ValueError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

        if has_drift:
            print("DRIFT DETECTED — generated output differs from on-disk files:")
            for line in diff_lines:
                print(line, end="")
            return 1
        else:
            print("✓ No drift — on-disk files match generated output.")
            return 0

    print(f"khimaira leads sync {project_name}")
    try:
        manifest = load_manifest(project_name)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"  project: {manifest.project_name}")
    print(f"  root:    {manifest.root_path}")
    print(f"  leads:   {', '.join(manifest.leads)}")
    print()

    try:
        summary = sync_leads(project_name)
    except (FileNotFoundError, ValueError, KeyError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    for line in summary:
        print(line)

    print()
    print(f"✓ Synced {len(manifest.leads)} lead(s).")
    return 0


def run(args: argparse.Namespace) -> int:
    return _run_sync(args)
