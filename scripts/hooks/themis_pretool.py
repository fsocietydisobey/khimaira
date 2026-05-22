#!/usr/bin/env python3
"""Themis PreToolUse hook — enforces role invariants by blocking violating
tool calls before they execute.

Failure mode: fail-open (D7). If the daemon is unreachable, stdin is
malformed, or any unexpected error occurs, the hook logs a warning and
allows the tool. Themis is a guardrail, not a security gate — daemon
downtime must not lock the user out of editing.

Block mechanism (verified via probe v2, 2026-05-21):
  emit {"decision": "block", "reason": "..."} on stdout + exit 0 → blocked.
  exit 0 with no stdout (or non-JSON stdout) → allowed.

Latency budget: p99 <300ms total (Python cold-start ~295ms on this machine).
  TIMEOUT_S = 0.1 (D7 must-fix #2 per architect-1 — 100ms covers daemon p99
  with 75ms slack; was 0.5 in spec pseudocode but 500ms + 295ms cold-start
  = 795ms total, well over 300ms p99 target).
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen

# Configurable daemon base URL — override via THEMIS_DAEMON env for testing.
DAEMON = os.environ.get("THEMIS_DAEMON", "http://127.0.0.1:8740")
TIMEOUT_S = 0.1
FAIL_OPEN_LOG = Path.home() / ".claude" / "hooks" / "themis_fail_open.log"


def _fail_open(reason: str) -> None:
    """Log a fail-open event and exit 0 (allow the tool)."""
    FAIL_OPEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    try:
        with FAIL_OPEN_LOG.open("a") as f:
            f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {reason}\n")
    except OSError:
        pass  # Can't log either — still fail-open
    sys.exit(0)


def _block(rule_id: str, message: str) -> None:
    """Emit the block decision on stdout and exit 0."""
    print(json.dumps({"decision": "block", "reason": message}))
    sys.exit(0)


def main() -> None:
    # ── 1. Parse stdin ──────────────────────────────────────────────────────
    try:
        raw = sys.stdin.read()
        payload = json.loads(raw)
    except Exception as exc:
        _fail_open(f"stdin parse failed: {exc}")
        return  # unreachable; satisfies type checker

    # ── 2. Extract fields ────────────────────────────────────────────────────
    session_id: str = payload.get("session_id") or os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    tool_name: str = payload.get("tool_name", "")
    tool_input: dict = payload.get("tool_input", {})
    cwd: str = payload.get("cwd", "")

    if not session_id or not tool_name:
        _fail_open(f"missing session_id or tool_name — session_id={session_id!r} tool={tool_name!r}")
        return

    # ── 3. POST /api/themis/check ─────────────────────────────────────────
    try:
        body = json.dumps(
            {
                "session_id": session_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "cwd": cwd,
            }
        ).encode()
        req = Request(
            f"{DAEMON}/api/themis/check",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-Session-ID": session_id,
            },
        )
        with urlopen(req, timeout=TIMEOUT_S) as resp:
            verdict = json.load(resp)
    except URLError as exc:
        _fail_open(f"daemon unreachable: {exc}")
        return
    except TimeoutError as exc:
        _fail_open(f"daemon timeout ({TIMEOUT_S}s): {exc}")
        return
    except Exception as exc:
        _fail_open(f"daemon /api/themis/check failed: {exc}")
        return

    # ── 4. Act on verdict ─────────────────────────────────────────────────
    try:
        ok = verdict.get("ok")
    except AttributeError:
        _fail_open(f"malformed daemon response (not a dict): {verdict!r}")
        return

    if ok:
        sys.exit(0)

    violation = verdict.get("violation") or {}
    severity = violation.get("severity", "")
    if severity == "block":
        rule_id = violation.get("rule_id", "IN-?")
        message = violation.get("message", "rule violated")
        _block(rule_id, message)
    else:
        # warn / audit — daemon already logged; allow the tool
        sys.exit(0)


if __name__ == "__main__":
    main()
