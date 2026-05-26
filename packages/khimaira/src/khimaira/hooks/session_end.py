#!/usr/bin/env python3
"""khimaira Stop hook — distill domain-lead sessions into mnemosyne.

Fires on Claude Code's Stop event. For sessions whose name matches a
domain-lead pattern (e.g. "backend-lead-1", "jp-frontend-lead-2"), POSTs
the session transcript to the local mnemosyne distillation service at
http://127.0.0.1:8766/distill. Non-lead sessions (domain=="general") exit 0
with no POST — this hook is a no-op for regular sessions.

Stop payload contract:
  - session_id: the session UUID
  - transcript_path: path to the session JSONL (may not exist for short sessions)
  - hook_event_name: "Stop"

Fail-open: any exception → exit 0 silently. This hook must NEVER block
Claude Code from exiting cleanly.

Stdlib only. No third-party deps.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

from khimaira.hooks.mnemosyne_client import distill as _mnemosyne_distill
from khimaira.hooks.session_end_utils import (
    detect_domain,
    detect_project,
    extract_transcript,
)

_DAEMON_URL = "http://127.0.0.1:8740"
_DAEMON_TIMEOUT_S = 1


def _get_session_name(session_id: str) -> str:
    """Fetch session name from khimaira daemon. Returns UUID prefix on any failure."""
    try:
        req = urllib.request.Request(
            f"{_DAEMON_URL}/api/sessions/{session_id}",
            headers={"Accept": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=_DAEMON_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        name = (data.get("name") or "").strip()
        return name if name else session_id[:8]
    except Exception:
        return session_id[:8]


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

    session_id = data.get("session_id") or ""
    if not session_id:
        return 0

    transcript_path = data.get("transcript_path") or None
    cwd = data.get("cwd") or os.getcwd()

    session_name = _get_session_name(session_id)
    domain = detect_domain(session_name)
    if domain == "general":
        return 0

    transcript = extract_transcript(
        session_id,
        transcript_path=transcript_path,
    )
    if not transcript:
        return 0

    # Qualify domain key as <project>:<domain> to prevent cross-project pollution.
    # Fail-open: if project detection fails, fall back to bare domain.
    try:
        project = detect_project(cwd)
        qualified_domain = (
            f"{project}:{domain}" if project and project != "unknown" else domain
        )
    except Exception:
        qualified_domain = domain

    _mnemosyne_distill(qualified_domain, transcript, session_name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
