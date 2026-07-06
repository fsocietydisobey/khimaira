"""`khimaira notebook-readonly {serve, mcp, install-service, uninstall-service,
install-mcp-service, uninstall-mcp-service}`.

Thin wrapper delegating to khimaira.notebook_readonly.cli handlers. Two
independent long-running processes now: `serve` (the loopback-only REST
proxy) and `mcp` (the Tailscale-reachable FastMCP HTTP server remote
engineers' .mcp.json points at — see khimaira.notebook_readonly.mcp_client's
docstring for why it's a separate process from `khimaira mcp`).
"""

from __future__ import annotations

import argparse


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    nb = subparsers.add_parser(
        "notebook-readonly",
        help="Run the read-only notebook proxy + its MCP server (Tailscale-reachable, ports 8742/8743).",
        description=(
            "khimaira-notebook-readonly — bearer-token-authenticated, read-only "
            "surface onto the notebook (search/get/ask only) for teammates who "
            "don't run khimaira at all. Two co-located processes: `serve` (the "
            "REST proxy, loopback-only) and `mcp` (the FastMCP HTTP server, "
            "Tailscale-reachable — this is what remote engineers' .mcp.json "
            "points at). Reads KHIMAIRA_NOTEBOOK_RO_TOKEN (required, REST proxy "
            "auth), KHIMAIRA_NOTEBOOK_MCP_TOKEN (required, MCP server auth — "
            "deliberately a DIFFERENT secret), KHIMAIRA_NOTEBOOK_RO_REPO "
            "(optional repo allowlist), KHIMAIRA_MONITOR_URL (daemon the REST "
            "proxy relays to, default http://127.0.0.1:8740)."
        ),
    )
    sub = nb.add_subparsers(dest="notebook_readonly_cmd", required=True)

    p_serve = sub.add_parser(
        "serve",
        help="Run the REST proxy in the foreground (no fork). Loopback-only.",
    )
    p_serve.set_defaults(func=_run_serve)

    p_mcp = sub.add_parser(
        "mcp",
        help="Run the read-only notebook MCP server over HTTP (Tailscale-reachable, for remote engineers).",
        description=(
            "Launches a standalone FastMCP HTTP server exposing exactly "
            "notebook_search/notebook_get/notebook_ask, wired to the co-located "
            "REST proxy (KHIMAIRA_NOTEBOOK_RO_URL, typically http://127.0.0.1:8742) "
            "with bearer auth (KHIMAIRA_NOTEBOOK_RO_TOKEN), and itself gated by a "
            "SEPARATE bearer token (KHIMAIRA_NOTEBOOK_MCP_TOKEN) via FastMCP's "
            "StaticTokenVerifier. Remote engineers register this as a pure-URL "
            "`mcpServers` entry (type=http) in their own .mcp.json — no local "
            "khimaira install needed, and the other ~116 khimaira tools never "
            "appear in their tool list. Host/port from KHIMAIRA_NOTEBOOK_MCP_HOST "
            "/ KHIMAIRA_NOTEBOOK_MCP_PORT (default 0.0.0.0:8743)."
        ),
    )
    p_mcp.set_defaults(func=_run_mcp)

    p_install = sub.add_parser(
        "install-service",
        help="Install the REST proxy's systemd user unit (Linux)",
        description=(
            "Writes ~/.config/systemd/user/khimaira-notebook-readonly.service. "
            "View logs with: journalctl --user -u khimaira-notebook-readonly -f"
        ),
    )
    p_install.add_argument("--enable", action="store_true", help="Also enable + start the service immediately")
    p_install.add_argument("--force", action="store_true", help="Overwrite an existing unit file")
    p_install.set_defaults(func=_run_install_service)

    p_uninstall = sub.add_parser("uninstall-service", help="Stop and remove the REST proxy's systemd unit")
    p_uninstall.set_defaults(func=_run_uninstall_service)

    p_install_mcp = sub.add_parser(
        "install-mcp-service",
        help="Install the MCP server's systemd user unit (Linux)",
        description=(
            "Writes ~/.config/systemd/user/khimaira-notebook-readonly-mcp.service. "
            "View logs with: journalctl --user -u khimaira-notebook-readonly-mcp -f"
        ),
    )
    p_install_mcp.add_argument("--enable", action="store_true", help="Also enable + start the service immediately")
    p_install_mcp.add_argument("--force", action="store_true", help="Overwrite an existing unit file")
    p_install_mcp.set_defaults(func=_run_install_mcp_service)

    p_uninstall_mcp = sub.add_parser("uninstall-mcp-service", help="Stop and remove the MCP server's systemd unit")
    p_uninstall_mcp.set_defaults(func=_run_uninstall_mcp_service)


def _run_serve(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_serve
    return _cmd_serve(args)


def _run_mcp(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_mcp
    return _cmd_mcp(args)


def _run_install_service(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_install_service
    return _cmd_install_service(args)


def _run_uninstall_service(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_uninstall_service
    return _cmd_uninstall_service(args)


def _run_install_mcp_service(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_install_mcp_service
    return _cmd_install_mcp_service(args)


def _run_uninstall_mcp_service(args: argparse.Namespace) -> int:
    from khimaira.notebook_readonly.cli import _cmd_uninstall_mcp_service
    return _cmd_uninstall_mcp_service(args)
