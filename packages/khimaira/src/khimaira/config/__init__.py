"""Configuration loader.

Resolution order (later overrides earlier):
  1. Shipped defaults (this package's `routing_table.yaml`)
  2. ~/.config/khimaira/routing_table.yaml — user-wide
  3. <project>/.khimaira/routing_table.yaml — per-project
  4. Environment variable overrides (e.g. KHIMAIRA_LOCAL_ONLY)
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from khimaira.log import get_logger

log = get_logger("config")

_PACKAGE_DIR = Path(__file__).parent
_DEFAULT_ROUTING_PATH = _PACKAGE_DIR / "routing_table.yaml"

USER_CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config"))
) / "khimaira"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge override into base. Override wins on scalars + lists."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


@lru_cache(maxsize=8)
def load_routing_table(project_path: str | None = None) -> dict[str, Any]:
    """Load + merge routing tables. Cached per project.

    Pass `project_path=None` for "user + defaults only" (e.g. classifier
    decisions made outside any specific project context).
    """
    with _DEFAULT_ROUTING_PATH.open() as f:
        config = yaml.safe_load(f)

    user_path = USER_CONFIG_DIR / "routing_table.yaml"
    if user_path.exists():
        with user_path.open() as f:
            user_overrides = yaml.safe_load(f) or {}
        config = _deep_merge(config, user_overrides)
        log.info("config: applied user override from %s", user_path)

    if project_path:
        proj_path = Path(project_path) / ".khimaira" / "routing_table.yaml"
        if proj_path.exists():
            with proj_path.open() as f:
                proj_overrides = yaml.safe_load(f) or {}
            config = _deep_merge(config, proj_overrides)
            log.info("config: applied project override from %s", proj_path)

    return config


def is_local_only_mode() -> bool:
    """User opted in to privacy mode — only local runners are eligible."""
    return os.environ.get("KHIMAIRA_LOCAL_ONLY", "").lower() in ("1", "true", "yes")


# Re-export ROOTS so monitor code can `from khimaira.config import ROOTS`
from .roots import ROOTS, ROOTS_FILE  # noqa: E402

# Legacy config (OrchestratorConfig + RoleConfig + ProviderConfig) used by
# the migrated graph nodes. Stays available until Phase 10 (API removal)
# is complete and the patterns are rewritten to use AMR routing instead.
from .loader import (  # noqa: E402
    OrchestratorConfig,
    ProviderConfig,
    RoleConfig,
    load_config,
)
from .models import get_classify_model  # noqa: E402

__all__ = [
    "load_routing_table",
    "is_local_only_mode",
    "ROOTS",
    "ROOTS_FILE",
    # legacy
    "OrchestratorConfig",
    "ProviderConfig",
    "RoleConfig",
    "load_config",
    "get_classify_model",
]
