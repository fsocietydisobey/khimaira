"""`khimaira monitor` CLI — start / stop / status subcommands."""

import argparse
import os
import signal
import socket
import sys
import time
import webbrowser
from pathlib import Path

from .paths import LOG_FILE, PID_FILE, ensure_dirs

DEFAULT_PORT = 8740


def _port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        try:
            sock.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None
    return pid


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _port() -> int:
    raw = os.environ.get("KHIMAIRA_MONITOR_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"khimaira monitor: invalid KHIMAIRA_MONITOR_PORT={raw!r}", file=sys.stderr)
        sys.exit(1)


def _cmd_start(args: argparse.Namespace) -> int:
    existing = _read_pid()
    if existing and _alive(existing):
        print(
            f"khimaira monitor already running (PID {existing}) — http://127.0.0.1:{_port()}"
        )
        if not args.no_browser:
            webbrowser.open(f"http://127.0.0.1:{_port()}")
        return 0

    if existing:
        # Stale PID file
        PID_FILE.unlink(missing_ok=True)

    ensure_dirs()
    from .daemon import daemonize_and_serve

    port = _port()
    if args.foreground:
        # Useful for debugging — runs in this process, no fork.
        from .server import serve

        serve(port=port)
        return 0

    pid = daemonize_and_serve(port=port)
    # Wait for uvicorn to bind, not just for the PID to exist. The auto-build
    # step in serve() can take a while on first run, so allow up to 60s
    # before giving up.
    deadline = time.time() + 60.0
    bound = False
    while time.time() < deadline:
        if not _alive(pid):
            print(
                "khimaira monitor: daemon exited before binding — check logs:",
                LOG_FILE,
                file=sys.stderr,
            )
            return 1
        if _port_is_listening(port):
            bound = True
            break
        time.sleep(0.1)

    if not bound:
        print(
            f"khimaira monitor: daemon (PID {pid}) didn't bind to port {port} — check logs:",
            LOG_FILE,
            file=sys.stderr,
        )
        return 1

    print(f"khimaira monitor started (PID {pid}) — http://127.0.0.1:{port}")
    print(f"logs: {LOG_FILE}")
    _maybe_nudge_about_supervisor()
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}")
    return 0


def _maybe_nudge_about_supervisor() -> None:
    """One-line tip on `khimaira monitor start` if no supervisor is active.

    Closes the gap that bit Joseph repeatedly: daemon was up, then
    died (OOM / SIGKILL / something), nothing restarted it, so the
    whole stack silently broke. With systemd unit active, dies are
    auto-recovered within RestartSec=5.

    Suppressible via KHIMAIRA_QUIET_NUDGE=1 for scripted environments.
    """
    if os.environ.get("KHIMAIRA_QUIET_NUDGE"):
        return
    import shutil
    import subprocess

    if sys.platform == "linux":
        if not shutil.which("systemctl"):
            return  # systemd not available — skip nudge
        try:
            result = subprocess.run(
                ["systemctl", "--user", "is-active", "khimaira-monitor"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if result.stdout.strip() == "active":
                return  # supervised already; no nudge needed
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return  # If we can't tell, don't nudge — might be misleading
    elif sys.platform == "darwin":
        if not shutil.which("launchctl"):
            return
        try:
            result = subprocess.run(
                ["launchctl", "list", "com.khimaira.monitor"],
                capture_output=True,
                text=True,
                timeout=1.5,
            )
            if result.returncode == 0:
                return  # already loaded — no nudge needed
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return
    else:
        # Other platforms: no native supervisor, point at the foreground watcher.
        print("tip: `khimaira monitor watch` in a tmux/screen pane for auto-restart")
        return

    print(
        "tip: `khimaira monitor install-service --enable` to auto-restart on crash + boot"
    )


def _cmd_restart(args: argparse.Namespace) -> int:
    """Stop the daemon (if running) then start. Tolerates no-PID-file."""
    import time

    # Best-effort stop — don't bail if there's nothing running.
    try:
        _cmd_stop(args)
    except SystemExit:
        pass
    # Brief pause so OS-level socket cleanup completes before rebind.
    time.sleep(1)
    return _cmd_start(args)


def _cmd_stop(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid is None:
        print("khimaira monitor: no PID file — not running")
        return 0
    if not _alive(pid):
        print(f"khimaira monitor: stale PID file (PID {pid} not alive) — cleaning up")
        PID_FILE.unlink(missing_ok=True)
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _alive(pid):
            PID_FILE.unlink(missing_ok=True)
            print(f"khimaira monitor stopped (PID {pid})")
            return 0
        time.sleep(0.1)

    print(
        f"khimaira monitor: PID {pid} did not exit on SIGTERM, sending SIGKILL",
        file=sys.stderr,
    )
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)
    return 0


def _cmd_rescan(args: argparse.Namespace) -> int:
    """Force a metadata rescan for one project (or all). Runs synchronously
    in this process; doesn't touch the running daemon. Useful when the
    project's architecture has shifted and the cached metadata is wrong."""
    import asyncio

    from khimaira.config import ROOTS

    from .discovery.project import discover
    from .metadata.scan import scan_project

    projects = discover(ROOTS)
    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            print(
                f"khimaira monitor: no project named {args.project!r} in roots",
                file=sys.stderr,
            )
            return 1

    if not projects:
        print("khimaira monitor: no langgraph projects discovered")
        return 0

    async def _run_all() -> int:
        ok = 0
        for p in projects:
            print(f"scanning {p.name}…", flush=True)
            metadata = await scan_project(p.name, p.path)
            if metadata is not None:
                print(f"  ✓ {p.name}: {len(metadata.graphs)} graphs enriched")
                ok += 1
            else:
                print(f"  ✗ {p.name}: scan failed (see daemon logs for details)")
        return 0 if ok == len(projects) else 1

    return asyncio.run(_run_all())


def _cmd_status(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid is None:
        print("khimaira monitor: not running")
        return 1
    if not _alive(pid):
        print(f"khimaira monitor: stale PID file (PID {pid} not alive)")
        return 1
    print(f"khimaira monitor: running (PID {pid}) — http://127.0.0.1:{_port()}")
    print(f"logs: {LOG_FILE}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Supervisor — run the daemon in foreground, restart on non-zero exit.

    For environments without systemd (or for users who don't want to
    install a system service). Use `khimaira monitor install-service`
    for the long-running production answer; this is the cross-platform
    fallback.

    Exponential backoff between restarts: 1s, 2s, 4s, 8s, ..., capped
    at 60s. Resets to 1s after the daemon stays up for >5 minutes
    (healthy run signal — flapping doesn't masquerade as stable).

    Ctrl-C exits cleanly; daemon receives SIGTERM, watcher waits for
    graceful exit, then returns.
    """
    import os
    import signal
    import subprocess
    import sys
    import time

    backoff = 1.0
    max_backoff = 60.0
    healthy_threshold = 300.0  # 5 min uptime = reset backoff
    cmd = [
        sys.executable,
        "-m",
        "khimaira.cli",
        "monitor",
        "start",
        "--foreground",
        "--no-browser",
    ]

    print(f"khimaira monitor watch: supervising — Ctrl-C to stop")
    print(f"  command: {' '.join(cmd)}")
    print(f"  logs: {LOG_FILE}")

    child: subprocess.Popen | None = None
    interrupted = {"flag": False}

    def _sigint_handler(signum, frame):  # noqa: ARG001
        interrupted["flag"] = True
        if child and child.poll() is None:
            try:
                child.send_signal(signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass

    signal.signal(signal.SIGINT, _sigint_handler)
    signal.signal(signal.SIGTERM, _sigint_handler)

    while not interrupted["flag"]:
        start_ts = time.time()
        try:
            child = subprocess.Popen(cmd)
        except OSError as e:
            print(
                f"khimaira monitor watch: spawn failed ({e}); retrying in {backoff:.0f}s",
                file=sys.stderr,
            )
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue

        rc = child.wait()
        uptime = time.time() - start_ts

        if interrupted["flag"]:
            print(f"khimaira monitor watch: interrupted; daemon exited with rc={rc}")
            return 0

        if rc == 0:
            print(
                f"khimaira monitor watch: daemon exited cleanly (rc=0); not restarting"
            )
            return 0

        # Reset backoff if the daemon ran healthy for a while
        if uptime >= healthy_threshold:
            backoff = 1.0

        print(
            f"khimaira monitor watch: daemon died (rc={rc}, uptime={uptime:.0f}s); "
            f"restarting in {backoff:.0f}s — see {LOG_FILE} for cause",
            file=sys.stderr,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)

    return 0


_LAUNCHD_LABEL = "com.khimaira.monitor"


def _systemd_unit_path() -> Path:
    """User-scoped systemd unit file path."""
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "systemd" / "user" / "khimaira-monitor.service"


def _launchd_plist_path() -> Path:
    """User-scoped launchd LaunchAgent plist path (macOS)."""
    return Path(os.path.expanduser(f"~/Library/LaunchAgents/{_LAUNCHD_LABEL}.plist"))


def _launchd_plist_content() -> str:
    """Render the launchd LaunchAgent plist.

    Mirrors the systemd unit's behavior:
      - Restart on failure (KeepAlive with SuccessfulExit=false so we
        treat both crashes AND clean exits as restart-worthy, matching
        the systemd unit's intent).
      - Run at user login (RunAtLoad).
      - Log stdout/stderr to ~/Library/Logs/khimaira-monitor.{out,err}.log.

    ProgramArguments uses `uv --directory <workspace> run khimaira monitor
    start --foreground --no-browser` when uv is available, mirroring the
    systemd path. Falls back to the current Python interpreter.
    """
    import shutil
    import sys

    uv = shutil.which("uv")
    # __file__ = .../khimaira/packages/khimaira/src/khimaira/monitor/cli.py
    # parents[5] = khimaira/ workspace root
    workspace_root = str(Path(__file__).resolve().parents[5])
    if uv:
        program_args = [
            uv,
            "--directory",
            workspace_root,
            "run",
            "khimaira",
            "monitor",
            "start",
            "--foreground",
            "--no-browser",
        ]
    else:
        program_args = [
            sys.executable,
            "-m",
            "khimaira.cli",
            "monitor",
            "start",
            "--foreground",
            "--no-browser",
        ]

    log_dir = os.path.expanduser("~/Library/Logs")
    args_xml = "\n".join(f"        <string>{a}</string>" for a in program_args)

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{_LAUNCHD_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
{args_xml}
    </array>

    <key>RunAtLoad</key>
    <true/>

    <!-- Restart on both crash AND clean exit — khimaira daemon's normal
         exit path is shutdown, so rc=0 mid-day means something killed
         it. Throttle so a stuck-loop crash doesn't spin. -->
    <key>KeepAlive</key>
    <dict>
        <key>SuccessfulExit</key>
        <false/>
    </dict>
    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>StandardOutPath</key>
    <string>{log_dir}/khimaira-monitor.out.log</string>
    <key>StandardErrorPath</key>
    <string>{log_dir}/khimaira-monitor.err.log</string>

    <key>ProcessType</key>
    <string>Background</string>
</dict>
</plist>
"""


def _cmd_install_launchd(args: argparse.Namespace) -> int:
    """Install a launchd LaunchAgent plist (macOS) so the daemon auto-
    starts on login + auto-restarts on failure. macOS-only analog of
    `install-service` on Linux.
    """
    import subprocess
    import sys

    if sys.platform != "darwin":
        print(
            f"khimaira monitor install-launchd: launchd is macOS-only "
            f"(detected {sys.platform}). On Linux use `install-service`; "
            f"elsewhere use `khimaira monitor watch`.",
            file=sys.stderr,
        )
        return 1

    if not shutil_which("launchctl"):
        print(
            "khimaira monitor install-launchd: launchctl not found on PATH. "
            "Use `khimaira monitor watch` instead.",
            file=sys.stderr,
        )
        return 1

    plist_path = _launchd_plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    content = _launchd_plist_content()

    if plist_path.exists() and not args.force:
        existing = plist_path.read_text(encoding="utf-8")
        if existing == content:
            print(
                f"khimaira monitor: plist already installed and current — {plist_path}"
            )
        else:
            print(
                f"khimaira monitor: plist exists at {plist_path} but contents differ. "
                f"Re-run with --force to overwrite.",
                file=sys.stderr,
            )
            return 1
    else:
        plist_path.write_text(content, encoding="utf-8")
        print(f"khimaira monitor: wrote plist → {plist_path}")

    if args.enable:
        # Unload any prior version first — bootstrap fails if already loaded.
        # `2>/dev/null` not available via subprocess.run, so swallow stderr
        # by capturing it; we don't care about errors here.
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        try:
            subprocess.run(
                ["launchctl", "load", "-w", str(plist_path)],
                check=True,
            )
            print(
                f"khimaira monitor: loaded + started — "
                f"logs in ~/Library/Logs/khimaira-monitor.{{out,err}}.log"
            )
        except subprocess.CalledProcessError as e:
            print(f"khimaira monitor: launchctl load failed: {e}", file=sys.stderr)
            return 1
    else:
        print(
            "khimaira monitor: plist installed but not loaded. To start now:\n"
            f"  launchctl load -w {plist_path}\n"
            "Or re-run with --enable."
        )
    return 0


def _cmd_uninstall_launchd(args: argparse.Namespace) -> int:
    """Unload + remove the launchd LaunchAgent plist (macOS)."""
    import subprocess
    import sys

    if sys.platform != "darwin":
        print(
            f"khimaira monitor uninstall-launchd: launchd is macOS-only.",
            file=sys.stderr,
        )
        return 1

    plist_path = _launchd_plist_path()
    if not plist_path.exists():
        print(f"khimaira monitor: no plist at {plist_path} — nothing to uninstall")
        return 0

    # Best-effort unload; suppress errors if it wasn't loaded.
    if shutil_which("launchctl"):
        subprocess.run(
            ["launchctl", "unload", str(plist_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )

    plist_path.unlink(missing_ok=True)
    print(f"khimaira monitor: removed {plist_path}")
    return 0


def _systemd_unit_content() -> str:
    """Render the systemd user unit. Uses the current Python interpreter
    + khimaira installation discovered at write time. Re-run install-service
    if you reinstall khimaira in a different venv."""
    import shutil
    import sys

    # Prefer `uv run` if available (handles workspace + lockfile resolution
    # transparently); fall back to direct python -m if not.
    uv = shutil.which("uv")
    # __file__ = .../khimaira/packages/khimaira/src/khimaira/monitor/cli.py
    # parents[5] = khimaira/ (workspace root, where pyproject.toml lives)
    workspace_root = str(Path(__file__).resolve().parents[5])
    if uv:
        exec_start = (
            f"{uv} --directory {workspace_root} run khimaira monitor start "
            f"--foreground --no-browser"
        )
    else:
        exec_start = (
            f"{sys.executable} -m khimaira.cli monitor start --foreground --no-browser"
        )

    return f"""[Unit]
Description=khimaira-monitor — local LangGraph observability daemon
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5
# Restart even on clean exits (rc=0) — khimaira daemon's normal exit
# path is shutdown, so seeing rc=0 mid-day means something killed it.
# Override to "on-failure" if you want the daemon to STAY stopped on
# clean exits.
# Environment=KHIMAIRA_MONITOR_PORT=8740

# Resource limits (optional; uncomment if you see OOM-related failures)
# MemoryMax=2G
# CPUQuota=200%

# Log to systemd journal — view with `journalctl --user -u khimaira-monitor -f`
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=default.target
"""


def _cmd_install_service(args: argparse.Namespace) -> int:
    """Install a host-native service so the daemon auto-starts on login
    + auto-restarts on failure.

    Dispatches by platform:
      - Linux → systemd user unit
      - macOS → launchd LaunchAgent plist (via install-launchd)
      - Other → suggest `khimaira monitor watch` (foreground supervisor)
    """
    import subprocess
    import sys

    if sys.platform == "darwin":
        return _cmd_install_launchd(args)

    if sys.platform != "linux":
        print(
            f"khimaira monitor install-service: no native supervisor "
            f"available for {sys.platform}. Use `khimaira monitor watch` "
            f"as a cross-platform fallback supervisor.",
            file=sys.stderr,
        )
        return 1

    if not shutil_which("systemctl"):
        print(
            "khimaira monitor install-service: systemctl not found on PATH. "
            "Use `khimaira monitor watch` instead.",
            file=sys.stderr,
        )
        return 1

    unit_path = _systemd_unit_path()
    unit_path.parent.mkdir(parents=True, exist_ok=True)
    content = _systemd_unit_content()

    if unit_path.exists() and not args.force:
        existing = unit_path.read_text(encoding="utf-8")
        if existing == content:
            print(f"khimaira monitor: unit already installed and current — {unit_path}")
        else:
            print(
                f"khimaira monitor: unit exists at {unit_path} but contents differ. "
                f"Re-run with --force to overwrite.",
                file=sys.stderr,
            )
            return 1
    else:
        unit_path.write_text(content, encoding="utf-8")
        print(f"khimaira monitor: wrote unit → {unit_path}")

    # Reload + enable + start
    try:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=True)
    except (subprocess.CalledProcessError, FileNotFoundError) as e:
        print(f"khimaira monitor: systemctl daemon-reload failed: {e}", file=sys.stderr)
        return 1

    if args.enable:
        try:
            subprocess.run(
                ["systemctl", "--user", "enable", "--now", "khimaira-monitor"],
                check=True,
            )
            print(
                "khimaira monitor: enabled + started — `journalctl --user -u khimaira-monitor -f` to follow"
            )
        except subprocess.CalledProcessError as e:
            print(f"khimaira monitor: enable failed: {e}", file=sys.stderr)
            return 1
    else:
        print(
            "khimaira monitor: unit installed but not enabled. To start now:\n"
            "  systemctl --user enable --now khimaira-monitor\n"
            "Or re-run with --enable."
        )
    return 0


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    """Remove the host-native service (systemd unit on Linux, launchd
    plist on macOS). Does not stop a currently-running daemon started
    outside the supervisor; use `khimaira monitor stop` for that."""
    import subprocess
    import sys

    if sys.platform == "darwin":
        return _cmd_uninstall_launchd(args)

    if sys.platform != "linux":
        print(
            f"khimaira monitor uninstall-service: no native supervisor "
            f"to uninstall on {sys.platform}.",
            file=sys.stderr,
        )
        return 1

    unit_path = _systemd_unit_path()
    if not unit_path.exists():
        print(f"khimaira monitor: no unit at {unit_path} — nothing to uninstall")
        return 0

    # Best-effort: stop + disable before removing the unit file
    if shutil_which("systemctl"):
        for action in (("disable",), ("stop",)):
            try:
                subprocess.run(
                    ["systemctl", "--user", *action, "khimaira-monitor"],
                    check=False,
                )
            except FileNotFoundError:
                pass

    unit_path.unlink(missing_ok=True)
    print(f"khimaira monitor: removed {unit_path}")

    if shutil_which("systemctl"):
        try:
            subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        except FileNotFoundError:
            pass
    return 0


def shutil_which(cmd: str) -> str | None:
    import shutil

    return shutil.which(cmd)


def _load_env() -> None:
    """Load khimaira's .env so Gemini/Anthropic credentials reach the
    monitor daemon (and the rescan subcommand). Mirrors what
    `khimaira.server.mcp` does at module-import time."""
    from pathlib import Path

    from dotenv import load_dotenv

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    load_dotenv(project_root / ".env")


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(prog="khimaira monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Daemonize the monitor server")
    p_start.add_argument(
        "--foreground", action="store_true", help="Run in foreground (no fork)"
    )
    p_start.add_argument(
        "--no-browser", action="store_true", help="Don't open the browser"
    )
    p_start.set_defaults(func=_cmd_start)

    p_stop = sub.add_parser("stop", help="Stop the monitor daemon")
    p_stop.set_defaults(func=_cmd_stop)

    # Convenience: stop + start in one command. Saves typing during
    # development when monitor backend changes need a fresh daemon
    # to take effect (e.g. after a `git pull`).
    p_restart = sub.add_parser("restart", help="Stop then start the monitor daemon")
    p_restart.add_argument(
        "--foreground", action="store_true", help="Run in foreground (no fork)"
    )
    p_restart.add_argument(
        "--no-browser", action="store_true", help="Don't open the browser"
    )
    p_restart.set_defaults(func=_cmd_restart)

    p_status = sub.add_parser("status", help="Report daemon status")
    p_status.set_defaults(func=_cmd_status)

    p_rescan = sub.add_parser(
        "rescan",
        help="Force a metadata rescan for one project (or all). Manual override.",
    )
    p_rescan.add_argument(
        "project", nargs="?", help="Project name to rescan; omit for all"
    )
    p_rescan.set_defaults(func=_cmd_rescan)

    args = parser.parse_args()
    sys.exit(args.func(args))
