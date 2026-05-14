"""Configuration loader. All env var access goes through here."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class SeanceConfig:
    """Immutable configuration for Séance, loaded from environment variables."""

    google_api_key: str
    storage_dir: Path = field(default_factory=lambda: Path.home() / ".seance")
    embedding_model: str = "gemini-embedding-001"
    chunk_overlap: int = 2

    def __post_init__(self) -> None:
        # Ensure storage directory exists
        self.storage_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> SeanceConfig:
    """Load configuration from environment variables.

    Raises:
        SystemExit: If required env vars are missing.
    """
    api_key = os.environ.get("GOOGLE_AI_API_KEY")
    if not api_key:
        raise SystemExit(
            "GOOGLE_AI_API_KEY is not set. "
            "Export it or add it to your .env file. "
            "See .env.example for details."
        )

    return SeanceConfig(
        google_api_key=api_key,
        storage_dir=Path(
            os.environ.get("SEANCE_STORAGE_DIR", str(Path.home() / ".seance"))
        ),
        embedding_model=os.environ.get("SEANCE_EMBEDDING_MODEL", "gemini-embedding-001"),
        chunk_overlap=int(os.environ.get("SEANCE_CHUNK_OVERLAP", "2")),
    )
