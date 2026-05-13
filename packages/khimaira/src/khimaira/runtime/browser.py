"""Launch Chrome with --remote-debugging-port for Specter to attach to.

Strategy:
  1. Find a Chromium-family binary: chromium, google-chrome, chrome,
     /Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome.
  2. Pick a free port (default 9222 — Chrome DevTools convention).
  3. Launch with a dedicated --user-data-dir so we don't trample the
     user's main browser profile.
  4. Open the dev server's URL when ready.

This isn't strictly required for `khimaira dev` to work — the dev server
runs fine without browser orchestration. But devs who use khimaira +
Specter benefit from "one command starts everything connected."
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import tempfile
from pathlib import Path

CHROME_CANDIDATES = [
    "chromium",
    "google-chrome",
    "google-chrome-stable",
    "chrome",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
]

DEFAULT_PORT = 9222


def find_chrome() -> str | None:
    """Locate a Chromium-family executable on this machine."""
    for candidate in CHROME_CANDIDATES:
        if os.path.isabs(candidate) and os.path.isfile(candidate):
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    return None


def free_port(start: int = DEFAULT_PORT) -> int:
    """Find a free TCP port, starting from `start` and walking up."""
    for port in range(start, start + 100):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"No free port in range {start}..{start + 100}")


def build_launch_cmd(
    *,
    url: str | None = None,
    port: int = DEFAULT_PORT,
    user_data_dir: str | None = None,
) -> list[str]:
    """Build the Chrome argv. Returns [] if Chrome isn't installed."""
    chrome = find_chrome()
    if not chrome:
        return []

    if user_data_dir is None:
        # Per-khimaira-dev-session profile so we don't trample the user's main browser
        user_data_dir = tempfile.mkdtemp(prefix="khimaira-dev-chrome-")

    cmd = [
        chrome,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={user_data_dir}",
        # Keep the new-tab page minimal — devs land directly on their app
        "--no-first-run",
        "--no-default-browser-check",
        # Disable some background services that aren't useful for dev:
        "--disable-background-timer-throttling",
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
    ]
    if url:
        cmd.append(url)
    return cmd


def installation_hint() -> str:
    """Helpful message when khimaira dev runs and Chrome isn't found."""
    if sys.platform == "darwin":
        return (
            "Chrome not found. Install: `brew install --cask google-chrome` "
            "or download from https://www.google.com/chrome/."
        )
    if sys.platform.startswith("linux"):
        return (
            "Chrome not found. Install: `sudo apt install chromium` (Debian/Ubuntu), "
            "`sudo pacman -S chromium` (Arch), or download Chrome from "
            "https://www.google.com/chrome/. Skipping browser auto-launch."
        )
    return "Chrome not found. Install a Chromium-family browser to enable Specter integration."
