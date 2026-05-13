"""Structured logging — stderr only.

MCP uses stdout for protocol messages, so all khimaira log output MUST go to
stderr. This module centralizes the convention so individual modules don't
have to remember.
"""

from __future__ import annotations

import logging
import os
import sys

_configured = False


def _configure_root() -> None:
    """One-time root logger config. Idempotent across imports."""
    global _configured
    if _configured:
        return
    _configured = True

    level = getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", "%H:%M:%S")
    )
    root = logging.getLogger()
    root.setLevel(level)
    # Don't double-attach if something already configured (pytest, uvicorn).
    if not any(isinstance(h, logging.StreamHandler) for h in root.handlers):
        root.addHandler(handler)


def get_logger(name: str) -> logging.Logger:
    """Return a logger scoped to the given name, with khimaira's stderr config applied."""
    _configure_root()
    return logging.getLogger(name)


def setup_logging() -> None:
    """Compatibility alias for legacy callers — no-op since `get_logger`
    already configures the root once. Kept so MCP server's `setup_logging()`
    call still works after migration.
    """
    _configure_root()
