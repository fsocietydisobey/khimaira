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

import contextlib
import json
import os
import sys
import tempfile
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
        body = json.dumps(
            {
                "retry_attempt": verdict.get("retry_attempt"),
                "max_retries": verdict.get("max_retries"),
                "overload_count": verdict.get("overload_count"),
                "last_timestamp": verdict.get("last_timestamp"),
                "message": verdict.get("message"),
            }
        ).encode("utf-8")
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


# ---------------------------------------------------------------------------
# Drain-before-idle keystone (lean-roster — the tracker-seat replacement).
# Before a seat idles, BLOCK the Stop and re-engage the model IF it still owes
# work (an owed verdict OR an unanswered DIRECTED chat message). The seat drains
# IN-SESSION instead of idling-owed and waiting for a daemon wake — this dissolves
# the critic re-wake loop, the directed-wake self-termination / MSG-vs-action gap
# (we force the chat reply here), and cold-idle-wake.
#
# ⚠️ FAIL-OPEN-ON-ERROR is mandatory. This is the ONE hook path that can block CC
# from stopping, so EVERY error path falls through to "don't block" — a hook
# exception must never wedge a seat at its prompt.
# ---------------------------------------------------------------------------

_DRAIN_ENABLED = os.environ.get("KHIMAIRA_DRAIN_BEFORE_IDLE", "1") != "0"
# Cross-stop-chain backstop: the native CLAUDE_CODE_STOP_HOOK_BLOCK_CAP bounds a
# single continuation chain; this file counter bounds re-blocking across SEPARATE
# Stop events so a genuinely-stuck owed seat fail-opens instead of being perpetually
# re-blocked. Reset when owed-work clears.
_DRAIN_BLOCK_CAP = int(os.environ.get("KHIMAIRA_DRAIN_BLOCK_CAP", "10") or "10")


def _stamp_turn_end(session_id: str) -> None:
    """Mark the turn as ended (seat going idle at the prompt).

    Paired with ``turn_start.txt`` (stamped by the UserPromptSubmit hook). The
    daemon's liveness check (``sessions.is_mid_turn``) treats start>end as an
    OPEN turn = alive-busy, so a long no-tool-call generation isn't mis-read as
    idle/unreachable. Stamped ONLY when the Stop proceeds — NOT when
    drain-before-idle blocks and the turn continues (a blocked Stop is still
    'working'). Fail-open: never raises into the Stop hook.
    """
    import datetime

    state_dir = (
        Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
        / "khimaira"
        / "sessions"
        / session_id
    )
    with contextlib.suppress(Exception):
        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "turn_end.txt").write_text(
            datetime.datetime.now(datetime.timezone.utc).isoformat(),
            encoding="utf-8",
        )


def _drain_counter_path(session_id: str) -> Path:
    return Path(tempfile.gettempdir()) / f"khimaira-drain-{session_id}.count"


def _drain_attempts(session_id: str) -> int:
    try:
        return int(_drain_counter_path(session_id).read_text().strip() or "0")
    except Exception:
        return 0


def _bump_drain(session_id: str) -> None:
    with contextlib.suppress(Exception):
        _drain_counter_path(session_id).write_text(str(_drain_attempts(session_id) + 1))


def _reset_drain(session_id: str) -> None:
    with contextlib.suppress(Exception):
        _drain_counter_path(session_id).unlink(missing_ok=True)


def _owed_work(session_id: str) -> dict | None:
    """Return ``{summary, drain_steps}`` if the seat owes a verdict or has an
    unanswered directed message, else None. Uses the daemon's read-only predicates
    via direct import (the hook runs in khimaira's venv — no HTTP). Any error in a
    given probe is swallowed → that probe contributes nothing (fail-open)."""
    summary_parts: list[str] = []
    steps: list[str] = []
    try:
        from khimaira.monitor.api.chats import _get_session_obligations

        obs = _get_session_obligations(session_id) or []
        owed_verdicts = sorted({o["task_id"] for o in obs if o.get("owed_verdict")})
        if owed_verdicts:
            ids = ", ".join(owed_verdicts)
            summary_parts.append(f"{len(owed_verdicts)} owed verdict(s): {ids}")
            steps.append(
                f"Post your owed verdict(s) via chat_task_verdict on {ids}, THEN post a "
                f"one-line chat_send reply in that chat — the chat reply is REQUIRED to "
                f"clear the signal (a verdict alone does NOT advance your last-own-post)."
            )
    except Exception:
        pass
    try:
        from khimaira.monitor.roster_recovery import _session_has_directed_unanswered

        if _session_has_directed_unanswered(session_id):
            summary_parts.append("an unanswered directed message")
            steps.append(
                "Reply to the directed message addressed to you with a chat_send in that chat."
            )
    except Exception:
        pass
    if not summary_parts:
        return None
    return {"summary": "; ".join(summary_parts), "drain_steps": " ".join(steps)}


def _emit_drain_block(reason: str, additional_context: str) -> int:
    """Emit the Stop-hook block decision; return the process exit code.

    Mechanism (CC hooks docs): structured control = JSON on STDOUT with
    ``decision="block"``; ``hookSpecificOutput.additionalContext`` requires the JSON
    path. The JSON-control path uses exit code 0 (the JSON, not the exit code,
    carries the block). `reason` is shown to the user + fed to the model; CC then
    re-enters the agentic loop with `additionalContext` injected — no new prompt.
    """
    print(
        json.dumps(
            {
                "decision": "block",
                "reason": reason,
                "hookSpecificOutput": {
                    "hookEventName": "Stop",
                    "additionalContext": additional_context,
                },
            }
        )
    )
    return 0


def _drain_before_idle(data: dict, *, throttled: bool) -> int | None:
    """Drain-before-idle gate. Returns an exit code if it BLOCKED the stop (the
    caller returns it), else None to let the stop proceed. Fail-open everywhere:
    any uncertainty → None (never block)."""
    if not _DRAIN_ENABLED:
        return None
    session_id = data.get("session_id") or ""
    if not session_id:
        return None
    # A rate-limited seat can't act on a block — let it stop; the daemon watchdog
    # owns the throttled case (it can't drain via a re-engage).
    if throttled:
        return None
    owed = _owed_work(session_id)
    if not owed:
        _reset_drain(session_id)  # cleared → reset the cross-chain backstop counter
        return None
    if _drain_attempts(session_id) >= _DRAIN_BLOCK_CAP:
        return None  # cross-chain backstop: stuck-owed seat fail-opens
    _bump_drain(session_id)
    return _emit_drain_block(
        reason=f"Drain before idle — you still owe: {owed['summary']}",
        additional_context=owed["drain_steps"],
    )


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
    verdict = None
    try:
        from khimaira.hooks.throttle_detect import detect_terminal_overload

        verdict = detect_terminal_overload(transcript_path)
        if verdict:
            _report_throttle(session_id, verdict)
    except Exception:
        pass

    # Drain-before-idle keystone — runs for ALL sessions (incl. "general" agents),
    # BEFORE the lead-only distill gate. If the seat owes a verdict / directed reply,
    # block the stop (exit 0 + decision=block JSON) + re-engage so it drains in-session.
    # Fail-open: the whole call is guarded so an error here can NEVER block CC exit.
    try:
        drain_exit = _drain_before_idle(data, throttled=bool(verdict))
        if drain_exit is not None:
            return drain_exit
    except Exception:
        pass

    # The Stop is proceeding (drain did NOT block) — the seat is going idle at
    # the prompt. Close the turn so the daemon stops reading it as mid-generation.
    # Placed AFTER the drain gate: a blocked Stop returns above and the turn stays
    # open (still working). Fail-open.
    _stamp_turn_end(session_id)

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
        qualified_domain = f"{project}:{domain}" if project and project != "unknown" else domain
    except Exception:
        qualified_domain = domain

    _mnemosyne_distill(qualified_domain, transcript, session_name)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
