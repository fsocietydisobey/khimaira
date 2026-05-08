"""XDG-compliant paths for monitor runtime state."""

import os
from pathlib import Path

_XDG_DATA = Path(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")))
_XDG_STATE = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))

PID_FILE = _XDG_DATA / "chimera" / "monitor.pid"
LOG_FILE = _XDG_STATE / "chimera" / "monitor.log"


def ensure_dirs() -> None:
    """Create parent dirs for PID + log files."""
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
