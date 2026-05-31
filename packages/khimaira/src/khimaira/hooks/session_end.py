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


def _report_throttle(session_id: str, verdict: dict) -> None:
    """POST a terminal-overload verdict to the daemon (#13b-heavy).

    Fire-and-forget: any failure is swallowed so the Stop hook never blocks
    CC from exiting. The daemon surfaces the 🟡 alert + escalation.
    """
    try:
        body = json.dumps({
            "retry_attempt": verdict.get("retry_attempt"),
            "max_retries": verdict.get("max_retries"),
            "overload_count": verdict.get("overload_count"),
            "last_timestamp": verdict.get("last_timestamp"),
            "message": verdict.get("message"),
        }).encode("utf-8")
        req = urllib.request.Request(
            f"{_DAEMON_URL}/api/sessions/{session_id}/throttle",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        urllib.request.urlopen(req, timeout=_DAEMON_TIMEOUT_S).close()
    except Exception:
        pass


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

    # #13b-heavy — terminal rate-limit detection runs for ALL sessions (not
    # just leads), BEFORE the lead-only distill gate below. A throttled-out
    # session with no task obligation never trips Guard-4; this is the only
    # signal that it stopped. Fail-open: detection never blocks CC exit.
    try:
        from khimaira.hooks.throttle_detect import detect_terminal_overload

        verdict = detect_terminal_overload(transcript_path)
        if verdict:
            _report_throttle(session_id, verdict)
    except Exception:
        pass

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
