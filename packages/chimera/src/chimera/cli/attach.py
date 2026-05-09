"""`chimera attach <project>` / `chimera detach <project>`.

Inject the chimera_observer package into a project's venv site-packages so
its LangGraph runs auto-emit heartbeats to chimera-monitor — without
modifying any committed source in the project. The injected files live
in the venv (which is gitignored everywhere), so production builds (which
recreate the venv from a manifest) don't see them.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path

from chimera.attach import (
    AttachResult,
    attach_project,
    detach_project,
    is_attached,
    list_attached,
    record_attach,
    record_detach,
)
from chimera.attach.inject import VenvNotFound


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p_attach = subparsers.add_parser(
        "attach",
        help="Inject chimera_observer into a project's venv (zero app changes).",
        description=(
            "Drops chimera_observer + .pth file into the project's venv "
            "site-packages so its LangGraph runs auto-emit heartbeats to "
            "chimera-monitor. App writes no code, sets no env vars, installs "
            "no packages. Idempotent."
        ),
    )
    p_attach.add_argument(
        "project_path", nargs="?", default=".",
        help="Project root (default: current dir). Must contain a venv.",
    )
    p_attach.add_argument(
        "--force", action="store_true",
        help="Rewrite files even if already up-to-date.",
    )
    p_attach.add_argument(
        "--label", default="",
        help="Friendly label for the project (default: directory name).",
    )
    p_attach.add_argument(
        "--no-register", action="store_true",
        help="Skip adding to the auto-reattach registry — one-shot inject only.",
    )
    p_attach.set_defaults(func=run_attach)

    p_detach = subparsers.add_parser(
        "detach",
        help="Remove chimera_observer from a project's venv.",
    )
    p_detach.add_argument("project_path", nargs="?", default=".")
    p_detach.add_argument(
        "--keep-registry", action="store_true",
        help="Remove files but keep registry entry (auto-reattach will reinstall).",
    )
    p_detach.set_defaults(func=run_detach)

    p_list = subparsers.add_parser(
        "attached",
        help="List projects currently attached for chimera observability.",
    )
    p_list.set_defaults(func=run_list)


def run_attach(args: argparse.Namespace) -> int:
    project = Path(args.project_path).expanduser().resolve()

    try:
        result: AttachResult = attach_project(project, force=args.force)
    except VenvNotFound as exc:
        print(f"[chimera attach] {exc}", flush=True)
        return 2
    except FileNotFoundError as exc:
        print(f"[chimera attach] {exc}", flush=True)
        return 2

    if not args.no_register:
        record_attach(result.project_path, result.venv_path, label=args.label)

    print(
        f"✅ chimera observer attached to {result.project_path}\n"
        f"   venv: {result.venv_path}\n"
        f"   site-packages: {result.site_packages}"
    )
    if result.reason:
        print(f"   ({result.reason})")

    # Helpful nudge on first attach
    if (
        not _seems_gitignored(result.venv_path, result.project_path)
        and (result.project_path / ".git").exists()
    ):
        print(
            f"\n⚠️  Heads up: {result.venv_path.name}/ may not be in this project's "
            ".gitignore. Add it before committing — chimera_observer files "
            "shouldn't reach production."
        )

    print(
        "\nThe app's LangGraph runs will now emit heartbeats to chimera-monitor "
        "on its next start. Restart the app to pick up the observer."
    )
    return 0


def run_detach(args: argparse.Namespace) -> int:
    project = Path(args.project_path).expanduser().resolve()
    try:
        result = detach_project(project)
    except VenvNotFound as exc:
        print(f"[chimera detach] {exc}", flush=True)
        return 2

    if not args.keep_registry:
        record_detach(result.project_path)

    if result.pth_written or result.package_written:
        print(f"✅ chimera observer removed from {result.project_path}")
    else:
        print(f"(nothing to remove — observer wasn't present at {result.site_packages})")
    return 0


def run_list(_args: argparse.Namespace) -> int:
    entries = list_attached()
    if not entries:
        print("No projects attached. Run `chimera attach <path>` to add one.")
        return 0

    print(f"{len(entries)} attached project(s):\n")
    for e in entries:
        project = Path(e.get("project_path", "?"))
        venv = Path(e.get("venv_path", "?"))
        label = e.get("label") or project.name
        # Verify the observer is actually there right now
        present = is_attached(venv) if venv.exists() else False
        marker = "✅ present" if present else "❌ MISSING (will re-inject when daemon detects)"
        print(f"  • {label}  [{marker}]")
        print(f"      project: {project}")
        print(f"      venv:    {venv}")
        print(f"      attached: {e.get('attached_at', '?')}")
        print()
    return 0


def _seems_gitignored(venv_path: Path, project_path: Path) -> bool:
    """Cheap check for the venv name appearing in .gitignore lines."""
    gitignore = project_path / ".gitignore"
    if not gitignore.is_file():
        return False
    try:
        text = gitignore.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    venv_name = venv_path.name
    needles = (venv_name, venv_name + "/", "/" + venv_name, "/" + venv_name + "/")
    return any(n in text for n in needles)
