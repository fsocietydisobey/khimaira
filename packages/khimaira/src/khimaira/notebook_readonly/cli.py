"""`khimaira notebook-readonly` CLI — serve / mcp / install-*-service subcommands.

No start/stop/status/watch: these services are meant to run under systemd
(Restart=on-failure), which already supervises the lifecycle. `serve` /
`mcp` are the foreground entry points systemd (or a developer) needs.

Two independent long-running processes, two independent systemd units:
  `serve`  — the REST proxy (`.server`). Loopback-only (127.0.0.1) — no
             longer directly reachable from the tailnet; only `mcp` calls it.
  `mcp`    — the FastMCP HTTP server (`.mcp_client`) remote engineers'
             .mcp.json points at. THIS is the tailnet-reachable surface now.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

_DEFAULT_PORT = 8742
_MCP_DEFAULT_PORT = 8743

_UNIT_NAME = "khimaira-notebook-readonly"
_MCP_UNIT_NAME = "khimaira-notebook-readonly-mcp"


def _port() -> int:
    raw = os.environ.get("KHIMAIRA_NOTEBOOK_RO_PORT")
    if not raw:
        return _DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"khimaira notebook-readonly: invalid KHIMAIRA_NOTEBOOK_RO_PORT={raw!r}", file=sys.stderr)
        sys.exit(1)


def _host() -> str:
    # Tightened back to loopback: the REST proxy is no longer directly
    # reachable from the tailnet — only `mcp` (co-located) calls it now.
    return os.environ.get("KHIMAIRA_NOTEBOOK_RO_HOST", "127.0.0.1")


def _mcp_port() -> int:
    raw = os.environ.get("KHIMAIRA_NOTEBOOK_MCP_PORT")
    if not raw:
        return _MCP_DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        print(f"khimaira notebook-readonly: invalid KHIMAIRA_NOTEBOOK_MCP_PORT={raw!r}", file=sys.stderr)
        sys.exit(1)


def _mcp_host() -> str:
    # This IS the interface meant to be tailnet-reachable.
    return os.environ.get("KHIMAIRA_NOTEBOOK_MCP_HOST", "0.0.0.0")


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except ImportError:
        pass


def _cmd_serve(args: argparse.Namespace) -> int:
    # _load_env() MUST run before `.server` is imported — server.py reads
    # KHIMAIRA_NOTEBOOK_RO_TOKEN into a module-level constant at import time,
    # so loading the .env file afterward would silently freeze an empty token.
    _load_env()
    from .server import serve

    serve(host=_host(), port=_port())
    return 0


def _cmd_mcp(args: argparse.Namespace) -> int:
    # Same load-order discipline as _cmd_serve: mcp_client.py reads
    # KHIMAIRA_NOTEBOOK_MCP_TOKEN / KHIMAIRA_NOTEBOOK_RO_* into module-level
    # constants at import time, so .env must load first.
    _load_env()
    from .mcp_client import serve

    serve(host=_mcp_host(), port=_mcp_port())
    return 0


def _systemd_unit_path(unit_name: str) -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "systemd" / "user" / f"{unit_name}.service"


def _systemd_unit_content(*, unit_name: str, description: str, subcommand: str) -> str:
    import shutil

    uv = shutil.which("uv")
    workspace_root = str(Path(__file__).resolve().parents[5])
    if uv:
        exec_start = f"{uv} --directory {workspace_root} run khimaira notebook-readonly {subcommand}"
    else:
        exec_start = f"{sys.executable} -m khimaira.cli notebook-readonly {subcommand}"

    return f"""\
[Unit]
Description={description}
After=network.target

[Service]
Type=simple
ExecStart={exec_start}
Restart=on-failure
RestartSec=5s

[Install]
WantedBy=default.target
"""


def _install_service(args: argparse.Namespace, *, unit_name: str, description: str, subcommand: str) -> int:
    if platform.system() != "Linux":
        print(f"khimaira notebook-readonly {subcommand}: only systemd (Linux) is supported", file=sys.stderr)
        return 1

    unit_path = _systemd_unit_path(unit_name)
    content = _systemd_unit_content(unit_name=unit_name, description=description, subcommand=subcommand)

    if unit_path.exists() and not args.force:
        existing = unit_path.read_text()
        if existing == content:
            print(f"khimaira notebook-readonly: unit already up to date at {unit_path}")
            return 0
        print(
            f"khimaira notebook-readonly: unit exists with different content at {unit_path}\n"
            f"  Use --force to overwrite.",
            file=sys.stderr,
        )
        return 1

    unit_path.parent.mkdir(parents=True, exist_ok=True)
    unit_path.write_text(content)
    print(f"khimaira notebook-readonly: wrote unit → {unit_path}")

    if args.enable:
        subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
        subprocess.run(["systemctl", "--user", "enable", "--now", f"{unit_name}.service"], check=False)
        print("khimaira notebook-readonly: service enabled + started")
    else:
        print(
            "  To enable: systemctl --user daemon-reload && "
            f"systemctl --user enable --now {unit_name}.service"
        )

    return 0


def _uninstall_service(args: argparse.Namespace, *, unit_name: str) -> int:
    if platform.system() != "Linux":
        print("khimaira notebook-readonly uninstall-service: only systemd (Linux) is supported", file=sys.stderr)
        return 1

    unit_path = _systemd_unit_path(unit_name)
    if not unit_path.exists():
        print(f"khimaira notebook-readonly: no unit file at {unit_path}")
        return 0

    subprocess.run(["systemctl", "--user", "stop", f"{unit_name}.service"], check=False)
    subprocess.run(["systemctl", "--user", "disable", f"{unit_name}.service"], check=False)
    unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"khimaira notebook-readonly: uninstalled service + removed {unit_path}")
    return 0


def _cmd_install_service(args: argparse.Namespace) -> int:
    return _install_service(
        args,
        unit_name=_UNIT_NAME,
        description="khimaira read-only notebook REST proxy (loopback-only)",
        subcommand="serve",
    )


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    return _uninstall_service(args, unit_name=_UNIT_NAME)


def _cmd_install_mcp_service(args: argparse.Namespace) -> int:
    return _install_service(
        args,
        unit_name=_MCP_UNIT_NAME,
        description="khimaira notebook-readonly MCP server (Tailscale-reachable)",
        subcommand="mcp",
    )


def _cmd_uninstall_mcp_service(args: argparse.Namespace) -> int:
    return _uninstall_service(args, unit_name=_MCP_UNIT_NAME)
