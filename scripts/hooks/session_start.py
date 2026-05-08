#!/usr/bin/env python3
"""chimera SessionStart hook — surface unread inbox + other active sessions.

Runs at Claude Code session boot (startup, resume, clear). Two jobs:

  1. Read THIS session's inbox.jsonl → mark unread notes read → format them
     into the model's context. Closes the multi-session loop: when window B
     posts an answer to window A's question, window A sees it on next start.

  2. Discover OTHER recently-active sessions (excluding this one). Surface
     them in the context block too so the agent automatically knows about
     parallel work without the user having to know to ask. Without this,
     the user has to explicitly say "what's my other session doing?" — with
     it, window B sees "📋 session A is active, status=implementing" the
     moment it boots.

If both inbox AND no-other-sessions: exit 0 silently with no output.
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


def _discover_other_active_sessions(
    self_session_id: str,
    *,
    within_minutes: int = 30,
) -> list[dict]:
    """Walk ~/.local/state/chimera/sessions/ and return other sessions
    that have been active within the window. Sorted newest-first.

    Reads each session's status.json + checks files_touched.jsonl mtime.
    Skips this session and any with no activity in the window.
    """
    import time

    if not _BASE_DIR.exists():
        return []

    cutoff = time.time() - within_minutes * 60
    out: list[dict] = []

    for d in _BASE_DIR.iterdir():
        if not d.is_dir() or d.name == self_session_id:
            continue

        # Most-recent activity = newest mtime among the session's files
        latest_mtime = 0.0
        for p in d.iterdir():
            if not p.is_file():
                continue
            try:
                m = p.stat().st_mtime
                if m > latest_mtime:
                    latest_mtime = m
            except OSError:
                continue

        if latest_mtime < cutoff:
            continue

        # Read status.json (best-effort)
        status_path = d / "status.json"
        status: dict | None = None
        if status_path.is_file():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                status = None

        # Cheap counts
        decisions = _read_jsonl(d / "decisions.jsonl")
        files = _read_jsonl(d / "files_touched.jsonl")
        questions = _read_jsonl(d / "questions.jsonl")
        open_q = sum(1 for q in questions if q.get("status") == "open")

        out.append({
            "session_id": d.name,
            "last_active_age_s": int(time.time() - latest_mtime),
            "status": status,
            "decision_count": len(decisions),
            "file_touch_count": len(files),
            "open_question_count": open_q,
        })

    out.sort(key=lambda r: r.get("last_active_age_s", 0))
    return out


def _format_active_sessions(sessions: list[dict]) -> str:
    """Render the 'other sessions currently active' block."""
    lines = [
        f"📋 chimera — {len(sessions)} other session(s) active in the last 30 min:",
        "",
    ]
    for s in sessions:
        sid = s.get("session_id", "?")
        status = s.get("status") or {}
        # Friendly name (set via session_set_name) — preferred handle
        name = status.get("name") if isinstance(status, dict) else None
        status_label = status.get("status", "?") if isinstance(status, dict) else "?"
        detail = status.get("detail", "") if isinstance(status, dict) else ""
        age_s = s.get("last_active_age_s", 0)
        age_str = f"{age_s // 60}m ago" if age_s >= 60 else f"{age_s}s ago"
        decisions = s.get("decision_count", 0)
        touches = s.get("file_touch_count", 0)
        open_q = s.get("open_question_count", 0)

        # If named, prefer the name as the handle other sessions use
        handle = f'"{name}"' if name else f'"{sid}"'
        ident_line = (
            f"- `{name}` (id: {sid})" if name else f"- `{sid}`"
        ) + f" (status: {status_label}{', ' + detail if detail else ''})"

        lines.append(ident_line)
        lines.append(
            f"  last active {age_str} · {decisions} decisions · "
            f"{touches} file touches · {open_q} open question(s)"
        )
        lines.append(
            f"  → use session_state({handle}) to read details"
        )
        lines.append("")
    lines.append(
        "If a question/idea you have relates to one of these sessions, you can:\n"
        "  - Read its state with `session_state(...)` — see what it's up to without interrupting\n"
        "  - Answer one of its open questions with `session_post_answer(...)` — its inbox surfaces it on next turn"
    )
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

    # Two parallel jobs: (1) read this session's inbox, (2) discover other
    # active sessions. Either may produce output; we concatenate when both do.
    notes = _consume_inbox(session_id)
    others = _discover_other_active_sessions(session_id, within_minutes=30)

    blocks: list[str] = []
    if notes:
        blocks.append(_format_inbox(notes))
    if others:
        blocks.append(_format_active_sessions(others))

    if not blocks:
        return 0

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n\n---\n\n".join(blocks),
        }
    }
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
