"""Parse and validate a project's .khimaira/leads.toml manifest.

Manifest location: <project_root>/.khimaira/leads.toml
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path


@dataclass
class LeadConfig:
    """Per-lead configuration parsed from [leads.<domain>] sections."""

    name: str
    paths: list[str]
    model: str = "sonnet"
    effort: str = "medium"


@dataclass
class Manifest:
    """Parsed leads.toml for a project."""

    project_name: str
    prefix: str  # roster session-name prefix, e.g. "jp" for jeevy; "" for khimaira
    roles_dir: Path
    themis_dir: Path
    knowledge_dir: Path
    leads: dict[str, LeadConfig]


def load_manifest(project_root: Path) -> Manifest:
    """Load and validate <project_root>/.khimaira/leads.toml.

    Raises ValueError on malformed or missing manifest — fail loud at the boundary.
    """
    manifest_path = project_root / ".khimaira" / "leads.toml"
    if not manifest_path.exists():
        raise ValueError(f"No leads.toml found at {manifest_path}")

    with open(manifest_path, "rb") as f:
        raw = tomllib.load(f)

    project = raw.get("project", {})
    if not project.get("name"):
        raise ValueError("leads.toml: [project].name is required")

    project_name = project["name"]
    prefix = project.get("prefix", "")
    roles_dir = project_root / project.get("roles_dir", "roles")
    themis_dir = project_root / project.get("themis_dir", "themis/rules")
    knowledge_dir = project_root / project.get("knowledge_dir", "docs/domain")

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

    return Manifest(
        project_name=project_name,
        prefix=prefix,
        roles_dir=roles_dir,
        themis_dir=themis_dir,
        knowledge_dir=knowledge_dir,
        leads=leads,
    )
