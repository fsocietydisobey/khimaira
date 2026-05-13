#!/usr/bin/env python3
"""khimaira SessionStart hook — surface unread inbox + other active sessions.

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
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

_STATE_ROOT = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
)
_BASE_DIR = _STATE_ROOT / "sessions"
_HANDOFFS_PATH = _STATE_ROOT / "handoffs.jsonl"

# Daemon HTTP base. Hook prefers HTTP (single source of truth = the
# daemon's code) and only falls back to file-direct ops when the daemon
# is unreachable. The fallback preserves the original safety net but
# the HTTP path eliminates the drift class where daemon semantics
# evolved while file-direct hook code lagged behind.
_ENDPOINT = os.environ.get("KHIMAIRA_ENDPOINT", "http://127.0.0.1:8740").rstrip("/")
_HTTP_TIMEOUT_S = 1.5


def _http_get_json(path: str) -> dict | None:
    """GET <endpoint>/<path> → parsed JSON, or None on any failure.

    Quiet by design — hooks must never bubble errors to Claude Code, and
    a daemon-down condition is expected to fall back to file-direct ops.
    """
    url = f"{_ENDPOINT}{path}"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


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

    HTTP-primary: hits /api/sessions/{sid}/pending?mark_read=true which is
    the daemon's authoritative drain-and-archive implementation. Falls
    back to file-direct ops only if the daemon is unreachable. The file
    fallback preserves the safety net but is structurally identical to
    daemon code that has drifted twice in the last day (archive miss,
    claim miss). The drift class is eliminated on the happy path.
    """
    # --- HTTP path (preferred) ---
    payload = _http_get_json(
        f"/api/sessions/{urllib.parse.quote(session_id)}/pending?mark_read=true"
    )
    if payload is not None:
        notes = payload.get("notes", [])
        return notes if isinstance(notes, list) else []

    # --- Fallback: file-direct drain + archive (daemon down) ---
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
        f"📬 khimaira inbox — {len(notes)} unread answer(s) from other sessions:",
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
    """Walk ~/.local/state/khimaira/sessions/ and return other sessions
    that have been active within the window. Sorted newest-first.

    HTTP-primary: hits /api/sessions (uses the daemon's cached list with
    2s TTL — 91× faster than re-scanning the filesystem and benefits
    from the daemon's cache amortization). Falls back to direct scan
    when daemon is unreachable.

    Skips this session and any with no activity in the window.
    """
    import time

    # --- HTTP path (preferred — uses daemon's cached list_sessions) ---
    payload = _http_get_json("/api/sessions")
    if payload is not None:
        cutoff_s = within_minutes * 60
        sessions = payload.get("sessions", []) or []
        out: list[dict] = []
        for s in sessions:
            sid = s.get("session_id") or ""
            if not sid or sid == self_session_id:
                continue
            age = s.get("last_active_age_s")
            if age is None or age > cutoff_s:
                continue
            out.append(
                {
                    "session_id": sid,
                    "last_active_age_s": int(age),
                    "status": s.get("status"),
                    "decision_count": s.get("decision_count", 0),
                    "file_touch_count": s.get("file_touch_count", 0),
                    "open_question_count": s.get("open_question_count", 0),
                }
            )
        out.sort(key=lambda r: r.get("last_active_age_s", 0))
        return out

    # --- Fallback: direct filesystem scan (daemon down) ---
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

        out.append(
            {
                "session_id": d.name,
                "last_active_age_s": int(time.time() - latest_mtime),
                "status": status,
                "decision_count": len(decisions),
                "file_touch_count": len(files),
                "open_question_count": open_q,
            }
        )

    out.sort(key=lambda r: r.get("last_active_age_s", 0))
    return out


def _consume_handoffs(session_id: str, cwd: str) -> list[dict]:
    """Read handoffs whose scope_cwd matches `cwd`; mark this session_id
    as having read them; return the matched set.

    HTTP-primary: hits /api/handoffs/consume which is the daemon's
    authoritative implementation (auto-claim, target_session_id filter,
    expired-entry GC). Falls back to file-direct ops if daemon is down.
    The fallback exists because cwd-scoped handoffs are the bootstrap
    mechanism for fresh sessions — losing them on daemon down means
    losing the only signal that prior work needs to be picked up.
    """
    # --- HTTP path (preferred) ---
    cwd_q = urllib.parse.quote(cwd, safe="")
    sid_q = urllib.parse.quote(session_id, safe="")
    payload = _http_get_json(f"/api/handoffs/consume?session_id={sid_q}&cwd={cwd_q}")
    if payload is not None:
        handoffs = payload.get("handoffs", [])
        return handoffs if isinstance(handoffs, list) else []

    # --- Fallback: file-direct consume + auto-claim + target filter ---
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
        # Targeted-invite filter — must mirror daemon's consume_handoffs.
        # If a handoff has target_session_id, only that session may
        # consume it; cwd-peers skip silently.
        target = h.get("target_session_id")
        if target and target != session_id:
            continue
        read_by = h.get("read_by") or []
        if session_id in read_by:
            continue

        # Auto-claim: first session to consume an unclaimed handoff
        # becomes owner. Mirrors daemon's consume_handoffs logic so the
        # render path (which keys off _claim_role) sees this handoff
        # as "owned by me" and renders the OWN directive framing.
        # Without this, the hook's local consume left _claim_role unset
        # and _format_handoffs would drop the handoff silently (matching
        # neither "owner" nor "observer" filter).
        existing_owner = h.get("owner_session_id")
        if not existing_owner:
            h["owner_session_id"] = session_id
            h["_claim_role"] = "owner"
        else:
            h["_claim_role"] = "observer"
            h["_owner_session_id"] = existing_owner

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
    # Defensive: any handoff without _claim_role defaults to "owner"
    # framing (it'd be silently dropped otherwise — the bug from
    # 2026-05-11 where pre-claim-logic handoffs surfaced as empty).
    owned = [h for h in handoffs if h.get("_claim_role", "owner") == "owner"]
    observed = [h for h in handoffs if h.get("_claim_role") == "observer"]

    lines: list[str] = []

    # --- OWNED handoffs — full directive framing ---
    if owned:
        lines.append(
            f"📦 khimaira handoffs — {len(owned)} directive(s) you now OWN "
            f"in this project ({cwd}):"
        )
        lines.append("")
        for h in owned:
            from_id = (h.get("from_session_id") or "?")[:8]
            ts = (h.get("ts") or "")[:19]
            text = (h.get("text") or "").strip()
            # Targeted invites carry parent_id + target_session_id. Surface
            # the invite framing so the agent knows this was delegated TO
            # them specifically (not a generic cwd-broadcast).
            parent = h.get("parent_id")
            target = h.get("target_session_id")
            if parent and target:
                lines.append(
                    f"- 🤝 [INVITE handoff {h['id'][:8]} · {ts} · "
                    f"from {from_id} · parent {parent[:8]}]"
                )
                lines.append(
                    f"  You were specifically invited to this slice; "
                    f"the parent handoff is owned by {from_id}."
                )
            else:
                lines.append(f"- [handoff {h['id'][:8]} · {ts} · from {from_id}]")
            lines.append(f"  {text}")
            lines.append("")
        lines.append(
            "**You are the PRIMARY OWNER of the handoff(s) above.** Your job:\n"
            "  1. Read referenced files / specs first.\n"
            "  2. Propose a concrete first action — pick the highest-priority "
            "item, summarize it in one sentence, file/line where you'll start.\n"
            '  3. Then START. Don\'t wait for "yes do that" — the handoff IS '
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
            f"👀 khimaira handoffs — {len(observed)} ALREADY-CLAIMED handoff(s) "
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
        f"📋 khimaira — {len(sessions)} other session(s) active in the last 30 min:",
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
        lines.append(f"  → use session_state({handle}) to read details")
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
    #   (1) Surface this session's khimaira id so the agent can pass it to
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
        f"🆔 khimaira session_id: `{session_id}`\n"
        "When you call `mcp__khimaira__session_log_*` / `session_set_*` tools, "
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
