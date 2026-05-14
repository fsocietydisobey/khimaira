"""Configuration for Specter."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SpecterConfig:
    """Configuration for the Specter debug connection."""

    # CDP remote debugging endpoint
    debug_host: str = "localhost"
    debug_port: int = 9222

    # Where to save screenshots
    screenshot_dir: Path = Path("/tmp/specter/screenshots")

    # Max buffered events per category
    max_buffer_size: int = 1000

    # Browser-internal URLs that are never the app. These are always skipped
    # when auto-selecting a tab. Everything else is fair game — Claude decides.
    browser_internal_urls: tuple[str, ...] = (
        "devtools://",
        "chrome://",
        "chrome-extension://",
        "about:",
        "edge://",
    )

    @property
    def http_endpoint(self) -> str:
        return f"http://{self.debug_host}:{self.debug_port}"

    @property
    def json_endpoint(self) -> str:
        return f"{self.http_endpoint}/json"

    def __post_init__(self) -> None:
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)


def load_config() -> SpecterConfig:
    """Load configuration from environment variables."""
    return SpecterConfig(
        debug_host=os.environ.get("SPECTER_DEBUG_HOST", "localhost"),
        debug_port=int(os.environ.get("SPECTER_DEBUG_PORT", "9222")),
        screenshot_dir=Path(
            os.environ.get("SPECTER_SCREENSHOT_DIR", "/tmp/specter/screenshots")
        ),
        max_buffer_size=int(os.environ.get("SPECTER_MAX_BUFFER", "1000")),
    )
