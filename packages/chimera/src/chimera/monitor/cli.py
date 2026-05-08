"""`chimera monitor` CLI — start / stop / status subcommands."""

import argparse
import os
import signal
import socket
import sys
import time
import webbrowser

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
    raw = os.environ.get("CHIMERA_MONITOR_PORT")
    if not raw:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"chimera monitor: invalid CHIMERA_MONITOR_PORT={raw!r}", file=sys.stderr)
        sys.exit(1)


def _cmd_start(args: argparse.Namespace) -> int:
    existing = _read_pid()
    if existing and _alive(existing):
        print(f"chimera monitor already running (PID {existing}) — http://127.0.0.1:{_port()}")
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
                "chimera monitor: daemon exited before binding — check logs:",
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
            f"chimera monitor: daemon (PID {pid}) didn't bind to port {port} — check logs:",
            LOG_FILE,
            file=sys.stderr,
        )
        return 1

    print(f"chimera monitor started (PID {pid}) — http://127.0.0.1:{port}")
    print(f"logs: {LOG_FILE}")
    if not args.no_browser:
        webbrowser.open(f"http://127.0.0.1:{port}")
    return 0


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
        print("chimera monitor: no PID file — not running")
        return 0
    if not _alive(pid):
        print(f"chimera monitor: stale PID file (PID {pid} not alive) — cleaning up")
        PID_FILE.unlink(missing_ok=True)
        return 0

    os.kill(pid, signal.SIGTERM)
    deadline = time.time() + 5.0
    while time.time() < deadline:
        if not _alive(pid):
            PID_FILE.unlink(missing_ok=True)
            print(f"chimera monitor stopped (PID {pid})")
            return 0
        time.sleep(0.1)

    print(f"chimera monitor: PID {pid} did not exit on SIGTERM, sending SIGKILL", file=sys.stderr)
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

    from chimera.config import ROOTS

    from .discovery.project import discover
    from .metadata.scan import scan_project

    projects = discover(ROOTS)
    if args.project:
        projects = [p for p in projects if p.name == args.project]
        if not projects:
            print(f"chimera monitor: no project named {args.project!r} in roots", file=sys.stderr)
            return 1

    if not projects:
        print("chimera monitor: no langgraph projects discovered")
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
        print("chimera monitor: not running")
        return 1
    if not _alive(pid):
        print(f"chimera monitor: stale PID file (PID {pid} not alive)")
        return 1
    print(f"chimera monitor: running (PID {pid}) — http://127.0.0.1:{_port()}")
    print(f"logs: {LOG_FILE}")
    return 0


def _load_env() -> None:
    """Load chimera's .env so Gemini/Anthropic credentials reach the
    monitor daemon (and the rescan subcommand). Mirrors what
    `chimera.server.mcp` does at module-import time."""
    from pathlib import Path

    from dotenv import load_dotenv

    project_root = Path(__file__).resolve().parent.parent.parent.parent
    load_dotenv(project_root / ".env")


def main() -> None:
    _load_env()
    parser = argparse.ArgumentParser(prog="chimera monitor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_start = sub.add_parser("start", help="Daemonize the monitor server")
    p_start.add_argument("--foreground", action="store_true", help="Run in foreground (no fork)")
    p_start.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    p_start.set_defaults(func=_cmd_start)

    p_stop = sub.add_parser("stop", help="Stop the monitor daemon")
    p_stop.set_defaults(func=_cmd_stop)

    # Convenience: stop + start in one command. Saves typing during
    # development when monitor backend changes need a fresh daemon
    # to take effect (e.g. after a `git pull`).
    p_restart = sub.add_parser("restart", help="Stop then start the monitor daemon")
    p_restart.add_argument("--foreground", action="store_true", help="Run in foreground (no fork)")
    p_restart.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    p_restart.set_defaults(func=_cmd_restart)

    p_status = sub.add_parser("status", help="Report daemon status")
    p_status.set_defaults(func=_cmd_status)

    p_rescan = sub.add_parser(
        "rescan",
        help="Force a metadata rescan for one project (or all). Manual override.",
    )
    p_rescan.add_argument("project", nargs="?", help="Project name to rescan; omit for all")
    p_rescan.set_defaults(func=_cmd_rescan)

    args = parser.parse_args()
    sys.exit(args.func(args))
