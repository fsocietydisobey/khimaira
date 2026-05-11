"""`chimera doctor` — diagnostic of the dev's environment.

Reports what chimera can see:
  - Which CLI runners are installed (Claude Code, Codex, Gemini, Ollama, llm)
  - Whether at least one is usable
  - Privacy mode (CHIMERA_LOCAL_ONLY)
  - Routing-table source (defaults / user / project)

Exits 0 when at least one runner works. Non-zero when chimera can't dispatch
anything — which is the failure mode `doctor` exists to detect.
"""

from __future__ import annotations

import argparse
import os

from chimera.config import is_local_only_mode
from chimera.dispatch.runners import RUNNERS, available_runners


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "doctor",
        help="Diagnose your chimera environment — which runners, which mode.",
    )
    p.set_defaults(func=run)


def run(_args: argparse.Namespace) -> int:
    print("chimera doctor")
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
    print(f"  privacy (CHIMERA_LOCAL_ONLY): {'on' if is_local_only_mode() else 'off'}")

    # Monitor daemon + supervisor status
    print("\nObservability daemon:")
    _check_monitor_status()

    # Env vars worth surfacing
    relevant_env = [
        "CHIMERA_CLAUDE_CMD",
        "CHIMERA_CLAUDE_MODEL",
        "CHIMERA_CODEX_CMD",
        "CHIMERA_CODEX_MODEL",
        "CHIMERA_GEMINI_CMD",
        "CHIMERA_GEMINI_MODEL",
        "CHIMERA_OLLAMA_CMD",
        "CHIMERA_OLLAMA_MODEL",
        "CHIMERA_LLM_CMD",
        "CHIMERA_LLM_MODEL",
        "CHIMERA_LOCAL_ONLY",
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
            f"✅ chimera is operational ({sum(avail.values())}/{len(avail)} runners installed)."
        )
        if not avail.get("ollama"):
            print(
                "   Tip: install Ollama for free local fallback — "
                "https://ollama.com/download"
            )
        return 0
    print("❌ NO runners installed. chimera cannot dispatch any tasks.")
    print("   Install at least one of:")
    print("     • Claude Code:  https://claude.com/claude-code")
    print("     • Codex CLI:    npm install -g @openai/codex")
    print("     • Gemini CLI:   npm install -g @google/gemini-cli")
    print("     • Ollama:       https://ollama.com/download")
    print("     • llm:          pip install llm")
    return 1


def _check_monitor_status() -> None:
    """Surface chimera-monitor daemon state + supervisor recommendation.

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
        print("  ❌ chimera-monitor daemon NOT running on 127.0.0.1:8740")
        print("     Start with: `chimera monitor start`")
        print("     For auto-start + auto-restart on crash:")
        print(
            "       `chimera monitor install-service --enable` (systemd on Linux, launchd on macOS)"
        )
        print("       `chimera monitor watch` (cross-platform foreground fallback)")
        return

    # 2. Is there a supervisor watching it?
    supervisor_active = False
    supervisor_name = ""
    if sys.platform == "linux" and shutil.which("systemctl"):
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "chimera-monitor"],
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
                ["launchctl", "list", "com.chimera.monitor"],
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
            print("     logs: `journalctl --user -u chimera-monitor -f`")
        elif supervisor_name == "launchd":
            print("     logs: ~/Library/Logs/chimera-monitor.{out,err}.log")
        return

    # Daemon up but no supervisor — the failure class users hit most
    print("  ⚠️  daemon up but NOT supervised — silent death class still possible")
    if sys.platform in ("linux", "darwin"):
        backend = (
            "systemd user unit" if sys.platform == "linux" else "launchd LaunchAgent"
        )
        print("     Recommended: `chimera monitor install-service --enable`")
        print(f"     (writes a {backend}; daemon auto-restarts on crash + boot)")
    else:
        print("     Recommended: `chimera monitor watch` in a tmux/screen pane")
        print("     (cross-platform fallback; no native supervisor for this OS)")
