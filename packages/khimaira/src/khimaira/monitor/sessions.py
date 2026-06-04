"""Multi-session shared state — externalize Claude Code session context.

The problem: when one Claude Code session is grinding on a task, you can't
ask related questions in another window without losing the working session's
context. Forks (Agent tool) solve "background work" but not "side conversation
that sees what the working agent is doing."

The solution: each session writes its decisions, file-touches, status, and
open questions to JSONL files khimaira tracks. Other sessions query that
state via MCP. Session B can post answers BACK to session A; A reads them
automatically on its next turn (via SessionStart hook).

Storage: ~/.local/state/khimaira/sessions/<session_id>/
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
import re
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any  # noqa: F401  (used in helper signatures)

from khimaira.log import get_logger
from khimaira.monitor import desktop_notify

log = get_logger("monitor.sessions")

_BASE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "sessions"
)

# Matches a canonical UUID4 (lowercase or uppercase hex, with hyphens).
_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


def _is_uuid(s: str) -> bool:
    return bool(_UUID4_RE.match(s))


def _session_dir_create(session_id: str) -> Path:
    """Resolve and create the per-session storage directory.

    Requires session_id to be a UUID4. Raises ValueError for friendly names
    or other non-UUID inputs — callers must resolve first via resolve_session_id().
    """
    if not _is_uuid(session_id):
        raise ValueError(
            f"session_id must be a UUID4; got {session_id!r}. "
            "Call resolve_session_id() first to convert a friendly name to its UUID."
        )
    d = _BASE_DIR / session_id
    d.mkdir(parents=True, exist_ok=True)
    return d


def _session_dir_read(session_id: str) -> Path | None:
    """Return the per-session directory path for read-only access.

    Does NOT create the directory. Returns None if the session directory does
    not exist, allowing callers to treat absence as "no data" gracefully.
    """
    d = _BASE_DIR / session_id
    return d if d.is_dir() else None


def _session_dir(session_id: str) -> Path:
    """Resolve the per-session storage directory, creating it lazily.

    Deprecated: prefer _session_dir_create (write paths) or _session_dir_read
    (read-only paths). This shim exists for call sites not yet audited.
    """
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
    from datetime import datetime

    return datetime.now(UTC).isoformat()


_AGENT_TAG_RE = re.compile(
    r"</?(thinking|scratchpad|reasoning|reflection|inner_monologue|"
    r"answer|invoke|parameter|body|tool_use|function_calls)\b[^>]*>",
    re.IGNORECASE,
)


def sanitize_agent_text(text: str) -> str:
    """Strip stray XML tags that agents sometimes leak into tool args.

    Observed in the wild: agents accidentally include `</thinking>`,
    `</answer>`, `</invoke>`, etc. in their tool parameter values
    (Claude Code prompt-rendering edge case where internal-monologue
    or tool-call scaffolding bleeds into a string). Defensive strip
    is applied at the daemon's text-acceptance boundary
    (post_answer's answer, post_notice's text, chats.send_message's
    body) so agents that don't leak see no change; those that do
    get clean stored text.

    Whitespace normalized (runs of spaces/tabs collapsed) so the
    strip doesn't leave double-spaces or leading/trailing space.
    Newlines preserved for multi-line bodies.
    """
    if not text:
        return text
    cleaned = _AGENT_TAG_RE.sub("", text)
    cleaned = re.sub(r"[ \t]+", " ", cleaned).strip()
    return cleaned


# ---------------------------------------------------------------------------
# Write side — called by the WORKING agent (session A) as it works
# ---------------------------------------------------------------------------


def log_decision(session_id: str, text: str, why: str = "") -> dict:
    """Record a decision the agent has made. Surfaces to other sessions
    via session_state(session_id). If this session owns any handoff,
    decision is also broadcast to that handoff's subscribers."""
    record = {
        "ts": _now_iso(),
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "why": why,
    }
    _append_jsonl(_session_dir(session_id) / "decisions.jsonl", record)
    _invalidate_list_sessions_cache()  # decision count is part of the cached digest
    # Fan out to handoff subscribers — best-effort, non-blocking
    # (broadcast itself is now async; this try/except is a final safety net)
    try:
        _broadcast_to_handoff_subscribers(session_id, "decision", text)
    except Exception:
        pass
    log.info("session %s: decision recorded — %s", session_id, text[:80])
    return record


_TOOL_CALL_CAP = 100
"""Maximum entries kept in tool_calls.jsonl per session (ring-buffer cap)."""


def log_tool_call(session_id: str, tool_name: str, params: dict) -> None:
    """Record a tool invocation to the session's tool_calls.jsonl ring-buffer.

    Capped at _TOOL_CALL_CAP (100) entries; oldest are dropped when the cap
    is exceeded. Called from the PostToolUse hook for every tool call so the
    PreToolUse Themis hook can inspect recent activity (e.g. IN-MASTER-4).

    Params should be the raw tool_input dict — callers should not pre-filter.
    """
    sd = _session_dir(session_id)
    path = sd / "tool_calls.jsonl"
    record = {"ts": _now_iso(), "tool": tool_name, "params": params}
    _append_jsonl(path, record)
    entries = _read_jsonl(path)
    if len(entries) > _TOOL_CALL_CAP:
        tmp = path.with_suffix(".jsonl.tmp")
        with tmp.open("w", encoding="utf-8") as f:
            for e in entries[-_TOOL_CALL_CAP:]:
                f.write(json.dumps(e, separators=(",", ":")) + "\n")
        tmp.replace(path)


def recent_tool_calls(session_id: str, limit: int = 20) -> list[dict]:
    """Return the last `limit` tool calls for this session.

    Returns [] for a fresh session with no tool_calls.jsonl.
    Used by the PreToolUse Themis hook to check for recent top-tier consults.
    """
    sd = _session_dir_read(session_id)
    if sd is None:
        return []
    path = sd / "tool_calls.jsonl"
    return _read_jsonl(path)[-limit:]


# Durable reverse map: session_id → ppid. Populated by set_session_ppid;
# persisted to status.json so it survives daemon restarts.
_session_ppid: dict[str, int] = {}


def set_session_ppid(session_id: str, ppid: int) -> None:
    """Record the Claude Code process PID for session_id.

    Persists to status.json["ppid"] so the value survives daemon restarts.
    Fail-open: persistence errors are swallowed; the in-memory entry is always set.
    """
    _session_ppid[session_id] = ppid
    try:
        path = _session_dir(session_id) / "status.json"
        existing: dict = {}
        if path.is_file():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing["ppid"] = ppid
        path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
        _invalidate_list_sessions_cache()
    except Exception:
        pass


def get_session_ppid(session_id: str) -> int | None:
    """Return the Claude Code PID for session_id, or None if unknown.

    Checks the in-memory cache first; falls back to status.json on a cache miss
    (e.g., after daemon restart). Warms the cache on a successful read-through.
    """
    if session_id in _session_ppid:
        return _session_ppid[session_id]
    try:
        sd = _session_dir_read(session_id)
        if sd is None:
            return None
        status_path = sd / "status.json"
        if not status_path.is_file():
            return None
        data = json.loads(status_path.read_text(encoding="utf-8"))
        ppid = data.get("ppid")
        if ppid is not None:
            _session_ppid[session_id] = int(ppid)
            return int(ppid)
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Roster wind-down flag — shared signal for Guard-4 + Guard-5 suppression.
# When the roster is deliberately offline (declared wind-down), both guards
# must pause their stall-clocks to avoid false-positive escalations (the
# 18h overnight ping class: in_progress sessions silently offline = normal
# wind-down, not a hang). Implemented once here; consumed everywhere.
# ---------------------------------------------------------------------------

# Persisted as a sentinel FILE (not an in-process global). An in-memory global
# is invisible across processes: an operator setting it in one process never
# reaches the daemon's guard loops in another. The sentinel is cross-process and
# survives a daemon restart (a wind-down spanning a restart stays in effect).
# Derived from _BASE_DIR so it honors XDG_STATE_HOME (test-isolatable).
_ROSTER_WIND_DOWN_SENTINEL = _BASE_DIR.parent / "roster_wind_down"


# ---------------------------------------------------------------------------
# GAP-5 canonical name-resolution predicate.
# resolve_active_session: resolves a friendly name → most-recently-active UUID.
# active_roster_member_ids: defined below (agent-1's implementation, ~line 420).
# ---------------------------------------------------------------------------


def resolve_active_session(name: str) -> str | None:
    """Resolve a friendly name → the MOST-RECENTLY-ACTIVE session UUID.

    When multiple session dirs share the same name (e.g. 4× 'khimaira-0' from
    restarts), returns the one with the highest last_active (most recently
    modified files), not the first alphabetically or by creation time.
    Stale/phantom dirs must NOT shadow a live session.

    Returns None instead of raising — callers that need an exception should
    use resolve_session_id() directly.

    Reads durable state only (no cache, no in-memory-global risk).
    """
    try:
        return resolve_session_id(name)
    except (ValueError, Exception):
        return None


def set_roster_wind_down(active: bool) -> None:
    """Declare or lift a roster wind-down. While active, Guard-4 and Guard-5
    suppress stall-escalation — sessions are intentionally offline, not hung.

    Operators may equivalently ``touch``/``rm`` the sentinel directly:
    ``$XDG_STATE_HOME/khimaira/roster_wind_down`` (default
    ``~/.local/state/khimaira/roster_wind_down``)."""
    if active:
        _ROSTER_WIND_DOWN_SENTINEL.parent.mkdir(parents=True, exist_ok=True)
        _ROSTER_WIND_DOWN_SENTINEL.touch(exist_ok=True)
    else:
        _ROSTER_WIND_DOWN_SENTINEL.unlink(missing_ok=True)


def is_roster_wind_down() -> bool:
    """True when the roster is in a declared wind-down. Guards should not
    escalate silent sessions during wind-down. Reads the sentinel file each
    call so the daemon picks up an operator's change without a restart."""
    return _ROSTER_WIND_DOWN_SENTINEL.exists()


# ---------------------------------------------------------------------------
# Canonical roster-membership predicate — single source of truth for all Guards
# ---------------------------------------------------------------------------

# Default window: chats with at least one message in the last 7 days.
_ROSTER_ACTIVE_CHAT_WINDOW_S: float = float(
    os.environ.get("KHIMAIRA_ROSTER_ACTIVE_WINDOW_S", str(7 * 24 * 3600))
)


def active_roster_member_ids(
    active_window_s: float | None = None,
) -> set[str]:
    """Return the set of session IDs that are ACCEPTED members of a recently-active chat.

    A session is on the roster iff:
      1. It is an accepted member of at least one khimaira chat, AND
      2. That chat has had at least one event (file mtime) within
         ``active_window_s`` seconds of now.

    Both checks use durable reads — accepted membership from JSONL events,
    recency from file mtime (no in-memory cache). This makes the predicate
    consistent across daemon restarts and safe to call from any guard.

    Used by Guard-4, Guard-5, Guard-6, and the watcher so all three agree on
    exactly who is on the roster. Do NOT roll per-guard membership checks —
    call this function.

    ``active_window_s`` defaults to ``KHIMAIRA_ROSTER_ACTIVE_WINDOW_S`` (7 days).
    """
    if active_window_s is None:
        active_window_s = _ROSTER_ACTIVE_CHAT_WINDOW_S

    # Lazy import — chats imports sessions, so this must stay inside the function.
    try:
        from khimaira.monitor import chats as _chats_mod  # noqa: PLC0415
    except Exception as exc:
        # Observable signal: fail-open-to-empty is correct (errs toward under-flag,
        # not the cross-project false-dark storm), but a PERSISTENT import failure
        # must be audible — empty-on-error looks identical to empty-because-quiet.
        log.warning(
            "active_roster_member_ids: roster computation failed (chats import: %s) — "
            "returning empty set (degraded, Guard-6/Guard-5 will under-flag not storm)",
            exc,
        )
        return set()

    chat_dir = _chats_mod._chat_dir()
    if not chat_dir.exists():
        return set()

    cutoff = time.time() - active_window_s
    member_ids: set[str] = set()
    total_chats = 0
    error_skips = 0

    for chat_path in chat_dir.glob("chat-*.jsonl"):
        total_chats += 1
        try:
            lines = _read_jsonl(chat_path)
            if not _chat_recently_active(lines, cutoff):
                continue
            _fold_accepted_members_from_lines(lines, member_ids)
        except Exception:
            error_skips += 1
            continue  # fail-open: skip unreadable chats

    # Observable signal for the all-chats-fail case: if every chat file raised an
    # error, the empty result is indistinguishable from "roster genuinely quiet"
    # unless we emit a warning. Single-file glitches stay correctly silent.
    if total_chats > 0 and error_skips == total_chats and not member_ids:
        log.warning(
            "active_roster_member_ids: ALL %d chat files raised read errors — "
            "returning empty set (degraded). Single-file errors silenced; "
            "this all-chats-fail case is audible.",
            total_chats,
        )

    return member_ids


def _active_roster_for_resolution(
    active_window_s: float | None = None,
) -> tuple[set[str], bool]:
    """Like active_roster_member_ids but exposes the error flag for P2 resolution.

    Returns (member_ids, had_error):
      - had_error=True means the computation failed (import/read error); the caller
        should NOT silently revert to the GAP-5 global-heuristic since that would
        re-open the bug P2 closes. Instead, log a strong warning and fall through
        with more caution.
      - had_error=False + member_ids={} → genuinely empty (first-run / no active chats).
      - had_error=False + member_ids={...} → valid scoped roster.
    """
    if active_window_s is None:
        active_window_s = _ROSTER_ACTIVE_CHAT_WINDOW_S

    try:
        from khimaira.monitor import chats as _chats_mod  # noqa: PLC0415
    except Exception as exc:
        log.warning(
            "active_roster_for_resolution: chats import error (%s) — "
            "resolution will fall back to global heuristic (GAP-5 window open).",
            exc,
        )
        return set(), True

    chat_dir = _chats_mod._chat_dir()
    if not chat_dir.exists():
        return set(), False

    cutoff = time.time() - active_window_s
    member_ids: set[str] = set()
    total_chats = 0
    error_skips = 0

    for chat_path in chat_dir.glob("chat-*.jsonl"):
        total_chats += 1
        try:
            lines = _read_jsonl(chat_path)
            if not _chat_recently_active(lines, cutoff):
                continue
            _fold_accepted_members_from_lines(lines, member_ids)
        except Exception:
            error_skips += 1
            continue

    had_error = total_chats > 0 and error_skips == total_chats and not member_ids
    if had_error:
        log.warning(
            "active_roster_for_resolution: ALL %d chat files raised read errors — "
            "resolver will fall back to global heuristic (GAP-5 window open).",
            total_chats,
        )
    return member_ids, had_error


def _chat_recently_active(lines: list[dict], cutoff: float) -> bool:
    """True if the most-recent event in ``lines`` has ts ≥ ``cutoff`` (unix seconds).

    Falls back to True on any parse error (fail-open: include ambiguous chats
    rather than silently dropping active members).
    """
    import datetime as _dt

    for line in reversed(lines):
        ts_str = line.get("ts")
        if not ts_str:
            continue
        try:
            dt = _dt.datetime.fromisoformat(str(ts_str).replace("Z", "+00:00"))
            return dt.timestamp() >= cutoff
        except (ValueError, TypeError):
            continue
    return True  # no parseable ts → assume active (fail-open)


def _fold_accepted_members_from_lines(lines: list[dict], out: set[str]) -> None:
    """Fold MEMBER events from JSONL lines into ``out`` (accepted session IDs).

    Final state per session_id: accepted → in out; left/rejected → removed.
    """
    accepted: set[str] = set()
    for line in lines:
        if line.get("kind") != "member":
            continue
        sid = line.get("session_id")
        if not sid:
            continue
        state = line.get("state")
        if state == "accepted":
            accepted.add(sid)
        elif state in ("left", "rejected"):
            accepted.discard(sid)
    out.update(accepted)


def write_sse_heartbeat(session_id: str) -> None:
    """Subscriber-side heartbeat. Called from the PostToolUse hook on every tool
    call. Stamps `last_sse_heartbeat` into status.json so daemon can detect
    subscribers whose subprocess is alive but SSE connection died.

    Cheap: one status.json read + write per tool call. Idempotent.
    """
    path = _session_dir(session_id) / "status.json"
    existing: dict = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing["last_sse_heartbeat"] = _now_iso()
    path.write_text(json.dumps(existing, indent=2))
    _invalidate_list_sessions_cache()


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


def recent_touches(session_id: str, limit: int = 20) -> list[dict]:
    """Return the last `limit` file-touch entries for this session.

    Returns [] for a fresh session with no files_touched.jsonl.
    """
    sd = _session_dir_read(session_id)
    if sd is None:
        return []
    path = sd / "files_touched.jsonl"
    return _read_jsonl(path)[-limit:] if path.exists() else []


def log_question(
    session_id: str,
    text: str,
    target_session_id: str | None = None,
    *,
    cross_workspace: bool = False,
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

    **Workspace guard:** when targeting a specific session, asker's and
    target's workspaces must match (both default to `"default"` if unset).
    To cross workspace boundaries explicitly, pass `cross_workspace=True`.
    Raises ValueError on mismatch without the flag — protects the
    multi-client / personal-vs-work isolation use case.
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

        # Workspace check happens AFTER resolve so a name lookup still works.
        # Only run when both sides are resolvable; if target doesn't exist
        # yet we can't know its workspace, so we trust the asker (the same
        # leniency resolve_session_id grants for not-yet-materialized names).
        if not cross_workspace:
            asker_ws = get_workspace(session_id)
            target_ws = get_workspace(resolved_target)
            if asker_ws != target_ws:
                raise ValueError(
                    f"Targeted question crosses workspaces: asker={asker_ws!r}, "
                    f"target={target_ws!r}. Pass cross_workspace=True to bypass."
                )

    record = {
        "ts": _now_iso(),
        "id": uuid.uuid4().hex[:12],
        "text": text,
        "status": "open",  # "open" | "answered" | "withdrawn"
        "answer": None,
        "answered_by": None,
        "answered_at": None,
        "target_session_id": resolved_target,  # None == broadcast
    }
    _append_jsonl(_session_dir(session_id) / "questions.jsonl", record)
    log.info(
        "session %s: question opened (id=%s, target=%s) — %s",
        session_id,
        record["id"],
        resolved_target or "broadcast",
        text[:80],
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
    _invalidate_list_sessions_cache()
    return record


# UUID-drift threshold: matches the "active in the last 30 min" window used
# elsewhere (SessionStart hook listing). Sessions older than this are
# treated as dead and their names are silently recyclable.
_UUID_DRIFT_STALE_THRESHOLD_S = 30 * 60


def _is_stub_session(status: dict | None, decisions: int) -> bool:
    """A 'stub' is a session record with a name but no proof of life.

    Excluded from name-resolution tiebreakers when a live competitor exists.
    Heuristic: never emitted SSE heartbeat AND no decisions logged.
    Pure status.json + name + zero activity → stub.
    """
    if not isinstance(status, dict):
        return True
    if status.get("last_sse_heartbeat"):
        return False
    if decisions > 0:
        return False
    return True


def _find_active_session_with_name(name: str, exclude_session_id: str) -> dict | None:
    """Scan every session's status.json for `name == name` (excluding self).
    Returns the most-recently-active match if any, else None.

    Used by set_name() UUID-drift detection. Cheap — one status.json read
    per session; not on a hot path.

    Tiebreaker prefers live sessions over stubs: (has_heartbeat, decisions, mtime).
    """
    if not _BASE_DIR.exists():
        return None
    candidates: list[tuple[int, int, float, str]] = []
    for d in _BASE_DIR.iterdir():
        if not d.is_dir() or d.name == exclude_session_id:
            continue
        status_path = d / "status.json"
        if not status_path.is_file():
            continue
        try:
            s = json.loads(status_path.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if s.get("name") != name:
            continue
        try:
            mtime = max(
                (p.stat().st_mtime for p in d.iterdir() if p.is_file()),
                default=0.0,
            )
        except OSError:
            mtime = 0.0
        decisions_path = d / "decisions.jsonl"
        decisions = (
            sum(1 for ln in decisions_path.open() if ln.strip())
            if decisions_path.exists()
            else 0
        )
        has_heartbeat = 1 if s.get("last_sse_heartbeat") else 0
        candidates.append((has_heartbeat, decisions, mtime, d.name))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    _hb, _dec, mtime, sid = candidates[0]
    return {"session_id": sid, "last_active": mtime, "age_s": time.time() - mtime}


def set_name(session_id: str, name: str) -> dict:
    """Set a friendly name for the session — surfaces in session_list and
    enables name-based resolution from other sessions.

    Names should be slug-shaped: lowercase, dashes, no spaces. Two sessions
    can share a name; lookup prefers most-recently-active.

    Phase B v1.3 UUID-drift detection: if another active session (mtime
    within ~30 min) already holds this name, the returned record includes
    `merge_needed=True` and `conflicts_with=<other_session_id>`. The
    daemon does not auto-merge — the orchestrator decides whether to
    migrate state, rename, or accept the collision. Stale conflicts
    (>30 min idle) are silently recyclable, preserving the existing
    "two sessions can share a name" affordance for legitimate re-use.
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
    _invalidate_list_sessions_cache()
    log.info("session %s: named %r", session_id, name)

    # UUID-drift detection: surface live collisions for the caller to handle.
    conflict = _find_active_session_with_name(name, exclude_session_id=session_id)
    if conflict is not None and conflict["age_s"] < _UUID_DRIFT_STALE_THRESHOLD_S:
        log.warning(
            "sessions.set_name: name %r already in use by session %s "
            "(last active %.0fs ago); merge_needed surfaced to caller.",
            name,
            conflict["session_id"],
            conflict["age_s"],
        )
        return {
            **record,
            "merge_needed": True,
            "conflicts_with": conflict["session_id"],
            "conflicts_with_last_active_s": conflict["age_s"],
        }
    return record


_WORKSPACE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
DEFAULT_WORKSPACE = "default"


def set_workspace(session_id: str, workspace: str) -> dict:
    """Place the session in a named workspace — privacy/noise boundary for
    multi-project session isolation.

    Workspaces group sessions that share visibility. By default every
    session is in workspace `"default"`. Cross-workspace operations
    (reading another workspace's state, posting targeted questions
    across workspaces) require explicit `workspace=...` overrides or
    `cross_workspace=True` flags.

    Names must be kebab-case (`^[a-z0-9][a-z0-9-]{0,39}$`) to prevent
    path-injection / shell-quoting surprises and keep them URL-safe.

    Backward-compatible: existing sessions without a workspace field
    are treated as `"default"` everywhere they're read.
    """
    if not _WORKSPACE_RE.match(workspace):
        raise ValueError(
            f"workspace {workspace!r} invalid — must match "
            f"^[a-z0-9][a-z0-9-]{{0,39}}$ (kebab-case, max 40 chars)."
        )
    path = _session_dir(session_id) / "status.json"
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    record = {**existing, "workspace": workspace, "updated_at": _now_iso()}
    record.setdefault("status", "idle")
    record.setdefault("detail", "")
    path.write_text(json.dumps(record, indent=2))
    _invalidate_list_sessions_cache()
    log.info("session %s: workspace=%r", session_id, workspace)
    return record


def set_session_slot(session_id: str, slot: str) -> dict:
    """Bind a session to its roster slot (e.g. '<instance_id>:agent-1').

    Called by the daemon at SessionStart when the session announces its
    KHIMAIRA_ROSTER_SLOT env var. The slot is stored in status.json as
    'roster_slot' for use by the instance-scoped resolver (resolve_session_id).
    Also updates the shared slot registry for slot_resolve (drift-healing).

    Idempotent: safe to call on every SessionStart reconnect (drift self-heals).
    Disk-durable: survives daemon restarts (unlike heartbeat in-memory state).
    """
    if ":" not in slot:
        raise ValueError(
            f"slot {slot!r} must be '<instance_id>:<name>' format (e.g. '<uuid>:agent-1')"
        )
    path = _session_dir(session_id) / "status.json"
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            existing = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            existing = {}
    record = {**existing, "roster_slot": slot, "updated_at": _now_iso()}
    record.setdefault("status", "idle")
    record.setdefault("detail", "")
    path.write_text(json.dumps(record, indent=2))
    _invalidate_list_sessions_cache()
    # Update shared slot registry (bounded prior-sid history for slot_resolve)
    _update_slot_registry(slot, session_id)
    log.info("session %s: roster_slot=%r", session_id, slot)
    return record


# ---------------------------------------------------------------------------
# Slot registry — shared sid↔slot binding for drift-healing (path-7 Part C)
# ---------------------------------------------------------------------------
#
# The slot registry tracks the current + immediately-prior sids per slot.
# Bounded to last-2 prior sids (SECURITY: old-sid-revival closed by construction —
# a harvested ancient sid is not in prior_sids and stays INERT; see analyst
# msg-46b2dec421ca 2026-06-03 + master addendum msg-b0f6a84b7e1e).
_SLOT_REGISTRY_MAX_PRIOR = 1  # keep only the immediately-prior sid (last-1 bound)


def _slot_registry_path() -> "Path":
    return _BASE_DIR.parent / "slot-registry.json"


def _read_slot_registry() -> "dict[str, dict]":
    """Read the shared slot registry. Returns {slot: {current_sid, prior_sids, updated_at}}."""
    path = _slot_registry_path()
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _write_slot_registry(registry: "dict[str, dict]") -> None:
    path = _slot_registry_path()
    path.write_text(json.dumps(registry, indent=2))


def _update_slot_registry(slot: str, new_sid: str) -> None:
    """Update the slot registry when a new sid claims a slot.

    Moves current_sid to prior_sids (bounded to _SLOT_REGISTRY_MAX_PRIOR).
    Rotates the displaced prior into revoked_sids (superseded-beyond-bound →
    explicit DENY set for _slot_heal_member_key inert-denial).

    If new_sid == current_sid (idempotent re-register), no-ops the prior list.
    """
    registry = _read_slot_registry()
    entry = registry.get(slot, {"current_sid": None, "prior_sids": [], "revoked_sids": []})
    old_current = entry.get("current_sid")

    if old_current and old_current != new_sid:
        old_prior = [s for s in entry.get("prior_sids", []) if s != new_sid]
        new_prior = ([old_current] + old_prior)[:_SLOT_REGISTRY_MAX_PRIOR]
        # Sids displaced from prior_sids (beyond the bound) move to revoked_sids.
        displaced = old_prior[_SLOT_REGISTRY_MAX_PRIOR - 1:]
        revoked = list({*entry.get("revoked_sids", []), *displaced} - {new_sid})
        entry["prior_sids"] = new_prior
        entry["revoked_sids"] = revoked
    elif not old_current:
        entry["prior_sids"] = []
        entry["revoked_sids"] = []

    entry["current_sid"] = new_sid
    entry["updated_at"] = _now_iso()
    registry[slot] = entry
    _write_slot_registry(registry)


def slot_resolve(sid: str) -> "str | None":
    """Map any sid → the slot's CURRENT authoritative sid (drift-healing).

    BOUNDED to last-1/2 prior sids (not full history) — NEUTRAL-BY-CONSTRUCTION:
    a harvested ancient sid is NOT in prior_sids and stays INERT (does not resolve),
    regardless of the same-uid trust boundary. The revival surface is
    once-legitimate sids (in chat history / handoffs / transfer logs); the bound
    keeps them inert once superseded.

    Returns:
    - sid itself if it is the current_sid for its slot (already authoritative)
    - the current_sid if sid is in the slot's recent prior_sids (heals reattach)
    - None if sid is not in the registry or too old (INERT — caller keeps original)
    """
    registry = _read_slot_registry()
    for _slot, entry in registry.items():
        current = entry.get("current_sid")
        if current == sid:
            return sid  # already the authoritative sid
        if sid in entry.get("prior_sids", []):
            return current  # immediately-prior → maps to current
    return None  # not in registry or beyond bound → INERT


def get_workspace(session_id: str) -> str:
    """Return the session's workspace, defaulting to `"default"` if unset.

    Used internally by read paths to filter visibility. Never raises;
    a missing or malformed status.json returns DEFAULT_WORKSPACE so
    we don't accidentally hide sessions whose status file is truncated.
    """
    sd = _session_dir_read(session_id)
    if sd is None:
        return DEFAULT_WORKSPACE
    path = sd / "status.json"
    if not path.exists():
        return DEFAULT_WORKSPACE
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return DEFAULT_WORKSPACE
    ws = data.get("workspace")
    if not isinstance(ws, str) or not ws:
        return DEFAULT_WORKSPACE
    return ws


def get_session_slot(session_id: str) -> str | None:
    """Return the session's roster slot (e.g. '<instance_id>:agent-1'), or None if unstamped.

    Populated by the daemon's slot-binding at SessionStart (Part C).
    Returns None for un-stamped sessions (pre-migration or non-roster sessions).
    The slot field is stored as 'roster_slot' in the session's status.json.
    """
    path = _BASE_DIR / session_id / "status.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    slot = data.get("roster_slot")
    if not isinstance(slot, str) or ":" not in slot:
        return None
    return slot


def _find_by_slot_globally(slot: str) -> str:
    """Find a session by its exact roster slot (e.g. '<instance>:agent-1').

    Returns the session_id if exactly one session has roster_slot == slot.
    Raises ValueError on 0 or ≥2 matches (a consistency error in the latter case).
    """
    matches: list[str] = []
    if not _BASE_DIR.exists():
        raise ValueError(f"No session with slot {slot!r}")
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
        if s.get("roster_slot") == slot:
            matches.append(d.name)

    if not matches:
        raise ValueError(f"No session with slot {slot!r}")
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous slot {slot!r}: {len(matches)} sessions match — "
            f"slot-binding consistency error ({matches})."
        )
    return matches[0]


def _find_by_name_in_candidates(
    name: str,
    candidate_ids: set[str],
    *,
    caller_instance: str | None = None,
) -> str:
    """Search candidate_ids for exactly one session with status.json name == name.

    Returns the UUID if exactly one matches.
    Raises ValueError on ambiguity (≥2) or not-found (0).

    Instance-awareness (roster-identity P1 — 2026-06-03):
    - caller_instance provided: prefer candidates whose roster_slot matches
      '<caller_instance>:<name>'; abort if ≥2 match within the same instance.
    - caller_instance=None + mixed stamped+unstamped candidates: abort with slot
      guidance — never silently pick the stamped one ("unstamped ≠ dead"; an
      un-migrated session can still be active; no basis to disambiguate).
    - caller_instance=None + all-unstamped + ≥2: original abort (today's behavior).

    Also handles legacy test fixtures where the session_id IS the name
    (non-UUID dir, no status.json name field set).
    """
    matches: list[str] = []
    for sid in candidate_ids:
        # Primary check: status.json has name == query
        status_path = _BASE_DIR / sid / "status.json"
        if status_path.is_file():
            try:
                s = json.loads(status_path.read_text())
            except (OSError, json.JSONDecodeError):
                s = {}
            if s.get("name") == name:
                matches.append(sid)
                continue

        # Dirname fallback: for legacy non-UUID session IDs where the
        # dirname is the session identity (no status.json name field set).
        # Production sessions always have names in status.json; this only
        # fires in test fixtures that use bare strings as session IDs.
        if not _is_uuid(sid) and sid == name and (_BASE_DIR / sid).is_dir():
            matches.append(sid)

    if len(matches) == 1:
        return matches[0]

    if not matches:
        raise ValueError(
            f"No session named {name!r} found in the resolution scope. "
            f"Use session_list() to see available sessions."
        )

    # ≥2 matches — instance-awareness disambiguates when caller_instance is known.
    if caller_instance is not None:
        inst_matches = [
            m for m in matches
            if get_session_slot(m) == f"{caller_instance}:{name}"
        ]
        if len(inst_matches) == 1:
            return inst_matches[0]
        if len(inst_matches) > 1:
            raise ValueError(
                f"Ambiguous name {name!r}: {len(inst_matches)} sessions match "
                f"within instance {caller_instance!r} ({inst_matches}). "
                f"Use a session UUID to be unambiguous. "
                f"See session_list() to find the right UUID."
            )
        # No instance-match → fall through to mixed/all-unstamped handling below.

    # Categorize by stamped/unstamped for the None-branch safe-abort rule.
    stamped = [m for m in matches if get_session_slot(m) is not None]
    unstamped = [m for m in matches if get_session_slot(m) is None]

    if caller_instance is None and stamped and unstamped:
        # Mixed — "unstamped ≠ dead": an un-migrated session can still be active.
        # No disambiguation basis without a caller instance → abort with guidance.
        raise ValueError(
            f"Ambiguous name {name!r}: {len(matches)} sessions match "
            f"(mixed stamped/unstamped roster instances — an un-migrated session "
            f"may still be active). "
            f"Use a UUID or slot (<instance>:{name}) to disambiguate. "
            f"See session_list() to find the right UUID."
        )

    # All-unstamped ≥2, or all-stamped ≥2 with no caller-instance context.
    raise ValueError(
        f"Ambiguous name {name!r}: {len(matches)} sessions match after scoping "
        f"({matches}). Use a session UUID to be unambiguous. "
        f"See session_list() to find the right UUID."
    )


def _resolve_name_global_legacy(name: str, *, caller_instance: str | None = None) -> str:
    """Legacy global name-resolution fallback (P2 — no mtime-heuristic guess).

    Finds all sessions with status.json name == ``name`` across ALL session dirs.
    Returns the single UUID match; ABORTS on ≥2 UUID matches (no mtime-guess —
    the ambiguity-abort is what kills GAP-5 uniformly across steps 2-3-4);
    falls back to dirname for legacy non-UUID session IDs (test fixtures only).

    Instance-awareness (roster-identity P1): caller_instance disambiguates among
    homonyms by preferring the candidate whose roster_slot matches the caller's
    instance. The F2 mixed-abort rule applies here too (same as in
    _find_by_name_in_candidates — abort must live in this function AND the shared
    matcher to prevent a mixed-instance pair aborting one tier too early).

    Called when: roster is empty (first-run/test-isolation) OR name not found in
    the active roster OR roster computation errored.
    """
    matches: list[str] = []
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
        if s.get("name") == name:
            if not _is_uuid(d.name):
                log.warning(
                    "resolve_session_id(%r): candidate dir %r is not UUID-shaped — "
                    "likely an orphan created by a routing bug (#63). "
                    "Run `khimaira migrate-orphan-inboxes` to consolidate.",
                    name,
                    d.name,
                )
            matches.append(d.name)

    if not matches:
        fallback_dir = _BASE_DIR / name
        if fallback_dir.is_dir() and not _is_uuid(name):
            log.warning(
                "resolve_session_id(%r): no name-match found; falling back to dirname. "
                "This dirname is not UUID-shaped — likely an orphan from routing bug #63. "
                "Update callers to use UUID session IDs.",
                name,
            )
            return name
        raise ValueError(
            f"No session named or id'd {name!r}. Use session_list() to see available sessions."
        )

    uuid_matches = [m for m in matches if _is_uuid(m)]

    # Instance-awareness: disambiguate ≥2 UUID matches when caller_instance is known.
    if len(uuid_matches) > 1 and caller_instance is not None:
        inst_matches = [
            m for m in uuid_matches
            if get_session_slot(m) == f"{caller_instance}:{name}"
        ]
        if len(inst_matches) == 1:
            return inst_matches[0]
        if len(inst_matches) > 1:
            raise ValueError(
                f"Ambiguous name {name!r}: {len(inst_matches)} sessions match "
                f"within instance {caller_instance!r} globally ({inst_matches}). "
                f"Use a session UUID."
            )

    # F2: mixed-abort must also live here (not just in _find_by_name_in_candidates).
    # A dead-unstamped + live-stamped homonym pair seen at this tier must abort-with-
    # guidance rather than silently preferring the stamped candidate.
    if len(uuid_matches) > 1 and caller_instance is None:
        stamped_uuids = [m for m in uuid_matches if get_session_slot(m) is not None]
        unstamped_uuids = [m for m in uuid_matches if get_session_slot(m) is None]
        if stamped_uuids and unstamped_uuids:
            raise ValueError(
                f"Ambiguous name {name!r}: {len(uuid_matches)} sessions match globally "
                f"(mixed stamped/unstamped roster instances — an un-migrated session "
                f"may still be active). "
                f"Use a UUID or slot (<instance>:{name}) to disambiguate. "
                f"See session_list() to find the right UUID."
            )

    # P2: abort on all-UUID ambiguity (GAP-5 root closed).
    if len(uuid_matches) > 1:
        raise ValueError(
            f"Ambiguous name {name!r}: {len(uuid_matches)} sessions match globally "
            f"({uuid_matches}). Use a session UUID to be unambiguous. "
            f"See session_list() to find the right UUID."
        )

    # 1-UUID + orphan(s): prefer the UUID — orphans are #63-artifacts.
    # ≥2-orphan (0 UUID): iterdir-arbitrary (non-production).
    if uuid_matches:
        return uuid_matches[0]
    return matches[0]


def resolve_session_id(
    query: str,
    *,
    chat_id: str | None = None,
    caller_instance: str | None = None,
) -> str:
    """Map a user-friendly query → exact session_id (UUID).

    Resolution precedence (P1 roster-identity bridge — 2026-06-03):

      1. UUID exact + dir-exists → return as-is.
      1b. SLOT format '<instance>:<name>' → _find_by_slot_globally (exact slot match).
      2. INSTANCE-SCOPED (caller_instance provided, bare name): match within the
         caller's roster instance via roster_slot field. Bare-name = INTRA-instance;
         cross-instance = slot or UUID. Un-stamped callers (caller_instance=None)
         skip this tier → exact current behavior (zero-breakage migration seam).
      3. CHAT-SCOPED (chat_id provided): match within that chat's accepted members.
         Fall-through on not-found (F3 — early-return → fall-through) so tiers 2+3
         both run. Ambiguity → ABORT.
      4. ROSTER-SCOPED (no chat_id, roster non-empty): match within
         active_roster_member_ids(). Ambiguity → ABORT.
      5. LEGACY FALLBACK (roster empty — first-run or test isolation).

    Instance-awareness (F1/F2/F3) per architect-1 Phase-A + analyst TRAP-1 clearance.
    _find_by_name_in_candidates carries the F2 mixed-abort so the collision-prevention
    applies uniformly at chat-scoped AND roster-scoped tiers (not just legacy).

    MEMBERSHIP ≠ DELIVERABILITY: reachability is NOT applied here.  Callers
    that need reachability filtering must apply ``is_reachable(sid)`` AFTER
    resolution. Durable surfaces resolve against MEMBERSHIP alone so msgs queue
    to alive-but-unreachable seats.
    """
    # 1. Fast path: UUID exact + dir-exists
    if _is_uuid(query) and (_BASE_DIR / query).is_dir():
        return query

    if not _BASE_DIR.exists():
        raise ValueError(f"No session named or id'd {query!r} (no sessions exist yet).")

    # 1b. Slot format: '<instance>:<name>' → direct slot lookup (P1)
    if ":" in query:
        try:
            return _find_by_slot_globally(query)
        except ValueError:
            pass  # not found as a slot → fall through to name-based resolution

    # 2. Instance-scoped (P1 bridge): resolve bare name within the caller's instance.
    # Un-stamped callers (caller_instance=None) skip this tier entirely —
    # exact current behavior preserved during the migration window.
    if caller_instance is not None:
        try:
            return _find_by_slot_globally(f"{caller_instance}:{query}")
        except ValueError:
            pass  # not in caller's instance → fall through

    # 3. Chat-scoped: caller provided a chat context.
    # CHANGED (F3): was an early-return; now a fall-through so the instance-scoped
    # tier (2) runs above it. Ambiguity within chat → ABORT; not-found → fall through.
    if chat_id is not None:
        try:
            from khimaira.monitor import chats as _chats  # noqa: PLC0415

            room = _chats.load_room(chat_id)
        except Exception as exc:
            raise ValueError(
                f"resolve_session_id({query!r}): cannot load chat {chat_id!r} for "
                f"chat-scoped resolution — {exc}"
            ) from exc
        # Include both accepted AND pending members.
        chat_members = {
            sid
            for sid, member in room["members"].items()
            if member.get("state") in ("accepted", "pending")
        }
        try:
            return _find_by_name_in_candidates(
                query, chat_members, caller_instance=caller_instance
            )
        except ValueError as exc:
            if "Ambiguous" in str(exc):
                raise ValueError(f"In chat {chat_id!r}: {exc}") from exc
            # Not found in this chat → fall through to roster/legacy.

    # 4. Roster-scoped: no chat context, use canonical MEMBERSHIP predicate.
    # AMBIGUITY → abort (≥2 roster members with this name).
    # NOT-FOUND → fall through to global legacy.
    roster, roster_had_error = _active_roster_for_resolution()
    if roster:
        try:
            return _find_by_name_in_candidates(
                query, roster, caller_instance=caller_instance
            )
        except ValueError as exc:
            err_str = str(exc)
            if "Ambiguous" in err_str:
                # Hard abort: ≥2 roster members share this name → never guess.
                raise
            # Not found in roster: fall through to global legacy scan.

    # 5. Legacy fallback: roster empty OR name not found in roster.
    if not roster:
        if roster_had_error:
            log.warning(
                "resolve_session_id(%r): roster computation FAILED — "
                "falling back to global mtime-heuristic (GAP-5 window open). "
                "Fix the roster read error to restore scoped resolution.",
                query,
            )
        else:
            log.warning(
                "resolve_session_id(%r): active roster is empty (no recently-active chats). "
                "Falling back to global mtime-heuristic scan. "
                "In a live roster this path should not be reached — "
                "check that khimaira chats are configured.",
                query,
            )
    return _resolve_name_global_legacy(query, caller_instance=caller_instance)


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
    # Part F path-11 write-time: resolve target → current slot sid.
    _slot_reg2 = _read_slot_registry()
    _is_revoked2 = any(
        target_session_id in entry.get("revoked_sids", [])
        for entry in _slot_reg2.values()
    )
    if _is_revoked2:
        log.info(
            "sessions: post_answer to %s suppressed — target in revoked_sids (inert)",
            target_session_id[:8],
        )
        return {"ok": False, "suppressed": "target-inert"}
    target_resolved2 = slot_resolve(target_session_id)
    if target_resolved2 is not None:
        target_session_id = target_resolved2
    answer = sanitize_agent_text(answer)
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
    desktop_notify.notify_answer(
        target_session_id, from_session_id, matched.get("text", "")
    )
    log.info(
        "session %s: answer posted by %s for q=%s",
        target_session_id,
        from_session_id,
        question_id,
    )
    return matched


# ---------------------------------------------------------------------------
# Read side — for B (querying A) and for A (reading its own inbox)
# ---------------------------------------------------------------------------


_USABLE_STATUSES = frozenset(
    {"listening", "working", "idle", "researching", "implementing"}
)


def _compute_effective_status(
    status: dict | None, last_tool_call_ts: float | None
) -> dict:
    """Return status dict with `effective_status` field added.

    Reads KHIMAIRA_DEMOTE_THRESHOLD_S at call time (not module-level) so
    test fixtures can monkeypatch the env var without needing importlib.reload.

    effective_status == status["status"] when subscriber is alive
    (last_sse_heartbeat OR last_tool_call within threshold). Otherwise
    effective_status = "unreachable" + adds `demoted_at` + `demoted_reason`.
    """
    demote_threshold_s = int(os.environ.get("KHIMAIRA_DEMOTE_THRESHOLD_S", 20 * 60))

    if not isinstance(status, dict):
        return {"effective_status": "unknown"}

    out = dict(status)
    raw_status = status.get("status", "unknown")
    out["effective_status"] = raw_status  # default: trust the field

    now = time.time()
    last_hb_iso = status.get("last_sse_heartbeat")
    last_hb_ts: float | None = None
    if last_hb_iso:
        try:
            last_hb_ts = datetime.fromisoformat(
                last_hb_iso.replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, AttributeError):
            pass

    candidates = [ts for ts in (last_hb_ts, last_tool_call_ts) if ts is not None]
    most_recent: float | None = max(candidates) if candidates else None

    if most_recent is None or (now - most_recent) > demote_threshold_s:
        out["effective_status"] = "unreachable"
        out["demoted_at"] = _now_iso()
        out["demoted_reason"] = (
            f"no SSE heartbeat or tool activity in last {demote_threshold_s}s"
        )
    return out


def state(session_id: str, recent: int = 10, *, workspace: str | None = None) -> dict:
    """Full digest of session_id's externalized state. The 'what is session A
    up to right now' query.

    Accepts either a session UUID OR a friendly name (set via set_name).

    Workspace gate (opt-in, backward-compatible): pass `workspace="X"`
    to assert the target session is in workspace X. Mismatch raises
    ValueError (mapped to 404 at the API layer). Passing None or "*"
    skips the check entirely — preserves prior behavior.
    """
    session_id = resolve_session_id(session_id)
    if workspace not in (None, "*"):
        target_ws = get_workspace(session_id)
        if target_ws != workspace:
            raise ValueError(
                f"No session named or id'd {session_id!r} in workspace "
                f"{workspace!r} (target is in {target_ws!r}). Pass "
                f"workspace='*' or omit to read cross-workspace."
            )
    d = _session_dir(session_id)
    decisions = _read_jsonl(d / "decisions.jsonl")
    files = _read_jsonl(d / "files_touched.jsonl")
    questions = _read_jsonl(d / "questions.jsonl")
    status_path = d / "status.json"
    status_raw = json.loads(status_path.read_text()) if status_path.exists() else None

    recent_calls = recent_tool_calls(session_id, limit=1)
    last_tool_ts: float | None = None
    if recent_calls:
        try:
            last_tool_ts = datetime.fromisoformat(
                recent_calls[0]["ts"].replace("Z", "+00:00")
            ).timestamp()
        except (ValueError, KeyError):
            pass
    status = _compute_effective_status(status_raw, last_tool_ts)

    return {
        "session_id": session_id,
        "status": status,
        "recent_decisions": decisions[-recent:],
        "decision_count": len(decisions),
        "recent_files": files[-recent:],
        "file_touch_count": len(files),
        "open_questions": [q for q in questions if q.get("status") == "open"],
        "answered_questions": [q for q in questions if q.get("status") == "answered"][
            -recent:
        ],
    }


def summary(session_id: str) -> dict:
    """Lightweight digest — counts + status + last_active. No record bodies.

    Use when polling "is X done yet?" or rendering a sessions overview.
    Substantially cheaper than `state()` for sessions with long history
    because it counts lines instead of JSON-parsing every record.
    """
    session_id = resolve_session_id(session_id)
    sd = _session_dir(session_id)

    last_mtime = max(
        (p.stat().st_mtime for p in sd.iterdir() if p.is_file()),
        default=0.0,
    )
    decisions_path = sd / "decisions.jsonl"
    files_path = sd / "files_touched.jsonl"
    decision_count = (
        sum(1 for ln in decisions_path.open() if ln.strip())
        if decisions_path.exists()
        else 0
    )
    file_touch_count = (
        sum(1 for ln in files_path.open() if ln.strip()) if files_path.exists() else 0
    )
    questions = _read_jsonl(sd / "questions.jsonl")
    open_question_count = sum(1 for q in questions if q.get("status") == "open")
    status_path = sd / "status.json"
    status = json.loads(status_path.read_text()) if status_path.exists() else None

    return {
        "session_id": session_id,
        "status": status,
        "decision_count": decision_count,
        "file_touch_count": file_touch_count,
        "open_question_count": open_question_count,
        "last_active": last_mtime,
        "last_active_age_s": time.time() - last_mtime if last_mtime else None,
    }


def recent_decisions(
    across_sessions: bool = True,
    recent_per_session: int = 5,
    *,
    workspace: str | None = None,
) -> list[dict]:
    """Recent decisions across all sessions (or just the active ones).

    Workspace filter (opt-in): pass `workspace="X"` to scope to one
    workspace. None / "*" returns everything (backward-compatible).
    """
    if not _BASE_DIR.exists():
        return []
    out: list[dict] = []
    for sd in _BASE_DIR.iterdir():
        if not sd.is_dir():
            continue
        if workspace not in (None, "*"):
            # Per-session lookup is cheap (single status.json read); we
            # do it before reading decisions.jsonl so we skip the more
            # expensive scan on mismatched sessions.
            if get_workspace(sd.name) != workspace:
                continue
        decisions = _read_jsonl(sd / "decisions.jsonl")[-recent_per_session:]
        for d in decisions:
            d["session_id"] = sd.name
            out.append(d)
    out.sort(key=lambda d: d.get("ts", ""), reverse=True)
    return out


def pending_notes(
    session_id: str,
    mark_read: bool = True,
    session_cwd: str | None = None,
) -> list[dict]:
    """A reads its inbox — unread notes from other sessions.

    Called by /inbox skill (mark_read=true) and by old SessionStart hooks.
    The newer auto-inject UserPromptSubmit hook uses surface_inbox_for_hook
    (different path — peek + count, doesn't drain).

    `session_cwd`: when provided, notices with a `scope_cwd` that does not
    match (or is not a parent of) `session_cwd` are silently excluded. Notices
    without `scope_cwd` always surface (backward-compatible broadcast).

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

    cwd_abs = os.path.abspath(session_cwd) if session_cwd else None

    def _cwd_matches(note: dict) -> bool:
        scope = note.get("scope_cwd") or ""
        if not scope or not cwd_abs:
            return True  # no scope or no caller cwd → always surface
        return cwd_abs == scope or cwd_abs.startswith(scope.rstrip("/") + "/")

    pending = [n for n in notes if not n.get("read") and _cwd_matches(n)]

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
    so /home/_3ntropy/dev/khimaira → -home--3ntropy-dev-khimaira. Different
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
                arg_summary = ", ".join(f"{k}={str(v)[:60]}" for k, v in args.items())[
                    :300
                ]
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
            excerpt_turns.append(
                {
                    "line_no": t["_line_no"],
                    "type": t.get("type") or "?",
                    "role": (
                        (t.get("message") or {}).get("role")
                        if isinstance(t.get("message"), dict)
                        else None
                    ),
                    "is_match": j == idx,
                    "text_preview": t["_text"][:500]
                    + ("…" if len(t["_text"]) > 500 else ""),
                }
            )
        matches.append(
            {
                "match_at_turn": idx,
                "match_at_line": turn["_line_no"],
                "excerpt": excerpt_turns,
            }
        )
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
                                        if (
                                            v.startswith("/")
                                            or v.startswith("./")
                                            or "." in v.rsplit("/", 1)[-1]
                                        ):
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


def _resolve_project_label_to_cwd(label: str) -> str | None:
    """Look up a khimaira-attached project by label. Returns its project_path
    (which is the cwd handoffs are scoped against).

    Uses ~/.local/state/khimaira/attached.json from the venv-injection
    registry. Sessions never need to type cwd paths directly — they
    address projects by the same name they used at `khimaira attach`.
    """
    try:
        from khimaira.attach.registry import list_attached
    except ImportError:
        return None
    for entry in list_attached():
        if entry.get("label") == label:
            return entry.get("project_path")
    return None


def route_message(
    target: str,
    text: str,
    *,
    from_session_id: str,
) -> dict:
    """Send `text` to `target` — smart-routes between notice and handoff.

    Resolution order:
      1. If `target` matches a known session name/UUID → post_notice
         (one-to-one delivery, target's inbox)
      2. If `target` matches a khimaira-attached project label →
         post_handoff scoped to that project's cwd (one-to-many, any
         future session in that project sees it)
      3. Otherwise → ValueError (caller picks alternative)

    Returns a dict with both the action taken and the underlying record,
    so callers can show "📨 sent as notice" vs "📦 sent as project handoff".
    """
    # Try session resolution first — fastest, most common case
    try:
        resolved = resolve_session_id(target)
        # Hit: this is a known session. Post a notice.
        note = post_notice(resolved, text, from_session_id=from_session_id)
        return {
            "routed_as": "notice",
            "target_session_id": resolved,
            "record": note,
        }
    except ValueError:
        pass  # Fall through to project lookup

    # Try project label lookup
    project_cwd = _resolve_project_label_to_cwd(target)
    if project_cwd:
        handoff = post_handoff(
            from_session_id,
            text,
            scope_cwd=project_cwd,
        )
        return {
            "routed_as": "project_handoff",
            "project_label": target,
            "scope_cwd": project_cwd,
            "record": handoff,
        }

    raise ValueError(
        f"No session named or id'd {target!r} and no khimaira-attached "
        f"project labeled {target!r}. Use session_list() to see active "
        f"sessions, or `khimaira attached` to see project labels."
    )


def post_handoff(
    from_session_id: str,
    text: str,
    *,
    scope_cwd: str | None = None,
    scope_project: str | None = None,
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
    # Project label takes precedence over file-touch inference but not
    # over an explicit scope_cwd. Resolution: scope_cwd > scope_project >
    # file-touch heuristic > os.getcwd().
    if inferred is None and scope_project:
        inferred = _resolve_project_label_to_cwd(scope_project)
        if inferred is None:
            raise ValueError(
                f"scope_project={scope_project!r} doesn't match any "
                f"khimaira-attached project. Run `khimaira attached` to "
                f"see labels, or pass scope_cwd explicitly."
            )
    if inferred is None:
        # Infer from session's most-recent file touch — that's the
        # directory they were working in. Fallback: process cwd.
        _sd = _session_dir_read(from_session_id)
        files = _read_jsonl(_sd / "files_touched.jsonl") if _sd else []
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
    desktop_notify.notify_handoff(from_session_id, inferred, text)
    log.info(
        "handoff posted by %s for cwd=%s — %s",
        from_session_id,
        inferred,
        text[:80],
    )
    return handoff


def subscribe_handoff(handoff_id: str, session_id: str) -> dict:
    """Session opts into receiving owner's progress updates for a handoff.

    Use case: user opens a second/third session in a project where a
    handoff is already being worked on by an owner. The new session
    subscribes; whenever owner logs a decision, subscribers see it in
    their inbox automatically. Lets multiple sessions collaborate on
    the same handoff without colliding on the primary work.

    Idempotent — subscribing twice is a no-op.
    """
    if not _HANDOFFS_PATH.exists():
        raise ValueError(f"No handoff with id {handoff_id!r}")

    # Lenient resolve: subscribers / releasers may not have an on-disk
    # session dir yet (fresh sessions that haven't logged anything).
    # If resolve_session_id fails, use the literal id.
    try:
        session_id = resolve_session_id(session_id)
    except ValueError:
        pass  # use the literal session_id as given
    handoffs = _read_jsonl(_HANDOFFS_PATH)
    matched: dict | None = None
    for h in handoffs:
        if h.get("id") == handoff_id:
            subscribers = h.setdefault("subscribers", [])
            if session_id not in subscribers:
                subscribers.append(session_id)
            matched = h
            break

    if matched is None:
        raise ValueError(
            f"No handoff with id {handoff_id!r}. Use list_handoffs to see "
            f"available handoffs in your cwd."
        )

    tmp = _HANDOFFS_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for h in handoffs:
            f.write(json.dumps(h, separators=(",", ":")) + "\n")
    tmp.replace(_HANDOFFS_PATH)
    return matched


def unsubscribe_handoff(handoff_id: str, session_id: str) -> dict:
    """Remove a session from a handoff's subscriber list."""
    if not _HANDOFFS_PATH.exists():
        raise ValueError(f"No handoff with id {handoff_id!r}")

    # Lenient resolve: subscribers / releasers may not have an on-disk
    # session dir yet (fresh sessions that haven't logged anything).
    # If resolve_session_id fails, use the literal id.
    try:
        session_id = resolve_session_id(session_id)
    except ValueError:
        pass  # use the literal session_id as given
    handoffs = _read_jsonl(_HANDOFFS_PATH)
    matched: dict | None = None
    for h in handoffs:
        if h.get("id") == handoff_id:
            subscribers = h.get("subscribers") or []
            if session_id in subscribers:
                subscribers.remove(session_id)
                h["subscribers"] = subscribers
            matched = h
            break

    if matched is None:
        raise ValueError(f"No handoff with id {handoff_id!r}")

    tmp = _HANDOFFS_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for h in handoffs:
            f.write(json.dumps(h, separators=(",", ":")) + "\n")
    tmp.replace(_HANDOFFS_PATH)
    return matched


def release_handoff(handoff_id: str, session_id: str) -> dict:
    """Owner steps aside; next session to consume the handoff becomes owner.

    Use when you've finished your part or realized this isn't your lane.
    Subscribers stay subscribed (still observers); only the owner slot
    clears.
    """
    if not _HANDOFFS_PATH.exists():
        raise ValueError(f"No handoff with id {handoff_id!r}")

    # Lenient resolve: subscribers / releasers may not have an on-disk
    # session dir yet (fresh sessions that haven't logged anything).
    # If resolve_session_id fails, use the literal id.
    try:
        session_id = resolve_session_id(session_id)
    except ValueError:
        pass  # use the literal session_id as given
    handoffs = _read_jsonl(_HANDOFFS_PATH)
    matched: dict | None = None
    for h in handoffs:
        if h.get("id") == handoff_id:
            if h.get("owner_session_id") != session_id:
                raise ValueError(
                    f"Session {session_id!r} doesn't own handoff "
                    f"{handoff_id!r}; owner is "
                    f"{h.get('owner_session_id') or 'unset'}."
                )
            h["owner_session_id"] = None
            matched = h
            break

    if matched is None:
        raise ValueError(f"No handoff with id {handoff_id!r}")

    tmp = _HANDOFFS_PATH.with_suffix(".jsonl.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for h in handoffs:
            f.write(json.dumps(h, separators=(",", ":")) + "\n")
    tmp.replace(_HANDOFFS_PATH)
    return matched


def invite_handoff(
    parent_handoff_id: str,
    owner_session_id: str,
    invitee_session_id: str,
    text: str,
    *,
    expires_in_hours: float = 168.0,
) -> dict:
    """Owner of a handoff delegates a slice of work to a specific session.

    Creates a CHILD handoff that:
      - links back to the parent via `parent_id`,
      - is scoped to a single session via `target_session_id` (only the
        named session can consume it; cwd-scoped consumers skip it),
      - inherits the parent's `scope_cwd` so the SessionStart hook still
        considers it in-scope when the invitee boots in the same project,
      - posts an inbox notice immediately so a currently-live invitee
        sees the invite mid-session without waiting for next boot.

    Use case: you've claimed handoff A but it has 3 distinct subtasks.
    You take subtask 1; invite sibling session X to subtask 2.
    X gets a real directive, not a vague "FYI", and the SessionStart
    hook framing applies (handoff = directive, not chat).

    Args:
        parent_handoff_id: 12-char id of the parent handoff. The caller
            must currently own this handoff.
        owner_session_id: caller's session id (must equal parent's
            owner_session_id).
        invitee_session_id: target session — UUID or friendly name. Resolved
            at invite time, so later renames don't redirect the invite.
        text: invite body — what specifically you want the invitee to do.
        expires_in_hours: invite TTL. Default 7 days. Shorter for urgent
            asks, longer for "whenever you get to it" delegations.

    Raises:
        ValueError: parent handoff doesn't exist; caller doesn't own it;
            invitee can't be resolved (and isn't a plausible literal id).
    """
    if not _HANDOFFS_PATH.exists():
        raise ValueError(f"No handoff with id {parent_handoff_id!r}")

    # Owner must resolve cleanly; lenient otherwise (matches release_handoff).
    try:
        owner_session_id = resolve_session_id(owner_session_id)
    except ValueError:
        pass

    # Invitee resolves at invite time — name→uuid snapshotted now, so a
    # later rename doesn't silently redirect the invite to someone else.
    # Lenient fallback: if the name doesn't resolve (sister session hasn't
    # logged anything yet), keep the literal so the invite still works
    # once they materialize.
    try:
        invitee_resolved = resolve_session_id(invitee_session_id)
    except ValueError:
        invitee_resolved = invitee_session_id

    parent: dict | None = None
    for h in _read_jsonl(_HANDOFFS_PATH):
        if h.get("id") == parent_handoff_id:
            parent = h
            break
    if parent is None:
        raise ValueError(
            f"No handoff with id {parent_handoff_id!r}. Use list_handoffs "
            f"to see available handoffs in your cwd."
        )
    if parent.get("owner_session_id") != owner_session_id:
        raise ValueError(
            f"Session {owner_session_id!r} doesn't own handoff "
            f"{parent_handoff_id!r}; owner is "
            f"{parent.get('owner_session_id') or 'unset'}. Only the "
            f"current owner can invite."
        )

    expires_at = time.time() + expires_in_hours * 3600.0
    child = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "from_session_id": owner_session_id,
        "parent_id": parent_handoff_id,
        "target_session_id": invitee_resolved,
        "text": text,
        "scope_cwd": parent.get("scope_cwd"),
        "expires_at": expires_at,
        "read_by": [],
    }
    _HANDOFFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    _append_jsonl(_HANDOFFS_PATH, child)

    # Best-effort: surface to currently-live invitee via inbox. If the
    # invitee session dir doesn't exist yet, skip — the SessionStart hook
    # will pick the handoff up when they boot.
    try:
        notice_text = (
            f"🤝 INVITE from {owner_session_id[:8]} (handoff {child['id']}, "
            f"parent {parent_handoff_id}): {text}"
        )
        post_notice(
            invitee_resolved,
            notice_text,
            from_session_id=owner_session_id,
            fire_desktop_notify=False,  # invite path fires notify_invite below
        )
    except ValueError:
        # Invitee not materialized yet — handoff alone will surface on
        # their next boot.
        pass

    desktop_notify.notify_invite(owner_session_id, invitee_resolved, text)
    log.info(
        "handoff %s inviting %s by %s (parent=%s) — %s",
        child["id"],
        invitee_resolved,
        owner_session_id,
        parent_handoff_id,
        text[:80],
    )
    return child


def _broadcast_to_handoff_subscribers(
    owner_session_id: str,
    kind: str,
    text: str,
) -> None:
    """Drop a notice into every subscriber of every handoff this session owns.

    Runs in a background daemon thread so the caller (log_decision /
    log_touch / set_status) returns immediately. Previously synchronous
    — measured 344ms outlier on log_decision because N subscribers
    meant N sequential file appends on the caller's hot path.

    The owner doesn't need to know whether subscribers actually received
    notices; eventual delivery is the contract. If the broadcast thread
    crashes, the owner's decision is still logged.

    Silent on every failure path — never block the owner's work on a
    broadcast issue, never propagate broadcast errors to the caller.
    """
    if not _HANDOFFS_PATH.exists():
        return

    # Cheap pre-check on the sync path: if we have no handoffs at all,
    # don't even spin up a thread. Eliminates overhead for the 95%+ of
    # log_decision calls where no broadcast is needed.
    try:
        handoffs_snapshot = _read_jsonl(_HANDOFFS_PATH)
    except OSError:
        return
    owned = [
        h for h in handoffs_snapshot if h.get("owner_session_id") == owner_session_id
    ]
    if not owned:
        return

    # Capture the data the thread needs so we don't re-read the file
    # (which might race with concurrent writes).
    work: list[tuple[str, str, dict]] = []
    for h in owned:
        for sub_id in h.get("subscribers") or []:
            note = {
                "id": uuid.uuid4().hex[:12],
                "ts": _now_iso(),
                "kind": "notice",
                "text": f"[handoff {h['id'][:8]} progress] {kind}: {text[:300]}",
                "from_session_id": owner_session_id,
                "read": False,
                "surface_count": 0,
            }
            work.append((h["id"], sub_id, note))

    if not work:
        return

    def _do_fanout() -> None:
        for _handoff_id, sub_id, note in work:
            try:
                _append_jsonl(_session_dir(sub_id) / "inbox.jsonl", note)
            except Exception:
                pass  # subscriber may have been deleted; skip
            # Desktop notification is gated separately — broadcasts are
            # high-volume so the env var defaults OFF. Opt in via
            # KHIMAIRA_DESKTOP_NOTIFY_BROADCAST=1.
            try:
                desktop_notify.notify_broadcast(owner_session_id, sub_id, kind, text)
            except Exception:
                pass

    import threading

    threading.Thread(target=_do_fanout, daemon=True).start()


def list_handoffs_in_scope(session_id: str, cwd: str) -> list[dict]:
    """List handoffs visible from this cwd, with owner + subscriber summaries.

    Distinct from consume_handoffs: read-only, doesn't mark read,
    surfaces full state (owner, subscriber count, claim status).
    """
    if not _HANDOFFS_PATH.exists():
        return []
    cwd_abs = os.path.abspath(cwd)
    handoffs = _read_jsonl(_HANDOFFS_PATH)
    now = time.time()
    out: list[dict] = []

    for h in handoffs:
        if h.get("expires_at", 0) < now:
            continue
        scope = h.get("scope_cwd") or ""
        if cwd_abs != scope and not cwd_abs.startswith(scope.rstrip("/") + "/"):
            continue
        out.append(h)

    out.sort(key=lambda h: h.get("ts", ""), reverse=True)
    return out


def consume_handoffs(
    session_id: str,
    cwd: str,
    mark_read: bool = True,
) -> list[dict]:
    """Return handoffs matching this session's cwd.

    Auto-claims ownership (first caller becomes owner) and optionally marks
    this session as having read the handoff.

    mark_read=True (default): adds session_id to read_by so the handoff
        won't re-surface on the next SessionStart. Use for explicit acks.
    mark_read=False: surfaces and auto-claims but does NOT add to read_by —
        the handoff re-surfaces on every SessionStart until explicitly acked.
        Use for the SessionStart hook so a session-compaction doesn't lose
        the handoff body permanently.

    Match: handoff.scope_cwd is == cwd OR cwd is a child of scope_cwd
    (so a handoff scoped at /repo/root surfaces in any session working
    in /repo/root/sub/path/...).

    Targeted handoffs (those with `target_session_id`, posted via
    `invite_handoff`) additionally require session_id == target. Other
    sessions in the same cwd MUST NOT consume someone else's invite.

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
        # Targeted-invite filter: if a handoff names a specific invitee,
        # only that session may consume it; cwd-peers skip silently.
        # Part F path-12 read-time: also allow a reattached session to consume
        # handoffs targeted at its prior sid (slot_resolve(target) == session_id).
        target = h.get("target_session_id")
        if target and target != session_id:
            # Check slot-heal: target may be a prior sid of the poller's slot.
            resolved_target = slot_resolve(target)
            if resolved_target != session_id:
                continue
        read_by = h.get("read_by") or []
        if session_id in read_by:
            continue

        # AUTO-CLAIM: first session to consume becomes owner. Subsequent
        # sessions get the handoff with owner info attached so they can
        # decide to subscribe (collaborate) or stand down. The session
        # sees this distinction in its SessionStart hook output.
        # Invites bypass the contested-ownership branch — they're already
        # 1:1 by construction.
        existing_owner = h.get("owner_session_id")
        if not existing_owner:
            h["owner_session_id"] = session_id
            h["_claim_role"] = "owner"  # transient flag for the hook
            needs_rewrite = True  # persist the ownership claim regardless of mark_read
        else:
            h["_claim_role"] = "observer"
            h["_owner_session_id"] = existing_owner

        matched.append(h)
        if mark_read:
            h["read_by"] = read_by + [session_id]
            needs_rewrite = True

    # Rewrite if EITHER we marked something read (or auto-claimed) OR there
    # are expired entries to drop. Without the second condition, an all-expired
    # file accumulates forever — the gc only runs when something else happens
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
            log.warning(
                "failed to rewrite handoffs.jsonl; read state may double-surface"
            )

    return matched


def post_notice(
    target_session_id: str,
    text: str,
    *,
    from_session_id: str = "external",
    fire_desktop_notify: bool = True,
    scope_cwd: str | None = None,
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
    is for attribution. `fire_desktop_notify=False` suppresses the
    desktop popup — used when a caller has already fired its own
    purpose-built notification.
    """
    target_session_id = resolve_session_id(target_session_id)
    # Part F path-10 write-time: resolve target → current slot sid so notices
    # land in the live session's inbox, not the stale prior's.
    # INERT-DENIAL: if target is in revoked_sids (superseded-beyond-last-1-bound),
    # suppress delivery. If NOT in the registry (un-slotted session), deliver as-is.
    # Distinguish "not in registry" (deliver) from "revoked beyond bound" (deny).
    _slot_reg = _read_slot_registry()
    _is_revoked = any(
        target_session_id in entry.get("revoked_sids", [])
        for entry in _slot_reg.values()
    )
    if _is_revoked:
        log.info(
            "sessions: post_notice to %s suppressed — target in revoked_sids (inert)",
            target_session_id[:8],
        )
        return {"ok": False, "suppressed": "target-inert"}
    target_resolved = slot_resolve(target_session_id)
    # If in registry (current or prior) → deliver to the current authoritative sid.
    # If NOT in registry (un-slotted) → deliver to original target (existing behavior).
    if target_resolved is not None:
        target_session_id = target_resolved
    text = sanitize_agent_text(text)
    note: dict = {
        "id": uuid.uuid4().hex[:12],
        "ts": _now_iso(),
        "kind": "notice",
        "text": text,
        "from_session_id": from_session_id,
        "read": False,
        "surface_count": 0,
    }
    if scope_cwd:
        note["scope_cwd"] = scope_cwd
    _append_jsonl(_session_dir(target_session_id) / "inbox.jsonl", note)

    # Loud-fail: surface target reachability so callers know if the notice
    # landed in a session that's likely unreachable.
    try:
        target_state = state(target_session_id)
        target_status = target_state.get("status") or {}
        effective = target_status.get("effective_status", "unknown")
        note["target_reachable"] = effective in _USABLE_STATUSES
        note["target_status"] = effective
        note["target_last_active_iso"] = target_status.get(
            "updated_at"
        ) or target_status.get("last_sse_heartbeat")
        if not note["target_reachable"]:
            note["reason_if_not_ok"] = target_status.get("demoted_reason") or (
                f"target status: {effective}"
            )
    except (ValueError, OSError):
        note["target_reachable"] = False
        note["target_status"] = "unknown"
        note["reason_if_not_ok"] = "could not resolve target state"

    if fire_desktop_notify:
        desktop_notify.notify_notice(target_session_id, from_session_id, text)
    log.info(
        "session %s: notice posted by %s (id=%s) — %s",
        target_session_id,
        from_session_id,
        note["id"],
        text[:80],
    )
    return note


_HOOK_AUTO_EXPIRE_AFTER = 3
"""Max times an inbox note re-surfaces on the hook before auto-marking read.

Safety net so an unresponsive agent (one that ignores notices in its
context block instead of surfacing them to the user) doesn't loop the
same notice into context forever. Agents SHOULD ack via session_ack_notes
when they've surfaced the content; this is the fallback.
"""


def surface_inbox_for_hook(
    session_id: str,
    session_cwd: str | None = None,
) -> list[dict]:
    """Hook-only fetch path. Returns unread notes, increments surface_count.

    Differs from pending_notes: doesn't mark read on first fetch. Notes
    re-surface each turn until the agent explicitly acks (via
    session_ack_notes) OR surface_count hits the auto-expire threshold,
    in which case they also get moved to archive.jsonl.

    Each returned note carries a `_remaining_surfaces` field so the hook
    can render urgency info ("[2/3 surfaces remaining — call ack]").

    `session_cwd`: when provided, notices whose `scope_cwd` doesn't match
    are skipped (not incremented, not archived — left for matching sessions).
    Notices without `scope_cwd` always surface (backward-compat broadcast).
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

    cwd_abs = os.path.abspath(session_cwd) if session_cwd else None

    for n in notes:
        if n.get("read"):
            archived.append(n)
            continue

        # scope_cwd filter: leave note untouched if cwd doesn't match.
        note_scope = n.get("scope_cwd") or ""
        if note_scope and cwd_abs:
            if cwd_abs != note_scope and not cwd_abs.startswith(
                note_scope.rstrip("/") + "/"
            ):
                remaining.append(n)
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

    Closes the "what did khimaira-builder say about Roboflow last week?"
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

    raise TimeoutError(
        f"No answer to question {question_id} on session {target_session_id} within {timeout:.0f}s"
    )


def incoming_questions(session_id: str) -> list[dict]:
    """Open questions on OTHER sessions that target this session.

    Symmetric counterpart to pending_notes. The khimaira multi-session
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
            out.append(
                {
                    **q,
                    "from_session_id": sd.name,
                }
            )
    out.sort(key=lambda q: q.get("ts", ""), reverse=True)
    return out


# list_sessions is hot — called 9+ times per day in measured usage, with
# p95=172ms (one outlier hit 776ms) when sessions accumulate. The work
# is per-session: open every session dir, stat every file, count lines
# in jsonl files, read status.json. Scales linearly with session count.
#
# Cache result for _LIST_SESSIONS_TTL seconds. 2s is short enough that
# stale-data risk is negligible (sessions update infrequently) and long
# enough that bursts of UI/MCP calls hit cache.
_LIST_SESSIONS_TTL = 2.0
_list_sessions_cache: tuple[float, list[dict]] | None = None
_list_sessions_lock = None  # lazy init to avoid threading import on cold path


def list_sessions(use_cache: bool = True, workspace: str | None = None) -> list[dict]:
    """All sessions with their last-modified timestamp + summary counts.

    Cached for 2s. Pass use_cache=False to force a fresh scan (rare —
    most callers prefer the cache because sessions don't move fast).

    Workspace filter (opt-in, backward-compatible):
      - workspace=None (default): return ALL sessions, regardless of
        workspace. Preserves prior behavior — existing callers see no
        change.
      - workspace="*": same as None — explicit "I want everything".
      - workspace="<name>": only sessions in that workspace. Sessions
        without an explicit workspace field are treated as `"default"`,
        so workspace="default" includes legacy / unmigrated sessions.
    """
    global _list_sessions_cache, _list_sessions_lock

    rows: list[dict]
    if use_cache and _list_sessions_cache is not None:
        cached_at, cached_data = _list_sessions_cache
        if time.time() - cached_at < _LIST_SESSIONS_TTL:
            rows = cached_data
            return _filter_sessions_by_workspace(rows, workspace)

    if _list_sessions_lock is None:
        import threading

        _list_sessions_lock = threading.Lock()

    with _list_sessions_lock:
        # Double-check after acquiring lock — another thread may have
        # already refreshed while we were waiting.
        if use_cache and _list_sessions_cache is not None:
            cached_at, cached_data = _list_sessions_cache
            if time.time() - cached_at < _LIST_SESSIONS_TTL:
                return _filter_sessions_by_workspace(cached_data, workspace)

        if not _BASE_DIR.exists():
            _list_sessions_cache = (time.time(), [])
            return []

        out: list[dict] = []
        for sd in _BASE_DIR.iterdir():
            if not sd.is_dir():
                continue
            last_mtime = max(
                (p.stat().st_mtime for p in sd.iterdir() if p.is_file()),
                default=0.0,
            )
            decisions = (
                sum(1 for _ in (sd / "decisions.jsonl").open() if _.strip())
                if (sd / "decisions.jsonl").exists()
                else 0
            )
            files = (
                sum(1 for _ in (sd / "files_touched.jsonl").open() if _.strip())
                if (sd / "files_touched.jsonl").exists()
                else 0
            )
            questions = _read_jsonl(sd / "questions.jsonl")
            open_q = sum(1 for q in questions if q.get("status") == "open")
            status_path = sd / "status.json"
            status_raw = (
                json.loads(status_path.read_text()) if status_path.exists() else None
            )

            # Compute effective_status for this row (lazy demote).
            recent_calls = recent_tool_calls(sd.name, limit=1)
            last_tool_ts: float | None = None
            if recent_calls:
                try:
                    last_tool_ts = datetime.fromisoformat(
                        recent_calls[0]["ts"].replace("Z", "+00:00")
                    ).timestamp()
                except (ValueError, KeyError):
                    pass
            status = _compute_effective_status(status_raw, last_tool_ts)

            # Resolve workspace from status.json once + surface in the row
            # so cached lookups don't need to re-stat status.json.
            ws_value = DEFAULT_WORKSPACE
            if isinstance(status_raw, dict):
                raw = status_raw.get("workspace")
                if isinstance(raw, str) and raw:
                    ws_value = raw

            out.append(
                {
                    "session_id": sd.name,
                    "name": (
                        status_raw.get("name") if isinstance(status_raw, dict) else None
                    ),
                    "workspace": ws_value,
                    "last_active": last_mtime,
                    "last_active_age_s": (
                        time.time() - last_mtime if last_mtime else None
                    ),
                    "status": status,
                    "decision_count": decisions,
                    "file_touch_count": files,
                    "open_question_count": open_q,
                }
            )
        out.sort(key=lambda r: r.get("last_active", 0), reverse=True)
        _list_sessions_cache = (time.time(), out)
        return _filter_sessions_by_workspace(out, workspace)


def _filter_sessions_by_workspace(
    rows: list[dict], workspace: str | None
) -> list[dict]:
    """Apply workspace filter to a list_sessions result.

    None or "*" returns the full list (no filter). A concrete name
    returns only matching rows; rows without a workspace field are
    treated as DEFAULT_WORKSPACE so legacy data isn't silently hidden.
    """
    if workspace in (None, "*"):
        return rows
    return [r for r in rows if (r.get("workspace") or DEFAULT_WORKSPACE) == workspace]


def _invalidate_list_sessions_cache() -> None:
    """Bust the cache. Called by write paths (set_name, set_status) so
    UI updates feel instant even though most reads are cached."""
    global _list_sessions_cache
    _list_sessions_cache = None


# ---------------------------------------------------------------------------
# Session deletion
# ---------------------------------------------------------------------------

_ARCHIVE_DIR = _BASE_DIR.parent / "sessions_archive"


def delete_session(session_id: str, force: bool = False) -> dict:
    """Remove a session from the registry.

    Guards:
    - If session has decisions and force=False: refuse with structured error.
    - If session has decisions and force=True: archive before deletion.
    - If session_id matches the env var CLAUDE_CODE_SESSION_ID: refuse (no self-delete).
    - If session_id is already gone: return structured error (idempotent-safe).

    Chat memberships: the session is marked as LEFT in every chat where it
    holds ACCEPTED or PENDING state (skips chats where it's master — those
    require an explicit hand-off first).

    Returns:
        {"deleted": True, "session_id": ..., "name": ..., "had_decisions": bool,
         "archived_to": path or None, "chats_left": [...], "chats_skipped_master": [...]}
    """
    from datetime import datetime

    # Self-delete guard
    self_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if self_id and session_id == self_id:
        return {"error": "cannot delete the current session", "session_id": session_id}

    # Resolve name→id and check existence
    try:
        resolved = resolve_session_id(session_id)
    except ValueError:
        return {"error": f"session not found: {session_id!r}", "session_id": session_id}

    session_dir = _BASE_DIR / resolved

    # Idempotent: already deleted
    if not session_dir.exists():
        return {"error": f"session not found: {resolved!r}", "session_id": resolved}

    # ALIVE-GUARD (root fix for chat-orphaning): refuse to delete a session that is
    # currently ACTIVE. delete_session marks the session LEFT in every chat, and
    # 'left' cannot self-rejoin — so deleting a still-running session silently orphans
    # it from the roster chat. This bites a roster cleanup that resolves a name to the
    # LIVE same-named session via name-collision (resolve_session_id picks most-recent).
    # An active session is never a valid deletion target. NOT overridable by `force`
    # (which is decisions-only): active roster sessions carry decisions, so a force=True
    # delete would otherwise bypass this. To delete a live session, stop it first.
    import time as _time

    # Env-tunable; read at call-time so test isolation / module reloads don't bake in a
    # stale value (KHIMAIRA_ALIVE_DELETE_GUARD_S=0 disables it).
    _alive_guard_s = float(os.environ.get("KHIMAIRA_ALIVE_DELETE_GUARD_S", "900"))
    try:
        _mtimes = [p.stat().st_mtime for p in session_dir.iterdir() if p.is_file()]
        _last_age = (_time.time() - max(_mtimes)) if _mtimes else float("inf")
    except OSError:
        _last_age = float("inf")
    if _last_age < _alive_guard_s:
        return {
            "error": (
                f"refusing to delete ACTIVE session {resolved!r} (last active "
                f"{_last_age:.0f}s ago). delete_session leaves all its chats "
                f"(state='left', which cannot self-rejoin) — deleting a running session "
                f"orphans it from the roster. If you meant an OLD same-named session, "
                f"target its EXACT session-id; to delete this live one, stop it first."
            ),
            "session_id": resolved,
            "active": True,
            "last_active_age_s": round(_last_age),
        }

    # Read decision count and name before touching anything
    decisions = _read_jsonl(session_dir / "decisions.jsonl")
    decision_count = len(decisions)
    status_path = session_dir / "status.json"
    name: str | None = None
    if status_path.exists():
        try:
            name = json.loads(status_path.read_text()).get("name")
        except (OSError, json.JSONDecodeError):
            pass

    # Guard: decisions present without force
    if decision_count > 0 and not force:
        return {
            "error": (
                f"session {resolved!r} has {decision_count} decision(s); "
                "pass force=True to delete anyway"
            ),
            "session_id": resolved,
            "decision_count": decision_count,
        }

    # Archive decisions before deletion
    archived_to: str | None = None
    if decision_count > 0 and force:
        _ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)
        ts_slug = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        archive_path = _ARCHIVE_DIR / f"{resolved}.deleted.{ts_slug}.json"
        archive_data = {
            "session_id": resolved,
            "name": name,
            "deleted_at": _now_iso(),
            "decisions": decisions,
        }
        archive_path.write_text(json.dumps(archive_data, indent=2))
        archived_to = str(archive_path)

    # Leave active chat memberships
    chats_left: list[str] = []
    chats_skipped_master: list[str] = []
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if chat_dir.exists():
            for chat_file in chat_dir.glob("chat-*.jsonl"):
                chat_id = chat_file.stem
                try:
                    room = chats_mod.load_room(chat_id)
                    member = room["members"].get(resolved)
                    if not member or member["state"] not in (
                        chats_mod.PENDING,
                        chats_mod.ACCEPTED,
                    ):
                        continue
                    if chats_mod._is_master(room, resolved):
                        chats_skipped_master.append(chat_id)
                        continue
                    chats_mod.leave(chat_id, resolved)
                    chats_left.append(chat_id)
                except Exception:
                    pass
    except Exception:
        pass

    # Delete session directory (atomic: rename to tmp then rmtree)
    import shutil
    import tempfile

    tmp_dir = session_dir.parent / f"_deleting_{resolved}"
    try:
        session_dir.rename(tmp_dir)
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except OSError:
        shutil.rmtree(session_dir, ignore_errors=True)

    _invalidate_list_sessions_cache()
    log.info(
        "session %s (%s) deleted (had_decisions=%s)", resolved, name, decision_count > 0
    )

    return {
        "deleted": True,
        "session_id": resolved,
        "name": name,
        "had_decisions": decision_count > 0,
        "archived_to": archived_to,
        "chats_left": chats_left,
        "chats_skipped_master": chats_skipped_master,
    }
