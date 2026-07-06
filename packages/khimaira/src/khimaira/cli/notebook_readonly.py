"""`khimaira notebook-readonly {serve, install-service, uninstall-service}`.

Thin wrapper delegating to khimaira.notebook_readonly.cli handlers.
"""

from __future__ import annotations

import argparse


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    nb = subparsers.add_parser(
        "notebook-readonly",
        help="Run the read-only notebook proxy (Tailscale-reachable, port 8742).",
        description=(
            "khimaira-notebook-readonly — bearer-token-authenticated, read-only "
            "surface onto the notebook (search/get/ask only) for teammates who "
            "don't run the full khimaira daemon locally. Reads KHIMAIRA_NOTEBOOK_RO_TOKEN "
            "(required), KHIMAIRA_NOTEBOOK_RO_REPO (optional repo allowlist), "
            "KHIMAIRA_MONITOR_URL (daemon to relay to, default http://127.0.0.1:8740)."
        ),
    )
    sub = nb.add_subparsers(dest="notebook_readonly_cmd", required=True)

    p_serve = sub.add_parser("serve", help="Run the proxy in the foreground (no fork)")
    p_serve.set_defaults(func=_run_serve)

    p_install = sub.add_parser(
        "install-service",
        help="Install a systemd user unit (Linux)",
        description=(
            "Writes ~/.config/systemd/user/khimaira-notebook-readonly.service. "
            "View logs with: journalctl --user -u khimaira-notebook-readonly -f"
        ),
    )
    p_install.add_argument("--enable", action="store_true", help="Also enable + start the service immediately")
    p_install.add_argument("--force", action="store_true", help="Overwrite an existing unit file")
    p_install.set_defaults(func=_run_install_service)

    p_uninstall = sub.add_parser("uninstall-service", help="Stop and remove the systemd unit")
    p_uninstall.set_defaults(func=_run_uninstall_service)


def _run_serve(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_serve
    return _cmd_serve(args)


def _run_install_service(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_install_service
    return _cmd_install_service(args)


def _run_uninstall_service(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_uninstall_service
    return _cmd_uninstall_service(args)
