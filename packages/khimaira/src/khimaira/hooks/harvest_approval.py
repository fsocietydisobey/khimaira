#!/usr/bin/env python3
"""khimaira PostToolUse hook — harvest knowledge on task approval.

Fires when a session calls mcp__khimaira-chat__chat_task_update with
new_status="approved". Assembles the assignee's recent decisions + the
task's done-report note, then POSTs to mnemosyne/distill as curated harvest
input. Haiku (mnemosyne's built-in distiller) extracts Q&A pairs.

Storage layout:
  ~/.local/state/khimaira/chats/{chat_id}.jsonl  — chat JSONL (read directly)
  http://127.0.0.1:8740/api/sessions/{sid}        — session state (HTTP GET)
  http://127.0.0.1:8766/distill                   — mnemosyne (HTTP POST)

Fail-open: any failure → exit 0 silently. Never blocks Claude Code.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from khimaira.hooks.mnemosyne_client import distill as _mnemosyne_distill
from khimaira.hooks.session_end_utils import detect_domain, detect_project

_DAEMON_URL = "http://127.0.0.1:8740"
_DAEMON_TIMEOUT_S = 1

_XDG_STATE = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
)
_CHATS_DIR = _XDG_STATE / "khimaira" / "chats"


def _get_session_state(session_id: str) -> dict | None:
    """GET /api/sessions/{session_id}; return state dict or None on any failure."""
    try:
        req = urllib.request.Request(
            f"{_DAEMON_URL}/api/sessions/{session_id}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_DAEMON_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception:
        return None


def _read_task_record(chat_id: str, task_id: str) -> dict:
    """Scan chat JSONL directly for task metadata and done-report note.

    Returns dict with keys:
      assignee_id  (str | None)
      assignee_name (str | None)
      task_body    (str | None)
      done_note    (str | None)  — note from the most recent done transition
    """
    result: dict = {
        "assignee_id": None,
        "assignee_name": None,
        "task_body": None,
        "done_note": None,
    }

    chat_path = _CHATS_DIR / f"{chat_id}.jsonl"
    if not chat_path.is_file():
        return result

    try:
        with chat_path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    rec = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("kind")
                if kind == "task" and rec.get("id") == task_id:
                    result["assignee_id"] = rec.get("assignee_id")
                    result["assignee_name"] = rec.get("assignee_name")
                    result["task_body"] = rec.get("body")
                elif (
                    kind == "task_update"
                    and rec.get("task_id") == task_id
                    and rec.get("status") == "done"
                ):
                    # Keep the last done-transition note (there's typically only one)
                    note = rec.get("note")
                    if note:
                        result["done_note"] = note
    except Exception:
        pass

    return result


def _build_curated_text(
    task_id: str,
    task_body: str | None,
    decisions: list[dict],
    done_note: str | None,
) -> str:
    """Assemble curated harvest input for the mnemosyne distiller."""
    parts: list[str] = [f"[Approved task: {task_id}]"]

    if task_body:
        # Truncate very long task bodies; the distiller only needs context
        parts.append(f"\nTask: {task_body[:500]}")

    if decisions:
        parts.append("\nDecisions made during this task:")
        for d in decisions:
            text = (d.get("text") or "").strip()
            why = (d.get("why") or "").strip()
            if text:
                entry = f"- {text}"
                if why:
                    entry += f" (why: {why})"
                parts.append(entry)

    if done_note:
        parts.append(f"\nDone report: {done_note}")

    return "\n".join(parts)


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except Exception:
        return 0

    if not isinstance(data, dict):
        return 0

    tool_name = data.get("tool_name") or ""
    if tool_name != "mcp__khimaira-chat__chat_task_update":
        return 0

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return 0

    if tool_input.get("new_status") != "approved":
        return 0

    task_id = tool_input.get("task_id") or ""
    chat_id = tool_input.get("chat_id") or ""
    if not task_id or not chat_id:
        return 0

    cwd = data.get("cwd") or os.getcwd()

    # Read task record from chat JSONL (assignee + done-report)
    task_info = _read_task_record(chat_id, task_id)
    assignee_id = task_info.get("assignee_id") or ""
    assignee_name = task_info.get("assignee_name") or ""
    task_body = task_info.get("task_body") or ""
    done_note = task_info.get("done_note") or ""

    if not assignee_id:
        return 0

    # Fetch assignee's recent decisions from daemon
    decisions: list[dict] = []
    session_state = _get_session_state(assignee_id)
    if session_state:
        decisions = session_state.get("recent_decisions") or []
        if not assignee_name:
            assignee_name = (session_state.get("name") or "").strip() or assignee_id[:8]

    if not decisions and not done_note:
        return 0  # Nothing useful to distill

    curated_text = _build_curated_text(task_id, task_body, decisions, done_note)

    # Resolve project:domain key
    domain = detect_domain(assignee_name)
    try:
        project = detect_project(cwd)
        qualified_domain = (
            f"{project}:{domain}" if project and project != "unknown" else domain
        )
    except Exception:
        qualified_domain = domain

    _mnemosyne_distill(qualified_domain, curated_text, f"harvest-{task_id}")

    # Post-approval backlog drain: inject a reminder if pending tasks remain
    # in the same chat. Prevents master from going idle after task completion.
    # Outputs additionalContext via hookSpecificOutput so master sees it.
    backlog_reminder = _backlog_drain_reminder(chat_id, task_id)
    if backlog_reminder:
        import json as _json
        out = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": backlog_reminder,
            }
        }
        sys.stdout.write(_json.dumps(out))

    return 0


def _backlog_drain_reminder(chat_id: str, just_approved_task_id: str) -> str | None:
    """Check if there are pending tasks remaining in the chat.

    Returns an ACTION REQUIRED string if pending tasks exist, None otherwise.
    Fail-open: any error → None (never blocks the hook).
    """
    chat_path = _CHATS_DIR / f"{chat_id}.jsonl"
    if not chat_path.is_file():
        return None

    pending_tasks: dict[str, dict] = {}  # task_id → {body, assignee_name, status}
    try:
        with chat_path.open(encoding="utf-8") as fh:
            for raw_line in fh:
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    rec = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                kind = rec.get("kind")
                tid = rec.get("id") or rec.get("task_id")
                if kind == "task" and tid:
                    pending_tasks[tid] = {
                        "body": (rec.get("body") or "")[:120],
                        "assignee_name": rec.get("assignee_name") or "unassigned",
                        "status": "pending",
                    }
                elif kind == "task_update" and tid and tid in pending_tasks:
                    pending_tasks[tid]["status"] = rec.get("status") or "pending"
    except Exception:
        return None

    remaining = [
        t for tid, t in pending_tasks.items()
        if t["status"] in ("pending", "in_progress")
        and tid != just_approved_task_id
    ]

    if not remaining:
        return None

    lines = [
        f"📋 BACKLOG DRAIN — {len(remaining)} task(s) still pending after this approval. "
        "Queue the next item now without waiting for user input:\n"
    ]
    for t in remaining[:3]:  # cap at 3 to keep context bounded
        lines.append(f"  • [{t['status']}] {t['assignee_name']}: {t['body']}...")
    if len(remaining) > 3:
        lines.append(f"  • ...and {len(remaining) - 3} more")
    lines.append(
        "\nFire BEGIN on the highest-priority unblocked item or unblock its spec. "
        "Do NOT wait for Joseph to prompt you."
    )
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
