#!/usr/bin/env python3
"""khimaira PostToolUse hook — auto-log file touches + tool calls to the session store.

Runs after every tool call. Records:
  - tool_calls.jsonl: every tool invocation (all tools), capped at _TOOL_CALL_CAP.
    Used by the PreToolUse Themis hook to inspect recent activity (e.g. IN-MASTER-4).
  - files_touched.jsonl: file-path mutations for Edit/Write/MultiEdit/NotebookEdit only.

Hard rules:
  - Never block Claude Code. ANY failure → exit 0 silently.
  - Stdlib only. The khimaira package may not be importable from here.
  - Direct filesystem write. Faster than HTTP and works when daemon's down.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

_BASE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "sessions"
)

_TOOL_CALL_CAP = 100


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_dir(session_id: str) -> Path:
    safe = session_id.replace("/", "_").replace("..", "_")
    d = _BASE_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


def _append_tool_call(session_id: str, tool_name: str, tool_input: dict) -> None:
    """Append one tool-call record and truncate to _TOOL_CALL_CAP if over cap."""
    record = {"ts": _now_iso(), "tool": tool_name, "params": tool_input}
    path = _session_dir(session_id) / "tool_calls.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")
    # Ring-buffer: drop oldest entries when over cap.
    try:
        lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if len(lines) > _TOOL_CALL_CAP:
            tmp = path.with_suffix(".jsonl.tmp")
            with tmp.open("w", encoding="utf-8") as f:
                f.write("\n".join(lines[-_TOOL_CALL_CAP:]) + "\n")
            tmp.replace(path)
    except OSError:
        pass


def _append_touch(session_id: str, file_path: str, summary: str) -> None:
    record = {
        "ts": _now_iso(),
        "file": file_path,
        "summary": summary,
        "line_start": None,
        "line_end": None,
    }
    path = _session_dir(session_id) / "files_touched.jsonl"
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


def _write_sse_heartbeat(session_id: str) -> None:
    """Minimal heartbeat writer — avoids importing khimaira.monitor.sessions.

    Writes last_sse_heartbeat timestamp into status.json so daemon can detect
    subscribers whose subprocess is alive but SSE connection died.
    """
    d = _session_dir(session_id)
    status_path = d / "status.json"
    existing: dict = {}
    if status_path.exists():
        try:
            existing = json.loads(status_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            existing = {}
    existing["last_sse_heartbeat"] = datetime.now(timezone.utc).isoformat()
    try:
        status_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")
    except OSError:
        pass


_SPECTER_LOOKBACK = 15  # How many recent tool calls to scan for a snapshot.


def _check_specter_verify(session_id: str, ui_files: list[str]) -> str | None:
    """Return a reminder string if specter_debug_snapshot wasn't called recently.

    Reads the last _SPECTER_LOOKBACK tool calls from the session's log, excluding
    the current Edit/Write that just fired. If specter_debug_snapshot does not
    appear in that window, returns a reminder message; otherwise returns None.

    Fail-open: any I/O or parse error returns None (no reminder, no block).
    """
    path = _session_dir(session_id) / "tool_calls.jsonl"
    try:
        raw_lines = [ln for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    except OSError:
        return None

    # Exclude the most recent entry (the Edit/Write that just triggered this hook).
    prior_lines = raw_lines[:-1] if raw_lines else []
    recent = prior_lines[-_SPECTER_LOOKBACK:]

    for line in recent:
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue
        if record.get("tool") == "mcp__khimaira__specter_debug_snapshot":
            return None  # Recent snapshot found — no reminder needed.

    # No snapshot found. Build a reminder.
    files_str = ", ".join(f"`{f.split('/')[-1]}`" for f in ui_files[:3])
    if len(ui_files) > 3:
        files_str += f" (+{len(ui_files) - 3} more)"
    return (
        f"⚠️ Specter verify: edited {files_str} (UI file). "
        f"`specter_debug_snapshot` was not called in the last {_SPECTER_LOOKBACK} tool calls. "
        "Run it now to confirm the change rendered correctly — "
        "`tsc passing` ≠ the user sees what you expect. "
        "(khimaira-tools.md rule: Specter is a VERIFY tool, not just a debug tool.)"
    )


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return 0

    session_id = data.get("session_id") or ""
    tool_name = data.get("tool_name") or ""
    if not session_id or not tool_name:
        return 0

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        tool_input = {}

    # Write SSE heartbeat to mark session as alive
    try:
        _write_sse_heartbeat(session_id)
    except OSError:
        pass

    # Always capture tool call for Themis / history inspection.
    try:
        _append_tool_call(session_id, tool_name, tool_input)
    except OSError:
        pass

    # Only log file touches for file-editing tools.
    if tool_name not in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return 0

    files: list[str] = []
    # Edit / Write
    fp = tool_input.get("file_path")
    if isinstance(fp, str) and fp:
        files.append(fp)
    # NotebookEdit
    np = tool_input.get("notebook_path")
    if isinstance(np, str) and np:
        files.append(np)
    # MultiEdit — list of edits, each with its own file_path
    edits = tool_input.get("edits")
    if isinstance(edits, list):
        for e in edits:
            if isinstance(e, dict):
                f = e.get("file_path")
                if isinstance(f, str) and f and f not in files:
                    files.append(f)

    summary = f"auto-logged from {tool_name} hook"
    for f in files:
        try:
            _append_touch(session_id, f, summary)
        except OSError:
            # Don't block Claude Code on filesystem errors
            pass

    # Specter-verify-after-UI-edit reminder (khimaira-tools.md personal rule).
    # After editing a frontend component file, check if specter_debug_snapshot
    # was called recently. If not, emit a reminder via additionalContext.
    _ui_extensions = (".tsx", ".jsx", ".vue", ".svelte")
    ui_files_edited = [f for f in files if any(f.endswith(ext) for ext in _ui_extensions)]
    if ui_files_edited:
        try:
            _specter_reminder = _check_specter_verify(session_id, ui_files_edited)
            if _specter_reminder:
                out = {"hookSpecificOutput": {"additionalContext": _specter_reminder}}
                sys.stdout.write(json.dumps(out))
        except Exception:
            pass  # Never block on reminder logic

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Catch-all — hooks must never bubble exceptions back to Claude Code
        sys.exit(0)
