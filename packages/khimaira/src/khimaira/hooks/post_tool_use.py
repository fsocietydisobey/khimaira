#!/usr/bin/env python3
"""khimaira PostToolUse hook — auto-log file touches to the session store.

Runs after Edit / Write / MultiEdit / NotebookEdit. Reads the hook JSON
from stdin, extracts file path + tool name, appends to the khimaira
session's files_touched.jsonl directly (no HTTP roundtrip — same JSONL
format the daemon reads).

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


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _session_dir(session_id: str) -> Path:
    safe = session_id.replace("/", "_").replace("..", "_")
    d = _BASE_DIR / safe
    d.mkdir(parents=True, exist_ok=True)
    return d


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
    if not session_id or tool_name not in (
        "Edit",
        "Write",
        "MultiEdit",
        "NotebookEdit",
    ):
        return 0

    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
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

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        # Catch-all — hooks must never bubble exceptions back to Claude Code
        sys.exit(0)
