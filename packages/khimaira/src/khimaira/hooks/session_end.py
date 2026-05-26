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
import sys
import urllib.error
import urllib.request
from pathlib import Path

# Import utilities from the companion module. Both live in the same hooks/
# package so the relative import is stable regardless of venv path.
from khimaira.hooks.session_end_utils import detect_domain, extract_transcript

_MNEMOSYNE_URL = "http://127.0.0.1:8766/distill"
_DAEMON_URL = "http://127.0.0.1:8740"
_POST_TIMEOUT_S = 2
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

    payload = json.dumps(
        {
            "domain": domain,
            "transcript": transcript,
            "session_slug": session_name,
        },
        separators=(",", ":"),
    ).encode("utf-8")

    try:
        req = urllib.request.Request(
            _MNEMOSYNE_URL,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=_POST_TIMEOUT_S):
            pass
    except (urllib.error.URLError, OSError, TimeoutError):
        pass

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
