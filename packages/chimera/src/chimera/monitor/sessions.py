"""Multi-session shared state — externalize Claude Code session context.

The problem: when one Claude Code session is grinding on a task, you can't
ask related questions in another window without losing the working session's
context. Forks (Agent tool) solve "background work" but not "side conversation
that sees what the working agent is doing."

The solution: each session writes its decisions, file-touches, status, and
open questions to JSONL files chimera tracks. Other sessions query that
state via MCP. Session B can post answers BACK to session A; A reads them
automatically on its next turn (via SessionStart hook).

Storage: ~/.local/state/chimera/sessions/<session_id>/
  - decisions.jsonl       — append-only log of agent's recorded decisions
  - files_touched.jsonl   — append-only log of file modifications
  - questions.jsonl       — open questions (with answer field updated in-place)
  - status.json           — current state ("researching"/"implementing"/"blocked")
  - inbox.jsonl           — answers from other sessions to this session's questions

Design notes (incorporated from review):
  1. File-touch is automated via PostToolUse hook on Edit/Write/MultiEdit —
     zero agent burden. Decisions/questions are nudged via periodic reminder
     injection, NOT auto-extracted from prose (extraction unreliable).
  2. Write-back is symmetric — `session_post_answer` (B→A) plus
     `session_pending_notes` + auto-read on SessionStart hook (A reads).
     Without these, the design collapses to "B reads A, human relays" which
     only solves half the problem.
  3. Inbox auto-read is critical. SessionStart calls session_pending_notes;
     unread answers surface in A's system prompt. Agent sees "B answered Q3"
     without the user having to know to ask.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Any  # noqa: F401  (used in helper signatures)

from chimera.log import get_logger

log = get_logger("monitor.sessions")

_BASE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "chimera" / "sessions"


def _session_dir(session_id: str) -> Path:
    """Resolve the per-session storage directory, creating it lazily."""
    safe = session_id.replace("/", "_").replace("..", "_")
    d = _BASE_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return out


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Write side — called by the WORKING agent (session A) as it works
# ---------------------------------------------------------------------------


def log_decision(session_id: str, text: str, why: str = "") -> dict:
    """Record a decision the agent has made. Surfaces to other sessions
    via session_state(session_id)."""
    record = {
        "ts": _now_iso(),
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "why": why,
    }
    _append_jsonl(_session_dir(session_id) / "decisions.jsonl", record)
    log.info("session %s: decision recorded — %s", session_id, text[:80])
    return record


def log_touch(
    session_id: str,
    file: str,
    summary: str = "",
    line_range: tuple[int, int] | None = None,
) -> dict:
    """Record a file modification. Typically called automatically from a
    PostToolUse hook on Edit/Write/MultiEdit — agent doesn't have to remember.
    """
    record = {
        "ts": _now_iso(),
        "file": file,
        "summary": summary,
        "line_start": line_range[0] if line_range else None,
        "line_end": line_range[1] if line_range else None,
    }
    _append_jsonl(_session_dir(session_id) / "files_touched.jsonl", record)
    return record


def log_question(
    session_id: str,
    text: str,
    target_session_id: str | None = None,
) -> dict:
    """Open a question that another session can answer.

    Returns the question record including its `id` — the handle B uses in
    `post_answer`.

    If `target_session_id` is provided, the question is *targeted* — the
    target session's UserPromptSubmit hook will surface it as an incoming
    question on its next turn, without requiring the target to poll
    session_state. Accepts either a UUID or a friendly name; resolved to
    UUID at write time so subsequent name changes don't orphan the link.

    If `target_session_id` is None, the question is "broadcast" — visible
    only to sessions that explicitly inspect session_state(this_session).
    """
    resolved_target: str | None = None
    if target_session_id:
        try:
            resolved_target = resolve_session_id(target_session_id)
        except ValueError:
            # Unresolvable target — log it as the literal value rather than
            # erroring; the target may not exist yet, and hooks should still
            # render the question on its session_state. Better than refusing
            # to log.
            resolved_target = target_session_id

    record = {
        "ts": _now_iso(),
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "status": "open",      # "open" | "answered" | "withdrawn"
        "answer": None,
        "answered_by": None,
        "answered_at": None,
        "target_session_id": resolved_target,  # None == broadcast
    }
    _append_jsonl(_session_dir(session_id) / "questions.jsonl", record)
    log.info(
        "session %s: question opened (id=%s, target=%s) — %s",
        session_id, record["id"], resolved_target or "broadcast", text[:80],
    )
    return record


def set_status(session_id: str, status: str, detail: str = "") -> dict:
    """Update the agent's high-level state. Other sessions see this in
    session_state. Free-form string but conventional values: 'researching',
    'implementing', 'blocked', 'awaiting-review', 'idle'.

    Preserves any existing `name` field — use `set_name()` to change that.
    """
    path = _session_dir(session_id) / "status.json"
    # Preserve name (and other future metadata) on status updates
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    record = {
        **existing,
        "status": status,
        "detail": detail,
        "updated_at": _now_iso(),
    }
    path.write_text(json.dumps(record, indent=2))
    return record


def set_name(session_id: str, name: str) -> dict:
    """Set a friendly name for the session — surfaces in session_list and
    enables name-based resolution from other sessions.

    Names should be slug-shaped: lowercase, dashes, no spaces. Two sessions
    can share a name; lookup prefers most-recently-active.
    """
    path = _session_dir(session_id) / "status.json"
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    record = {**existing, "name": name, "updated_at": _now_iso()}
    record.setdefault("status", "idle")
    record.setdefault("detail", "")
    path.write_text(json.dumps(record, indent=2))
    log.info("session %s: named %r", session_id, name)
    return record


def resolve_session_id(query: str) -> str:
    """Map a user-friendly query → exact session_id (UUID).

    Resolution order:
      1. If query is an existing session_id (directory exists), return as-is.
      2. Otherwise, search every session's status.json for a `name` match;
         if multiple sessions share the name, return the most-recently-active.
      3. Otherwise, raise ValueError with a helpful message.

    Used by the read-side tools (state, pending_notes, post_answer) so users
    can pass either UUIDs or names interchangeably.
    """
    safe = query.replace("/", "_").replace("..", "_")
    if (_BASE_DIR / safe).is_dir():
        return safe

    # Name-based search
    if not _BASE_DIR.exists():
        raise ValueError(f"No session named or id'd {query!r} (no sessions exist yet).")

    candidates: list[tuple[float, str]] = []
    for d in _BASE_DIR.iterdir():
        if not d.is_dir():
            continue
        status_path = d / "status.json"
        if not status_path.is_file():
            continue
        try:
            s = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if s.get("name") == query:
            try:
                mtime = max(
                    (p.stat().st_mtime for p in d.iterdir() if p.is_file()),
                    default=0.0,
                )
            except OSError:
                mtime = 0.0
            candidates.append((mtime, d.name))

    if not candidates:
        raise ValueError(
            f"No session named or id'd {query!r}. "
            f"Use session_list() to see available sessions."
        )

    # Most-recently-active wins on name collision
    candidates.sort(reverse=True)
    return candidates[0][1]


# ---------------------------------------------------------------------------
# Cross-session — B writes back to A
# ---------------------------------------------------------------------------


def post_answer(
    target_session_id: str,
    question_id: str,
    answer: str,
    *,
    from_session_id: str = "external",
) -> dict:
    """Session B answers session A's open question.

    Updates the question's record in-place (status → answered) AND drops a
    note in A's inbox. A's SessionStart hook calls session_pending_notes,
    which reads the inbox and surfaces unread answers.

    `target_session_id` accepts either a UUID or a friendly name.
    """
    target_session_id = resolve_session_id(target_session_id)
    qpath = _session_dir(target_session_id) / "questions.jsonl"
    questions = _read_jsonl(qpath)
    matched: dict | None = None
    rewritten: list[dict] = []
    for q in questions:
        if q.get("id") == question_id and q.get("status") == "open":
            q["status"] = "answered"
            q["answer"] = answer
            q["answered_by"] = from_session_id
            q["answered_at"] = _now_iso()
            matched = q
        rewritten.append(q)

    if matched is None:
        raise ValueError(
            f"No open question with id={question_id!r} in session {target_session_id!r}. "
            f"Was it already answered, or wrong id?"
        )

    # Atomic rewrite
    tmp = qpath.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for q in rewritten:
            f.write(json.dumps(q, separators=(",", ":")) + "\n")
    tmp.replace(qpath)

    # Drop a note in the inbox so A surfaces it on next read
    note = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "kind": "answer",
        "question_id": question_id,
        "question_text": matched.get("text", ""),
        "answer": answer,
        "from_session_id": from_session_id,
        "read": False,
        "surface_count": 0,
    }
    _append_jsonl(_session_dir(target_session_id) / "inbox.jsonl", note)
    log.info(
        "session %s: answer posted by %s for q=%s",
        target_session_id, from_session_id, question_id,
    )
    return matched


# ---------------------------------------------------------------------------
# Read side — for B (querying A) and for A (reading its own inbox)
# ---------------------------------------------------------------------------


def state(session_id: str, recent: int = 10) -> dict:
    """Full digest of session_id's externalized state. The 'what is session A
    up to right now' query.

    Accepts either a session UUID OR a friendly name (set via set_name).
    """
    session_id = resolve_session_id(session_id)
    d = _session_dir(session_id)
    decisions = _read_jsonl(d / "decisions.jsonl")
    files = _read_jsonl(d / "files_touched.jsonl")
    questions = _read_jsonl(d / "questions.jsonl")
    status_path = d / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else None

    return {
        "session_id": session_id,
        "status": status,
        "recent_decisions": decisions[-recent:],
        "decision_count": len(decisions),
        "recent_files": files[-recent:],
        "file_touch_count": len(files),
        "open_questions": [q for q in questions if q.get("status") == "open"],
        "answered_questions": [q for q in questions if q.get("status") == "answered"][-recent:],
    }


def recent_decisions(across_sessions: bool = True, recent_per_session: int = 5) -> list[dict]:
    """Recent decisions across all sessions (or just the active ones)."""
    if not _BASE_DIR.exists():
        return []
    out: list[dict] = []
    for sd in _BASE_DIR.iterdir():
        if not sd.is_dir():
            continue
        decisions = _read_jsonl(sd / "decisions.jsonl")[-recent_per_session:]
        for d in decisions:
            d["session_id"] = sd.name
            out.append(d)
    out.sort(key=lambda d: d.get("ts", ""), reverse=True)
    return out


def pending_notes(session_id: str, mark_read: bool = True) -> list[dict]:
    """A reads its inbox — unread notes from other sessions.

    Called by /inbox skill (mark_read=true) and by old SessionStart hooks.
    The newer auto-inject UserPromptSubmit hook uses surface_inbox_for_hook
    (different path — peek + count, doesn't drain).

    When mark_read=True, drained notes get moved to archive.jsonl (not
    just marked read in inbox.jsonl) so the inbox stays focused on
    current pending. History remains queryable via search_archive.

    `session_id` accepts either a UUID or a friendly name.
    """
    session_id = resolve_session_id(session_id)
    sd = _session_dir(session_id)
    inbox_path = sd / "inbox.jsonl"
    archive_path = sd / "archive.jsonl"
    notes = _read_jsonl(inbox_path)
    pending = [n for n in notes if not n.get("read")]

    if mark_read and pending:
        archived: list[dict] = []
        remaining: list[dict] = []
        for n in notes:
            if n.get("read"):
                archived.append(n)
                continue
            n["read"] = True
            n["read_at"] = _now_iso()
            n["read_reason"] = n.get("read_reason") or "pending_notes_drain"
            archived.append(n)

        with archive_path.open("a", encoding="utf-8") as f:
            for n in archived:
                f.write(json.dumps(n, separators=(",", ":")) + "\n")
        tmp = inbox_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for n in remaining:
                f.write(json.dumps(n, separators=(",", ":")) + "\n")
        tmp.replace(inbox_path)

    return pending


_HANDOFFS_PATH = _BASE_DIR.parent / "handoffs.jsonl"


_CLAUDE_PROJECTS_DIR = Path(os.path.expanduser("~/.claude/projects"))


def _find_transcript(session_id: str) -> Path | None:
    """Locate the Claude Code transcript file for `session_id`.

    Claude Code stores transcripts at ~/.claude/projects/<encoded-cwd>/<uuid>.jsonl.
    Encoded cwd: leading slash + each path separator replaced with '-',
    so /home/_3ntropy/dev/chimera → -home--3ntropy-dev-chimera. Different
    projects each get their own subdir, so we scan all of them.

    Returns the first match, or None if no transcript exists for this id
    (session never logged anything to disk, or has been deleted).
    """
    if not _CLAUDE_PROJECTS_DIR.exists():
        return None
    target = f"{session_id}.jsonl"
    for project_dir in _CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        candidate = project_dir / target
        if candidate.is_file():
            return candidate
    return None


def _extract_text_from_message(msg: Any) -> str:
    """Pull readable text out of a transcript message field.

    Claude Code transcript JSONL has nested message structures:
    - user messages: {message: {content: "string"}} OR {message: {content: [{type, text}]}}
    - assistant messages: {message: {content: [{type: "text", text: "..."}, {type: "tool_use", ...}]}}
    - tool_result: {tool_use_id, content: "..." OR [{type, text}]}

    Returns concatenated readable text; tool_use args/results stringified.
    """
    if isinstance(msg, str):
        return msg
    if isinstance(msg, list):
        out = []
        for part in msg:
            out.append(_extract_text_from_message(part))
        return " ".join(p for p in out if p)
    if isinstance(msg, dict):
        # Direct text content
        if "text" in msg and isinstance(msg["text"], str):
            return msg["text"]
        # Tool use — return name + brief arg summary
        if msg.get("type") == "tool_use":
            tname = msg.get("name", "?")
            args = msg.get("input", {})
            if isinstance(args, dict):
                arg_summary = ", ".join(
                    f"{k}={str(v)[:60]}" for k, v in args.items()
                )[:300]
            else:
                arg_summary = str(args)[:300]
            return f"[tool_use {tname}({arg_summary})]"
        # Tool result
        if msg.get("type") == "tool_result":
            content = msg.get("content")
            return f"[tool_result {_extract_text_from_message(content)[:500]}]"
        # Nested message
        if "message" in msg:
            return _extract_text_from_message(msg["message"])
        if "content" in msg:
            return _extract_text_from_message(msg["content"])
    return ""


def query_transcript(
    session_id: str,
    query: str,
    *,
    context_lines: int = 1,
    max_matches: int = 20,
) -> dict:
    """Grep a session's Claude Code transcript for `query` (case-insensitive
    substring). Returns matched turns with surrounding context.

    Use case: a future session needs to know what a now-stopped session
    discussed about a specific topic. Read what they said without being
    able to re-prompt them.

    `context_lines`: how many adjacent turns to include before+after each
    match (1 = the turn before and after; 0 = match only).
    `max_matches`: cap result set so a query like "the" doesn't return
    thousands of hits.
    """
    transcript = _find_transcript(session_id)
    if transcript is None:
        return {
            "session_id": session_id,
            "found": False,
            "error": f"no transcript on disk for session {session_id!r}",
            "matches": [],
        }

    # Load all turns
    turns: list[dict] = []
    try:
        with transcript.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                line = line.strip()
                if not line:
                    continue
                try:
                    turn = json.loads(line)
                except json.JSONDecodeError:
                    continue
                turn["_line_no"] = i
                turn["_text"] = _extract_text_from_message(turn)
                turns.append(turn)
    except OSError as e:
        return {
            "session_id": session_id,
            "found": False,
            "error": f"transcript read failed: {e}",
            "matches": [],
        }

    q = query.lower()
    matches: list[dict] = []
    for idx, turn in enumerate(turns):
        if q not in turn["_text"].lower():
            continue
        start = max(0, idx - context_lines)
        end = min(len(turns), idx + context_lines + 1)
        excerpt_turns = []
        for j in range(start, end):
            t = turns[j]
            excerpt_turns.append({
                "line_no": t["_line_no"],
                "type": t.get("type") or "?",
                "role": (t.get("message") or {}).get("role") if isinstance(t.get("message"), dict) else None,
                "is_match": j == idx,
                "text_preview": t["_text"][:500] + ("…" if len(t["_text"]) > 500 else ""),
            })
        matches.append({
            "match_at_turn": idx,
            "match_at_line": turn["_line_no"],
            "excerpt": excerpt_turns,
        })
        if len(matches) >= max_matches:
            break

    return {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "total_turns": len(turns),
        "query": query,
        "match_count": len(matches),
        "truncated": len(matches) >= max_matches,
        "matches": matches,
    }


def summarize_transcript(
    session_id: str,
    *,
    focus: str | None = None,
) -> dict:
    """Heuristic summary of a session's transcript — no LLM call.

    Returns: turn counts by role, top tool calls by frequency, list of
    file paths mentioned in tool_use args, recent user messages (often
    convey "what was the user asking about"), and recent assistant
    text-message intros (first 200 chars of each assistant text turn).

    The calling agent can read this and reconstruct what the prior
    session was working on, then dig deeper with query_transcript on
    specific keywords. No tokens spent on LLM-side summarization.

    `focus`: when provided, also runs query_transcript(focus) and
    embeds the results in the response.
    """
    from collections import Counter

    transcript = _find_transcript(session_id)
    if transcript is None:
        return {
            "session_id": session_id,
            "found": False,
            "error": f"no transcript on disk for session {session_id!r}",
        }

    turns_by_role: Counter[str] = Counter()
    tool_uses: Counter[str] = Counter()
    file_paths: set[str] = set()
    user_messages: list[str] = []
    assistant_text_intros: list[str] = []

    try:
        with transcript.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    turn = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ttype = turn.get("type") or "?"
                msg = turn.get("message") or {}
                role = msg.get("role") if isinstance(msg, dict) else None
                turns_by_role[role or ttype] += 1

                # Extract tool_use names + file paths
                content = msg.get("content") if isinstance(msg, dict) else None
                if isinstance(content, list):
                    for part in content:
                        if not isinstance(part, dict):
                            continue
                        if part.get("type") == "tool_use":
                            tool_uses[part.get("name", "?")] += 1
                            args = part.get("input", {})
                            if isinstance(args, dict):
                                for v in args.values():
                                    if isinstance(v, str) and "/" in v and len(v) < 300:
                                        # Heuristic: looks like a file path
                                        if v.startswith("/") or v.startswith("./") or "." in v.rsplit("/", 1)[-1]:
                                            file_paths.add(v)
                        if part.get("type") == "text" and role == "assistant":
                            text = part.get("text", "").strip()
                            if text:
                                assistant_text_intros.append(text[:200])

                if role == "user":
                    text = _extract_text_from_message(msg)
                    if text:
                        user_messages.append(text[:300])
    except OSError as e:
        return {
            "session_id": session_id,
            "found": False,
            "error": f"transcript read failed: {e}",
        }

    summary: dict = {
        "session_id": session_id,
        "transcript_path": str(transcript),
        "transcript_size_kb": round(transcript.stat().st_size / 1024, 1),
        "turns_by_role": dict(turns_by_role),
        "top_tools_used": dict(tool_uses.most_common(15)),
        "files_touched_count": len(file_paths),
        "files_touched_sample": sorted(file_paths)[:30],
        "user_messages_count": len(user_messages),
        "user_messages_recent": user_messages[-10:],
        "assistant_text_count": len(assistant_text_intros),
        "assistant_text_recent_intros": assistant_text_intros[-10:],
    }

    if focus:
        focused = query_transcript(session_id, focus, max_matches=10)
        summary["focus_query"] = focus
        summary["focus_matches"] = focused.get("matches", [])
        summary["focus_match_count"] = focused.get("match_count", 0)

    return summary


def post_handoff(
    from_session_id: str,
    text: str,
    *,
    scope_cwd: str | None = None,
    expires_in_hours: float = 168.0,
) -> dict:
    """Drop a handoff note any FUTURE session in this project will read.

    Closes the gap that post_notice left open: post_notice requires a
    target_session_id, but cross-session handoffs to sessions that
    don't exist yet (e.g. "next chat that picks up this work") have
    no target. Workaround was naming yourself + logging a HANDOFF
    decision, then having the user relay a bootstrap prompt to the
    new chat — manual + lossy.

    Handoffs are scoped by working directory. When a new session
    starts via SessionStart hook, it reads handoffs.jsonl and surfaces
    any whose `scope_cwd` is == or a prefix of the new session's cwd
    (and that the new session hasn't already read).

    `scope_cwd=None` infers from the asker's most-recent file_touched
    parent directory — the dir that session was working in. Override
    when the asker has been touching files across multiple project
    roots and wants to disambiguate.

    Default `expires_in_hours=168` (7 days) — work moves on; stale
    handoffs become noise. Pass a larger value for "permanent context"
    notes, smaller for time-bounded asks.
    """
    inferred = scope_cwd
    if inferred is None:
        # Infer from session's most-recent file touch — that's the
        # directory they were working in. Fallback: process cwd.
        files = _read_jsonl(_session_dir(from_session_id) / "files_touched.jsonl")
        if files:
            most_recent = files[-1].get("file") or ""
            if most_recent:
                inferred = os.path.dirname(most_recent)
        if not inferred:
            inferred = os.getcwd()

    inferred = os.path.abspath(inferred)
    expires_at = time.time() + expires_in_hours * 3600.0

    handoff = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "from_session_id": from_session_id,
        "text": text,
        "scope_cwd": inferred,
        "expires_at": expires_at,
        "read_by": [],
    }
    _HANDOFFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _append_jsonl(_HANDOFFS_PATH, handoff)
    log.info(
        "handoff posted by %s for cwd=%s — %s",
        from_session_id, inferred, text[:80],
    )
    return handoff


def consume_handoffs(session_id: str, cwd: str) -> list[dict]:
    """Return handoffs matching this session's cwd; mark this session as
    having read them (no double-surface on session resume).

    Match: handoff.scope_cwd is == cwd OR cwd is a child of scope_cwd
    (so a handoff scoped at /repo/root surfaces in any session working
    in /repo/root/sub/path/...).

    Excluded: handoffs already in this session's read_by, or expired.
    """
    if not _HANDOFFS_PATH.exists():
        return []
    cwd_abs = os.path.abspath(cwd)
    handoffs = _read_jsonl(_HANDOFFS_PATH)
    now = time.time()
    matched: list[dict] = []
    needs_rewrite = False
    has_expired = False

    for h in handoffs:
        if h.get("expires_at", 0) < now:
            has_expired = True
            continue
        scope = h.get("scope_cwd") or ""
        if not scope:
            continue
        # Match: cwd is the scope, or starts with scope + os.sep
        if cwd_abs != scope and not cwd_abs.startswith(scope.rstrip("/") + "/"):
            continue
        read_by = h.get("read_by") or []
        if session_id in read_by:
            continue
        matched.append(h)
        h["read_by"] = read_by + [session_id]
        needs_rewrite = True

    # Rewrite if EITHER we marked something read OR there are expired
    # entries to drop. Without the second condition, an all-expired file
    # accumulates forever — the gc only runs when something else happens
    # to fire, which may never.
    if needs_rewrite or has_expired:
        tmp = _HANDOFFS_PATH.with_suffix(".jsonl.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                for h in handoffs:
                    if h.get("expires_at", 0) < now:
                        continue
                    f.write(json.dumps(h, separators=(",", ":")) + "\n")
            tmp.replace(_HANDOFFS_PATH)
        except OSError:
            log.warning("failed to rewrite handoffs.jsonl; read state may double-surface")

    return matched


def post_notice(
    target_session_id: str,
    text: str,
    *,
    from_session_id: str = "external",
) -> dict:
    """Drop a "FYI" / "ack" note in another session's inbox. No question
    required, no answer expected.

    Fills the gap between session_log_question (requires answer) and
    session_log_decision (only visible on pull). Use cases: "thanks,
    landed" / "FYI I went with option C" / "your patch fixed it" — info
    that the other session benefits from seeing but shouldn't have to
    respond to.

    The note re-surfaces on the target's UserPromptSubmit hook every
    turn until either:
      • The agent explicitly calls `session_ack_notes` after surfacing
        the notice content to the user, OR
      • surface_count exceeds the auto-expire threshold (3 surfaces)
        as a safety net so an unresponsive agent doesn't loop forever.

    `target_session_id` accepts UUID or friendly name. `from_session_id`
    is for attribution.
    """
    target_session_id = resolve_session_id(target_session_id)
    note = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "kind": "notice",
        "text": text,
        "from_session_id": from_session_id,
        "read": False,
        "surface_count": 0,
    }
    _append_jsonl(_session_dir(target_session_id) / "inbox.jsonl", note)
    log.info(
        "session %s: notice posted by %s (id=%s) — %s",
        target_session_id, from_session_id, note["id"], text[:80],
    )
    return note


_HOOK_AUTO_EXPIRE_AFTER = 3
"""Max times an inbox note re-surfaces on the hook before auto-marking read.

Safety net so an unresponsive agent (one that ignores notices in its
context block instead of surfacing them to the user) doesn't loop the
same notice into context forever. Agents SHOULD ack via session_ack_notes
when they've surfaced the content; this is the fallback.
"""


def surface_inbox_for_hook(session_id: str) -> list[dict]:
    """Hook-only fetch path. Returns unread notes, increments surface_count.

    Differs from pending_notes: doesn't mark read on first fetch. Notes
    re-surface each turn until the agent explicitly acks (via
    session_ack_notes) OR surface_count hits the auto-expire threshold,
    in which case they also get moved to archive.jsonl.

    Each returned note carries a `_remaining_surfaces` field so the hook
    can render urgency info ("[2/3 surfaces remaining — call ack]").
    """
    session_id = resolve_session_id(session_id)
    sd = _session_dir(session_id)
    inbox_path = sd / "inbox.jsonl"
    archive_path = sd / "archive.jsonl"
    notes = _read_jsonl(inbox_path)
    surfaced: list[dict] = []
    archived: list[dict] = []
    remaining: list[dict] = []
    modified = False

    for n in notes:
        if n.get("read"):
            archived.append(n)
            continue
        n["surface_count"] = int(n.get("surface_count") or 0) + 1
        modified = True

        if n["surface_count"] >= _HOOK_AUTO_EXPIRE_AFTER:
            n["read"] = True
            n["read_at"] = _now_iso()
            n["read_reason"] = "auto_after_surfaces"
            archived.append(n)
        else:
            remaining.append(n)

        copy = dict(n)
        copy["_remaining_surfaces"] = max(
            0, _HOOK_AUTO_EXPIRE_AFTER - n["surface_count"]
        )
        surfaced.append(copy)

    if modified:
        if archived:
            with archive_path.open("a", encoding="utf-8") as f:
                for n in archived:
                    f.write(json.dumps(n, separators=(",", ":")) + "\n")
        tmp = inbox_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for n in remaining:
                f.write(json.dumps(n, separators=(",", ":")) + "\n")
        tmp.replace(inbox_path)

    return surfaced


def ack_notes(
    session_id: str,
    note_ids: list[str] | None = None,
) -> int:
    """Explicitly mark inbox notes as read AND move them to archive.

    Called by the agent after surfacing notice content to the user, so
    the same notice doesn't re-loop into context next turn. Pass
    `note_ids=None` to ack all currently-unread notes.

    Read notes get moved from inbox.jsonl → archive.jsonl so the inbox
    stays small (just current pending) while history remains greppable
    via search_archive(). Past behavior left read notes in inbox.jsonl
    forever — fine functionally but bloated the file over time.

    Returns the count of notes newly marked read.
    """
    session_id = resolve_session_id(session_id)
    sd = _session_dir(session_id)
    inbox_path = sd / "inbox.jsonl"
    archive_path = sd / "archive.jsonl"
    notes = _read_jsonl(inbox_path)
    count = 0

    target_set = set(note_ids) if note_ids else None
    archived: list[dict] = []
    remaining: list[dict] = []

    for n in notes:
        if n.get("read"):
            archived.append(n)  # already-read notes also archive cleanly
            continue
        if target_set is not None and n.get("id") not in target_set:
            remaining.append(n)
            continue
        n["read"] = True
        n["read_at"] = _now_iso()
        n["read_reason"] = "agent_ack"
        archived.append(n)
        count += 1

    if archived:
        # Append to archive (preserves archive history across multiple acks)
        with archive_path.open("a", encoding="utf-8") as f:
            for n in archived:
                f.write(json.dumps(n, separators=(",", ":")) + "\n")
        # Rewrite inbox with only the still-unread/unmatched notes
        tmp = inbox_path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for n in remaining:
                f.write(json.dumps(n, separators=(",", ":")) + "\n")
        tmp.replace(inbox_path)

    return count


def search_archive(
    session_id: str,
    query: str | None = None,
    limit: int = 50,
) -> list[dict]:
    """Search archived (already-read) inbox notes by substring match.

    Closes the "what did chimera-builder say about Roboflow last week?"
    workflow — read notes were previously lost to inbox.jsonl bloat.
    With ack_notes archiving them, this returns matching past notes.

    `query` is a case-insensitive substring match against the note's
    body field (`answer` for kind="answer", `text` for kind="notice")
    AND its question_text if present. Pass None to return all archived
    notes (most-recent-first).

    `limit` caps the result set; archive can grow large over time.
    """
    session_id = resolve_session_id(session_id)
    archive_path = _session_dir(session_id) / "archive.jsonl"
    archived = _read_jsonl(archive_path)

    if query:
        q = query.lower()
        def _matches(n: dict) -> bool:
            body = (n.get("answer") or n.get("text") or "").lower()
            qtext = (n.get("question_text") or "").lower()
            return q in body or q in qtext
        archived = [n for n in archived if _matches(n)]

    archived.sort(key=lambda n: n.get("ts", ""), reverse=True)
    return archived[:limit]


async def wait_for_answer(
    target_session_id: str,
    question_id: str,
    timeout: float = 300.0,
    poll_interval: float = 1.0,
) -> dict:
    """Block until a specific question is answered, or timeout.

    Real-time-ish coordination primitive: session A logs a targeted
    question on B, then awaits B's answer in the SAME TURN. Without this,
    A's turn ends and A only sees B's answer on its next user prompt —
    forcing the user to type "ok" twice (wake A again) just to relay
    information that's already in the system.

    Implementation: tail-poll questions.jsonl every poll_interval. The
    target session may answer via session_post_answer (atomic rewrite),
    so we always re-read from disk rather than caching.

    `target_session_id` accepts UUID or friendly name; resolved once at
    entry. `question_id` is the 12-char hex id returned by log_question.

    Raises asyncio.TimeoutError if no answer arrives in time.
    """
    import asyncio

    target_session_id = resolve_session_id(target_session_id)
    qpath = _session_dir(target_session_id) / "questions.jsonl"
    deadline = time.time() + timeout

    while time.time() < deadline:
        questions = _read_jsonl(qpath)
        for q in questions:
            if q.get("id") != question_id:
                continue
            status = q.get("status")
            if status == "answered":
                return q
            if status == "withdrawn":
                raise ValueError(
                    f"Question {question_id} was withdrawn before being answered."
                )
            break  # found the question but not yet answered; keep polling
        await asyncio.sleep(poll_interval)

    raise asyncio.TimeoutError(
        f"No answer to question {question_id} on session "
        f"{target_session_id} within {timeout:.0f}s"
    )


def incoming_questions(session_id: str) -> list[dict]:
    """Open questions on OTHER sessions that target this session.

    Symmetric counterpart to pending_notes. The chimera multi-session
    model originally only had two write paths — A logs a question (broadcast,
    no target) and B answers it (lands in A's inbox). To ASK B a question,
    A had to log a question on A's session and rely on B polling A's
    session_state — a discipline-dependent step that produced "their inbox
    is empty" confusion (B looking for incoming questions in their own
    inbox, finding nothing because A's question lives on A's session).

    This function closes the loop: it scans all sessions' questions.jsonl
    files for OPEN questions where target_session_id == this session, and
    returns them with the asking-session id attached. The UserPromptSubmit
    hook fetches this and injects it alongside the inbox so B sees A's
    targeted question on B's next turn without poll-the-other-session
    discipline.

    `session_id` accepts either a UUID or a friendly name. Resolves to
    UUID before scanning so name-changes after the question was logged
    still match.
    """
    session_id = resolve_session_id(session_id)
    if not _BASE_DIR.exists():
        return []
    out: list[dict] = []
    for sd in _BASE_DIR.iterdir():
        if not sd.is_dir():
            continue
        if sd.name == session_id:
            continue  # don't surface our own questions to ourselves
        questions = _read_jsonl(sd / "questions.jsonl")
        for q in questions:
            if q.get("status") != "open":
                continue
            if q.get("target_session_id") != session_id:
                continue
            out.append({
                **q,
                "from_session_id": sd.name,
            })
    out.sort(key=lambda q: q.get("ts", ""), reverse=True)
    return out


def list_sessions() -> list[dict]:
    """All sessions with their last-modified timestamp + summary counts."""
    if not _BASE_DIR.exists():
        return []
    out: list[dict] = []
    for sd in _BASE_DIR.iterdir():
        if not sd.is_dir():
            continue
        last_mtime = max(
            (p.stat().st_mtime for p in sd.iterdir() if p.is_file()),
            default=0.0,
        )
        decisions = sum(1 for _ in (sd / "decisions.jsonl").open() if _.strip()) if (sd / "decisions.jsonl").exists() else 0
        files = sum(1 for _ in (sd / "files_touched.jsonl").open() if _.strip()) if (sd / "files_touched.jsonl").exists() else 0
        questions = _read_jsonl(sd / "questions.jsonl")
        open_q = sum(1 for q in questions if q.get("status") == "open")
        status_path = sd / "status.json"
        status = json.loads(status_path.read_text()) if status_path.exists() else None

        out.append({
            "session_id": sd.name,
            "name": (status.get("name") if isinstance(status, dict) else None),
            "last_active": last_mtime,
            "last_active_age_s": time.time() - last_mtime if last_mtime else None,
            "status": status,
            "decision_count": decisions,
            "file_touch_count": files,
            "open_question_count": open_q,
        })
    out.sort(key=lambda r: r.get("last_active", 0), reverse=True)
    return out
