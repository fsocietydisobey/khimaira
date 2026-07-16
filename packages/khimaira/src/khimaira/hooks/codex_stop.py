"""khimaira Codex Stop hook — idle marker for the chat-watcher daemon.

2026-07-15: mirrors khimaira.hooks.session_end's `_stamp_turn_end` exactly,
writing to the SAME file path convention (`turn_end.txt` in the session's
state dir) — Codex session_ids are distinct UUIDs from Claude Code's, so
there's no collision risk, and this means `sessions.is_mid_turn()` (the
existing daemon-side liveness check) works transparently for Codex sessions
with zero changes: it fails open to "idle" when no marker exists yet, and
once both this hook and codex_user_prompt_submit's turn_start.txt stamp are
wired, it gives an authoritative open/closed-turn signal instead of
screen-scraping the TUI's rendered text.

Not part of the Themis/roster work — this exists for the machine-level
chat-watcher (injects khimaira-chat messages into an idle Codex window via
kitty remote control) to know when a target session is actually idle.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return 0

    session_id = data.get("session_id") or ""
    if not session_id:
        return 0

    try:
        state_dir = (
            Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
            / "khimaira"
            / "sessions"
            / session_id
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "turn_end.txt").write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
    except Exception:
        pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
