"""`chimera monitor {start, stop, restart, status, rescan}` — observability daemon control.

Thin wrapper that delegates to chimera.monitor.cli's existing _cmd_* handlers.
The monitor daemon code itself is the substantial migration from chimera-legacy
(packages/chimera/src/chimera/monitor/); this module just wires it onto the
new top-level `chimera` argparse dispatcher.
"""

from __future__ import annotations

import argparse


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    monitor = subparsers.add_parser(
        "monitor",
        help="Run the observability daemon (FastAPI on 127.0.0.1:8740).",
        description=(
            "chimera-monitor — local observability dashboard for LangGraph "
            "applications. Loopback-only by design (the loopback bind IS "
            "the auth layer)."
        ),
    )
    sub = monitor.add_subparsers(dest="monitor_cmd", required=True)

    # Lazy imports — keep import time fast; monitor pulls heavy deps (FastAPI etc.)
    p_start = sub.add_parser("start", help="Daemonize the monitor server")
    p_start.add_argument("--foreground", action="store_true", help="Run in foreground (no fork)")
    p_start.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    p_start.set_defaults(func=_run_start)

    p_stop = sub.add_parser("stop", help="Stop the monitor daemon")
    p_stop.set_defaults(func=_run_stop)

    p_restart = sub.add_parser("restart", help="Stop then start the monitor daemon")
    p_restart.add_argument("--foreground", action="store_true", help="Run in foreground")
    p_restart.add_argument("--no-browser", action="store_true", help="Don't open the browser")
    p_restart.set_defaults(func=_run_restart)

    p_status = sub.add_parser("status", help="Report daemon status")
    p_status.set_defaults(func=_run_status)

    p_rescan = sub.add_parser("rescan", help="Force a metadata rescan for one project (or all)")
    p_rescan.add_argument("project", nargs="?", help="Project name to rescan; omit for all")
    p_rescan.set_defaults(func=_run_rescan)


def _run_start(args: argparse.Namespace) -> int:
    from chimera.monitor.cli import _cmd_start, _load_env
    _load_env()
    return _cmd_start(args)


def _run_stop(args: argparse.Namespace) -> int:
    from chimera.monitor.cli import _cmd_stop
    return _cmd_stop(args)


def _run_restart(args: argparse.Namespace) -> int:
    from chimera.monitor.cli import _cmd_restart, _load_env
    _load_env()
    return _cmd_restart(args)


def _run_status(args: argparse.Namespace) -> int:
    from chimera.monitor.cli import _cmd_status
    return _cmd_status(args)


def _run_rescan(args: argparse.Namespace) -> int:
    from chimera.monitor.cli import _cmd_rescan, _load_env
    _load_env()
    return _cmd_rescan(args)
