#!/usr/bin/env python3
"""chimera UserPromptSubmit hook — inbox auto-read + periodic reminders.

Runs before each user prompt is processed. Two responsibilities:

1. INBOX AUTO-READ (every turn): Calls the chimera daemon's
   /api/sessions/{sid}/pending endpoint to fetch any unread answers
   another session posted to this session's inbox. If there are any,
   they are injected into the agent's context for this turn so cross-
   session coordination doesn't depend on the agent remembering to
   call session_pending_notes manually.

2. PERIODIC REMINDER (every Nth turn): Soft nudge that the agent
   should externalize decisions/questions. Counter is per-session.

We deliberately DO NOT auto-extract decisions from prose — agents tested
poorly at recognizing 'this was a decision'. Manual logging stays manual;
we just nudge.

Counter persisted at:
  ~/.local/state/chimera/hook-counters/<session_id>.count

Daemon endpoint is configurable via CHIMERA_ENDPOINT (default
http://127.0.0.1:8740). Failure to reach the daemon is silent — hooks
must never block or surface errors that interrupt the user's flow.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

_REMINDER_EVERY = int(os.environ.get("CHIMERA_HOOK_REMINDER_EVERY", "8"))
_ENDPOINT = os.environ.get("CHIMERA_ENDPOINT", "http://127.0.0.1:8740").rstrip("/")
_INBOX_TIMEOUT_S = 0.8

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


def _fetch_pending_notes(session_id: str) -> list[dict]:
    """Hit /api/sessions/{sid}/pending; return notes list or [] on any failure.

    mark_read=true so notes don't re-surface on subsequent turns. The agent
    sees each note exactly once unless another session re-posts.
    """
    url = f"{_ENDPOINT}/api/sessions/{session_id}/pending?mark_read=true"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_INBOX_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
        notes = payload.get("notes", [])
        return notes if isinstance(notes, list) else []
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []


def _format_inbox(notes: list[dict]) -> str:
    """Render notes as compact context block. Truncates long bodies."""
    lines = [f"📬 chimera inbox: {len(notes)} new note(s) from other sessions:"]
    for n in notes:
        kind = n.get("kind") or "note"
        from_sid = (n.get("from_session_id") or "")[:8] or "external"
        text = (n.get("text") or "").strip()
        if len(text) > 600:
            text = text[:600] + "…"
        question_text = (n.get("question_text") or "").strip()
        if question_text and len(question_text) > 200:
            question_text = question_text[:200] + "…"
        lines.append(f"  • [{kind} from {from_sid}]")
        if question_text:
            lines.append(f"    re Q: {question_text}")
        lines.append(f"    {text}")
    lines.append("(auto-read; no need to call session_pending_notes for these)")
    return "\n".join(lines)


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

    # --- Inbox auto-read (every turn) -------------------------------------
    inbox_block = ""
    notes = _fetch_pending_notes(session_id)
    if notes:
        inbox_block = _format_inbox(notes)

    # --- Periodic decision/question reminder (every Nth turn) -------------
    safe = session_id.replace("/", "_").replace("..", "_")
    counter_file = _COUNTER_DIR / f"{safe}.count"
    count = _read_count(counter_file)
    new_count = count + 1
    _write_count(counter_file, new_count)

    reminder_block = ""
    if new_count >= 2 and new_count % _REMINDER_EVERY == 0:
        reminder_block = (
            "💡 chimera reminder: any new decisions or open questions worth logging?\n"
            f"  - `session_log_decision(session_id=\"{session_id}\", text=\"...\", why=\"...\")` for commitments\n"
            f"  - `session_log_question(session_id=\"{session_id}\", text=\"...\")` for things a parallel session can research\n"
            "Skip if nothing to log."
        )

    if not inbox_block and not reminder_block:
        return 0

    additional_context = "\n\n".join(b for b in (inbox_block, reminder_block) if b)

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
