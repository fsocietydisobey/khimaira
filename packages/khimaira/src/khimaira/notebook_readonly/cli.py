"""`khimaira notebook-readonly` CLI — serve / install-service subcommands.

No start/stop/status/watch: this service is meant to run under systemd
(Restart=on-failure), which already supervises the lifecycle. `serve` is
the only foreground entry point systemd (or a developer) needs.
"""

from __future__ import annotations

import argparse
import os
import platform
import subprocess
import sys
from pathlib import Path

_DEFAULT_PORT = 8742


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
    return os.environ.get("KHIMAIRA_NOTEBOOK_RO_HOST", "0.0.0.0")


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


def _systemd_unit_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(xdg) / "systemd" / "user" / "khimaira-notebook-readonly.service"


def _systemd_unit_content() -> str:
    import shutil

    uv = shutil.which("uv")
    workspace_root = str(Path(__file__).resolve().parents[5])
    if uv:
        exec_start = f"{uv} --directory {workspace_root} run khimaira notebook-readonly serve"
    else:
        exec_start = f"{sys.executable} -m khimaira.cli notebook-readonly serve"

    return f"""\
[Unit]
Description=khimaira read-only notebook proxy (Tailscale-reachable)
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
    if platform.system() != "Linux":
        print("khimaira notebook-readonly install-service: only systemd (Linux) is supported", file=sys.stderr)
        return 1

    unit_path = _systemd_unit_path()
    content = _systemd_unit_content()

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
        subprocess.run(
            ["systemctl", "--user", "enable", "--now", "khimaira-notebook-readonly.service"], check=False
        )
        print("khimaira notebook-readonly: service enabled + started")
    else:
        print(
            "  To enable: systemctl --user daemon-reload && "
            "systemctl --user enable --now khimaira-notebook-readonly.service"
        )

    return 0


def _cmd_uninstall_service(args: argparse.Namespace) -> int:
    if platform.system() != "Linux":
        print("khimaira notebook-readonly uninstall-service: only systemd (Linux) is supported", file=sys.stderr)
        return 1

    unit_path = _systemd_unit_path()
    if not unit_path.exists():
        print(f"khimaira notebook-readonly: no unit file at {unit_path}")
        return 0

    subprocess.run(["systemctl", "--user", "stop", "khimaira-notebook-readonly.service"], check=False)
    subprocess.run(["systemctl", "--user", "disable", "khimaira-notebook-readonly.service"], check=False)
    unit_path.unlink()
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print(f"khimaira notebook-readonly: uninstalled service + removed {unit_path}")
    return 0
