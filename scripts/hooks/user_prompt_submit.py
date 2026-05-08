#!/usr/bin/env python3
"""chimera UserPromptSubmit hook — periodic decision/question reminder.

Runs before each user prompt is processed. Every Nth invocation (default 8)
emits a soft reminder that the agent should externalize decisions and open
questions via session_log_decision / session_log_question. Avoids agent-side
amnesia about the multi-session memory feature.

We deliberately DO NOT auto-extract decisions from prose — agents tested
poorly at recognizing 'this was a decision'. Manual logging stays manual;
we just nudge.

Counter is per-session, persisted at:
  ~/.local/state/chimera/hook-counters/<session_id>.count
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_REMINDER_EVERY = int(os.environ.get("CHIMERA_HOOK_REMINDER_EVERY", "8"))

_COUNTER_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "chimera" / "hook-counters"


def _read_count(path: Path) -> int:
    try:
        text = path.read_text(encoding="utf-8").strip()
        return int(text) if text.isdigit() else 0
    except (OSError, ValueError):
        return 0


def _write_count(path: Path, n: int) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".count.tmp")
        tmp.write_text(str(n), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


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

    safe = session_id.replace("/", "_").replace("..", "_")
    counter_file = _COUNTER_DIR / f"{safe}.count"
    count = _read_count(counter_file)
    new_count = count + 1
    _write_count(counter_file, new_count)

    # Skip turn 1 (let the agent settle in); fire every Nth thereafter.
    if new_count < 2 or new_count % _REMINDER_EVERY != 0:
        return 0

    reminder = (
        "💡 chimera reminder: any new decisions or open questions worth logging?\n"
        f"  - `session_log_decision(session_id=\"{session_id}\", text=\"...\", why=\"...\")` for commitments\n"
        f"  - `session_log_question(session_id=\"{session_id}\", text=\"...\")` for things a parallel session can research\n"
        "Skip if nothing to log."
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": reminder,
        }
    }
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
