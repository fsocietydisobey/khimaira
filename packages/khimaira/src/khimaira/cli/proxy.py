"""`khimaira proxy {serve, start, stop, status, watch, install-service}`.

Thin wrapper delegating to khimaira.proxy.cli handlers.
"""

from __future__ import annotations

import argparse


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    proxy = subparsers.add_parser(
        "proxy",
        help="Run the Anthropic concurrency-proxy (loopback, port 8741).",
        description=(
            "khimaira-proxy — local Anthropic reverse-proxy that adds cross-session "
            "concurrency-cap + adaptive-retry to eliminate server-throttle 429s under "
            "full-roster load (~32 concurrent sessions). Point ANTHROPIC_BASE_URL here.\n\n"
            "IMPORTANT: also set ENABLE_TOOL_SEARCH=1 — ANTHROPIC_BASE_URL disables "
            "MCP tool-search unless this env var is set."
        ),
    )
    sub = proxy.add_subparsers(dest="proxy_cmd", required=True)

    p_serve = sub.add_parser("serve", help="Run the proxy in the foreground (no fork)")
    p_serve.set_defaults(func=_run_serve)

    p_start = sub.add_parser("start", help="Daemonize the proxy server")
    p_start.add_argument("--foreground", action="store_true", help="Run in foreground (no fork)")
    p_start.set_defaults(func=_run_start)

    p_stop = sub.add_parser("stop", help="Stop the proxy daemon")
    p_stop.set_defaults(func=_run_stop)

    p_status = sub.add_parser("status", help="Report proxy daemon status")
    p_status.set_defaults(func=_run_status)

    p_watch = sub.add_parser(
        "watch",
        help="Supervise the proxy — restart on non-zero exit (cross-platform fallback)",
        description=(
            "Run the proxy in foreground with auto-restart on non-zero exit. "
            "Exponential backoff (1s → 60s) with a 5-min healthy-uptime reset. "
            "Prefer `install-service` for the long-running production setup."
        ),
    )
    p_watch.set_defaults(func=_run_watch)

    p_install = sub.add_parser(
        "install-service",
        help="Install a systemd user unit (Linux)",
        description=(
            "Writes ~/.config/systemd/user/khimaira-proxy.service. "
            "View logs with: journalctl --user -u khimaira-proxy -f"
        ),
    )
    p_install.add_argument("--enable", action="store_true", help="Also enable + start the service immediately")
    p_install.add_argument("--force", action="store_true", help="Overwrite an existing unit file")
    p_install.set_defaults(func=_run_install_service)

    p_uninstall = sub.add_parser("uninstall-service", help="Stop and remove the systemd unit")
    p_uninstall.set_defaults(func=_run_uninstall_service)


def _run_serve(args: argparse.Namespace) -> int:
    from khimaira.proxy.cli import _cmd_serve
    return _cmd_serve(args)


def _run_start(args: argparse.Namespace) -> int:
    from khimaira.proxy.cli import _cmd_start
    return _cmd_start(args)


def _run_stop(args: argparse.Namespace) -> int:
    from khimaira.proxy.cli import _cmd_stop
    return _cmd_stop(args)


def _run_status(args: argparse.Namespace) -> int:
    from khimaira.proxy.cli import _cmd_status
    return _cmd_status(args)


def _run_watch(args: argparse.Namespace) -> int:
    from khimaira.proxy.cli import _cmd_watch
    return _cmd_watch(args)


def _run_install_service(args: argparse.Namespace) -> int:
    from khimaira.proxy.cli import _cmd_install_service
    return _cmd_install_service(args)


def _run_uninstall_service(args: argparse.Namespace) -> int:
    from khimaira.proxy.cli import _cmd_uninstall_service
    return _cmd_uninstall_service(args)
