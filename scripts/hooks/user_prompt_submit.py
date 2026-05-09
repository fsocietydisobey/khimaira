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
    """Hit /api/sessions/{sid}/inbox/surface; return notes or [] on failure.

    Uses the surface endpoint (NOT /pending) so notes are NOT marked read
    on first fetch. Notes re-surface every turn until either:
      • The agent calls session_ack_notes after surfacing the content
      • surface_count exceeds the auto-expire threshold (3 surfaces)

    This is symmetric with the incoming-questions behavior: unread/
    unanswered cross-session info stays in context until handled, rather
    than being silently consumed by the hook (which was the v1 design's
    flaw — agents could ignore the injected block and the user would
    never see the message).
    """
    url = f"{_ENDPOINT}/api/sessions/{session_id}/inbox/surface"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_INBOX_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
        notes = payload.get("notes", [])
        return notes if isinstance(notes, list) else []
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []


def _fetch_incoming_questions(session_id: str) -> list[dict]:
    """Hit /api/sessions/{sid}/incoming; return questions or [] on failure.

    Returns OPEN questions from OTHER sessions that target this session
    (target_session_id == this session). These re-surface every turn until
    answered — that's intentional. Unlike inbox notes, an unanswered
    incoming question is still actionable, so we want it visible until
    handled.
    """
    url = f"{_ENDPOINT}/api/sessions/{session_id}/incoming"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_INBOX_TIMEOUT_S) as resp:
            payload = json.loads(resp.read())
        questions = payload.get("questions", [])
        return questions if isinstance(questions, list) else []
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return []


def _format_inbox(notes: list[dict], session_id: str) -> str:
    """Render notes as compact context block.

    Each note carries a `_remaining_surfaces` field — how many more turns
    this note will keep re-injecting before auto-expiring. The agent is
    expected to surface the content to the user AND call session_ack_notes
    to clear the unread flag immediately (not wait for auto-expire).
    """
    lines = [
        f"📬 chimera inbox: {len(notes)} unread note(s) from other sessions.",
        "**ACTION REQUIRED:** surface these to the user in your response,",
        f"then call `session_ack_notes(session_id=\"{session_id}\")` to",
        "clear them. Without ack, they re-surface each turn (up to 3) then",
        "auto-expire — you risk the user never seeing them.",
        "",
    ]
    for n in notes:
        kind = n.get("kind") or "note"
        from_sid = (n.get("from_session_id") or "")[:8] or "external"
        nid = n.get("id", "?")
        remaining = n.get("_remaining_surfaces")
        # 'answer' notes have answer text in `answer` field, not `text`.
        body = (n.get("answer") or n.get("text") or "").strip()
        if len(body) > 600:
            body = body[:600] + "…"
        question_text = (n.get("question_text") or "").strip()
        if question_text and len(question_text) > 200:
            question_text = question_text[:200] + "…"
        urgency = ""
        if remaining is not None:
            if remaining <= 0:
                urgency = " — LAST SURFACE before auto-expire"
            elif remaining == 1:
                urgency = f" — {remaining} more surface remaining"
            else:
                urgency = f" — {remaining} more surfaces remaining"
        lines.append(f"  • [{kind} from {from_sid} | id={nid}{urgency}]")
        if question_text:
            lines.append(f"    re Q: {question_text}")
        lines.append(f"    {body}")
    return "\n".join(lines)


def _format_incoming(questions: list[dict], my_session_id: str) -> str:
    """Render incoming questions targeting this session as a context block.

    Re-surfaces every turn until the question is answered or withdrawn.
    Includes the answer-back snippet so the agent can respond inline.
    """
    lines = [f"📨 chimera incoming: {len(questions)} open question(s) targeting you:"]
    for q in questions:
        from_sid = (q.get("from_session_id") or "")[:8] or "external"
        qid = q.get("id", "?")
        text = (q.get("text") or "").strip()
        if len(text) > 700:
            text = text[:700] + "…"
        lines.append(f"  • [Q={qid} from {from_sid}]")
        lines.append(f"    {text}")
        lines.append(
            f"    ➜ answer with `session_post_answer(target_session_id="
            f"\"{q.get('from_session_id')}\", question_id=\"{qid}\", answer=\"...\")`"
        )
    lines.append("(re-surfaces every turn until answered; address or withdraw to clear)")
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
        inbox_block = _format_inbox(notes, session_id)

    # --- Incoming questions targeting this session (every turn) -----------
    # Re-fetched each turn (no mark-read concept) — open questions stay
    # visible until answered/withdrawn.
    incoming_block = ""
    incoming = _fetch_incoming_questions(session_id)
    if incoming:
        incoming_block = _format_incoming(incoming, session_id)

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

    if not inbox_block and not incoming_block and not reminder_block:
        return 0

    additional_context = "\n\n".join(
        b for b in (inbox_block, incoming_block, reminder_block) if b
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
