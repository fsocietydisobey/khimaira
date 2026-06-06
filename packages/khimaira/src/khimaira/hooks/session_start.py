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

from khimaira.hooks.mnemosyne_client import query as _mnemosyne_query
from khimaira.hooks.session_end_utils import detect_domain, detect_project

_STATE_ROOT = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
)
_BASE_DIR = _STATE_ROOT / "sessions"
_HANDOFFS_PATH = _STATE_ROOT / "handoffs.jsonl"
_ROLES_DIR = Path(__file__).parent.parent / "roles"

# Daemon HTTP base. Hook prefers HTTP (single source of truth = the
# daemon's code) and only falls back to file-direct ops when the daemon
# is unreachable. The fallback preserves the original safety net but
# the HTTP path eliminates the drift class where daemon semantics
# evolved while file-direct hook code lagged behind.
_ENDPOINT = os.environ.get("KHIMAIRA_ENDPOINT", "http://127.0.0.1:8740").rstrip("/")
_HTTP_TIMEOUT_S = 1.5


def _ensure_chat_mcp_registered() -> None:
    """Claude Code's MCP supervisor periodically prunes the khimaira-chat
    entry (subprocess errors during daemon restart trigger removal).
    Detect-and-restore: if `claude mcp list` doesn't show it, run
    `claude mcp add` silently. ~200ms total when entry is present
    (just one process spawn for the list check).
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=3.0,
        )
    except (subprocess.SubprocessError, FileNotFoundError, OSError):
        return  # claude CLI not on PATH, or list timed out — nothing we can do

    if "khimaira-chat" in (proc.stdout or ""):
        return  # already registered

    # Self-heal — match the registration the bootstrap profile uses.
    try:
        subprocess.run(
            [
                "claude",
                "mcp",
                "add",
                "khimaira-chat",
                "-s",
                "user",
                "--",
                "bash",
                "-lc",
                "uv --directory ~/dev/khimaira run khimaira-chat 2>>/tmp/khimaira-chat.log",
            ],
            capture_output=True,
            text=True,
            timeout=5.0,
        )
    except (subprocess.SubprocessError, OSError):
        pass


def _http_post_json(path: str, body: dict) -> dict | None:
    """POST JSON to <endpoint>/<path>; return parsed response or None.

    Quiet by design — hooks must never bubble errors to Claude Code.
    """
    url = f"{_ENDPOINT}{path}"
    try:
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _http_post_json_retry(
    path: str, body: dict, attempts: int = 3, base_delay: float = 0.4
) -> dict | None:
    """POST with bounded retry — for CRITICAL registration calls that must
    survive a transient daemon hiccup or a roster boot-storm.

    A roster launches N sessions at once; their concurrent registration POSTs
    can push a busy daemon past the tight _HTTP_TIMEOUT_S, and the single-attempt
    best-effort POST then silently drops the registration (the session boots fine
    but never appears in session_list — observed live: a 14-session launch
    registered only 2). The slot + register-pending endpoints return a dict on
    success, so None means genuine failure → retry with backoff. Happy path is
    one fast POST (no added latency); only failure pays the retry cost. Never
    raises — boot must not block.
    """
    import time

    for _attempt in range(attempts):
        resp = _http_post_json(path, body)
        if resp is not None:
            return resp
        if _attempt < attempts - 1:
            time.sleep(base_delay * (_attempt + 1))  # 0.4s, 0.8s
    return None


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


def _consume_inbox(session_id: str, cwd: str | None = None) -> list[dict]:
    """Read unread notes, mark them read, MOVE TO archive.jsonl, return the
    drained set.

    HTTP-primary: hits /api/sessions/{sid}/pending?mark_read=true which is
    the daemon's authoritative drain-and-archive implementation. Falls
    back to file-direct ops only if the daemon is unreachable. The file
    fallback preserves the safety net but is structurally identical to
    daemon code that has drifted twice in the last day (archive miss,
    claim miss). The drift class is eliminated on the happy path.

    `cwd`: when provided, notices scoped to a different project are excluded
    from the drained set. They remain unread in inbox.jsonl for the correct
    session to claim.
    """
    # --- HTTP path (preferred) ---
    qs = f"mark_read=true{('&cwd=' + urllib.parse.quote(cwd, safe='')) if cwd else ''}"
    payload = _http_get_json(
        f"/api/sessions/{urllib.parse.quote(session_id)}/pending?{qs}"
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
    # Bounded surface (same class as the handoff caps, 2026-06-06): a session
    # that was away while peers posted N long answers must not boot with all
    # of them injected in full. Newest 10 shown, each answer length-capped.
    # Overflow notes are already drained to archive.jsonl by _consume_inbox,
    # so the browse primitive for the rest is session_search_archive.
    _INBOX_CAP = 10
    _ANSWER_CHAR_CAP = 2000

    total = len(notes)
    shown = sorted(notes, key=lambda n: n.get("ts") or "", reverse=True)[:_INBOX_CAP]

    lines = [
        f"📬 khimaira inbox — {total} unread answer(s) from other sessions:",
        "",
    ]
    for n in shown:
        from_id = n.get("from_session_id") or "unknown"
        q = n.get("question_text") or "?"
        a = n.get("answer") or "?"
        if len(a) > _ANSWER_CHAR_CAP:
            a = a[:_ANSWER_CHAR_CAP] + " … (truncated; full text via session_search_archive)"
        lines.append(f"- (from {from_id})")
        lines.append(f"  Q: {q}")
        lines.append(f"  A: {a}")
        lines.append("")
    if total > _INBOX_CAP:
        lines.append(
            f"… +{total - _INBOX_CAP} more note(s) not shown (newest {_INBOX_CAP} above). "
            f"All notes are archived — browse via `session_search_archive`."
        )
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


# Phase B v1.6.1: role→budget recommendation table. Mirrors the ROLE_BUDGET
# constant in packages/khimaira/src/khimaira/monitor/chats.py (Phase B v1.5).
# Kept duplicated rather than imported because this hook must run in a
# stdlib-only subprocess that boots fast; pulling in the chats module would
# transitively pull all of monitor/* and slow SessionStart.
_ROLE_BUDGET: dict[str, dict[str, str]] = {
    "master": {"model": "opus", "effort": "max"},
    "agent": {"model": "sonnet", "effort": "medium"},
    "observer": {"model": "sonnet", "effort": "low"},
    "architect": {"model": "opus", "effort": "max"},
    "intake": {"model": "sonnet", "effort": "medium"},
    "analyst": {"model": "opus", "effort": "max"},
    "verifier": {"model": "sonnet", "effort": "medium"},
    "tracker": {"model": "sonnet", "effort": "medium"},
    # critic intentionally absent — no default
    # Domain leads: sonnet/medium (mirrors ROLE_BUDGET in chats.py — kept duplicated
    # here because session_start must run in a stdlib-only subprocess). Entries must
    # match the lead roles in the themis rule registry.
    "backend-lead": {"model": "sonnet", "effort": "medium"},
    "data-lead": {"model": "sonnet", "effort": "medium"},
    "jp-backend-lead": {"model": "sonnet", "effort": "medium"},
    "jp-data-lead": {"model": "sonnet", "effort": "medium"},
    "jp-frontend-lead": {"model": "sonnet", "effort": "medium"},
}


def _discover_chat_roles(session_id: str) -> list[dict]:
    """Phase B v1.6.1: return role + recommended budget per chat this session
    is an accepted member of. Closes the v1.5-directive-only-fires-on-change
    gap — v1.5's role-grant directive surfaces state CHANGES; this surfaces
    CURRENT STATE on session boot so a fresh window joining an existing chat
    sees its role + recommended budget without having to query.

    File-scan primary: walks ~/.local/state/khimaira/chats/*.jsonl directly
    because the daemon's /api/chats list endpoint returns a light shape
    (no member_roles, no created_by) which is insufficient for role
    resolution. The JSONL META records are the authoritative source.
    Per-chat HTTP fetch (a viable alternative) adds N round-trips per
    session boot; file scan is one syscall per chat. v1.6.1.1 fix.

    Returns list of dicts: chat_id, title, role, budget (or None for critic),
    annotation (v1.6 deputize state if applicable).
    """
    chats_dir = _STATE_ROOT / "chats"
    if not chats_dir.exists():
        return []
    results = []
    for path in sorted(chats_dir.glob("*.jsonl")):
        records = _read_jsonl(path)
        if not records:
            continue
        # Most recent meta record carries member_roles / created_by / deputize marker
        last_meta = None
        for r in reversed(records):
            if r.get("kind") == "meta":
                last_meta = r
                break
        if not last_meta:
            continue
        member_roles = last_meta.get("member_roles") or {}
        role = member_roles.get(session_id)
        if not role:
            if last_meta.get("created_by") == session_id:
                role = "master"
            else:
                # Not in explicit member_roles and not creator — walk member
                # records; fall back to "agent" for accepted invites (v1.6.1.2)
                for r in reversed(records):
                    if r.get("kind") == "member" and r.get("session_id") == session_id:
                        if r.get("state") == "accepted":
                            role = "agent"
                        break
                if not role:
                    continue  # genuinely not a member
        # Verify accepted membership by walking member records — pick the
        # latest member record for this session_id; skip if not accepted.
        latest_state = None
        for r in reversed(records):
            if r.get("kind") == "member" and r.get("session_id") == session_id:
                latest_state = r.get("state")
                break
        if latest_state and latest_state != "accepted":
            continue
        annotation = ""
        depy = last_meta.get("deputized_original_master")
        if depy:
            if depy == session_id:
                annotation = " (paused — vice active)"
            elif role == "master":
                annotation = f" (vice — original: {depy[:8]})"
        results.append(
            {
                "chat_id": last_meta.get("chat_id") or path.stem,
                "title": (last_meta.get("title") or "")[:50],
                "role": role,
                "annotation": annotation,
                "budget": _ROLE_BUDGET.get(role),
            }
        )
    return results


def _format_chat_roles(roles: list[dict]) -> str:
    """Phase B v1.6.1: render role-budget reminder block."""
    if not roles:
        return ""
    lines = [
        f"🎚️ khimaira chat roles + recommended budgets ({len(roles)} chat(s)):",
        "",
    ]
    for r in roles:
        chat_id_short = (r.get("chat_id") or "")[:18]
        title = r.get("title") or ""
        role = r.get("role") or "?"
        annotation = r.get("annotation") or ""
        budget = r.get("budget")
        if budget:
            budget_str = f"/model {budget['model']}, /effort {budget['effort']}"
        else:
            budget_str = "(no default — orchestrator's discretion)"
        title_part = f' "{title}"' if title else ""
        lines.append(
            f"  {chat_id_short}{title_part} — {role}{annotation} → {budget_str}"
        )
    lines.append("")
    lines.append(
        "Type the budget commands in this window if you want to match the "
        "recommended tier. Reference: "
        "docs/khimaira-chat.md#token-cost-budgeting"
    )
    return "\n".join(lines)


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
    # mark_read=false: surface without consuming — handoff re-surfaces on
    # every SessionStart until the agent explicitly acks it. This prevents
    # session-compaction from silently losing the handoff body (the premature-
    # mark-read bug where a compacted session could never recover a handoff
    # it had "consumed" but not yet acted on).
    cwd_q = urllib.parse.quote(cwd, safe="")
    sid_q = urllib.parse.quote(session_id, safe="")
    payload = _http_get_json(
        f"/api/handoffs/consume?session_id={sid_q}&cwd={cwd_q}&mark_read=false"
    )
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
            modified = True  # persist the ownership claim
        else:
            h["_claim_role"] = "observer"
            h["_owner_session_id"] = existing_owner

        matched.append(h)
        # Don't mark read — handoff re-surfaces on every SessionStart until
        # the agent explicitly acks. Mirrors the HTTP path's mark_read=false.

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


def _fetch_hook_safe_tasks() -> list[dict]:
    """Fetch tasks from every enabled task source that's hook-safe.

    Hook-safe = can be called from a stdlib-only subprocess with no
    MCP / network. The JSONL adapter (default) qualifies; Linear /
    GitHub adapters would not (they need daemon-side dispatch — see
    `tasks/task-sources/IMPLEMENTATION.md`).

    Returns a list of dicts (Task.__dict__-shape) so this stays
    JSON-renderable without pulling in dataclass-asdict machinery.
    Never raises — a broken source returns [] cleanly via the
    adapter's contract.
    """
    try:
        # Lazy import: avoid the cost on every SessionStart if the user
        # has no task sources configured. khimaira package is importable
        # from the hook (see install_hooks.py), so this is fine.
        import asyncio

        from khimaira.task_sources.config import fetch_all_open_tasks

        tasks = asyncio.run(fetch_all_open_tasks(hook_safe_only=True))
    except Exception:  # noqa: BLE001 — hook must not break SessionStart
        return []

    return [
        {
            "id": t.id,
            "title": t.title,
            "state": t.state,
            "source": t.source,
            "project": t.project,
            "url": t.url,
        }
        for t in tasks
    ]


def _format_tasks(tasks: list[dict]) -> str:
    """Render the task list as the SessionStart task block.

    Format mirrors the handoff / inbox blocks — bullet list with state
    + title, source noted if non-trivial.
    """
    if not tasks:
        return ""
    # Sort: by source first (group source-wise), then by state
    # (in-progress / in-review surface above todo).
    state_order = {
        "in progress": 0,
        "in-progress": 0,
        "in review": 1,
        "in-review": 1,
        "todo": 2,
    }

    def _sort_key(t: dict) -> tuple:
        state = (t.get("state") or "").lower()
        return (t.get("source", ""), state_order.get(state, 9), t.get("id", ""))

    sorted_tasks = sorted(tasks, key=_sort_key)
    # Bounded surface (same class as the handoff/inbox caps, 2026-06-06):
    # in-progress/in-review sort first, so the cap drops the stalest todos.
    _TASKS_CAP = 15
    total = len(sorted_tasks)
    shown = sorted_tasks[:_TASKS_CAP]

    lines = [f"📋 khimaira tasks — {total} open assignment(s):", ""]
    for t in shown:
        state = (t.get("state") or "").strip()
        state_label = f" ({state})" if state else ""
        source_label = f" [{t['source']}]" if t.get("source") else ""
        line = f"  • {t['id']}{state_label} — {t['title']}{source_label}"
        if len(line) > 110:
            line = line[:107] + "..."
        lines.append(line)
    if total > _TASKS_CAP:
        lines.append(
            f"  … +{total - _TASKS_CAP} more (browse via `chat_task_status` "
            f"or the daemon API)."
        )
    return "\n".join(lines)


def _format_handoffs(handoffs: list[dict], cwd: str) -> str:
    # Split by role assigned during consume: this session may have
    # auto-claimed ownership of fresh handoffs OR be an observer on
    # handoffs already claimed by another session.
    # Defensive: any handoff without _claim_role defaults to "owner"
    # framing (it'd be silently dropped otherwise — the bug from
    # 2026-05-11 where pre-claim-logic handoffs surfaced as empty).
    owned = [h for h in handoffs if h.get("_claim_role", "owner") == "owner"]
    observed = [h for h in handoffs if h.get("_claim_role") == "observer"]

    # Hard caps — the surfaces must stay bounded no matter how the data layer
    # misbehaves (observed 2026-06-06: 412 stale guard handoffs → 118KB block,
    # ~30k tokens injected into every boot in the project). Newest first; the
    # overflow is summarized in one line with the browse primitive named.
    _OWNED_CAP = 10
    _OBSERVED_CAP = 5

    def _newest_first(items: list[dict]) -> list[dict]:
        return sorted(items, key=lambda h: h.get("ts") or "", reverse=True)

    owned_total, observed_total = len(owned), len(observed)
    owned = _newest_first(owned)[:_OWNED_CAP]
    observed = _newest_first(observed)[:_OBSERVED_CAP]

    lines: list[str] = []

    # --- OWNED handoffs — full directive framing ---
    if owned:
        lines.append(
            f"📦 khimaira handoffs — {owned_total} directive(s) you now OWN in this project ({cwd}):"
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
        if owned_total > _OWNED_CAP:
            lines.append("")
            lines.append(
                f"… +{owned_total - _OWNED_CAP} more owned handoff(s) not shown "
                f"(newest {_OWNED_CAP} above). Browse the rest via "
                f"`session_consume_handoffs` or the daemon API."
            )

    # --- OBSERVED handoffs — owner already exists ---
    if observed:
        if owned:
            lines.append("")
            lines.append("---")
            lines.append("")
        lines.append(
            f"👀 khimaira handoffs — {observed_total} ALREADY-CLAIMED handoff(s) "
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
            lines.append(f"  {text}")
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
        if observed_total > _OBSERVED_CAP:
            lines.append("")
            lines.append(
                f"… +{observed_total - _OBSERVED_CAP} more already-claimed "
                f"handoff(s) not shown (newest {_OBSERVED_CAP} above)."
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


def _inject_domain_memory(session_id: str, cwd: str) -> str:
    """Return a PROVISIONAL domain-memory block for lead sessions, or '' on any failure.

    Fetches the session name from the daemon; returns '' immediately if the session
    is not a lead session or if mnemosyne is down.

    Fail-open: any exception returns '' and never blocks session boot.
    """
    try:
        session_info = _http_get_json(f"/api/sessions/{urllib.parse.quote(session_id)}")
        session_name = ((session_info or {}).get("name") or "").strip()
        if not session_name:
            return ""

        lower = session_name.lower()
        if "-lead" not in lower:
            return ""
        domain = detect_domain(session_name)
        if domain == "general":
            return ""

        project = detect_project(cwd)
        qualified = (
            f"{project}:{domain}" if project and project != "unknown" else domain
        )

        result = _mnemosyne_query(qualified)
        if not result:
            return ""
        answer = result.get("answer") or ""
        if not answer:
            return ""
        count = result.get("training_pairs_available") or 0

        return (
            f"🧠 PROVISIONAL domain memory — {qualified} ({count} pair(s) accumulated)\n"
            "⚠️  Auto-distilled from prior sessions — unreviewed. "
            "Curated knowledge doc (if any) is authoritative.\n\n" + answer
        )
    except Exception:
        return ""


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

    # Bridge to chat MCP subprocess: post {ppid, session_id} so the
    # subprocess (same parent ppid as this hook) can self-register on
    # startup without waiting for the agent's first chat tool call.
    # Best-effort — if daemon is down or the chat MCP isn't registered,
    # the agent still has the explicit session_id in the context block
    # below and can register lazily. Failure here must NEVER fail the
    # hook (would block session boot).
    #
    # Order matters: this POST runs BEFORE _ensure_chat_mcp_registered
    # because the latter calls `claude mcp list` (with health checks
    # that can take seconds). The chat MCP subprocess starts in
    # parallel and retries the lookup for ~3s; we need the POST to
    # land within that window or the bridge falls back to lazy.
    try:
        _http_post_json_retry(
            "/api/chats/register-pending-session",
            {"ppid": os.getppid(), "session_id": session_id},
        )
    except Exception:
        pass

    # v1.3 self-heal: Claude Code intermittently prunes the
    # khimaira-chat MCP entry from ~/.claude.json (subprocess crash
    # during daemon restart, supervisor health-check, or some other
    # unknown trigger). Manual `khimaira sync` is the workaround but
    # it's friction. Auto-detect-and-restore here so each fresh
    # `claude-chat` launch self-heals: if `claude mcp list` doesn't
    # show khimaira-chat, run the registration command silently.
    # Best-effort, must never block session boot. Runs AFTER the ppid
    # POST so the slow `claude mcp list` doesn't delay the bridge.
    try:
        _ensure_chat_mcp_registered()
    except Exception:
        pass

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

    # roster-identity slot-binding (Phase-B Part E wiring — Resolution A).
    # POST /sessions/{id}/slot BEFORE the SSE-open (chat_my_chats below) so the
    # subscriber key resolves to the slot at subscribe-time → structurally
    # avoids the open-before-bind race; makes (i) slot-key-at-subscribe durable.
    # Fail-open: never block session boot; a missing env / TRAP-2 mismatch is
    # logged by the daemon but doesn't prevent the session from starting.
    _roster_slot = os.environ.get("KHIMAIRA_ROSTER_SLOT", "").strip()
    _kitty_wid = os.environ.get("KITTY_WINDOW_ID", "").strip()
    if _roster_slot and _kitty_wid:
        # Retry — a transient daemon hiccup or boot-storm timeout must NOT silently
        # un-slot the session (that recreates the exact silent-non-binding this code
        # exists to prevent). _http_post_json returns None on failure (never raises),
        # so the old bare `except` here was DEAD CODE: check the return value and WARN
        # only on a genuine post-retry failure.
        _slot_resp = None
        try:
            _slot_resp = _http_post_json_retry(
                f"/api/sessions/{session_id}/slot",
                {"slot": _roster_slot, "window_id": int(_kitty_wid)},
            )
        except Exception:
            _slot_resp = None
        if _slot_resp is None:
            print(
                f"[khimaira-hook] WARN: slot-bind POST failed (after retries) for "
                f"{session_id[:8]} slot={_roster_slot!r} — session un-slotted "
                "(drift-heal will not fire)",
                file=sys.stderr,
            )

    # Roster auto-accept (closes the last-launched-session pending tail, 2026-06-06).
    # A worker pre-registers its MASTER in its auto-accept allowlist at boot — BEFORE the
    # master bootstraps the roster chat. Then chats.create_room's existing auto-accept
    # (should_auto_accept) admits the worker SERVER-SIDE at room creation, with no
    # dependency on the SSE invite block reaching the worker or the worker being awake to
    # accept it. Without this, the last-launched sessions (observer/tracker) whose SSE
    # subscriber wasn't live when create_room ran stay `pending` forever.
    #
    # We WRITE the durable by-name allowlist file directly rather than POSTing the daemon
    # endpoint: at SessionStart the worker has no transcript yet, so it isn't resolvable —
    # the endpoint's _resolve_or_uuid would 404. The file is keyed by the worker's roster
    # name and read at create_room time, when the worker IS named/registered. This mirrors
    # exactly what chats.set_auto_accept writes (auto-accept-by-name-<name>.json). Workers
    # only — the master's own name == KHIMAIRA_ROSTER_MASTER, so it skips itself.
    _roster_master = os.environ.get("KHIMAIRA_ROSTER_MASTER", "").strip()
    _slot_name = _roster_slot.split(":")[-1] if ":" in _roster_slot else ""
    if _roster_master and _slot_name and _slot_name != _roster_master:
        try:
            import datetime as _dt

            _state = os.environ.get("XDG_STATE_HOME") or os.path.expanduser(
                "~/.local/state"
            )
            _aa_dir = os.path.join(_state, "khimaira", "chats")
            os.makedirs(_aa_dir, exist_ok=True)
            _aa_path = os.path.join(
                _aa_dir, f"auto-accept-by-name-{_slot_name}.json"
            )
            with open(_aa_path, "w", encoding="utf-8") as _f:
                json.dump(
                    {
                        "allow": [_roster_master],
                        "updated_at": _dt.datetime.now(
                            _dt.timezone.utc
                        ).isoformat(),
                    },
                    _f,
                )
        except Exception:
            pass  # fail-open — a miss just falls back to the prior SSE-invite accept path

    # Real-time chat registration — emitted immediately after identity block so
    # it appears at the very top before inbox, handoffs, and role file content.
    # Without this call the SSE subscriber never starts and chat_send messages
    # queue until the next user-prompted turn. The soft nudge at line ~820 was
    # being skipped in compacted-session resumptions; moving it to the top and
    # making it ACTION REQUIRED makes it impossible to miss.
    # Tool-not-found errors (chat MCP not registered) are silently ignored by
    # the agent, so emitting unconditionally is safe.
    blocks.append(
        "⚡ ACTION REQUIRED — real-time chat registration:\n"
        f'Invoke the MCP TOOL `mcp__khimaira-chat__chat_my_chats(session_id="{session_id}")` '
        "(a TOOL CALL — NOT a slash-command or skill; do NOT use the Skill tool) "
        "NOW, before anything else. This starts the SSE subscriber so "
        '`<channel source="khimaira-chat">` messages arrive in real time. '
        "Without it you will NOT receive chat messages as they arrive — only "
        "on your next prompted turn. One call per session; do it first."
    )

    # Optional context blocks — each needs a daemon HTTP call. Under a boot-storm the
    # daemon can be slow/erroring; an UNCAUGHT raise here previously took down the ENTIRE
    # hook output (the entry point swallows the exception → 0 bytes written), losing the
    # critical 🆔 + chat-registration blocks above. Agents then couldn't self-identify →
    # they mis-registered as another session (corrupting it) or stalled. Make every
    # optional block independently fail-safe so the identity blocks are ALWAYS emitted.
    cwd = data.get("cwd") or os.getcwd()

    def _safe(fn, *a, **k):
        try:
            return fn(*a, **k)
        except Exception:
            return None

    notes = _safe(_consume_inbox, session_id, cwd=cwd or None)
    others = _safe(_discover_other_active_sessions, session_id, within_minutes=30)
    chat_roles = _safe(_discover_chat_roles, session_id)

    # Role-gate the heavy coordination blocks. Handoffs are CONTEXT FOR THE
    # COORDINATOR — the worker role docs explicitly say "do NOT act on any
    # surfaced handoff" — so injecting them into worker boots is pure context
    # bloat (observed 2026-06-06: 118KB of stale guard handoffs × 14 sessions
    # per roster launch). Worse, _consume_handoffs AUTO-CLAIMS: a worker
    # booting before master could steal ownership of a coordinator-bound
    # handoff. Gating the CONSUME call (not just the formatting) closes both.
    # Master + role-less (solo) sessions keep the full surface; every other
    # roster role boots with identity + role file + budgets only.
    _primary_role = (chat_roles[0].get("role") or "").strip() if chat_roles else ""
    _is_roster_worker = bool(_primary_role) and _primary_role != "master"

    handoffs = None if _is_roster_worker else _safe(_consume_handoffs, session_id, cwd)
    tasks = None if _is_roster_worker else _safe(_fetch_hook_safe_tasks)

    try:
        if notes:
            blocks.append(_format_inbox(notes))
        if handoffs:
            blocks.append(_format_handoffs(handoffs, cwd))
        if tasks:
            blocks.append(_format_tasks(tasks))
        if chat_roles:
            primary_role = chat_roles[0].get("role")
            if primary_role:
                role_path = _ROLES_DIR / f"{primary_role}.md"
                if role_path.exists():
                    try:
                        role_contents = role_path.read_text(encoding="utf-8")
                        blocks.append(f"📖 ROLE FILE — {primary_role}\n{role_contents}")
                    except OSError:
                        pass
            blocks.append(_format_chat_roles(chat_roles))
        domain_memory = _safe(_inject_domain_memory, session_id, cwd)
        if domain_memory:
            blocks.append(domain_memory)
        if others:
            blocks.append(_format_active_sessions(others))
    except Exception:
        pass  # never lose the identity blocks already in `blocks`

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
