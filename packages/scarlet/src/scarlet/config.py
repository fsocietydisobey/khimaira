"""Configuration loader for Scarlet.

Scarlet config lives in `.scarlet.yml` at the root of each project being
documented. The config declares project conventions: framework, state
management, where features live, what the CLAUDE.md template looks like.
This makes Scarlet generic across projects.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

CONFIG_FILENAME = ".scarlet.yml"


@dataclass(frozen=True)
class ScarletConfig:
    """Per-project configuration loaded from .scarlet.yml."""

    project_type: str = "generic"
    state_management: str | None = None
    test_framework: str | None = None
    features_root: str = "src/features"
    study_guides_path: str | None = None
    barrel_export_strategy: str = "re_export_default"
    claude_md_template: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ScarletConfig":
        known_keys = {
            "project_type",
            "state_management",
            "test_framework",
            "features_root",
            "study_guides_path",
            "barrel_export_strategy",
            "claude_md_template",
        }
        extra = {k: v for k, v in data.items() if k not in known_keys}
        return cls(
            project_type=data.get("project_type", "generic"),
            state_management=data.get("state_management"),
            test_framework=data.get("test_framework"),
            features_root=data.get("features_root", "src/features"),
            study_guides_path=data.get("study_guides_path"),
            barrel_export_strategy=data.get("barrel_export_strategy", "re_export_default"),
            claude_md_template=data.get("claude_md_template"),
            extra=extra,
        )


def load_config(project_path: Path) -> ScarletConfig:
    """Load .scarlet.yml from a project root.

    If no config file exists, returns sensible defaults. Scarlet should
    work zero-config on most projects, with .scarlet.yml as the override.

    Args:
        project_path: Root directory of the project.

    Returns:
        ScarletConfig instance.
    """
    config_path = project_path / CONFIG_FILENAME

    if not config_path.exists():
        return ScarletConfig()

    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    return ScarletConfig.from_dict(data)
