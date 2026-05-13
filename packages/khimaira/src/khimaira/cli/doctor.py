"""`khimaira doctor` — diagnostic of the dev's environment.

Reports what khimaira can see:
  - Which CLI runners are installed (Claude Code, Codex, Gemini, Ollama, llm)
  - Whether at least one is usable
  - Privacy mode (KHIMAIRA_LOCAL_ONLY)
  - Routing-table source (defaults / user / project)

Exits 0 when at least one runner works. Non-zero when khimaira can't dispatch
anything — which is the failure mode `doctor` exists to detect.
"""

from __future__ import annotations

import argparse
import os

from khimaira.config import is_local_only_mode
from khimaira.dispatch.runners import RUNNERS, available_runners


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "doctor",
        help="Diagnose your khimaira environment — runners, daemon, profile drift.",
    )
    p.add_argument(
        "--profile",
        default=None,
        help=(
            "Path or http(s) URL to a khimaira profile YAML to check "
            "drift against. Defaults to the standard resolution: "
            "KHIMAIRA_PROFILE env, ~/.config/khimaira/profile.yaml, "
            "or the shipped khimaira-only baseline. Skip the profile "
            "section by passing --no-profile."
        ),
    )
    p.add_argument(
        "--no-profile",
        action="store_true",
        help="Skip the profile-drift check (runs faster, only checks runners + daemon).",
    )
    p.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    print("khimaira doctor")
    print("=" * 60)

    # Runners
    print("\nCLI runners:")
    avail = available_runners()
    for name, runner in RUNNERS.items():
        marker = "✅" if avail[name] else "❌"
        cmd = getattr(runner, "cmd", "?")
        default_model = getattr(runner, "default_model", "?")
        if avail[name]:
            print(f"  {marker} {name:8s} → {cmd!r:20s}  default model: {default_model}")
        else:
            print(f"  {marker} {name:8s} → {cmd!r:20s}  NOT FOUND")

    # Modes
    print("\nModes:")
    print(f"  privacy (KHIMAIRA_LOCAL_ONLY): {'on' if is_local_only_mode() else 'off'}")

    # Monitor daemon + supervisor status
    print("\nObservability daemon:")
    _check_monitor_status()

    # Profile drift — what would `khimaira bootstrap` need to do?
    if not getattr(args, "no_profile", False):
        print("\nProfile drift:")
        profile_drift_failures = _check_profile_drift(getattr(args, "profile", None))
    else:
        profile_drift_failures = False

    # Env vars worth surfacing
    relevant_env = [
        "KHIMAIRA_CLAUDE_CMD",
        "KHIMAIRA_CLAUDE_MODEL",
        "KHIMAIRA_CODEX_CMD",
        "KHIMAIRA_CODEX_MODEL",
        "KHIMAIRA_GEMINI_CMD",
        "KHIMAIRA_GEMINI_MODEL",
        "KHIMAIRA_OLLAMA_CMD",
        "KHIMAIRA_OLLAMA_MODEL",
        "KHIMAIRA_LLM_CMD",
        "KHIMAIRA_LLM_MODEL",
        "KHIMAIRA_LOCAL_ONLY",
    ]
    set_vars = [(k, os.environ[k]) for k in relevant_env if k in os.environ]
    if set_vars:
        print("\nOverrides set:")
        for k, v in set_vars:
            print(f"  {k}={v}")

    # Verdict
    any_available = any(avail.values())
    print()
    if any_available:
        print(
            f"✅ khimaira is operational ({sum(avail.values())}/{len(avail)} runners installed)."
        )
        if not avail.get("ollama"):
            print(
                "   Tip: install Ollama for free local fallback — "
                "https://ollama.com/download"
            )
        # Non-fatal note about drift so the doctor return code stays
        # 0 when runners are healthy. Drift surfaces as a hint, not
        # a failure — drift is normal mid-iteration; users opt into
        # fixing it with `khimaira bootstrap`.
        if profile_drift_failures:
            print(
                "   ⚠️  Profile drift detected above — run `khimaira bootstrap` "
                "to apply, or `khimaira bootstrap --check` for the full diff."
            )
        return 0
    print("❌ NO runners installed. khimaira cannot dispatch any tasks.")
    print("   Install at least one of:")
    print("     • Claude Code:  https://claude.com/claude-code")
    print("     • Codex CLI:    npm install -g @openai/codex")
    print("     • Gemini CLI:   npm install -g @google/gemini-cli")
    print("     • Ollama:       https://ollama.com/download")
    print("     • llm:          pip install llm")
    return 1


def _check_monitor_status() -> None:
    """Surface khimaira-monitor daemon state + supervisor recommendation.

    Three states worth reporting:
      1. Daemon down → tell user how to start it
      2. Daemon up but no supervisor → recommend install-service so it
         auto-restarts on crash + boot (closes the "daemon died and
         I didn't notice" failure class)
      3. Daemon up AND supervised → all good, mention how to view logs
    """
    import shutil
    import subprocess
    import sys
    import urllib.error
    import urllib.request

    # 1. Is the daemon responding on the loopback port?
    daemon_url = "http://127.0.0.1:8740/api/heartbeats/stats"
    daemon_up = False
    try:
        with urllib.request.urlopen(daemon_url, timeout=1.5) as r:
            daemon_up = r.status == 200
    except (urllib.error.URLError, OSError, TimeoutError):
        daemon_up = False

    if not daemon_up:
        print("  ❌ khimaira-monitor daemon NOT running on 127.0.0.1:8740")
        print("     Start with: `khimaira monitor start`")
        print("     For auto-start + auto-restart on crash:")
        print(
            "       `khimaira monitor install-service --enable` (systemd on Linux, launchd on macOS)"
        )
        print("       `khimaira monitor watch` (cross-platform foreground fallback)")
        return

    # 2. Is there a supervisor watching it?
    supervisor_active = False
    supervisor_name = ""
    if sys.platform == "linux" and shutil.which("systemctl"):
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "khimaira-monitor"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            supervisor_active = result.stdout.strip() == "active"
            supervisor_name = "systemd"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass
    elif sys.platform == "darwin" and shutil.which("launchctl"):
        # `launchctl list <label>` exits 0 and prints the dict when loaded,
        # nonzero otherwise. Cheaper than `print` (which can return blocks).
        try:
            result = subprocess.run(
                ["launchctl", "list", "com.khimaira.monitor"],
                capture_output=True,
                text=True,
                timeout=2.0,
            )
            supervisor_active = result.returncode == 0
            supervisor_name = "launchd"
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            pass

    if supervisor_active:
        print(f"  ✅ daemon up, supervised by {supervisor_name} (auto-restart enabled)")
        if supervisor_name == "systemd":
            print("     logs: `journalctl --user -u khimaira-monitor -f`")
        elif supervisor_name == "launchd":
            print("     logs: ~/Library/Logs/khimaira-monitor.{out,err}.log")
        return

    # Daemon up but no supervisor — the failure class users hit most
    print("  ⚠️  daemon up but NOT supervised — silent death class still possible")
    if sys.platform in ("linux", "darwin"):
        backend = (
            "systemd user unit" if sys.platform == "linux" else "launchd LaunchAgent"
        )
        print("     Recommended: `khimaira monitor install-service --enable`")
        print(f"     (writes a {backend}; daemon auto-restarts on crash + boot)")
    else:
        print("     Recommended: `khimaira monitor watch` in a tmux/screen pane")
        print("     (cross-platform fallback; no native supervisor for this OS)")


def _check_profile_drift(profile_arg: str | None) -> bool:
    """Run a check_bootstrap and surface a compact summary.

    Returns True if drift was detected (would-create / would-update
    rows present, or any failures). Doctor uses the bool to decide
    whether to nudge the user at the bottom.

    Output is intentionally terse — just counts per status + the first
    few drift rows. For the full diff the user runs
    `khimaira bootstrap --check`.
    """
    try:
        from khimaira.bootstrap import ProfileError, load_profile
        from khimaira.bootstrap.runner import check_bootstrap
    except ImportError as e:
        print(f"  ⚠️  can't import khimaira.bootstrap ({e}) — skipping profile check")
        return False

    try:
        profile, source = load_profile(profile_arg)
    except ProfileError as e:
        print(f"  ❌ profile failed to load: {e}")
        return True  # this IS drift the user should know about
    except Exception as e:
        print(f"  ⚠️  unexpected error loading profile: {e}")
        return False

    print(f"  profile: {profile.name}  (from {source})")
    report = check_bootstrap(profile)
    summary = report.summary

    drift_count = summary.get("created", 0) + summary.get("updated", 0)
    current_count = summary.get("unchanged", 0)
    failed_count = summary.get("failed", 0)
    skipped_count = summary.get("skipped", 0)

    if failed_count:
        print(
            f"  ❌ {failed_count} failed — drift is unrecoverable without intervention"
        )
        # Surface the failed rows
        for r in report.results:
            if r.status == "failed":
                print(f"      ✗ {r.op:<16}  {r.target}  — {r.detail}")
    if drift_count == 0 and failed_count == 0:
        print(
            f"  ✅ no drift — {current_count} ops match profile (+ {skipped_count} skipped)"
        )
        return False

    if drift_count:
        creates = summary.get("created", 0)
        updates = summary.get("updated", 0)
        print(
            f"  🔄 {drift_count} drift item(s): "
            f"{creates} would-create, {updates} would-update "
            f"({current_count} current, {skipped_count} skipped)"
        )
        # Show first 3 drift rows for context
        drift_rows = [r for r in report.results if r.status in ("created", "updated")][
            :3
        ]
        for r in drift_rows:
            label = "would-create" if r.status == "created" else "would-update"
            print(f"      • {r.op:<16}  {r.target}  [{label}]")
        if drift_count > 3:
            print(
                f"      … and {drift_count - 3} more — run `khimaira bootstrap --check` for full diff"
            )
    return drift_count > 0 or failed_count > 0
