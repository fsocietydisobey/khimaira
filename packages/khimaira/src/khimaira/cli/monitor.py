"""`khimaira monitor {start, stop, restart, status, rescan}` — observability daemon control.

Thin wrapper that delegates to khimaira.monitor.cli's existing _cmd_* handlers.
The monitor daemon code itself is the substantial migration from khimaira-legacy
(packages/khimaira/src/khimaira/monitor/); this module just wires it onto the
new top-level `khimaira` argparse dispatcher.
"""

from __future__ import annotations

import argparse


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    monitor = subparsers.add_parser(
        "monitor",
        help="Run the observability daemon (FastAPI on 127.0.0.1:8740).",
        description=(
            "khimaira-monitor — local observability dashboard for LangGraph "
            "applications. Loopback-only by design (the loopback bind IS "
            "the auth layer)."
        ),
    )
    sub = monitor.add_subparsers(dest="monitor_cmd", required=True)

    # Lazy imports — keep import time fast; monitor pulls heavy deps (FastAPI etc.)
    p_start = sub.add_parser("start", help="Daemonize the monitor server")
    p_start.add_argument(
        "--foreground", action="store_true", help="Run in foreground (no fork)"
    )
    p_start.add_argument(
        "--no-browser", action="store_true", help="Don't open the browser"
    )
    p_start.set_defaults(func=_run_start)

    p_stop = sub.add_parser("stop", help="Stop the monitor daemon")
    p_stop.set_defaults(func=_run_stop)

    p_restart = sub.add_parser("restart", help="Stop then start the monitor daemon")
    p_restart.add_argument(
        "--foreground", action="store_true", help="Run in foreground"
    )
    p_restart.add_argument(
        "--no-browser", action="store_true", help="Don't open the browser"
    )
    p_restart.set_defaults(func=_run_restart)

    p_status = sub.add_parser("status", help="Report daemon status")
    p_status.set_defaults(func=_run_status)

    p_rescan = sub.add_parser(
        "rescan", help="Force a metadata rescan for one project (or all)"
    )
    p_rescan.add_argument(
        "project", nargs="?", help="Project name to rescan; omit for all"
    )
    p_rescan.set_defaults(func=_run_rescan)

    p_watch = sub.add_parser(
        "watch",
        help="Supervise the daemon — restart on non-zero exit (cross-platform fallback)",
        description=(
            "Run the monitor daemon in foreground with auto-restart on "
            "non-zero exit. Exponential backoff (1s → 60s) with a 5-min "
            "healthy-uptime reset. Cross-platform fallback to "
            "`install-service` for users without systemd."
        ),
    )
    p_watch.set_defaults(func=_run_watch)

    p_install = sub.add_parser(
        "install-service",
        help="Install a host-native supervisor (systemd on Linux, launchd on macOS)",
        description=(
            "Dispatches by platform. Linux: writes "
            "~/.config/systemd/user/khimaira-monitor.service (view logs with "
            "`journalctl --user -u khimaira-monitor -f`). macOS: writes "
            "~/Library/LaunchAgents/com.khimaira.monitor.plist (logs in "
            "~/Library/Logs/khimaira-monitor.{out,err}.log)."
        ),
    )
    p_install.add_argument(
        "--enable",
        action="store_true",
        help="Also enable + start the service immediately",
    )
    p_install.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing unit file with different contents",
    )
    p_install.set_defaults(func=_run_install_service)

    p_uninstall = sub.add_parser(
        "uninstall-service",
        help="Stop and remove the host-native supervisor",
    )
    p_uninstall.set_defaults(func=_run_uninstall_service)

    # Explicit per-platform commands for users who want the specific
    # backend without going through the dispatcher.
    p_install_launchd = sub.add_parser(
        "install-launchd",
        help="Install a launchd LaunchAgent plist (macOS) — explicit form of install-service",
        description=(
            "Writes ~/Library/LaunchAgents/com.khimaira.monitor.plist. "
            "Logs land at ~/Library/Logs/khimaira-monitor.{out,err}.log. "
            "On Linux/other platforms, use `install-service` instead."
        ),
    )
    p_install_launchd.add_argument(
        "--enable",
        action="store_true",
        help="Also launchctl load -w the plist immediately",
    )
    p_install_launchd.add_argument(
        "--force",
        action="store_true",
        help="Overwrite an existing plist with different contents",
    )
    p_install_launchd.set_defaults(func=_run_install_launchd)

    p_uninstall_launchd = sub.add_parser(
        "uninstall-launchd",
        help="Unload + remove the launchd LaunchAgent plist (macOS)",
    )
    p_uninstall_launchd.set_defaults(func=_run_uninstall_launchd)


def _run_start(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_start, _load_env

    _load_env()
    return _cmd_start(args)


def _run_stop(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_stop

    return _cmd_stop(args)


def _run_restart(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_restart, _load_env

    _load_env()
    return _cmd_restart(args)


def _run_status(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_status

    return _cmd_status(args)


def _run_rescan(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_rescan, _load_env

    _load_env()
    return _cmd_rescan(args)


def _run_watch(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_watch, _load_env

    _load_env()
    return _cmd_watch(args)


def _run_install_service(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_install_service

    return _cmd_install_service(args)


def _run_uninstall_service(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_uninstall_service

    return _cmd_uninstall_service(args)


def _run_install_launchd(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_install_launchd

    return _cmd_install_launchd(args)


def _run_uninstall_launchd(args: argparse.Namespace) -> int:
    from khimaira.monitor.cli import _cmd_uninstall_launchd

    return _cmd_uninstall_launchd(args)
