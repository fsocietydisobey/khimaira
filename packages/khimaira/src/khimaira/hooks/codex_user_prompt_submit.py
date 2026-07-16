"""khimaira Codex UserPromptSubmit hook — per-turn chat delivery.

2026-07-15: first cut of the Codex adapter. Reuses the pure-stdlib HTTP
helpers already proven in khimaira.hooks.user_prompt_submit rather than
duplicating them — _poll_missed_chat_events in particular is the exact
"near-live, turn-boundary" delivery mechanism this Codex adapter needs:
it diffs each accepted chat against a per-chat watermark and surfaces
anything new as additionalContext. No Claude-Code dependency in any of
these three functions (plain urllib against the khimaira daemon), so they
port to Codex unmodified.

Scope: missed-chat + inbox + incoming-questions only. The rest of the
Claude Code hook's feature set (pending-assignment banners, BEGIN banners,
stale-ack detection, bottleneck prompts, dynamic context classification)
assumes a roster WORKER already has task assignments flowing through it —
premature to port before a Codex session has actually taken on that role.
Add incrementally once this base layer is validated live.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

from khimaira.hooks.user_prompt_submit import (
    _fetch_incoming_questions,
    _fetch_pending_notes,
    _format_inbox,
    _format_incoming,
    _poll_missed_chat_events,
)


def _stamp_turn_start(session_id: str) -> None:
    """Pairs with codex_stop's turn_end.txt stamp — same file convention
    khimaira.hooks.user_prompt_submit uses for Claude Code, so
    sessions.is_mid_turn() works transparently for Codex sessions too.
    """
    try:
        state_dir = (
            Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
            / "khimaira"
            / "sessions"
            / session_id
        )
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "turn_start.txt").write_text(
            datetime.now(timezone.utc).isoformat(), encoding="utf-8"
        )
    except Exception:
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
    session_cwd = data.get("cwd") or ""

    _stamp_turn_start(session_id)

    missed_chat_block = ""
    try:
        missed_chat_block = _poll_missed_chat_events(session_id)
    except Exception:
        pass

    inbox_block = ""
    try:
        notes = _fetch_pending_notes(session_id, cwd=session_cwd or None)
        if notes:
            inbox_block = _format_inbox(notes, session_id)
    except Exception:
        pass

    incoming_block = ""
    try:
        questions = _fetch_incoming_questions(session_id)
        if questions:
            incoming_block = _format_incoming(questions, session_id)
    except Exception:
        pass

    if not missed_chat_block and not inbox_block and not incoming_block:
        return 0

    additional_context = "\n\n".join(
        b for b in (missed_chat_block, inbox_block, incoming_block) if b
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
