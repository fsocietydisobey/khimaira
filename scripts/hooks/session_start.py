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

_STATE_ROOT = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "chimera"
_BASE_DIR = _STATE_ROOT / "sessions"
_HANDOFFS_PATH = _STATE_ROOT / "handoffs.jsonl"


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
    """Read unread notes, mark them read, MOVE TO archive.jsonl, return the
    drained set.

    Mirrors chimera.monitor.sessions.pending_notes(mark_read=True): drained
    notes move from inbox.jsonl → archive.jsonl atomically. Earlier version
    of this hook marked-read in-place but never archived, so notes appeared
    "missing" — present in inbox.jsonl as read=true records but invisible
    to session_search_archive. 2026-05-11 bug: fixed to match daemon's
    semantics.

    Hook runs as a separate process from the daemon; both use atomic
    rename for the rewrite so concurrent writes don't corrupt the file.
    """
    inbox = _session_dir(session_id) / "inbox.jsonl"
    archive = _session_dir(session_id) / "archive.jsonl"
    notes = _read_jsonl(inbox)
    pending = [n for n in notes if not n.get("read")]
    if not pending:
        return []

    # Partition: notes that were already read OR are being drained now →
    # archived; nothing remains in inbox after this pass (since pending was
    # everything unread, and we're also archiving any previously-read
    # entries that may have been left behind by older code paths).
    archived: list[dict] = []
    for n in notes:
        if not n.get("read"):
            n["read"] = True
            n["read_at"] = _now_iso()
            n["read_reason"] = "session_start_drain"
        archived.append(n)

    try:
        # Append-only write to archive.jsonl (preserves history across
        # multiple drains)
        with archive.open("a", encoding="utf-8") as f:
            for n in archived:
                f.write(json.dumps(n, separators=(",", ":")) + "\n")
        # Atomic rewrite of inbox to empty (everything moved)
        tmp = inbox.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            pass  # empty file
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


def _consume_handoffs(session_id: str, cwd: str) -> list[dict]:
    """Read handoffs whose scope_cwd matches `cwd`; mark this session_id
    as having read them; return the matched set.

    Cwd-scoped handoffs are how prior sessions leave notes for FUTURE
    sessions ("here's where I left off; pick up at file X commit Y").
    Without this, the only fallback was naming the prior session +
    relying on the user to type a bootstrap prompt referencing it.
    """
    import time

    if not _HANDOFFS_PATH.exists():
        return []
    cwd_abs = os.path.abspath(cwd)
    handoffs = _read_jsonl(_HANDOFFS_PATH)
    now = time.time()
    matched: list[dict] = []
    modified = False

    for h in handoffs:
        if h.get("expires_at", 0) < now:
            continue
        scope = h.get("scope_cwd") or ""
        if not scope:
            continue
        # Match: cwd is the scope OR a child of scope
        if cwd_abs != scope and not cwd_abs.startswith(scope.rstrip("/") + "/"):
            continue
        read_by = h.get("read_by") or []
        if session_id in read_by:
            continue
        matched.append(h)
        h["read_by"] = read_by + [session_id]
        modified = True

    if modified:
        tmp = _HANDOFFS_PATH.with_suffix(".jsonl.tmp")
        try:
            with tmp.open("w", encoding="utf-8") as f:
                for h in handoffs:
                    if h.get("expires_at", 0) < now:
                        continue
                    f.write(json.dumps(h, separators=(",", ":")) + "\n")
            tmp.replace(_HANDOFFS_PATH)
        except OSError:
            pass

    return matched


def _format_handoffs(handoffs: list[dict], cwd: str) -> str:
    # Split by role assigned during consume: this session may have
    # auto-claimed ownership of fresh handoffs OR be an observer on
    # handoffs already claimed by another session.
    owned = [h for h in handoffs if h.get("_claim_role") == "owner"]
    observed = [h for h in handoffs if h.get("_claim_role") == "observer"]

    lines: list[str] = []

    # --- OWNED handoffs — full directive framing ---
    if owned:
        lines.append(
            f"📦 chimera handoffs — {len(owned)} directive(s) you now OWN "
            f"in this project ({cwd}):"
        )
        lines.append("")
        for h in owned:
            from_id = (h.get("from_session_id") or "?")[:8]
            ts = (h.get("ts") or "")[:19]
            text = (h.get("text") or "").strip()
            lines.append(f"- [handoff {h['id'][:8]} · {ts} · from {from_id}]")
            lines.append(f"  {text}")
            lines.append("")
        lines.append(
            "**You are the PRIMARY OWNER of the handoff(s) above.** Your job:\n"
            "  1. Read referenced files / specs first.\n"
            "  2. Propose a concrete first action — pick the highest-priority "
            "item, summarize it in one sentence, file/line where you'll start.\n"
            "  3. Then START. Don't wait for \"yes do that\" — the handoff IS "
            "the authorization.\n"
            "  4. If ambiguous, ask ONE clarifying question — don't enumerate.\n"
            "  5. As you make decisions, call `session_log_decision` — "
            "subscribers (other sessions observing this handoff) will see "
            "your progress in their inboxes automatically.\n"
            "  6. If you finish or realize this isn't your lane, call "
            "`session_release_handoff(id)` so the next session in scope "
            "can pick it up."
        )

    # --- OBSERVED handoffs — owner already exists ---
    if observed:
        if owned:
            lines.append("")
            lines.append("---")
            lines.append("")
        lines.append(
            f"👀 chimera handoffs — {len(observed)} ALREADY-CLAIMED handoff(s) "
            f"visible in this project ({cwd}):"
        )
        lines.append("")
        for h in observed:
            from_id = (h.get("from_session_id") or "?")[:8]
            owner = (h.get("_owner_session_id") or h.get("owner_session_id") or "?")[:8]
            ts = (h.get("ts") or "")[:19]
            text = (h.get("text") or "").strip()
            sub_count = len(h.get("subscribers") or [])
            lines.append(
                f"- [handoff {h['id'][:8]} · {ts} · from {from_id} · "
                f"OWNED BY session {owner} · {sub_count} subscriber(s)]"
            )
            lines.append(f"  {text[:400]}{'…' if len(text) > 400 else ''}")
            lines.append("")
        lines.append(
            "**These handoffs are ALREADY OWNED by another session.** Default: "
            "do NOT pick items from these. Your options:\n"
            "  • **Subscribe** to receive owner's progress in your inbox: "
            "`session_subscribe_handoff(handoff_id, session_id)`. Use when "
            "you want to observe, offer review, or be available for sub-tasks.\n"
            "  • **Read owner's state**: `session_state(<owner_session_id>)` "
            "or `session_query_transcript` for what they've already done.\n"
            "  • **Send the owner a notice** if you spot something relevant: "
            "`session_post_notice(target_session_id=<owner>, text=...)`.\n"
            "  • **Stand down** and work on something else.\n"
            "\n"
            "Don't duplicate the owner's work. Propose your role (subscribe / "
            "observe / stand down) in your first response."
        )

    if not lines:
        return ""
    lines.append("")
    lines.append(
        "Each handoff was marked read by this session — they won't re-"
        "surface on resume. If you need them again, query the daemon API "
        "directly or use `session_state` of the sender."
    )
    return "\n".join(lines).rstrip()


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

    # Three parallel jobs:
    #   (1) Surface this session's chimera id so the agent can pass it to
    #       session_log_* tools without first having to discover it.
    #   (2) Read this session's inbox — answers other sessions have posted.
    #   (3) Discover other active sessions so the agent knows about
    #       parallel work without the user having to ask.
    blocks: list[str] = []

    # Identity block — always emitted, very short. Solves the "agent doesn't
    # know its own session_id" friction; without this, every CLAUDE.md
    # instruction telling the agent to log decisions has to start with
    # "first run session_list and figure out which one is you," which is
    # awkward and error-prone.
    blocks.append(
        f"🆔 chimera session_id: `{session_id}`\n"
        "When you call `mcp__chimera__session_log_*` / `session_set_*` tools, "
        "pass this id as `session_id`. Other sessions can refer to you by name "
        "after you call `session_set_name(...)`."
    )

    notes = _consume_inbox(session_id)
    others = _discover_other_active_sessions(session_id, within_minutes=30)
    cwd = data.get("cwd") or os.getcwd()
    handoffs = _consume_handoffs(session_id, cwd)

    if notes:
        blocks.append(_format_inbox(notes))
    if handoffs:
        blocks.append(_format_handoffs(handoffs, cwd))
    if others:
        blocks.append(_format_active_sessions(others))

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
