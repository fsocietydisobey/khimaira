"""`khimaira proxy` CLI — serve / watch / install-service subcommands."""

from __future__ import annotations

import argparse
import os
import signal
import socket
import sys
import time
from pathlib import Path

from .paths import DEFAULT_PORT, LOG_FILE, PID_FILE, ensure_dirs


def _port() -> int:
    raw = os.environ.get("KHIMAIRA_PROXY_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"khimaira proxy: invalid KHIMAIRA_PROXY_PORT={raw!r}", file=sys.stderr)
        sys.exit(1)


def _port_is_listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect((host, port))
            return True
        except (ConnectionRefusedError, OSError):
            return False


def _read_pid() -> int | None:
    if not PID_FILE.exists():
        return None
    try:
        return int(PID_FILE.read_text().strip())
    except (ValueError, OSError):
        return None


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ImportError:
        pass


def _cmd_serve(args: argparse.Namespace) -> int:
    # _load_env() MUST run before `.server` is imported — server.py reads its
    # config (KHIMAIRA_PROXY_*, backup-creds path, failover flags) into
    # module-level constants at import time, so loading .env afterward would
    # silently freeze those at their no-.env defaults. This is the exact
    # command `khimaira proxy serve` (and its systemd unit) invokes, so this
    # was a live bug, not just a theoretical one.
    _load_env()
    from .server import serve

    serve(port=_port())
    return 0


def _cmd_start(args: argparse.Namespace) -> int:
    _load_env()
    existing = _read_pid()
    if existing and _alive(existing):
        print(f"khimaira proxy already running (PID {existing}) — http://127.0.0.1:{_port()}")
        return 0

    if existing:
        PID_FILE.unlink(missing_ok=True)

    ensure_dirs()
    port = _port()

    if args.foreground:
        from .server import serve

        serve(port=port)
        return 0

    from .daemon import daemonize_and_serve

    pid = daemonize_and_serve(port=port)
    deadline = time.time() + 30.0
    bound = False
    while time.time() < deadline:
        if not _alive(pid):
            print("khimaira proxy: daemon exited before binding — check logs:", LOG_FILE, file=sys.stderr)
            return 1
        if _port_is_listening(port):
            bound = True
            break
        time.sleep(0.1)

    if not bound:
        print(f"khimaira proxy: daemon (PID {pid}) didn't bind to port {port} — check logs:", LOG_FILE, file=sys.stderr)
        return 1

    print(f"khimaira proxy started (PID {pid}) — http://127.0.0.1:{port}")
    print(f"logs: {LOG_FILE}")
    print(f"  Set in sessions: ANTHROPIC_BASE_URL=http://127.0.0.1:{port}  ENABLE_TOOL_SEARCH=1")
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid is None:
        print("khimaira proxy: not running (no PID file)")
        return 0
    if not _alive(pid):
        print(f"khimaira proxy: stale PID file (PID {pid} not alive)")
        PID_FILE.unlink(missing_ok=True)
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 10.0
    while time.time() < deadline:
        if not _alive(pid):
            PID_FILE.unlink(missing_ok=True)
            print(f"khimaira proxy: stopped (PID {pid})")
            return 0
        time.sleep(0.1)

    print(f"khimaira proxy: PID {pid} did not exit on SIGTERM, sending SIGKILL", file=sys.stderr)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    PID_FILE.unlink(missing_ok=True)
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    pid = _read_pid()
    if pid is None:
        print("khimaira proxy: not running")
        return 1
    if not _alive(pid):
        print(f"khimaira proxy: stale PID file (PID {pid} not alive)")
        return 1
    print(f"khimaira proxy: running (PID {pid}) — http://127.0.0.1:{_port()}")
    print(f"logs: {LOG_FILE}")
    return 0


def _cmd_watch(args: argparse.Namespace) -> int:
    """Supervisor — run proxy in foreground, restart on non-zero exit.

    Exponential backoff: 1s → 60s, resets after 5 min healthy uptime.
    """
    _load_env()
    backoff = 1.0
    max_backoff = 60.0
    healthy_threshold = 300.0
    cmd = [sys.executable, "-m", "khimaira.cli", "proxy", "serve"]

    print(f"khimaira proxy watch: supervising — Ctrl-C to stop")
    print(f"  logs: {LOG_FILE}")

    import subprocess

    child: subprocess.Popen | None = None
    interrupted = {"flag": False}

    def _sigint_handler(signum, frame):
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
            print(f"khimaira proxy watch: spawn failed ({e}); retrying in {backoff:.0f}s", file=sys.stderr)
            time.sleep(backoff)
            backoff = min(backoff * 2, max_backoff)
            continue

        rc = child.wait()
        uptime = time.time() - start_ts

        if interrupted["flag"]:
            print(f"khimaira proxy watch: interrupted; proxy exited with rc={rc}")
            return 0

        if rc == 0:
            print("khimaira proxy watch: proxy exited cleanly (rc=0); not restarting")
            return 0

        if uptime >= healthy_threshold:
            backoff = 1.0

        print(
            f"khimaira proxy watch: proxy died (rc={rc}, uptime={uptime:.0f}s); "
            f"restarting in {backoff:.0f}s — see {LOG_FILE}",
            file=sys.stderr,
        )
        time.sleep(backoff)
        backoff = min(backoff * 2, max_backoff)

    return 0


def _systemd_unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "systemd" / "user" / "khimaira-proxy.service"


def _systemd_unit_content() -> str:
    import shutil

    uv = shutil.which("uv")
    workspace_root = str(Path(__file__).resolve().parents[5])
    if uv:
        exec_start = (
            f"{uv} --directory {workspace_root} run khimaira proxy serve"
        )
    else:
        exec_start = f"{sys.executable} -m khimaira.cli proxy serve"

    return f"""\
[Unit]
Description=khimaira Anthropic concurrency-proxy
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
"""


def _cmd_install_service(args: argparse.Namespace) -> int:
    import platform

    if platform.system() != "Linux":
        print("khimaira proxy install-service: only systemd (Linux) is supported", file=sys.stderr)
        return 1

    unit_path = _systemd_unit_path()
    content = _systemd_unit_content()

    if unit_path.exists() and not args.force:
        existing = unit_path.read_text()
        if existing == content:
            print(f"khimaira proxy: unit already up to date at {unit_path}")
            return 0
        print(
            f"khimaira proxy: unit exists with different content at {unit_path}\n"
            f"  Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(content)
    print(f"khimaira proxy: wrote unit → {unit_path}")

    if args.enable:
        import subprocess

        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", "khimaira-proxy.service"], check=False)
        print("khimaira proxy: service enabled + started")
    else:
        print("  To enable: systemctl --user daemon-reload && systemctl --user enable --now khimaira-proxy.service")

    return 0


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    import platform
    import subprocess

    if platform.system() != "Linux":
        print("khimaira proxy uninstall-service: only systemd (Linux) is supported", file=sys.stderr)
        return 1

    unit_path = _systemd_unit_path()
    if not unit_path.exists():
        print(f"khimaira proxy: no unit file at {unit_path}")
        return 0

    subprocess.run(["systemctl", "--user", "stop", "khimaira-proxy.service"], check=False)
    subprocess.run(["systemctl", "--user", "disable", "khimaira-proxy.service"], check=False)
    unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"khimaira proxy: uninstalled service + removed {unit_path}")
    return 0
