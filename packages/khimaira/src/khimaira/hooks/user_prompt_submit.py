#!/usr/bin/env python3
"""khimaira UserPromptSubmit hook — inbox auto-read + periodic reminders.

Runs before each user prompt is processed. Two responsibilities:

1. INBOX AUTO-READ (every turn): Calls the khimaira daemon's
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
  ~/.local/state/khimaira/hook-counters/<session_id>.count

Daemon endpoint is configurable via KHIMAIRA_ENDPOINT (default
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

_REMINDER_EVERY = int(os.environ.get("KHIMAIRA_HOOK_REMINDER_EVERY", "8"))
_ENDPOINT = os.environ.get("KHIMAIRA_ENDPOINT", "http://127.0.0.1:8740").rstrip("/")
_INBOX_TIMEOUT_S = 0.8

_COUNTER_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "hook-counters"
)


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


def _sync_rename_to_khimaira(session_id: str) -> None:
    """Auto-sync Claude Code's /rename to khimaira's session_set_name.

    Closes the gap that makes addressing fresh sessions painful:
      1. User runs /rename my-new-session in a fresh Claude Code chat
      2. Claude Code writes a {type: "custom-title"} entry to the
         session's transcript JSONL
      3. But khimaira daemon's session_set_name is never called, so
         other sessions can't address by the renamed handle

    This hook walks the session's own transcript (~/.claude/projects/
    <encoded-cwd>/<session-uuid>.jsonl), finds the most recent custom-
    title, and compares against the khimaira-stored name. If they
    differ, POST to /api/sessions/{id}/name to sync.

    Silent on every failure path — hooks must not block the user.
    Cheap: bounded reverse-iteration over the JSONL file (most recent
    title is usually within the last ~50 lines).
    """
    try:
        # Find the transcript: scan ~/.claude/projects/*/{session_id}.jsonl
        claude_projects = Path(os.path.expanduser("~/.claude/projects"))
        if not claude_projects.exists():
            return
        target_filename = f"{session_id}.jsonl"
        transcript: Path | None = None
        for project_dir in claude_projects.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / target_filename
            if candidate.is_file():
                transcript = candidate
                break
        if transcript is None:
            return

        # Read transcript, find most recent custom-title entry
        latest_title: str | None = None
        with transcript.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or '"custom-title"' not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                # Possible shapes:
                #   {"type": "custom-title", "title": "...", ...}
                #   {"type": "custom-title", "customTitle": "...", ...}
                if rec.get("type") != "custom-title":
                    continue
                title = (
                    rec.get("title") or rec.get("customTitle") or rec.get("name") or ""
                )
                if title:
                    latest_title = title  # keep the last one (most recent)

        if not latest_title:
            return

        # ALWAYS SYNC: /rename is the user's most direct rename intent.
        # It wins over any prior name set via session_set_name (agent's
        # inference). Previous "don't clobber" rule caused fresh /rename
        # events to be silently ignored when khimaira had a stale name.
        # Skip if it would be a no-op (same name) to avoid wasted POSTs.
        try:
            url = f"{_ENDPOINT}/api/sessions/{session_id}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_INBOX_TIMEOUT_S) as resp:
                state = json.loads(resp.read())
            current_name = (state.get("status") or {}).get("name") or ""
            if current_name == latest_title:
                # Already synced — no-op
                return
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            # Couldn't read current name — push the sync anyway; idempotent
            pass

        # POST the new name
        try:
            url = f"{_ENDPOINT}/api/sessions/{session_id}/name"
            data = json.dumps({"name": latest_title}).encode("utf-8")
            req = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=_INBOX_TIMEOUT_S) as _:
                pass
        except (urllib.error.URLError, OSError, TimeoutError):
            pass  # Best-effort; will retry on next prompt
    except Exception:
        pass  # Silent — never break the user's flow over a sync error


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
    expected to surface the content + actively engage with it, not just
    ack and continue.
    """
    lines = [
        f"📬 khimaira inbox: {len(notes)} unread note(s) from other sessions.",
        "**ACTION REQUIRED — handle each note:**",
        "  1. Surface the content to the user (don't just say 'got a note', show it).",
        "  2. **Engage actively** — for each note:",
        "     • If it conveys NEW information that warrants a substantive reply",
        "       (questions, observations needing acknowledgment, decisions",
        "       requiring confirmation, follow-ups to your earlier message):",
        "       draft a response. If the response is clear, send it via",
        "       `/tell <sender_session> '...'`. If uncertain whether to",
        '       respond or HOW to respond, ASK the user: "<sender> said X.',
        '       Should I reply with Y, or do you want to handle?"',
        "     • If it's pure FYI (no implicit ask), surface + ack is enough.",
        '  3. Call `session_ack_notes(session_id="' + session_id + '")` to clear.',
        "",
        "**Don't ack-and-continue silently.** The user posted the message",
        "expecting engagement, not a passive read. Even if you're mid-task,",
        "pause briefly to handle the note properly.",
        "",
    ]
    for n in notes:
        kind = n.get("kind") or "note"
        from_sid = (n.get("from_session_id") or "")[:8] or "external"
        nid = n.get("id", "?")
        remaining = n.get("_remaining_surfaces")
        # 'answer' notes have answer text in `answer` field, not `text`.
        body = (n.get("answer") or n.get("text") or "").strip()
        # 2500 chars (~625 tokens) — bounded by the 3-surface auto-expire.
        # Previous 600-char limit truncated answers mid-content; receivers
        # then reported "body cut off" without the key info even reaching
        # them. Better to spend a few hundred extra tokens than lose the
        # message. Notes longer than this are rare; if they happen, the
        # receiver can call session_pending_notes manually for full body.
        if len(body) > 2500:
            body = (
                body[:2500]
                + f"\n…[truncated, {len(body) - 2500} more chars — call session_pending_notes for full body]"
            )
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
    lines = [f"📨 khimaira incoming: {len(questions)} open question(s) targeting you:"]
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
    lines.append(
        "(re-surfaces every turn until answered; address or withdraw to clear)"
    )
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

    # --- Sync Claude Code's /rename → khimaira's session name (every turn) ---
    # Cheap idempotent check; only POSTs when the names differ. Closes the
    # gap where /rename in Claude Code is UI-only and other sessions can't
    # address by the new name until the agent in the renamed session
    # manually calls session_set_name.
    _sync_rename_to_khimaira(session_id)

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
            "💡 khimaira reminder: any new decisions or open questions worth logging?\n"
            f'  - `session_log_decision(session_id="{session_id}", text="...", why="...")` for commitments\n'
            f'  - `session_log_question(session_id="{session_id}", text="...")` for things a parallel session can research\n'
            "Skip if nothing to log."
        )

    # --- Auto-delegate nudge (opt-in; saves tokens on trivial prompts) ---
    # Heuristic — no API call from this hot path; if the user's prompt
    # looks trivial, surface a strong "consider delegating" nudge so
    # Opus routes it to mcp__khimaira__delegate (haiku-class model)
    # instead of burning thinking budget. Set
    # KHIMAIRA_AUTO_DELEGATE_NUDGE=1 to enable; off by default because
    # the heuristic can trigger false positives that feel naggy.
    delegate_block = ""
    if os.environ.get("KHIMAIRA_AUTO_DELEGATE_NUDGE") in ("1", "true", "yes"):
        prompt_text = (data.get("prompt") or "").strip()
        if _looks_trivial(prompt_text):
            delegate_block = (
                "💡 khimaira auto-delegate: this prompt looks low-effort "
                "(short, factual / lookup-style, no code blocks). Strong "
                "suggestion: call "
                '`mcp__khimaira__delegate(prompt=<user\'s question>, tier="auto")` '
                "to route it to a cheaper model. Skip the delegate ONLY if "
                "you genuinely need Opus's depth (multi-step reasoning, "
                "architectural decisions, debugging that needs full context)."
            )

    if (
        not inbox_block
        and not incoming_block
        and not reminder_block
        and not delegate_block
    ):
        return 0

    additional_context = "\n\n".join(
        b for b in (delegate_block, inbox_block, incoming_block, reminder_block) if b
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
    sys.stdout.write(json.dumps(output))
    return 0


def _looks_trivial(prompt: str) -> bool:
    """Heuristic: does this prompt look like a question that doesn't
    need Opus thinking budget?

    Pure-stdlib, no API call — runs in the hot UserPromptSubmit path
    where latency matters. False-positive rate is intentionally low
    (better to miss delegating than to nag on every prompt). Signals:

      - Short (≤ 20 words) → simple lookup vs multi-step task.
      - Starts with a question word or is interrogative.
      - No code blocks, no file paths, no diff indicators — those
        usually need full context.
      - No imperative work verbs ('implement', 'refactor', 'debug',
        'write a', 'add a') — those want Opus.

    Returns True only when ALL trivial signals fire. Conservative on
    purpose.
    """
    if not prompt:
        return False
    p = prompt.strip()

    # Length gate — long prompts almost always carry context Opus needs.
    word_count = len(p.split())
    if word_count > 20:
        return False

    # Code/diff/path indicators → not trivial.
    if (
        "```" in p
        or "/" in p
        and any(p_seg.endswith(".py") or p_seg.endswith(".ts") for p_seg in p.split())
    ):
        return False
    if any(marker in p for marker in ("$ ", "@@", "diff --git", "<file>", "<path>")):
        return False

    lower = p.lower()

    # Heavy work verbs — these want Opus reasoning.
    heavy_verbs = (
        "implement",
        "refactor",
        "debug",
        "design",
        "architect",
        "review",
        "audit",
        "rewrite",
        "write a function",
        "write a class",
        "write a test",
        "add a feature",
        "add a method",
        "fix the bug",
    )
    if any(v in lower for v in heavy_verbs):
        return False

    # Interrogative / lookup markers — these are good delegate candidates.
    light_markers = (
        "what is",
        "what's",
        "what does",
        "how do i",
        "how does",
        "how to",
        "is this",
        "is the",
        "is it",
        "why is",
        "why does",
        "when should",
        "where is",
        "where does",
        "explain",
        "define",
        "summarize",
        "list",
        "show me",
    )
    if any(p.lower().startswith(m) or m in lower[:30] for m in light_markers):
        return True

    # Short and ends with a question mark — likely a lookup.
    if word_count <= 10 and p.endswith("?"):
        return True

    return False


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
