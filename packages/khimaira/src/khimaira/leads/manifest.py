"""Parse and validate a project's central leads manifest.

Manifest location: ${XDG_DATA_HOME:-~/.local/share}/khimaira/leads/<project_name>.toml
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path


def _leads_dir() -> Path:
    """Resolve the central leads directory, creating it if absent.

    Follows the XDG Base Directory Specification:
    ${XDG_DATA_HOME:-~/.local/share}/khimaira/leads/

    Side effect: creates the directory tree on every call, including from
    pure-read callers like check_drift(). This is intentional for manifest.py
    (write paths dominate), but callers that want strict read semantics should
    check existence separately rather than relying on this function.
    """
    xdg = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    leads = Path(xdg) / "khimaira" / "leads"
    leads.mkdir(parents=True, exist_ok=True)
    return leads


@dataclass
class LeadConfig:
    """Per-lead configuration parsed from [leads.<domain>] sections."""

    name: str
    paths: list[str]
    model: str = "sonnet"
    effort: str = "medium"


@dataclass
class Manifest:
    """Parsed central manifest for a project."""

    project_name: str
    prefix: str  # roster session-name prefix, e.g. "jp" for jeevy; "" for khimaira
    root_path: Path  # absolute project root — all relative dirs join here
    roles_dir: Path
    themis_dir: Path
    knowledge_dir: Path
    leads: dict[str, LeadConfig]
    propose_only: bool = False  # if True, all leads in this project are write-blocked


def load_manifest(project_name: str) -> Manifest:
    """Load and validate the central manifest for a project.

    Reads ${XDG_DATA_HOME:-~/.local/share}/khimaira/leads/<project_name>.toml.

    Raises FileNotFoundError if the manifest is absent.
    Raises ValueError on malformed manifest — fail loud at the boundary.
    """
    manifest_path = _leads_dir() / f"{project_name}.toml"
    if not manifest_path.exists():
        raise FileNotFoundError(
            f"No central manifest for '{project_name}': expected at {manifest_path}"
        )

    with open(manifest_path, "rb") as f:
        raw = tomllib.load(f)

    project = raw.get("project", {})
    if not project.get("name"):
        raise ValueError("leads.toml: [project].name is required")

    project_name_parsed = project["name"]
    prefix = project.get("prefix", "")

    root_path_str = project.get("root_path", "")
    if not root_path_str:
        raise ValueError("leads.toml: [project].root_path is required")
    root_path = Path(root_path_str)
    if not root_path.is_absolute():
        raise ValueError(
            f"leads.toml: [project].root_path must be absolute, got '{root_path_str}'"
        )

    roles_dir = root_path / project.get("roles_dir", "roles")
    themis_dir = root_path / project.get("themis_dir", "themis/rules")
    knowledge_dir = root_path / project.get("knowledge_dir", "docs/domain")

    leads_raw = raw.get("leads", {})
    if not leads_raw:
        raise ValueError(
            "leads.toml: at least one [leads.<domain>] section is required"
        )

    leads: dict[str, LeadConfig] = {}
    for domain, cfg in leads_raw.items():
        if not isinstance(cfg, dict):
            raise ValueError(
                f"leads.toml: [leads.{domain}] must be a table, got {type(cfg).__name__}"
            )
        paths = cfg.get("paths")
        if not paths:
            raise ValueError(
                f"leads.toml: [leads.{domain}].paths is required and must be non-empty"
            )
        leads[domain] = LeadConfig(
            name=domain,
            paths=list(paths),
            model=cfg.get("model", "sonnet"),
            effort=cfg.get("effort", "medium"),
        )

    propose_only = bool(project.get("propose_only", False))

    return Manifest(
        project_name=project_name_parsed,
        prefix=prefix,
        root_path=root_path,
        roles_dir=roles_dir,
        themis_dir=themis_dir,
        knowledge_dir=knowledge_dir,
        leads=leads,
        propose_only=propose_only,
    )
