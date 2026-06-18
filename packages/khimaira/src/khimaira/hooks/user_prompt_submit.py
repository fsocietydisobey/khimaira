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
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_REMINDER_EVERY = int(os.environ.get("KHIMAIRA_HOOK_REMINDER_EVERY", "8"))
_ENDPOINT = os.environ.get("KHIMAIRA_ENDPOINT", "http://127.0.0.1:8740").rstrip("/")
_INBOX_TIMEOUT_S = 0.8

# Compiled once at import time — used by _channel_event_response_level.
_SYSREM_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_CHANNEL_ONLY_RE = re.compile(
    r'^\s*(?:<channel\s[^>]*source="khimaira-chat"[^>]*>.*?</channel>\s*)+\s*$',
    re.DOTALL,
)
_CHANNEL_TAG_RE = re.compile(r"<channel\s([^>]*)>", re.DOTALL)
_ATTR_RE = re.compile(r'(\w+)="([^"]*)"')

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


def _fetch_pending_notes(session_id: str, cwd: str | None = None) -> list[dict]:
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

    `cwd`: when provided, notices scoped to a different project are withheld.
    """
    url = f"{_ENDPOINT}/api/sessions/{session_id}/inbox/surface"
    if cwd:
        url += f"?cwd={urllib.parse.quote(cwd, safe='')}"
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
                title = rec.get("title") or rec.get("customTitle") or rec.get("name") or ""
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
        # 'answer' notes have body in `answer`; 'notice' in `text`;
        # 'scheduled-task' (daemon-side scheduler) in `prompt`.
        body = (n.get("answer") or n.get("text") or n.get("prompt") or "").strip()
        if kind == "scheduled-task":
            task_id = n.get("task_id", "?")
            body = f"🕒 scheduled task `{task_id}` fired — run this prompt:\n\n{body}"
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
            f'"{q.get("from_session_id")}", question_id="{qid}", answer="...")`'
        )
    lines.append("(re-surfaces every turn until answered; address or withdraw to clear)")
    return "\n".join(lines)


def _discover_pending_assignments(session_id: str) -> list[dict]:
    """Walk ~/.local/state/khimaira/chats/*.jsonl and return task assignments
    targeted at session_id that have not yet been acked.

    An assignment is a kind=msg record whose body starts with
    '🔔 TASK ASSIGNMENT' and whose `to` list includes session_id. It's
    acked when a later record in the same chat has sender_id==session_id and
    body containing '✅ ready [task-id: <task_id>]'.

    Returns list of dicts sorted newest-first:
        {chat_id, chat_title, task_id, task_body, required_model,
         required_effort, sender_name, ts}

    Errors are silent — hook must never break on bad JSONL or missing dirs.
    """
    state_root = (
        Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))) / "khimaira"
    )
    chats_dir = state_root / "chats"
    if not chats_dir.exists():
        return []

    results: list[dict] = []

    for path in sorted(chats_dir.glob("*.jsonl")):
        try:
            records: list[dict] = []
            try:
                with path.open("r", encoding="utf-8") as f:
                    for raw_line in f:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            records.append(json.loads(raw_line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

            if not records:
                continue

            # Title from the latest meta record.
            chat_id = path.stem
            chat_title = ""
            for r in reversed(records):
                if r.get("kind") == "meta":
                    chat_id = r.get("chat_id") or path.stem
                    chat_title = (r.get("title") or "")[:50]
                    break

            # First pass: collect assignments, acks, and task statuses.
            assignments: list[tuple[int, dict]] = []
            acks: dict[str, int] = {}  # task_id → index of latest ack msg
            task_statuses: dict[str, str] = {}  # task_id → current status

            for i, r in enumerate(records):
                k = r.get("kind")

                if k == "task":
                    tid = r.get("id")
                    if tid:
                        task_statuses[tid] = r.get("status") or "pending"

                elif k == "task_update":
                    tid = r.get("task_id")
                    if tid and r.get("status"):
                        task_statuses[tid] = r["status"]

                elif k == "msg":
                    body = r.get("body") or ""

                    if body.startswith("🔔 TASK ASSIGNMENT"):
                        to_list = r.get("to")
                        if isinstance(to_list, list) and session_id in to_list:
                            assignments.append((i, r))

                    if r.get("sender_id") == session_id and "✅ ready [task-id:" in body:
                        m = re.search(r"task-id:\s*(task-[a-f0-9]+)", body)
                        if m:
                            tid = m.group(1)
                            if tid not in acks or i > acks[tid]:
                                acks[tid] = i

            # Second pass: emit only unacked, non-terminal assignments.
            for idx, r in assignments:
                body = r.get("body") or ""

                m = re.search(r"task-id:\s*(task-[a-f0-9]+)", body)
                if not m:
                    continue
                task_id = m.group(1)

                # Skip if already done or approved via task lifecycle.
                if task_statuses.get(task_id) in ("done", "approved"):
                    continue

                # Skip if a later ack exists for this task.
                ack_idx = acks.get(task_id)
                if ack_idx is not None and ack_idx > idx:
                    continue

                # Parse required_model + required_effort — only from the
                # "Required budget" section to avoid matching prose references
                # (e.g. "model/effort settings" in the body free-text).
                required_model: str | None = None
                required_effort: str | None = None
                in_budget = False
                for line in body.splitlines():
                    if "Required budget" in line:
                        in_budget = True
                        continue
                    if not in_budget:
                        continue
                    if not line.strip():
                        break  # blank line ends the budget block
                    if required_model is None:
                        mm = re.search(r"/model\s+(\w+)", line)
                        if mm:
                            required_model = mm.group(1)
                    if required_effort is None:
                        em = re.search(r"/effort\s+(\w+)", line)
                        if em:
                            required_effort = em.group(1)
                    if required_model and required_effort:
                        break

                # task_body: text after "Task: " on that line; if the suffix
                # is empty (Task: on its own line), fall through to the next
                # non-empty line — handles both inline and continuation forms.
                task_body = ""
                want_next = False
                for line in body.splitlines():
                    if line.startswith("Task: "):
                        after = line[len("Task: ") :].strip()
                        if after:
                            task_body = after
                            break
                        want_next = True
                        continue
                    if want_next and line.strip():
                        task_body = line.strip()
                        break

                results.append(
                    {
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "task_id": task_id,
                        "task_body": task_body,
                        "required_model": required_model,
                        "required_effort": required_effort,
                        "sender_name": r.get("sender_name") or (r.get("sender_id") or "")[:8],
                        "ts": r.get("ts") or "",
                    }
                )
        except Exception:  # noqa: BLE001 — hook must never break
            continue

    results.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return results


def _format_pending_assignments(assignments: list[dict]) -> str:
    """Render pending (unacked) task assignments as a context block.

    Mirrors the style of _format_chat_roles in session_start — bullet
    list with task ID, truncated body, required budget, and a call to
    action. Returns "" when assignments is empty so the caller can
    test truthiness before including the block.
    """
    if not assignments:
        return ""
    lines = [
        f"⏳ KHIMAIRA PENDING ASSIGNMENT(S) — {len(assignments)} unacked task(s) for this session:",
        "",
    ]
    for a in assignments:
        task_id = a.get("task_id") or "?"
        task_body = (a.get("task_body") or "").strip()
        if len(task_body) > 80:
            task_body = task_body[:80] + "..."
        required_model = a.get("required_model")
        required_effort = a.get("required_effort")
        sender_name = a.get("sender_name") or "?"
        chat_id = (a.get("chat_id") or "")[:18]
        lines.append(f"  [{task_id}] {task_body}")
        if required_model or required_effort:
            model_str = f"/model {required_model}" if required_model else ""
            effort_str = f"/effort {required_effort}" if required_effort else ""
            budget_parts = " ".join(p for p in (model_str, effort_str) if p)
            lines.append(f"  Required: {budget_parts}")
        lines.append(f"  From: {sender_name} ({chat_id})")
        lines.append("")
    lines += [
        "⚠️ ENFORCEMENT GATE ACTIVE while pending — suppress default reflexes:",
        "  - DO NOT pre-read files (settings.json, project files) — verification happens AT ready, not before",
        "  - DO NOT pre-plan or gather reconnaissance state",
        '  - Set /model + /effort, then run /agent-ready (auto-fills task-id) OR type "ready [task-id: <hex>]" manually',
        "  - Hold silently until master fires the 🟢 begin signal",
    ]
    return "\n".join(lines).rstrip()


def _discover_unfired_acks(session_id: str) -> list[dict]:
    """Walk ~/.local/state/khimaira/chats/*.jsonl and return tasks created BY
    this session that haven't been acked yet (no in_progress or later update).

    Finds kind=task records where sender_id == session_id, then checks for
    any task_update that moves the task beyond pending. Returns only those
    still in pending state with no ack fired.

    Returns list of dicts sorted newest-first:
        {task_id, chat_id, assignee_name, body_snippet, created_ts}

    Errors are silent — hook must never break.
    """
    state_root = (
        Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))) / "khimaira"
    )
    chats_dir = state_root / "chats"
    if not chats_dir.exists():
        return []

    results: list[dict] = []

    for path in sorted(chats_dir.glob("*.jsonl")):
        try:
            records: list[dict] = []
            try:
                with path.open("r", encoding="utf-8") as f:
                    for raw_line in f:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            records.append(json.loads(raw_line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

            if not records:
                continue

            chat_id = path.stem
            for r in reversed(records):
                if r.get("kind") == "meta":
                    chat_id = r.get("chat_id") or path.stem
                    break

            # Collect tasks sent by this session and track their latest statuses.
            tasks_by_id: dict[str, dict] = {}
            task_statuses: dict[str, str] = {}

            for r in records:
                k = r.get("kind")
                if k == "task":
                    tid = r.get("id")
                    if tid and r.get("sender_id") == session_id:
                        tasks_by_id[tid] = r
                        task_statuses.setdefault(tid, r.get("status") or "pending")
                elif k == "task_update":
                    tid = r.get("task_id")
                    if tid and r.get("status"):
                        task_statuses[tid] = r["status"]

            for tid, task_rec in tasks_by_id.items():
                if task_statuses.get(tid, "pending") != "pending":
                    continue
                body = (task_rec.get("body") or "").strip()
                results.append(
                    {
                        "task_id": tid,
                        "chat_id": chat_id,
                        "assignee_name": task_rec.get("assignee_name") or "?",
                        "body_snippet": body[:60],
                        "created_ts": task_rec.get("ts") or "",
                    }
                )
        except Exception:  # noqa: BLE001 — hook must never break
            continue

    results.sort(key=lambda x: x.get("created_ts") or "", reverse=True)
    return results


def _format_unfired_acks(tasks: list[dict]) -> str:
    """Render tasks awaiting agent ack as a master-facing context block.

    Returns "" when tasks is empty so the caller can test truthiness.
    """
    if not tasks:
        return ""
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    lines = [f"📊 ASSIGNMENTS AWAITING ACK — {len(tasks)} agent(s) haven't started yet"]
    for t in tasks:
        assignee = t.get("assignee_name") or "?"
        tid = t.get("task_id") or "?"
        snippet = t.get("body_snippet") or ""
        created_ts = t.get("created_ts") or ""
        age_str = ""
        if created_ts:
            try:
                ts = created_ts
                if ts.endswith("Z"):
                    ts = ts[:-1] + "+00:00"
                dt = datetime.fromisoformat(ts)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                age_secs = int((now - dt).total_seconds())
                if age_secs < 60:
                    age_str = f"{age_secs}s ago"
                elif age_secs < 3600:
                    age_str = f"{age_secs // 60}m ago"
                else:
                    age_str = f"{age_secs // 3600}h ago"
            except (ValueError, TypeError):
                pass
        age_part = f" (assigned {age_str})" if age_str else ""
        ellipsis = "..." if len(snippet) == 60 else ""
        lines.append(f'  • {assignee} [{tid}]: "{snippet}{ellipsis}"{age_part}')
    return "\n".join(lines)


def _discover_begun_not_started(session_id: str) -> list[dict]:
    """Walk chat JSONLs and return tasks where BEGIN has been fired but
    this session hasn't self-transitioned to in_progress yet.

    A task is "begun-not-started" when ALL of:
      (a) this session is the assignee (task.assignee_id == session_id)
      (b) a kind=task_signal with signal="start" exists for that task_id
      (c) the task's latest status is still "pending" (no in_progress update)

    Returns newest-first list of dicts:
        {chat_id, task_id, task_body, signal_ts}

    Errors are silent — hook must never break.
    """
    state_root = (
        Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))) / "khimaira"
    )
    chats_dir = state_root / "chats"
    if not chats_dir.exists():
        return []

    results: list[dict] = []

    for path in sorted(chats_dir.glob("*.jsonl")):
        try:
            records: list[dict] = []
            try:
                with path.open("r", encoding="utf-8") as f:
                    for raw_line in f:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            records.append(json.loads(raw_line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

            if not records:
                continue

            chat_id = path.stem
            for r in reversed(records):
                if r.get("kind") == "meta":
                    chat_id = r.get("chat_id") or path.stem
                    break

            # Collect tasks assigned to this session, their statuses, and
            # whether a task_signal start has been fired for each.
            tasks: dict[str, dict] = {}        # task_id → task record
            task_statuses: dict[str, str] = {} # task_id → latest status
            begin_signals: dict[str, str] = {} # task_id → signal_ts

            for r in records:
                k = r.get("kind")
                if k == "task":
                    tid = r.get("id")
                    if tid and r.get("assignee_id") == session_id:
                        tasks[tid] = r
                        task_statuses.setdefault(tid, r.get("status") or "pending")
                elif k == "task_update":
                    tid = r.get("task_id")
                    if tid and r.get("status"):
                        task_statuses[tid] = r["status"]
                elif k == "task_signal":
                    tid = r.get("task_id")
                    if tid and r.get("signal") == "start":
                        begin_signals[tid] = r.get("ts") or ""

            for tid, task_rec in tasks.items():
                if tid not in begin_signals:
                    continue  # BEGIN not yet fired
                if task_statuses.get(tid, "pending") != "pending":
                    continue  # already in_progress or beyond
                body = (task_rec.get("body") or "")[:80].strip()
                results.append({
                    "chat_id": chat_id,
                    "task_id": tid,
                    "task_body": body,
                    "signal_ts": begin_signals[tid],
                })
        except Exception:  # noqa: BLE001 — hook must never break
            continue

    results.sort(key=lambda x: x.get("signal_ts") or "", reverse=True)
    return results


def _format_begun_not_started(tasks: list[dict]) -> str:
    """Render BEGUN-but-not-in_progress tasks as an agent-facing banner.

    Returns "" when tasks is empty.
    """
    if not tasks:
        return ""
    lines = [
        f"🟢 START NOW — BEGIN received for {len(tasks)} task(s) — mark in_progress to begin:",
        "",
    ]
    for t in tasks:
        tid = t.get("task_id") or "?"
        body = t.get("task_body") or ""
        ellipsis = "..." if len(body) >= 80 else ""
        lines.append(f'  • [{tid}]: "{body}{ellipsis}"')
        lines.append(f'    → chat_task_update(task_id="{tid}", new_status="in_progress") then begin work')
        lines.append("")
    lines += [
        "This banner re-surfaces every turn until you self-transition.",
        "(If you believe this is stale, check chat_history for the task's current status.)",
    ]
    return "\n".join(lines).rstrip()


def _check_stale_acks(session_id: str) -> list[dict]:
    """Detect acked /khimaira-assign tasks whose budget has become stale.

    Session-restart resets /model to the Opus default — model is not
    persisted in settings.json. If an agent acked a task with model=sonnet
    and the session restarts, the ack is stale: the agent is no longer
    running at the promised budget.

    Walk ~/.local/state/khimaira/chats/*.jsonl. For each ack from this
    session matching ``✅ ready [task-id: …] | model=X effort=Y``, compare
    the acked model/effort against current settings.json values. Exclude
    tasks whose latest status is done or approved.

    Returns newest-first list of dicts:
        {chat_id, chat_title, task_id, task_body, acked_model,
         acked_effort, current_model, current_effort, ack_ts}

    Errors are silent — hook must never break.
    """
    settings_path = Path(os.path.expanduser("~/.claude/settings.json"))
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return []

    # /model is per-session runtime state (absent from settings.json after
    # restart); default "opus" matches Claude Code's factory default.
    # effortLevel persists; absent → "" means skip effort comparison.
    current_model = (settings.get("model") or "opus").lower()
    current_effort = (settings.get("effortLevel") or "").lower()

    state_root = (
        Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))) / "khimaira"
    )
    chats_dir = state_root / "chats"
    if not chats_dir.exists():
        return []

    ack_re = re.compile(
        r"✅ ready \[task-id:\s*(task-[a-f0-9]+)\]\s*\|\s*model=(\w+)\s+effort=(\w+)",
        re.IGNORECASE,
    )

    results: list[dict] = []

    for path in sorted(chats_dir.glob("*.jsonl")):
        try:
            records: list[dict] = []
            try:
                with path.open("r", encoding="utf-8") as f:
                    for raw_line in f:
                        raw_line = raw_line.strip()
                        if not raw_line:
                            continue
                        try:
                            records.append(json.loads(raw_line))
                        except json.JSONDecodeError:
                            continue
            except OSError:
                continue

            if not records:
                continue

            chat_id = path.stem
            chat_title = ""
            for r in reversed(records):
                if r.get("kind") == "meta":
                    chat_id = r.get("chat_id") or path.stem
                    chat_title = (r.get("title") or "")[:50]
                    break

            # Track latest task status (task_update wins over initial task record).
            task_statuses: dict[str, str] = {}
            for r in records:
                kind = r.get("kind")
                tid = r.get("task_id") or ""
                if not tid:
                    continue
                if kind == "task" and tid not in task_statuses:
                    task_statuses[tid] = r.get("status") or ""
                elif kind == "task_update":
                    task_statuses[tid] = r.get("status") or ""

            # Collect assignment bodies for task_body context.
            assignments: dict[str, str] = {}
            for r in records:
                if r.get("kind") != "msg":
                    continue
                body = r.get("body") or ""
                if not body.startswith("🔔 TASK ASSIGNMENT"):
                    continue
                m = re.search(r"task-id:\s*(task-[a-f0-9]+)", body)
                if not m:
                    continue
                tid = m.group(1)
                if tid in assignments:
                    continue
                task_body = ""
                want_next = False
                for line in body.splitlines():
                    if line.startswith("Task: "):
                        after = line[len("Task: ") :].strip()
                        if after:
                            task_body = after
                            break
                        want_next = True
                        continue
                    if want_next and line.strip():
                        task_body = line.strip()
                        break
                assignments[tid] = task_body

            # Find the latest ack per task_id from this session.
            latest_acks: dict[str, dict] = {}
            for r in records:
                if r.get("kind") != "msg" or r.get("sender_id") != session_id:
                    continue
                m = ack_re.search(r.get("body") or "")
                if not m:
                    continue
                tid = m.group(1)
                am = m.group(2).lower()
                ae = m.group(3).lower()
                existing = latest_acks.get(tid)
                r_ts = r.get("ts") or ""
                if existing is None or r_ts > (existing.get("ack_ts") or ""):
                    latest_acks[tid] = {"acked_model": am, "acked_effort": ae, "ack_ts": r_ts}

            for tid, ack in latest_acks.items():
                if task_statuses.get(tid) in ("done", "approved"):
                    continue
                am = ack["acked_model"]
                ae = ack["acked_effort"]
                model_stale = current_model != am
                effort_stale = bool(current_effort) and current_effort != ae
                if not (model_stale or effort_stale):
                    continue
                results.append(
                    {
                        "chat_id": chat_id,
                        "chat_title": chat_title,
                        "task_id": tid,
                        "task_body": assignments.get(tid, ""),
                        "acked_model": am,
                        "acked_effort": ae,
                        "current_model": current_model,
                        "current_effort": current_effort,
                        "ack_ts": ack["ack_ts"],
                    }
                )
        except Exception:  # noqa: BLE001 — hook must never break
            continue

    results.sort(key=lambda x: x.get("ack_ts") or "", reverse=True)
    return results


def _format_stale_acks(stale: list[dict]) -> str:
    """Render stale-ack entries as a separate warning block.

    A stale ack means this session previously confirmed budget compliance
    for a task assignment, but the current session config no longer matches
    — most commonly because /model reverts to Opus after a restart (model
    is runtime state, not persisted in settings.json).

    Kept separate from _format_pending_assignments: the required action
    differs (re-apply budget and re-ack via /agent-ready, not a fresh gate).
    Returns "" when stale is empty.
    """
    if not stale:
        return ""
    lines = [
        f"⚠️ STALE TASK ACK(S) — {len(stale)} assignment(s) with budget drift (likely post-restart):",
        "",
    ]
    for s in stale:
        title = (s.get("chat_title") or s.get("chat_id", ""))[:18]
        tid = s.get("task_id", "?")
        task_body = (s.get("task_body") or "").strip()
        am = s.get("acked_model", "?")
        ae = s.get("acked_effort", "?")
        cm = s.get("current_model", "?")
        ce = s.get("current_effort") or "(unknown)"
        lines.append(f"  [{tid}] {title}")
        if task_body:
            snippet = task_body[:80] + ("..." if len(task_body) > 80 else "")
            lines.append(f"  Task: {snippet}")
        lines.append(f"  Acked: model={am} effort={ae} | Now: model={cm} effort={ce}")
        lines.append(f"  Re-apply: `/model {am}` + `/effort {ae}` then run /agent-ready")
        lines.append("")
    return "\n".join(lines).rstrip()


_WATERMARKS_PATH = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "chat_poll_watermarks.json"
)


def _poll_missed_chat_events(session_id: str) -> str:
    """Poll each accepted chat for messages that arrived while this session was idle.

    Loads a per-chat watermark (last-seen event_id) from chat_poll_watermarks.json.
    Fetches up to 20 messages since the watermark; skips own messages and any
    message older than 10 minutes (staleness cap). Updates watermarks after each
    successful fetch.

    Returns a formatted block, or "" when nothing new.
    Silently returns "" on daemon-down or any error.
    Opt-out: KHIMAIRA_CHAT_POLL_BANNER=0.
    """
    from datetime import datetime, timezone, timedelta

    try:
        watermarks: dict[str, str] = {}
        if _WATERMARKS_PATH.exists():
            watermarks = json.loads(_WATERMARKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        watermarks = {}

    # Cold-start staleness bound ONLY. When a per-chat watermark exists the fetch
    # below uses `&since=watermark`, so `messages` are already bounded to UNSEEN
    # events — applying an age cap on top of that drops unseen-but-old dispatches,
    # which is exactly the SSE-deaf-idle black hole (muther ISSUE 1/2, 2026-06-18):
    # roster_recovery wake latency (idle floor 300s + cooldown 300s + WIP 900s) can
    # exceed any short age cap, so a stale-but-unseen wake target stays invisible and
    # the agent re-idles having seen nothing. The cap is therefore applied ONLY on
    # cold-start (no watermark for the chat) to keep a brand-new session from
    # replaying a full day of history. Default 60 min >> max wake latency; env-tunable.
    _staleness_min = int(os.environ.get("KHIMAIRA_CHAT_POLL_STALENESS_MIN", "60"))
    cutoff_iso = (datetime.now(timezone.utc) - timedelta(minutes=_staleness_min)).isoformat()

    try:
        url = f"{_ENDPOINT}/api/chats?session_id={urllib.parse.quote(session_id, safe='')}"
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_INBOX_TIMEOUT_S) as resp:
            my_chats: list[dict] = json.loads(resp.read()).get("chats", [])
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return ""

    new_watermarks = dict(watermarks)
    all_new: list[tuple[str, str, list[dict]]] = []

    for chat in my_chats:
        if chat.get("my_state") != "accepted":
            continue
        chat_id = chat["chat_id"]
        title = chat.get("title") or chat_id[:18]
        watermark = watermarks.get(chat_id)

        try:
            url = (
                f"{_ENDPOINT}/api/chats/{chat_id}/messages"
                f"?session_id={urllib.parse.quote(session_id, safe='')}&limit=20"
            )
            if watermark:
                url += f"&since={urllib.parse.quote(watermark, safe='')}"
            req = urllib.request.Request(url, method="GET")
            with urllib.request.urlopen(req, timeout=_INBOX_TIMEOUT_S) as resp:
                messages: list[dict] = json.loads(resp.read()).get("messages", [])
        except (urllib.error.URLError, OSError, ValueError, TimeoutError):
            continue

        if messages:
            last_eid = messages[-1].get("event_id")
            if last_eid:
                new_watermarks[chat_id] = last_eid

        # Age cap applies ONLY on cold-start (no watermark). With a watermark the
        # `&since=` fetch already bounds to unseen events, so an unseen-but-old
        # dispatch (the SSE-deaf-idle case) MUST still surface regardless of age.
        new_msgs = [
            m for m in messages
            if m.get("kind") == "msg"
            and m.get("sender_id") != session_id
            and (watermark is not None or (m.get("ts") or "") >= cutoff_iso)
        ]
        if new_msgs:
            all_new.append((chat_id, title, new_msgs))

    try:
        _WATERMARKS_PATH.parent.mkdir(parents=True, exist_ok=True)
        _WATERMARKS_PATH.write_text(json.dumps(new_watermarks), encoding="utf-8")
    except OSError:
        pass

    if not all_new:
        return ""

    lines: list[str] = []
    for chat_id, _title, msgs in all_new:
        lines.append(f"💬 MISSED CHAT EVENTS — {chat_id} ({len(msgs)} new)")
        for m in msgs:
            sender = m.get("sender_name") or (m.get("sender_id") or "?")[:8]
            ts_raw = m.get("ts") or ""
            try:
                dt = datetime.fromisoformat(ts_raw)
                ts_fmt = dt.strftime("%H:%M")
            except ValueError:
                ts_fmt = ts_raw[:5]
            body = (m.get("body") or "").replace("\n", " ")
            if len(body) > 120:
                body = body[:117] + "..."
            lines.append(f"  [{sender} → {ts_fmt}]: {body}")
    return "\n".join(lines)


def _channel_event_response_level(prompt: str) -> str:
    """Classify a channel-only prompt for response-suppression purposes.

    Returns one of:
        "minimal"  — status-only notification; 1-sentence ack is enough
        "review"   — work completed or changes requested; master must engage
        ""         — not channel-only, or event type needs a full response

    Logic:
        1. Strip <system-reminder> wrappers (injected by Claude Code).
        2. If remainder isn't purely <channel source="khimaira-chat"> block(s),
           return "" (user text present → full response, no suppression).
        3. Parse kind= and status= attributes from each channel opening tag:
             kind=task_update, status in {done, approved}         → "review"
             kind=task_update, status=changes_requested           → "review"
             kind=task                                            → "review"
             kind=task_update, status in {in_progress, pending}   → "minimal"
             kind=invite                                         → "review"
             kind=msg                                             → "minimal"
             unknown kind / no kind                               → "minimal"
        4. If multiple blocks: take highest level (review > minimal).
    """
    if not prompt or not prompt.strip():
        return ""
    stripped = _SYSREM_RE.sub("", prompt).strip()
    if not stripped:
        return ""
    if not _CHANNEL_ONLY_RE.match(stripped):
        return ""

    level = "minimal"
    for tag_attrs in _CHANNEL_TAG_RE.findall(stripped):
        attrs = dict(_ATTR_RE.findall(tag_attrs))
        kind = attrs.get("kind", "")
        status = attrs.get("status", "")
        if kind == "task":
            level = "review"
        elif kind == "task_update":
            if status in ("done", "approved", "changes_requested"):
                level = "review"
            # in_progress/pending/unknown → stays "minimal"
        elif kind == "invite":
            level = "review"  # invites require action (chat_accept), not silence
        # kind=msg or absent → stays "minimal"
        if level == "review":
            break  # can't go higher

    return level


def _classify_prompt(prompt: str) -> str:
    """Classify a user prompt to decide which context to inject (task #66).

    Returns one of: "architecture" | "bugfix" | "coordination" | "simple".

    Keyword-based — pure stdlib, no LLM call, runs in the UserPromptSubmit
    hot path where latency matters (v1; an LLM classifier is the v2 upgrade).

    The DEFAULT is "coordination" (the full-context class that matches
    pre-#66 behavior). This is deliberate: a misclassification must never
    strip a coordination or safety signal. Only "simple" suppresses the
    ambient reminder blocks, and it fires only for clearly-trivial lookups
    (via the conservative `_looks_trivial`). Channel-only roster events
    classify as "coordination" by definition.

    Precedence (first match wins): channel-event → coordination keywords →
    bug-fix signals → architecture signals → trivial-lookup (simple) →
    default coordination.
    """
    if not prompt or not prompt.strip():
        return "coordination"

    # Channel-only roster events are coordination — never strip their context.
    stripped = _SYSREM_RE.sub("", prompt).strip()
    if _CHANNEL_ONLY_RE.match(stripped):
        return "coordination"

    lower = prompt.lower()

    # Coordination: roster / delegation / multi-session orchestration. Checked
    # first so a roster prompt is never downgraded to "simple" and stripped.
    coordination_markers = (
        "delegate", "assign", "roster", "dispatch", "handoff", "hand off",
        "begin signal", "/khimaira", "session_", "chat_", "the agent",
        "the master", "intake", "verifier", "tracker",
    )
    if any(m in lower for m in coordination_markers):
        return "coordination"

    # Bug-fix: failure language or a concrete source-file reference.
    bugfix_markers = (
        "bug", "error", "traceback", "exception", "failing", "fails",
        "broken", "not working", "doesn't work", "crash", "stack trace",
        "regression", "fix the", "throws", "stacktrace",
    )
    has_source_path = any(
        seg.strip("`'\"(),").endswith((".py", ".ts", ".tsx", ".js", ".jsx", ".vue"))
        for seg in prompt.split()
    )
    if has_source_path or any(m in lower for m in bugfix_markers):
        return "bugfix"

    # Architecture / design: structure + trade-off language.
    architecture_markers = (
        "architecture", "architect", "design", "trade-off", "tradeoff",
        "should we", "how should i structure", "structure this",
        "approach", "refactor", "restructure", "module boundar",
        "abstraction", "data flow", "best way to", "high-level",
    )
    if any(m in lower for m in architecture_markers):
        return "architecture"

    # Simple: short interrogative lookups (reuse the conservative heuristic).
    if _looks_trivial(prompt):
        return "simple"

    return "coordination"


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

    # cwd is provided by Claude Code in hook input; used for scope_cwd filtering.
    session_cwd = data.get("cwd") or ""

    # --- Stamp turn_start.txt (used by Themis chat_my_chats_fresh condition) --
    # Written at top of every turn so the PreToolUse hook can compare
    # subscriber_last_heartbeat < turn_start_ts to detect stale SSE.
    _state_base_dir = Path(
        os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    ) / "khimaira" / "sessions" / session_id
    try:
        _state_base_dir.mkdir(parents=True, exist_ok=True)
        (_state_base_dir / "turn_start.txt").write_text(
            __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat(),
            encoding="utf-8",
        )
    except OSError:
        pass

    # --- Sync Claude Code's /rename → khimaira's session name (every turn) ---
    # Cheap idempotent check; only POSTs when the names differ. Closes the
    # gap where /rename in Claude Code is UI-only and other sessions can't
    # address by the new name until the agent in the renamed session
    # manually calls session_set_name.
    _sync_rename_to_khimaira(session_id)

    # --- Inbox auto-read (every turn) -------------------------------------
    inbox_block = ""
    notes = _fetch_pending_notes(session_id, cwd=session_cwd or None)
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

    # --- First-turn chat_my_chats reminder --------------------------------
    # On turn 1 of a session (count == 0 before increment → new_count == 1),
    # if this session is in any accepted chats, remind it to call
    # chat_my_chats. SessionStart already nudges this, but agents skip the
    # text during compacted-session resumption. Firing it again on the first
    # actual prompt submission catches that gap. Silent after turn 1.
    chat_register_block = ""
    if new_count == 1:
        try:
            from khimaira.hooks.session_start import _discover_chat_roles

            if _discover_chat_roles(session_id):
                chat_register_block = (
                    "⚡ FIRST TURN — call this NOW before anything else:\n"
                    f'`mcp__khimaira-chat__chat_my_chats(session_id="{session_id}")`\n'
                    "Registers the SSE subscriber for real-time chat delivery. "
                    "Without it, chat_send messages from peers won't arrive until "
                    "your next prompted turn — you're effectively offline to the roster."
                )
        except Exception:  # noqa: BLE001 — hook must not break
            chat_register_block = ""

    # On turn 1, surface any owned handoffs as ACTION REQUIRED so the session
    # auto-acts on them without needing an explicit user prompt. The SessionStart
    # hook already injects handoffs into the system context, but only as passive
    # text — the session won't act until the user types something. This block
    # makes the FIRST user turn (even just pressing Enter) trigger handoff action.
    handoff_boot_block = ""
    if new_count == 1:
        try:
            from khimaira.hooks.session_start import _consume_handoffs

            # Use session_cwd from hook stdin (set at line 1000), not os.environ PWD
            # which may not match the project directory in the hook subprocess.
            owned_handoffs = _consume_handoffs(session_id, session_cwd)
            # Show all handoffs relevant to this cwd — owner OR observer.
            # The distinction matters for routing but not for boot-display.
            owned = [h for h in owned_handoffs if h.get("text")]
            if owned:
                handoff_boot_block = (
                    "🚨 ACTION REQUIRED — you have pending handoff(s) to act on:\n\n"
                )
                for h in owned[:3]:  # cap at 3 to keep context bounded
                    text = (h.get("text") or "").strip()
                    handoff_boot_block += f"{text[:2000]}\n\n"
                handoff_boot_block += (
                    "**Start immediately** — pick the highest-priority item above, "
                    "state which file/line you're starting at, then begin. "
                    "The handoff IS the authorization. Do not wait for further confirmation."
                )
        except Exception:  # noqa: BLE001 — hook must not break
            handoff_boot_block = ""

    # --- Phase B v1.7.2: per-turn role-budget reminder --------------------
    # Surfaces "you are role X in chat Y → /model A, /effort B" on every
    # prompt submission across every session. Closes the gap where v1.5's
    # role-directive emit fires only on role change (one-shot) and v1.6.1's
    # SessionStart hook fires only on boot — between boots + non-change-
    # events, the user could forget to apply the budget recommendation.
    # Reusing v1.6.1's _discover_chat_roles + _format_chat_roles helpers
    # (file-scan-primary; same logic that surfaces in the SessionStart block).
    # Silent when no chats have a role for this session (empty result).
    # Opt-out: KHIMAIRA_ROLE_BUDGET_REMINDER=0.
    role_budget_block = ""
    if os.environ.get("KHIMAIRA_ROLE_BUDGET_REMINDER") not in ("0", "false", "no"):
        try:
            from khimaira.hooks.session_start import (
                _discover_chat_roles,
                _format_chat_roles,
            )

            _roles = _discover_chat_roles(session_id)
            if _roles:
                role_budget_block = _format_chat_roles(_roles)
        except Exception:  # noqa: BLE001 — hook must not break
            role_budget_block = ""

    # --- v1.9.7: missed chat events poll (every turn) ---------------------
    # Fetches messages that arrived in accepted chats while this session was
    # idle between turns. SSE delivery is turn-gated; this poll closes the
    # gap. Block is injected FIRST — highest-priority context. Watermarks
    # tracked in chat_poll_watermarks.json so each event surfaces once.
    # Opt-out: KHIMAIRA_CHAT_POLL_BANNER=0.
    missed_chat_block = ""
    if os.environ.get("KHIMAIRA_CHAT_POLL_BANNER") not in ("0", "false", "no"):
        try:
            missed_chat_block = _poll_missed_chat_events(session_id)
        except Exception:  # noqa: BLE001 — hook must not break
            missed_chat_block = ""

    # --- Phase B v1.8: persistent banner — pending task assignments -------
    # Solves the "in-window prompt gets lost in noisy sessions" UX gap:
    # the agent's first-response prompt for a /khimaira-assign task can
    # scroll past unnoticed. This banner re-surfaces every turn until the
    # agent acks, so the prompt cannot be missed regardless of chat noise.
    # Silent when no pending assignments. Opt-out: KHIMAIRA_ASSIGN_BANNER=0.
    pending_assignments_block = ""
    if os.environ.get("KHIMAIRA_ASSIGN_BANNER") not in ("0", "false", "no"):
        try:
            _pending = _discover_pending_assignments(session_id)
            if _pending:
                pending_assignments_block = _format_pending_assignments(_pending)
        except Exception:  # noqa: BLE001 — hook must not break
            pending_assignments_block = ""

    # --- #14b: un-missable BEGIN banner — BEGUN tasks not yet in_progress ---
    # Closes handshake stall #5: after a ready-ack, the pending banner stops,
    # but a missed BEGIN SSE event leaves the agent idle on an officially-started
    # task. This banner re-surfaces the task every turn until the agent calls
    # chat_task_update(in_progress). Opt-out: KHIMAIRA_BEGUN_BANNER=0.
    begun_not_started_block = ""
    if os.environ.get("KHIMAIRA_BEGUN_BANNER") not in ("0", "false", "no"):
        try:
            _begun = _discover_begun_not_started(session_id)
            if _begun:
                begun_not_started_block = _format_begun_not_started(_begun)
        except Exception:  # noqa: BLE001 — hook must not break
            begun_not_started_block = ""

    # --- v1.9.6: inverse banner — assignments awaiting agent ack ----------
    # Master-facing counterpart to the agent's pending-assignments banner.
    # Surfaces tasks created BY this session that have no in_progress update
    # yet — so master knows which agents haven't started. Silent when this
    # session created no tasks (non-master sessions). Opt-out: KHIMAIRA_UNFIRED_ACK_BANNER=0.
    unfired_acks_block = ""
    if os.environ.get("KHIMAIRA_UNFIRED_ACK_BANNER") not in ("0", "false", "no"):
        try:
            _unfired = _discover_unfired_acks(session_id)
            if _unfired:
                unfired_acks_block = _format_unfired_acks(_unfired)
        except Exception:  # noqa: BLE001 — hook must not break
            unfired_acks_block = ""

    # --- Phase B v1.8.1: stale-ack detection ------------------------------
    # `/model` is per-session runtime state (not persisted in settings.json),
    # so after a restart a previously-acked assignment can have stale
    # compliance — the agent acked when settings matched, but post-restart
    # the model reverted to Opus default. This banner catches that case and
    # tells the user to re-set the budget. Distinct from pending_assignments
    # (which is for unacked work). Opt-out: KHIMAIRA_STALE_ACK_BANNER=0.
    stale_acks_block = ""
    if os.environ.get("KHIMAIRA_STALE_ACK_BANNER") not in ("0", "false", "no"):
        try:
            _stale = _check_stale_acks(session_id)
            if _stale:
                stale_acks_block = _format_stale_acks(_stale)
        except Exception:  # noqa: BLE001 — hook must not break
            stale_acks_block = ""

    # --- Phase B v1.7.1: real-time bottleneck check (every turn) ----------
    # Mirrors khimaira-bottleneck-watch.sh's heuristic but runs synchronously
    # in the UserPromptSubmit hot path. Detection cost: ~10-30ms (one daemon
    # HTTP call). Value: real-time per-turn signal vs the watcher's 5-min
    # polling cadence — every session sees the saturation prompt at the exact
    # moment they submit a prompt, not after a delayed inbox-post.
    #
    # If your daemon is down OR the check throws, it's a silent no-op (the
    # hook must never break SessionStart / UserPromptSubmit).
    bottleneck_block = ""
    if os.environ.get("KHIMAIRA_BOTTLENECK_PROMPT") not in ("0", "false", "no"):
        try:
            bottleneck_block = _check_bottleneck(session_id)
        except Exception:  # noqa: BLE001 — hook must not break
            bottleneck_block = ""

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

    # --- Phase B v1.9: channel-event response-level prompt ------------------
    # When the user's prompt is JUST a <channel source="khimaira-chat"> block,
    # classify the event and inject the appropriate directive:
    #   "minimal" (status-only, e.g. in_progress) → 🔇 suppress verbose reply
    #   "review"  (done/changes_requested)         → 📋 master must engage
    # Prevents Opus burn on pure status pings while ensuring completed work
    # is never silently swallowed. Opt-out: KHIMAIRA_QUIET_CHANNEL_RESPONSES=0.
    quiet_channel_block = ""
    if os.environ.get("KHIMAIRA_QUIET_CHANNEL_RESPONSES") not in ("0", "false", "no"):
        prompt_text = (data.get("prompt") or "").strip()
        _ch_level = _channel_event_response_level(prompt_text)
        if _ch_level == "minimal":
            quiet_channel_block = (
                "🔇 channel-only event — respond minimally:\n"
                "This turn was triggered by an incoming khimaira-chat channel block, "
                "not by user input.\n"
                "Default behavior: acknowledge in 1 sentence (or stay silent if no "
                "action required).\n"
                "DO NOT synthesize, summarize, or write detailed responses unless the "
                "channel content explicitly asks for action (e.g., a ready signal or "
                "a consult request directed at you). Master saves Opus tokens; the "
                "channel content is the record — your response is just acknowledgment."
            )
        elif _ch_level == "review":
            quiet_channel_block = (
                "📋 channel event — master review required:\n"
                "This turn contains a task completion or change-request from an agent. "
                "You MUST engage:\n"
                "  1. Read the task_update status + any note in the channel block.\n"
                "  2. Inspect the agent's work (file edits, chat messages, task notes).\n"
                "  3. Approve (chat_task_update → approved) or request changes "
                "(changes_requested + specific feedback).\n"
                "DO NOT treat this as a status-only ping. A done task without master "
                "review blocks the pipeline."
            )

    # --- Task #66: dynamic per-prompt context injection -------------------
    # Classify the user's prompt and tailor context: suppress the ambient
    # reminder blocks for trivial "simple" prompts (the roster-noise the
    # "Static Memory → Dynamic RAM" pitch targets), and surface a relevant-
    # context pointer for architecture / bug-fix prompts. Actionable/safety
    # blocks (inbox, incoming, assignments, acks, handoffs, missed-chat,
    # quiet-channel, bottleneck) are NEVER gated — suppressing a coordination
    # signal by guessing prompt-type would break the roster. The default
    # class is "coordination" (full context) so a misclassification can only
    # add a pointer or drop a reminder, never strip a safety signal.
    # Opt-out: KHIMAIRA_DYNAMIC_CONTEXT=0 restores pre-#66 (always-full) behavior.
    dynamic_context_block = ""
    if os.environ.get("KHIMAIRA_DYNAMIC_CONTEXT") not in ("0", "false", "no"):
        prompt_class = _classify_prompt(data.get("prompt") or "")
        if prompt_class == "simple":
            # Trivial lookup — drop the ambient reminders only. delegate_block
            # is intentionally KEPT (a simple prompt is exactly when delegation
            # is worth suggesting), as is every actionable/event-driven block.
            role_budget_block = ""
            reminder_block = ""
        elif prompt_class in ("architecture", "bugfix", "coordination"):
            try:
                from khimaira.context_inject import resolve_context
                ctx = resolve_context(session_cwd or os.getcwd(), prompt_class)
                hint = ctx.get("hint") or ""
                pointers = ctx.get("pointers") or []
                prose = ctx.get("prose") or {}
                parts: list[str] = []
                if hint:
                    parts.append(hint)
                if pointers:
                    parts.extend(f"  • {p}" for p in pointers)
                # Append prose fields (dep_graph_note, framework) after pointers
                if prose.get("dep_graph_note"):
                    parts.append(f"  ↳ {prose['dep_graph_note']}")
                if prose.get("framework"):
                    parts.append(f"  ↳ framework: {prose['framework']}")
                dynamic_context_block = "\n".join(parts)
            except Exception:
                # Fall back to pre-#66 hardcoded blocks on any import/resolve error
                if prompt_class == "architecture":
                    dynamic_context_block = (
                        "🏛️ architecture/design prompt — read before proposing structure:\n"
                        "  • CLAUDE.md (project conventions + engineering rules)\n"
                        "  • tasks/BUILD-PLAN.md (phase status) + tasks/<name>/IMPLEMENTATION.md (open specs)"
                    )
                elif prompt_class == "bugfix":
                    dynamic_context_block = (
                        "🐛 bug-fix prompt — suggested context: the failing file + its tests, "
                        "and recent git history for that path (`git log -p -S '<symbol>'`). "
                        "Reproduce first, then trace the data flow to the root cause."
                    )

    if (
        not inbox_block
        and not incoming_block
        and not reminder_block
        and not delegate_block
        and not bottleneck_block
        and not role_budget_block
        and not pending_assignments_block
        and not begun_not_started_block
        and not unfired_acks_block
        and not stale_acks_block
        and not quiet_channel_block
        and not missed_chat_block
        and not chat_register_block
        and not handoff_boot_block
        and not dynamic_context_block
    ):
        return 0

    # Ordering: handoff_boot (turn-1 owned handoffs, absolute top priority) →
    # chat_register (turn-1 only) → missed_chat → quiet_channel → bottleneck →
    # pending_assignments → unfired_acks → stale_acks → role_budget → delegate →
    # inbox → incoming questions → reminder.
    additional_context = "\n\n".join(
        b
        for b in (
            handoff_boot_block,
            chat_register_block,
            missed_chat_block,
            quiet_channel_block,
            dynamic_context_block,
            bottleneck_block,
            pending_assignments_block,
            begun_not_started_block,
            unfired_acks_block,
            stale_acks_block,
            role_budget_block,
            delegate_block,
            inbox_block,
            incoming_block,
            reminder_block,
        )
        if b
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": additional_context,
        }
    }
    sys.stdout.write(json.dumps(output))
    return 0


def _check_bottleneck(session_id: str) -> str:
    """Phase B v1.7.1: real-time per-turn bottleneck check.

    Mirrors khimaira-bottleneck-watch.sh's heuristic (≥2 sessions in
    `awaiting-review` for > 30 min AND at least one `orchestrating`
    session whose latest decision is > 20 min stale) but runs
    synchronously in the UserPromptSubmit hot path. Surfaces a
    personalized prompt depending on whether the caller is the master
    or an agent — master gets a strong "drop tier OR deputize" prompt;
    agents get a softer "consider dropping tier" prompt.

    Returns the prompt text to surface, or "" if no bottleneck. Quiet
    on daemon-down (returns "").
    """
    from datetime import datetime, timezone, timedelta

    url = f"{_ENDPOINT}/api/sessions"
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=_INBOX_TIMEOUT_S) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return ""

    sessions = payload if isinstance(payload, list) else (payload.get("sessions") or [])
    if not sessions:
        return ""

    now = datetime.now(timezone.utc)
    threshold = timedelta(minutes=30)
    decision_stale = timedelta(minutes=20)

    awaiting_count = 0
    master_stale_name = None
    master_stale_sid = None
    master_stale_minutes = None
    am_master = False

    for s in sessions:
        sid = s.get("session_id") or ""
        status = s.get("status") or ""
        la = s.get("last_active_at") or s.get("updated_at") or ""
        if not la:
            continue
        try:
            if la.endswith("Z"):
                la = la[:-1] + "+00:00"
            dt = datetime.fromisoformat(la)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            continue
        age = now - dt
        if status == "awaiting-review" and age > threshold:
            awaiting_count += 1
        if status == "orchestrating":
            recents = s.get("recent_decisions") or []
            if recents:
                ts = recents[0].get("ts", "")
                try:
                    if ts.endswith("Z"):
                        ts = ts[:-1] + "+00:00"
                    ldt = datetime.fromisoformat(ts)
                    if ldt.tzinfo is None:
                        ldt = ldt.replace(tzinfo=timezone.utc)
                    decision_age = now - ldt
                    if decision_age > decision_stale:
                        master_stale_name = s.get("name") or (sid[:8] if sid else "?")
                        master_stale_sid = sid
                        master_stale_minutes = int(decision_age.total_seconds() / 60)
                        if sid == session_id:
                            am_master = True
                except (ValueError, TypeError):
                    pass

    if awaiting_count < 2 or master_stale_minutes is None:
        return ""

    vice_name = (master_stale_name or "master") + "-vice"

    if am_master:
        return (
            f"⚠️ khimaira bottleneck signal — YOU (master) are saturated.\n"
            f"  • {awaiting_count} session(s) awaiting review\n"
            f"  • Your last decision was {master_stale_minutes}m ago\n"
            f"\n"
            f"Take action RIGHT NOW (type in this window):\n"
            f"  /model sonnet      — drop model tier (~5x cost reduction)\n"
            f"  /effort medium     — drop thinking tier (~5-10x reduction)\n"
            f"  /khimaira-deputize {vice_name} 'rate-limit'\n"
            f"\n"
            f"Auto-deputize T2 fires at ~15m total elapsed (opt-out: KHIMAIRA_AUTO_DEPUTIZE=0).\n"
            f"This prompt fires real-time per turn; opt-out: KHIMAIRA_BOTTLENECK_PROMPT=0."
        )
    else:
        return (
            f"⚠️ khimaira bottleneck signal — master {master_stale_name} saturated "
            f"({awaiting_count} session(s) awaiting review; master idle {master_stale_minutes}m).\n"
            f"\n"
            f"If you're saturated too, drop tier (type in this window):\n"
            f"  /model sonnet      — drop model tier\n"
            f"  /effort medium     — drop thinking tier\n"
            f"\n"
            f"Opt-out for this prompt: KHIMAIRA_BOTTLENECK_PROMPT=0."
        )


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
