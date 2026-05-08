"""Load and validate the orchestrator config (config.yaml)."""

from pathlib import Path

import yaml
from pydantic import BaseModel


class ProviderConfig(BaseModel):
    """Config for a single AI provider."""

    api_key_env: str  # env var name holding the API key
    default_model: str


class RoleConfig(BaseModel):
    """Config for a single role (research, architect, etc.)."""

    provider: str  # key into providers dict
    model: str  # model ID override (or use provider default)
    description: str = ""
    max_tokens: int = 4096


class OrchestratorConfig(BaseModel):
    """Top-level config for the orchestrator."""

    providers: dict[str, ProviderConfig]
    roles: dict[str, RoleConfig]


def load_config(config_path: Path | str | None = None) -> OrchestratorConfig:
    """Load config from YAML file.

    Searches in order:
    1. Explicit path argument
    2. ./config.yaml (next to the script)
    3. ~/.config/chimera/config.yaml
    """
    search_paths = [
        Path(config_path) if config_path else None,
        Path(__file__).parent.parent.parent.parent / "config.yaml",
        Path.home() / ".config" / "chimera" / "config.yaml",
    ]

    for path in search_paths:
        if path and path.exists():
            raw = yaml.safe_load(path.read_text())
            return OrchestratorConfig(**raw)

    # No config found — return sensible defaults
    return OrchestratorConfig(
        providers={
            "anthropic": ProviderConfig(
                api_key_env="ANTHROPIC_API_KEY",
                default_model="claude-sonnet-4-20250514",
            ),
            "google": ProviderConfig(
                api_key_env="GOOGLE_AI_API_KEY",
                default_model="gemini-2.0-flash",
            ),
        },
        roles={
            "research": RoleConfig(
                provider="google",
                model="gemini-2.0-pro",
                description="Deep domain research, large context analysis",
            ),
            "architect": RoleConfig(
                provider="anthropic",
                model="claude-sonnet-4-20250514",
                description="Design decisions, task specs, multi-file coordination",
            ),
            "classify": RoleConfig(
                provider="anthropic",
                model="claude-haiku-4-5-20251001",
                description="Fast classification, supervisor routing, validation",
                max_tokens=1024,
            ),
        },
    )
