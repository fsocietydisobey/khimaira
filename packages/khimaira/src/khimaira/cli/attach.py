"""`khimaira attach <project>` / `khimaira detach <project>`.

Inject the khimaira_observer package into a project's venv site-packages so
its LangGraph runs auto-emit heartbeats to khimaira-monitor — without
modifying any committed source in the project. The injected files live
in the venv (which is gitignored everywhere), so production builds (which
recreate the venv from a manifest) don't see them.

First-attach onboarding: on the user's first `khimaira attach`, prompt
about installing a systemd user unit for daemon auto-restart. Persists
a `supervisor_prompted` flag so we only ask once. Skipped in non-TTY
environments (CI / scripts) and suppressible via KHIMAIRA_QUIET_SETUP=1.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from khimaira.attach import (
    AttachResult,
    attach_project,
    detach_project,
    is_attached,
    list_attached,
    record_attach,
    record_detach,
)
from khimaira.attach.inject import VenvNotFound

_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
)
_SETUP_STATE_PATH = _STATE_DIR / "setup-state.json"


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p_attach = subparsers.add_parser(
        "attach",
        help="Inject khimaira_observer into a project's venv (zero app changes).",
        description=(
            "Drops khimaira_observer + .pth file into the project's venv "
            "site-packages so its LangGraph runs auto-emit heartbeats to "
            "khimaira-monitor. App writes no code, sets no env vars, installs "
            "no packages. Idempotent."
        ),
    )
    p_attach.add_argument(
        "project_path",
        nargs="?",
        default=".",
        help="Project root (default: current dir). Must contain a venv.",
    )
    p_attach.add_argument(
        "--force",
        action="store_true",
        help="Rewrite files even if already up-to-date.",
    )
    p_attach.add_argument(
        "--label",
        default="",
        help="Friendly label for the project (default: directory name).",
    )
    p_attach.add_argument(
        "--no-register",
        action="store_true",
        help="Skip adding to the auto-reattach registry — one-shot inject only.",
    )
    p_attach.set_defaults(func=run_attach)

    p_detach = subparsers.add_parser(
        "detach",
        help="Remove khimaira_observer from a project's venv.",
    )
    p_detach.add_argument("project_path", nargs="?", default=".")
    p_detach.add_argument(
        "--keep-registry",
        action="store_true",
        help="Remove files but keep registry entry (auto-reattach will reinstall).",
    )
    p_detach.set_defaults(func=run_detach)

    p_list = subparsers.add_parser(
        "attached",
        help="List projects currently attached for khimaira observability.",
    )
    p_list.set_defaults(func=run_list)


def run_attach(args: argparse.Namespace) -> int:
    project = Path(args.project_path).expanduser().resolve()

    try:
        result: AttachResult = attach_project(project, force=args.force)
    except VenvNotFound as exc:
        print(f"[khimaira attach] {exc}", flush=True)
        return 2
    except FileNotFoundError as exc:
        print(f"[khimaira attach] {exc}", flush=True)
        return 2

    if not args.no_register:
        record_attach(result.project_path, result.venv_path, label=args.label)

    print(
        f"✅ khimaira observer attached to {result.project_path}\n"
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
            ".gitignore. Add it before committing — khimaira_observer files "
            "shouldn't reach production."
        )

    print(
        "\nThe app's LangGraph runs will now emit heartbeats to khimaira-monitor "
        "on its next start. Restart the app to pick up the observer."
    )

    # First-attach onboarding — prompt about installing the systemd unit
    # so the daemon auto-restarts on crash + boot. Only fires once
    # (persisted via setup-state.json) and only in interactive TTY
    # environments (so CI / scripts don't get prompted).
    _maybe_prompt_supervisor_install()

    return 0


def _read_setup_state() -> dict:
    try:
        return json.loads(_SETUP_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_setup_state(state: dict) -> None:
    try:
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = _SETUP_STATE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(state, indent=2), encoding="utf-8")
        tmp.replace(_SETUP_STATE_PATH)
    except OSError:
        pass  # best-effort; failing here doesn't break the attach


def _supervisor_is_active() -> bool:
    """True if a host-native supervisor (systemd on Linux, launchd on macOS)
    is actively running khimaira-monitor."""
    if sys.platform == "linux" and shutil.which("systemctl"):
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "khimaira-monitor"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            return result.stdout.strip() == "active"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False
    if sys.platform == "darwin" and shutil.which("launchctl"):
        try:
            result = subprocess.run(
                ["launchctl", "list", "com.khimaira.monitor"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            return result.returncode == 0
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return False
    return False


# Back-compat alias — old name retained for any external callers.
_systemd_unit_is_active = _supervisor_is_active


def _maybe_prompt_supervisor_install() -> None:
    """Prompt about installing a host-native supervisor on first attach.

    Linux → systemd user unit. macOS → launchd LaunchAgent plist.
    Other platforms → one-line tip pointing at `khimaira monitor watch`.

    Skipped if:
      • KHIMAIRA_QUIET_SETUP=1 (CI / scripting opt-out)
      • Not a TTY (stdout/stdin not interactive — pipes, redirects)
      • Already prompted (setup-state.json has supervisor_prompted=true)
      • Supervisor already active (no need to ask)
    """
    if os.environ.get("KHIMAIRA_QUIET_SETUP"):
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return

    state = _read_setup_state()
    if state.get("supervisor_prompted"):
        return

    # Already supervised? Record the flag and move on without prompting.
    if _supervisor_is_active():
        state["supervisor_prompted"] = True
        state["supervisor_status_at_first_attach"] = "already_active"
        _write_setup_state(state)
        return

    if sys.platform not in ("linux", "darwin"):
        # Windows / BSD / etc — no native supervisor we can install.
        print(
            "\n💡 first-attach tip: khimaira-monitor has no auto-restart "
            "supervisor on this platform yet. For long-running use, run "
            "`khimaira monitor watch` in a tmux/screen pane."
        )
        state["supervisor_prompted"] = True
        state["supervisor_status_at_first_attach"] = "unsupported_platform"
        _write_setup_state(state)
        return

    if sys.platform == "linux" and not shutil.which("systemctl"):
        # Linux but no systemd (alpine, busybox)
        print(
            "\n💡 first-attach tip: khimaira-monitor has no auto-restart "
            "supervisor active. systemctl not found; use "
            "`khimaira monitor watch` for a cross-platform fallback."
        )
        state["supervisor_prompted"] = True
        state["supervisor_status_at_first_attach"] = "no_systemctl"
        _write_setup_state(state)
        return

    if sys.platform == "darwin" and not shutil.which("launchctl"):
        # macOS but launchctl missing (rare; usually means a broken PATH)
        print(
            "\n💡 first-attach tip: launchctl not found on PATH. Use "
            "`khimaira monitor watch` for a cross-platform fallback."
        )
        state["supervisor_prompted"] = True
        state["supervisor_status_at_first_attach"] = "no_launchctl"
        _write_setup_state(state)
        return

    # Interactive Linux/macOS with native supervisor available — prompt.
    backend_name = (
        "systemd user service" if sys.platform == "linux" else "launchd LaunchAgent"
    )
    print(
        f"\n💡 First-attach setup question:\n"
        f"   The khimaira-monitor daemon should stay running across crashes\n"
        f"   and reboots so observability stays live. Install a {backend_name}\n"
        f"   for auto-restart? (Recommended.)"
    )
    try:
        answer = input("   Install? [Y/n]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        # User aborted — don't mark as prompted; they may want it next time
        print()  # cosmetic newline after ^C
        return

    state["supervisor_prompted"] = True

    if answer in ("", "y", "yes"):
        # `install-service` dispatches by platform internally (systemd vs launchd).
        try:
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "khimaira.cli",
                    "monitor",
                    "install-service",
                    "--enable",
                ],
                check=True,
            )
            state["supervisor_status_at_first_attach"] = "installed"
            if sys.platform == "linux":
                print(
                    "   ✅ systemd unit installed + enabled. "
                    "Logs: `journalctl --user -u khimaira-monitor -f`"
                )
            else:
                print(
                    "   ✅ launchd plist installed + loaded. "
                    "Logs: ~/Library/Logs/khimaira-monitor.{out,err}.log"
                )
        except subprocess.CalledProcessError as e:
            state["supervisor_status_at_first_attach"] = "install_failed"
            print(
                f"   ⚠️  install-service failed ({e}). "
                f"You can retry manually: "
                f"`khimaira monitor install-service --enable`"
            )
    else:
        state["supervisor_status_at_first_attach"] = "declined"
        print(
            "   Skipped. You can install later with:\n"
            "     `khimaira monitor install-service --enable` (native supervisor)\n"
            "     `khimaira monitor watch` (cross-platform foreground)"
        )

    _write_setup_state(state)


def run_detach(args: argparse.Namespace) -> int:
    project = Path(args.project_path).expanduser().resolve()
    try:
        result = detach_project(project)
    except VenvNotFound as exc:
        print(f"[khimaira detach] {exc}", flush=True)
        return 2

    if not args.keep_registry:
        record_detach(result.project_path)

    if result.pth_written or result.package_written:
        print(f"✅ khimaira observer removed from {result.project_path}")
    else:
        print(
            f"(nothing to remove — observer wasn't present at {result.site_packages})"
        )
    return 0


def run_list(_args: argparse.Namespace) -> int:
    entries = list_attached()
    if not entries:
        print("No projects attached. Run `khimaira attach <path>` to add one.")
        return 0

    print(f"{len(entries)} attached project(s):\n")
    for e in entries:
        project = Path(e.get("project_path", "?"))
        venv = Path(e.get("venv_path", "?"))
        label = e.get("label") or project.name
        # Verify the observer is actually there right now
        present = is_attached(venv) if venv.exists() else False
        marker = (
            "✅ present"
            if present
            else "❌ MISSING (will re-inject when daemon detects)"
        )
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
