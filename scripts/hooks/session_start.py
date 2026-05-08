#!/usr/bin/env python3
"""chimera SessionStart hook — surface unread inbox notes from other sessions.

Runs at Claude Code session boot (startup, resume, clear). Reads this
session's inbox.jsonl, marks unread notes as read, emits a JSON output
that Claude Code injects into the model's context for the next turn.

If inbox is empty / file missing / any error: exit 0 silently with no output.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_BASE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "chimera" / "sessions"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_dir(session_id: str) -> Path:
    safe = session_id.replace("/", "_").replace("..", "_")
    return _BASE_DIR / safe


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        return []
    return out


def _consume_inbox(session_id: str) -> list[dict]:
    """Read unread notes; mark them all read; return the unread set.

    Same logic as chimera.monitor.sessions.pending_notes(mark_read=True).
    Hook runs as a separate process from the daemon; both use atomic
    rename for the rewrite, so concurrent writes don't corrupt the file.
    """
    inbox = _session_dir(session_id) / "inbox.jsonl"
    notes = _read_jsonl(inbox)
    pending = [n for n in notes if not n.get("read")]
    if not pending:
        return []

    # Mark all unread → read, atomic rewrite
    for n in notes:
        if not n.get("read"):
            n["read"] = True
            n["read_at"] = _now_iso()
    tmp = inbox.with_suffix(".jsonl.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as f:
            for n in notes:
                f.write(json.dumps(n, separators=(",", ":")) + "\n")
        tmp.replace(inbox)
    except OSError:
        # Couldn't rewrite — return empty so we don't lose the notes by
        # claiming we read them when we didn't
        return []

    return pending


def _format_inbox(notes: list[dict]) -> str:
    lines = [
        f"📬 chimera inbox — {len(notes)} unread answer(s) from other sessions:",
        "",
    ]
    for n in notes:
        from_id = n.get("from_session_id") or "unknown"
        q = n.get("question_text") or "?"
        a = n.get("answer") or "?"
        lines.append(f"- (from {from_id})")
        lines.append(f"  Q: {q}")
        lines.append(f"  A: {a}")
        lines.append("")
    return "\n".join(lines).rstrip()


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

    notes = _consume_inbox(session_id)
    if not notes:
        return 0

    # Claude Code's SessionStart hook reads JSON from stdout. The
    # `additionalContext` field gets injected into the model's context.
    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": _format_inbox(notes),
        }
    }
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
