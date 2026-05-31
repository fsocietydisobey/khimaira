"""Roster auto-recovery watcher.

Daemon-side watcher that monitors kitty terminal windows for roster sessions
at high context usage, then distills + compacts them automatically.

WAKE VECTOR: ``kitty @ send-text`` is REAL keystroke injection — not a passive
note consumed only on the next user turn. This module creates the wake vector
that ``#13b-heavy``'s audit declared non-existent (at audit time, this module
didn't exist). Vector-exists ≠ safe-to-auto-resume, however:
- ``/compact`` injection (benign, idle-moment) is in scope here.
- Work-resume injection stays escalate-only; a ``#13b v2`` must clear its own
  safety bar before that path is built.

SAFETY GUARDS (all mandatory, per architect ruling, audit-grade):

(a) TARGET-VERIFY
    Match ``cmdline -r <role>`` AND cross-check the session UUID from the chat
    member_roles before acting. Never act on window-id alone — kitty window IDs
    can shift across restarts.

(b) SAFE-TIMING
    Inject ONLY when the window is idle (no ``esc to interrupt``, no
    ``Compacting`` marker). Immediately before submitting (``send-key enter``),
    re-read the buffer and verify it contains ONLY the injected text — no
    user keystrokes have landed since the injection (TOCTOU guard). Abort and
    clear the buffer if the content has changed.

(c) OPT-OUT + AUDIT
    Set ``KHIMAIRA_ROSTER_RECOVERY=0`` to disable globally. Sessions with a
    ``.nocompact`` file in their state directory are skipped. Every injection
    attempt (target, action, reason, outcome) is logged at INFO level so there
    is a full audit trail.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import subprocess
import time
from pathlib import Path
from typing import Any

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration (environment-overridable)
# ---------------------------------------------------------------------------
_COMPACT_THRESHOLD = int(os.environ.get("KHIMAIRA_ROSTER_COMPACT_PCT", "85"))
_IDLE_MIN_S = float(os.environ.get("KHIMAIRA_ROSTER_IDLE_MIN_S", "300"))  # 5 min
_WATCH_INTERVAL_S = float(os.environ.get("KHIMAIRA_ROSTER_WATCH_S", "60"))
_COMPACT_COOLDOWN_S = 300.0  # 5 min between compact/wake attempts per window

# Patterns that indicate the terminal is actively working (NOT idle).
_BUSY_MARKERS = ("esc to interrupt", "compacting…", "compacting...", "compacting ")

_CONTEXT_PCT_RE = re.compile(r"(\d+)%\s+context\s+used", re.IGNORECASE)

# Debounce table: (window_id, action) → last_attempt_ts
_DEBOUNCE: dict[tuple[int, str], float] = {}


# ---------------------------------------------------------------------------
# Environment / opt-out
# ---------------------------------------------------------------------------

def _env_enabled() -> bool:
    """Return False if the global kill-switch is set."""
    return os.environ.get("KHIMAIRA_ROSTER_RECOVERY", "1") != "0"


def _session_nocompact_path(session_id: str) -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "khimaira" / "sessions" / session_id / ".nocompact"


def _session_opt_out(session_id: str) -> bool:
    return _session_nocompact_path(session_id).exists()


# ---------------------------------------------------------------------------
# kitty remote-control helpers
# ---------------------------------------------------------------------------

def _kitty(
    *args: str,
    input_text: str | None = None,
    timeout: float = 5.0,
) -> str | None:
    """Run ``kitty @ <args>``; return stdout or None on any failure."""
    cmd = ["kitty", "@", *args]
    try:
        r = subprocess.run(
            cmd,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        if r.returncode == 0:
            return r.stdout
        _log.debug("kitty @ %s failed (rc=%d): %s", args[0], r.returncode, r.stderr.strip())
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _log.debug("kitty unavailable: %s", exc)
        return None


def _discover_roster_windows() -> list[dict[str, Any]]:
    """Return roster claude-chat windows as ``[{window_id, role, cmdline}]``.

    Handles both bare names (``agent-1``) and prefixed names (``jp-agent-1``,
    ``jp-frontend-lead-1``): the ``-r <name>`` value is normalized through
    ``infer_role_from_name`` so the returned ``role`` always matches what is
    stored in ``member_roles`` (e.g. ``agent``, ``jp-agent``, ``frontend-lead``).
    Windows whose name doesn't resolve to a known role are skipped.
    """
    raw = _kitty("ls")
    if not raw:
        return []
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return []

    try:
        from khimaira.monitor.chats import infer_role_from_name
    except Exception:
        infer_role_from_name = None  # type: ignore[assignment]

    roster: list[dict[str, Any]] = []
    for os_win in data:
        for tab in os_win.get("tabs", []):
            for win in tab.get("windows", []):
                wid = win.get("id")
                if wid is None:
                    continue
                cmdline: list[str] = [str(c) for c in (win.get("cmdline") or [])]
                joined = " ".join(cmdline)
                # Must be a claude-chat invocation with a -r <name> flag.
                if "claude" not in joined:
                    continue
                raw_name: str | None = None
                for i, arg in enumerate(cmdline):
                    if arg == "-r" and i + 1 < len(cmdline):
                        raw_name = cmdline[i + 1]
                        break
                if not raw_name:
                    continue
                # Normalize through infer_role_from_name to handle prefixed names
                # (jp-agent-1 → jp-agent, jp-frontend-lead-1 → jp-frontend-lead).
                if infer_role_from_name is not None:
                    role = infer_role_from_name(raw_name)
                else:
                    # Fallback: strip trailing -N suffix manually
                    parts = raw_name.rsplit("-", 1)
                    role = parts[0] if (len(parts) == 2 and parts[1].isdigit()) else raw_name
                if role:
                    roster.append({"window_id": wid, "role": role, "cmdline": joined})
    return roster


def _get_screen(window_id: int) -> str | None:
    """Read current screen text for a kitty window."""
    return _kitty("get-text", f"--match=id:{window_id}")


def _parse_context_pct(text: str) -> int | None:
    """Extract ``NN% context used`` from terminal screen content."""
    m = _CONTEXT_PCT_RE.search(text)
    return int(m.group(1)) if m else None


def _is_busy(text: str) -> bool:
    """Return True if the window is actively working (unsafe to inject)."""
    if not text:
        return True  # unknown → be conservative
    lower = text.lower()
    return any(marker in lower for marker in _BUSY_MARKERS)


# ---------------------------------------------------------------------------
# Guard (a): target-verify
# ---------------------------------------------------------------------------

def _resolve_session_for_role(role: str) -> str | None:
    """Look up the session UUID for a roster role via chat member_roles.

    Scans all active chat rooms for a session that holds ``role``.
    Returns the UUID string, or None if not found OR if multiple sessions match
    (ambiguity abort — acting on the wrong one is the Specter wrong-tab failure).
    """
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return None
        matches: list[str] = []
        for chat_path in chat_dir.glob("chat-*.jsonl"):
            room_id = chat_path.stem
            try:
                room = chats_mod.load_room(room_id)
                member_roles: dict[str, str] = room["meta"].get("member_roles") or {}
                for sid, r in member_roles.items():
                    if r == role and sid not in matches:
                        matches.append(sid)
            except Exception:
                continue
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            _log.warning(
                "roster-recovery: ambiguous target — %d sessions match role=%r, aborting",
                len(matches),
                role,
            )
            return None
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Distillation (data-safety: must run before /compact)
# ---------------------------------------------------------------------------

async def _distill_session(session_id: str, role: str) -> None:
    """Trigger mnemosyne distillation for the session (fail-open)."""
    try:
        # Run in executor to avoid blocking the event loop on file I/O
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, _distill_sync, session_id, role)
    except Exception as exc:
        _log.debug("roster-recovery: distill error for %s: %s", session_id[:8], exc)


def _distill_sync(session_id: str, role: str) -> None:
    try:
        from khimaira.hooks.session_end_utils import extract_transcript, detect_domain
        from khimaira.hooks.mnemosyne_client import distill

        transcript = extract_transcript(session_id)
        if not transcript:
            _log.debug("roster-recovery: no transcript for %s, skipping distill", session_id[:8])
            return
        domain = detect_domain(role)
        result = distill(domain=domain, transcript=transcript, session_slug=role)
        if result:
            _log.info(
                "roster-recovery: distilled session %s role=%s domain=%s",
                session_id[:8],
                role,
                domain,
            )
    except Exception as exc:
        _log.debug("roster-recovery: _distill_sync failed for %s: %s", session_id[:8], exc)


# ---------------------------------------------------------------------------
# Guard (b): TOCTOU-safe injection
# ---------------------------------------------------------------------------

def _inject_text_and_submit(window_id: int, text: str) -> bool:
    """Inject ``text`` into a kitty window and submit with Enter.

    Steps:
    1. Send the text (does NOT submit yet).
    2. Wait briefly for the terminal to echo it.
    3. Re-read the buffer — verify it ends with ONLY our text (TOCTOU guard).
       Abort + clear (Ctrl-C) if changed.
    4. Submit with Enter.

    Returns True if submitted, False if aborted.
    """
    # Step 1: send text
    if _kitty("send-text", f"--match=id:{window_id}", "--", text) is None:
        _log.warning("roster-recovery: send-text failed for window %d", window_id)
        return False

    # Step 2: brief pause for echo
    time.sleep(0.15)

    # Step 3: TOCTOU — re-read and verify
    buffer = _get_screen(window_id)
    if buffer is None:
        # Can't verify — abort safely
        _kitty("send-key", f"--match=id:{window_id}", "ctrl+c")
        _log.warning(
            "roster-recovery: TOCTOU verify read failed for window %d — aborted",
            window_id,
        )
        return False

    lines = [line.rstrip() for line in buffer.splitlines() if line.strip()]
    last = lines[-1] if lines else ""
    expected = text.rstrip()
    # Exact match: the last line must be ONLY our injected text.
    # endswith() is insufficient — it passes if the user typed before our text
    # (e.g. "user_input/compact" still endswith "/compact" but is a raced buffer).
    if last != expected:
        # Buffer has changed — user may have typed between our steps; abort
        _kitty("send-key", f"--match=id:{window_id}", "ctrl+c")
        _log.warning(
            "roster-recovery: TOCTOU mismatch on window %d — expected %r, got %r, aborted",
            window_id,
            expected,
            last,
        )
        return False

    # Step 4: submit
    if _kitty("send-key", f"--match=id:{window_id}", "enter") is None:
        _log.warning("roster-recovery: send-key enter failed for window %d", window_id)
        return False

    return True


# ---------------------------------------------------------------------------
# Per-window decision logic
# ---------------------------------------------------------------------------

async def _process_window(win: dict[str, Any]) -> None:
    """Assess a single roster window and act if appropriate."""
    window_id: int = win["window_id"]
    role: str = win["role"]

    # Guard (c): global opt-out
    if not _env_enabled():
        return

    # Guard (a): resolve session UUID — never act on window-id alone
    session_id = await asyncio.get_event_loop().run_in_executor(
        None, _resolve_session_for_role, role
    )
    if not session_id:
        _log.debug(
            "roster-recovery: no session UUID for role=%s window=%d — skip",
            role,
            window_id,
        )
        return

    # Guard (c): per-session opt-out
    if _session_opt_out(session_id):
        return

    # Read window screen
    text = await asyncio.get_event_loop().run_in_executor(None, _get_screen, window_id)
    if text is None:
        return

    context_pct = _parse_context_pct(text)
    busy = _is_busy(text)

    # -----------------------------------------------------------------------
    # Compact path: context at threshold and window not already compacting
    # -----------------------------------------------------------------------
    if context_pct is not None and context_pct >= _COMPACT_THRESHOLD:
        action_key = (window_id, "compact")
        last_attempt = _DEBOUNCE.get(action_key, 0.0)
        if time.time() - last_attempt < _COMPACT_COOLDOWN_S:
            _log.debug(
                "roster-recovery: compact debounced for window %d role=%s (%.0fs ago)",
                window_id,
                role,
                time.time() - last_attempt,
            )
            return

        # Guard (b): only compact when NOT actively running tools / compacting
        if busy:
            _log.debug(
                "roster-recovery: window %d role=%s at %d%% but busy — will retry later",
                window_id,
                role,
                context_pct,
            )
            return

        _log.info(
            "roster-recovery: window %d role=%s at %d%% — distill-then-compact",
            window_id,
            role,
            context_pct,
        )

        # Data-safety invariant: DISTILL BEFORE COMPACT (knowledge preserved first)
        await _distill_session(session_id, role)

        # Guard (b): re-check after async distill — session may have started work
        text_after = await asyncio.get_event_loop().run_in_executor(
            None, _get_screen, window_id
        )
        if text_after and _is_busy(text_after):
            _log.info(
                "roster-recovery: window %d became busy during distill — aborting compact",
                window_id,
            )
            return

        # Guard (b) + TOCTOU: inject /compact with buffer-verify before submit
        submitted = await asyncio.get_event_loop().run_in_executor(
            None, _inject_text_and_submit, window_id, "/compact"
        )
        if submitted:
            _DEBOUNCE[action_key] = time.time()
            _log.info(
                "roster-recovery: /compact submitted to window %d role=%s session=%s",
                window_id,
                role,
                session_id[:8],
            )
        return

    # -----------------------------------------------------------------------
    # Wake path: idle session with pending obligation at normal context
    # -----------------------------------------------------------------------
    if busy:
        return

    try:
        from khimaira.monitor.api.chats import _get_session_obligations
        from khimaira.monitor import sessions as sessions_mod

        obligations = await asyncio.get_event_loop().run_in_executor(
            None, _get_session_obligations, session_id
        )
        if not obligations:
            return

        rows = sessions_mod.list_sessions(use_cache=True)
        row = next((r for r in rows if r.get("session_id") == session_id), None)
        if not row:
            return
        idle_s = float(row.get("last_active_age_s") or 0)
        if idle_s < _IDLE_MIN_S:
            return  # not idle long enough

        action_key = (window_id, "wake")
        last_attempt = _DEBOUNCE.get(action_key, 0.0)
        if time.time() - last_attempt < _COMPACT_COOLDOWN_S:
            return

        wake_msg = "⏰ resume: call chat_my_chats + act on your pending task"
        submitted = await asyncio.get_event_loop().run_in_executor(
            None, _inject_text_and_submit, window_id, wake_msg
        )
        if submitted:
            _DEBOUNCE[action_key] = time.time()
            _log.info(
                "roster-recovery: wake injected to window %d role=%s session=%s",
                window_id,
                role,
                session_id[:8],
            )
    except Exception as exc:
        _log.debug("roster-recovery: wake-path error for %s: %s", role, exc)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def check_once() -> None:
    """Single sweep of all discovered roster windows."""
    if not _env_enabled():
        return
    windows = await asyncio.get_event_loop().run_in_executor(
        None, _discover_roster_windows
    )
    for win in windows:
        try:
            await _process_window(win)
        except Exception as exc:
            _log.warning(
                "roster-recovery: error on window %d role=%s: %s",
                win.get("window_id", -1),
                win.get("role", "?"),
                exc,
            )


async def watcher_loop() -> None:
    """Daemon watcher loop — started at server startup alongside _guard4_watcher."""
    _log.info(
        "roster-recovery: watcher started (threshold=%d%%, interval=%.0fs)",
        _COMPACT_THRESHOLD,
        _WATCH_INTERVAL_S,
    )
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_S)
        try:
            await check_once()
        except Exception as exc:
            _log.warning("roster-recovery: sweep error: %s", exc)
