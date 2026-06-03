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
import hashlib
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
_RATE_LIMIT_COOLDOWN_S = 600.0  # 10 min between rate-limit escalations per window
# Disk-WIP threshold: how recently a task-target file must have been modified to
# count as ALIVE-BUT-WORKING. Errs long (recoverable-default): a false-no-wake
# delay self-heals next cycle; a false-wake interrupting active work is the harm.
_WIP_THRESHOLD_S = float(os.environ.get("KHIMAIRA_WIP_THRESHOLD_S", "900"))  # 15 min

# Patterns that indicate the terminal is actively working (NOT idle).
_BUSY_MARKERS = ("esc to interrupt", "compacting…", "compacting...", "compacting ")

# Context window per model family (tokens). All current Claude models share 200k;
# Opus-1M variants use 1M. Overridable via KHIMAIRA_CONTEXT_WINDOW env var.
_CONTEXT_WINDOW_DEFAULT = 200_000
_CONTEXT_WINDOW_1M = 1_000_000

# Debounce table: (window_id, action) → last_attempt_ts
_DEBOUNCE: dict[tuple[int, str], float] = {}

# Escalation-dedupe table: window_id → content-hash of last-escalated HITL prompt.
# Prevents the same unresolved prompt from flooding master's inbox every cooldown cycle.
# Cleared when the prompt disappears (no-HITL scan cycle) so a future re-appearance
# or a changed prompt escalates fresh.
_HITL_ESCALATED: dict[int, str] = {}

# ---------------------------------------------------------------------------
# HITL auto-answering configuration
# ---------------------------------------------------------------------------

# Guard A — destructive denylist: any match → ESCALATE, never auto-answer.
_HITL_DENYLIST: list[re.Pattern[str]] = [
    re.compile(p, re.IGNORECASE) for p in [
        r"rm\s+-[rRfF]*[fF]",               # rm -rf / -fr
        r"git\s+push\s+(.*\s)?--force",      # git push --force
        r"git\s+push\s+(.*\s+)?-f\b",        # git push -f
        r"git\s+reset\s+(.*\s+)?--hard",     # git reset --hard
        r"\bDROP\s+(TABLE|DATABASE)\b",      # SQL destructive
        r"\bsudo\b",                         # sudo
        r"\bmkfs\b",                         # mkfs
        r"\bdd\b.*\bif=",                   # dd if=
        r":\(\)\s*\{[^}]*\}[^;]*;",         # fork bomb :(){};
        r"chmod\s+-R\s+777",                 # chmod -R 777
        r">\s*/dev/sd",                      # > /dev/sd*
    ]
]

# Prompt patterns that indicate a Claude Code HITL permission dialog.
_HITL_PROMPT_RE = re.compile(
    r"(Do you want|❯\s*1\.|^\s*1\.\s+(Yes|Allow|Proceed)|\(y/n\)|Continue\?|Allow this action)",
    re.IGNORECASE | re.MULTILINE,
)

# Roles that hold NO_FILE_EDIT permission; file-edit prompts on these → escalate.
_NO_FILE_EDIT_ROLES = frozenset(["analyst", "observer", "tracker"])


# ---------------------------------------------------------------------------
# Environment / opt-out
# ---------------------------------------------------------------------------

def _env_enabled() -> bool:
    """Return False if the global kill-switch is set."""
    return os.environ.get("KHIMAIRA_ROSTER_RECOVERY", "1") != "0"


def _env_auto_hitl_enabled() -> bool:
    """Return False if the HITL auto-answering kill-switch is set."""
    return os.environ.get("KHIMAIRA_AUTO_HITL", "1") != "0"


def _session_nocompact_path(session_id: str) -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "khimaira" / "sessions" / session_id / ".nocompact"


def _session_nohitl_path(session_id: str) -> Path:
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "khimaira" / "sessions" / session_id / ".nohitl"


def _session_hitl_opt_out(session_id: str) -> bool:
    return _session_nohitl_path(session_id).exists()


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
    """Run ``kitty @ <args>``; return stdout or None on any failure.

    When ``KITTY_LISTEN_ON`` is set (always the case under the daemon, which
    runs without a controlling TTY), passes ``--to=<socket>`` so kitty uses
    the IPC socket directly instead of trying to open /dev/tty.
    """
    listen = os.environ.get("KITTY_LISTEN_ON")
    if listen:
        cmd = ["kitty", "@", f"--to={listen}", *args]
    else:
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
        _log.warning("kitty @ %s failed (rc=%d): %s", args[0], r.returncode, r.stderr.strip())
        return None
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        _log.warning("kitty unavailable: %s", exc)
        return None


def _get_roster_member_ids() -> frozenset[str]:
    """Return the set of session IDs that are on THIS roster.

    Delegates to sessions.active_roster_member_ids() (the ONE canonical
    predicate per master ruling) with a fail-open empty-set fallback.
    """
    try:
        from khimaira.monitor import sessions as _sess
        fn = getattr(_sess, "active_roster_member_ids", None)
        if fn is not None:
            _log.info("roster-recovery: roster source: canonical (active_roster_member_ids)")
            return frozenset(fn())
    except Exception:
        pass
    _log.warning("roster-recovery: roster source: FALLBACK (active_roster_member_ids unavailable — fail-open, no cross-project scoping)")
    return frozenset()  # fail-open: return empty set → all windows pass filter until canonical lands


def _discover_roster_windows() -> list[dict[str, Any]]:
    """Return roster claude-chat windows as ``[{window_id, role, cmdline}]``.

    Handles both bare names (``agent-1``) and prefixed names (``jp-agent-1``,
    ``jp-frontend-lead-1``): the ``-r <name>`` value is normalized through
    ``infer_role_from_name`` so the returned ``role`` always matches what is
    stored in ``member_roles`` (e.g. ``agent``, ``jp-agent``, ``frontend-lead``).
    Windows whose name doesn't resolve to a known role are skipped.

    Cross-project scoping: only windows whose session UUID is in
    active_roster_member_ids() (this daemon's roster) are included. jp-*
    windows from other projects are excluded.
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

    # Cross-project scoping: only include sessions that are on this daemon's roster.
    # Fail-open: if the canonical predicate isn't available yet, roster_ids is empty
    # and the `if roster_ids` guard below lets ALL windows through (safe default until
    # agent-1 lands active_roster_member_ids).
    roster_ids = _get_roster_member_ids()

    # Build a name→session_id lookup for membership checks.
    _session_name_map: dict[str, str] = {}
    try:
        from khimaira.monitor import sessions as _sess_mod
        for row in _sess_mod.list_sessions(use_cache=True):
            name = row.get("name")
            sid = row.get("session_id")
            if name and sid:
                _session_name_map[name] = sid
    except Exception:
        pass

    roster: list[dict[str, Any]] = []
    for os_win in data:
        for tab in os_win.get("tabs", []):
            for win in tab.get("windows", []):
                wid = win.get("id")
                if wid is None:
                    continue
                cmdline: list[str] = [str(c) for c in (win.get("cmdline") or [])]
                joined = " ".join(cmdline)
                # Must be a claude-chat invocation.
                if "claude" not in joined:
                    continue
                # Resolve the session name. Windows launch as
                #   bash -ic "... claude-chat -r <name> ..."
                # so the -r/-n value is buried INSIDE the shell command string,
                # NOT a standalone argv token — scanning argv for `-r` never
                # matched, so this returned 0 windows and the ENTIRE watcher
                # (compaction/wake/HITL) was a silent no-op. Resolve the name from
                # two reliable signals and use whichever maps to a known role:
                #   (1) the window TITLE, which is set to the session name
                #       (e.g. "analyst-1", "jp-agent-1", "khimaira-0"); and
                #   (2) a regex pull of -r/-n <name> from the command string.
                title_name = (win.get("title") or "").strip() or None
                _m = re.search(r"claude-chat\b[^\n]*?\s-(?:r|n)\s+(\S+)", joined)
                cmd_name = _m.group(1) if _m else None

                def _resolve_role(nm: str | None) -> str | None:
                    if not nm:
                        return None
                    if infer_role_from_name is not None:
                        return infer_role_from_name(nm)
                    parts = nm.rsplit("-", 1)
                    return parts[0] if (len(parts) == 2 and parts[1].isdigit()) else nm

                # Prefer the title; fall back to the command-string name. Use
                # whichever resolves to a role so a stray title can't drop a window.
                raw_name = title_name or cmd_name
                role = _resolve_role(title_name) or _resolve_role(cmd_name)
                if not raw_name:
                    continue
                if not role:
                    continue

                # Cross-project filter: if roster_ids is populated, skip windows
                # whose session UUID is not in this roster.
                if roster_ids:
                    session_id = _session_name_map.get(raw_name)
                    if not session_id or session_id not in roster_ids:
                        _log.debug(
                            "roster-recovery: skipping window %d (%r) — not in this roster",
                            wid, raw_name,
                        )
                        continue

                roster.append({"window_id": wid, "role": role, "raw_name": raw_name, "cmdline": joined})
    return roster


def _get_screen(window_id: int) -> str | None:
    """Read current screen text for a kitty window."""
    return _kitty("get-text", f"--match=id:{window_id}")


def _compute_context_pct(session_id: str) -> int | None:
    """Return context window usage as an integer percentage (0–100), or None on failure.

    Reads the last assistant turn's usage block from the transcript JSONL.
    This is UI-independent — it works regardless of how Claude Code renders
    context in the terminal (which does NOT show "NN% context used").

    Context window detection:
    - KHIMAIRA_CONTEXT_WINDOW env override (always takes precedence)
    - High-water-mark: if the MAX observed context_tokens across the transcript
      exceeds 200k, this session has a 1M window (CC's only context tier above
      200k). A roster session at 256k definitely has a 1M window; "claude-sonnet-4-6"
      in the model string does NOT indicate window size — that is a runtime/account
      setting not encoded in the model ID.
    - Otherwise: 200k default.

    context_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    pct = round(100 * context_tokens / context_window)

    Fail-safe: returns None on any read failure so we never compact blindly.
    """
    try:
        from khimaira.monitor import sessions as _sess
        transcript = _sess._find_transcript(session_id)
        if transcript is None or not transcript.is_file():
            return None

        lines = transcript.read_text(errors="replace").splitlines()

        # First pass: compute the high-water-mark context_tokens to infer the window.
        max_ctx = 0
        last_ctx: int | None = None
        for line in lines:
            try:
                obj = json.loads(line)
            except (json.JSONDecodeError, ValueError):
                continue
            if obj.get("type") != "assistant":
                continue
            usage = (obj.get("message") or {}).get("usage")
            if not isinstance(usage, dict):
                continue
            ctx = (
                int(usage.get("input_tokens") or 0)
                + int(usage.get("cache_creation_input_tokens") or 0)
                + int(usage.get("cache_read_input_tokens") or 0)
            )
            if ctx > max_ctx:
                max_ctx = ctx
            last_ctx = ctx

        if last_ctx is None:
            return None  # no usage records found

        # Determine context window.
        # Roster sessions run Claude Code at 1M context — use 1M as the safe default.
        # Under-estimating the window causes PREMATURE compaction (irreversible data loss);
        # over-estimating means CC's own auto-compact is the backstop (recoverable).
        # Direction matters: always err toward the LARGER window.
        env_override = os.environ.get("KHIMAIRA_CONTEXT_WINDOW")
        if env_override:
            context_window = int(env_override)
        elif max_ctx > _CONTEXT_WINDOW_DEFAULT:
            # High-water-mark confirms 1M (belt-and-suspenders for non-roster sessions).
            context_window = _CONTEXT_WINDOW_1M
        else:
            # Fresh 1M session or unknown → default to 1M (safe, avoids premature compact).
            context_window = _CONTEXT_WINDOW_1M

        return round(100 * last_ctx / context_window)
    except Exception:
        return None


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
        loop = asyncio.get_running_loop()
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
# Wake-gate helpers: pending task / pending invite checks
# ---------------------------------------------------------------------------

def _session_has_pending_task(session_id: str) -> bool:
    """Return True if any chat has a task assigned to session_id with status=pending.

    Complements _get_session_obligations (which covers in-progress obligations and
    role-class review-tasks). The wake-gate ORs both, so role-class tasks are caught
    by _get_session_obligations; this function covers named-assignee pending tasks.
    Fail-open: returns False on any read error.
    """
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return False
        for chat_path in chat_dir.glob("chat-*.jsonl"):
            chat_id = chat_path.stem
            try:
                tasks: dict[str, dict] = {}
                for line in chats_mod._read(chat_id):
                    k = line.get("kind")
                    if k == chats_mod.TASK:
                        tid = line.get("id")
                        if tid:
                            tasks[tid] = {
                                "assignee_id": line.get("assignee_id"),
                                "status": line.get("status"),
                            }
                    elif k == chats_mod.TASK_UPDATE:
                        tid = line.get("task_id")
                        if tid and tid in tasks:
                            tasks[tid]["status"] = line.get("status")
                for task in tasks.values():
                    if (
                        task.get("assignee_id") == session_id
                        and task.get("status") == chats_mod.TASK_PENDING
                    ):
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _session_has_pending_invite(session_id: str) -> bool:
    """Return True if any chat has session_id in 'pending' membership state.

    Detects sessions with unaccepted invites so the watcher can prod them to
    accept. Fail-open: returns False on any read error.
    """
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return False
        for chat_path in chat_dir.glob("chat-*.jsonl"):
            chat_id = chat_path.stem
            try:
                room = chats_mod.load_room(chat_id)
                member = room["members"].get(session_id)
                if member and member.get("state") == chats_mod.PENDING:
                    return True
            except Exception:
                continue
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# HITL auto-answering helpers
# ---------------------------------------------------------------------------

# Match "⚡ Edit file: path" / "⚡ Write(...)" — the stable tool-call identifier line.
# Anchored to end of line (not greedy beyond \n) so volatile trailing chrome
# (elapsed timers, token counts, cursor noise) on the same line can't contaminate.
_HITL_TOOL_CALL_RE = re.compile(r"⚡\s+(.+?)(?:\n|$)")

# Match a "Do you want..." / "Continue?" permission question line — fallback for
# ⚡-less pure-question prompts (no tool-call header).
_HITL_QUESTION_LINE_RE = re.compile(
    r"(Do you want[^\n]+|Continue\?[^\n]*|Allow this action[^\n]*)",
    re.IGNORECASE,
)


def _prompt_content_hash(raw_block: str) -> str:
    """Hash the stable identifying content of a HITL prompt for escalation dedupe.

    Three-tier extraction — each tier is volatile-free by construction; no
    assumption about raw_block byte-stability is required:

    1. Bash prompt: _extract_bash_command() pulls the command out of Bash(<cmd>).
    2. Non-Bash tool-call prompt (⚡ Edit/Write/Read...): extracts the action line.
       Volatile trailing chrome (token counts, elapsed timers) can't follow the ⚡
       line past the first newline — anchored extraction.
    3. ⚡-less pure-question prompts: extracts the permission-question text line.
    """
    bash_cmd = _extract_bash_command(raw_block)
    if bash_cmd:
        return hashlib.md5(bash_cmd.encode(), usedforsecurity=False).hexdigest()
    m = _HITL_TOOL_CALL_RE.search(raw_block)
    if m:
        return hashlib.md5(m.group(1).strip().encode(), usedforsecurity=False).hexdigest()
    q = _HITL_QUESTION_LINE_RE.search(raw_block)
    if q:
        return hashlib.md5(q.group(1).strip().encode(), usedforsecurity=False).hexdigest()
    # Fallback: full raw_block (reached only if all extractors fail; the screen-tail
    # may carry volatile fields — use only as a last resort).
    return hashlib.md5(raw_block.encode(), usedforsecurity=False).hexdigest()


def _detect_hitl_prompt(text: str) -> dict[str, str] | None:
    """Return {raw_block, answer_key, kind} if text looks like a HITL permission dialog.

    Detection: screen contains one of the Claude Code prompt markers. The
    answer_key is the key to inject: "1" for numbered options (prefer the
    "don't ask again" option when present), "y" for yes/no dialogs.
    Returns None when no prompt is found.
    """
    if not _HITL_PROMPT_RE.search(text):
        return None

    # Prefer numbered "don't ask again / allow session" option (option 1)
    if re.search(r"❯\s*1\.", text) or re.search(r"^\s*1\.\s+(Yes|Allow|Proceed)", text, re.MULTILINE):
        answer_key, kind = "1", "numbered"
    elif re.search(r"\(y/n\)", text, re.IGNORECASE):
        answer_key, kind = "y", "yes_no"
    elif re.search(r"Continue\?", text, re.IGNORECASE):
        answer_key, kind = "y", "continue"
    else:
        # Matched the RE but kind is unrecognised → bias-to-escalate
        answer_key, kind = "1", "unknown"

    return {"raw_block": text[-600:], "answer_key": answer_key, "kind": kind}


def _check_destructive(text: str) -> str | None:
    """Guard A: return the first matching denylist snippet, or None if clean."""
    for pat in _HITL_DENYLIST:
        m = pat.search(text)
        if m:
            return m.group(0)
    return None


def _get_session_active_task_body(session_id: str) -> str | None:
    """Return the body of the most recent in-progress task assigned to this session."""
    try:
        import json
        from khimaira.monitor.chats import _CHAT_DIR, TASK_IN_PROGRESS  # type: ignore[attr-defined]

        latest_body: str | None = None
        for chat_file in sorted(_CHAT_DIR.glob("chat-*.jsonl")):
            try:
                lines = chat_file.read_text().splitlines()
            except OSError:
                continue
            tasks: dict[str, dict] = {}
            for line in lines:
                try:
                    ev = json.loads(line)
                except (json.JSONDecodeError, ValueError):
                    continue
                kind = ev.get("kind", "")
                tid = ev.get("task_id")
                if not tid:
                    continue
                if kind == "task":
                    tasks[tid] = ev
                elif kind == "task_update" and tid in tasks:
                    tasks[tid] = {**tasks[tid], "status": ev.get("status", tasks[tid].get("status"))}
            for task in tasks.values():
                if (task.get("assignee_id") == session_id
                        and task.get("status") == TASK_IN_PROGRESS):
                    latest_body = task.get("body") or ""
        return latest_body
    except Exception:
        return None


def _is_in_task_scope(prompt_text: str, task_body: str | None) -> bool:
    """Guard B: heuristic check that the prompted action is within the task's scope.

    Bias-to-escalate: returns False (escalate) whenever scope cannot be confirmed.
    """
    if not task_body:
        return False  # no task body → can't verify → escalate

    # Extract file/path references from the prompt
    path_matches = re.findall(
        r"(?:file|path|Edit|Write)\s*[:]\s*([^\s\n]+)|`([^`\n]+\.[a-z]{1,6})`",
        prompt_text, re.IGNORECASE,
    )
    prompt_paths = [m[0] or m[1] for m in path_matches if m[0] or m[1]]

    if not prompt_paths:
        return False  # no path in prompt → can't verify → escalate

    for path in prompt_paths:
        filename = path.split("/")[-1]
        if filename and len(filename) > 3 and filename in task_body:
            return True
        # Check the last 3 path components for breadth
        for part in [p for p in path.split("/") if p][-3:]:
            if len(part) > 3 and part in task_body:
                return True

    return False  # could not confirm → escalate


def _role_blocks_file_edit(role: str, prompt_text: str) -> bool:
    """Guard C: return True if role cannot perform the prompted file edit."""
    if role not in _NO_FILE_EDIT_ROLES:
        return False
    # Prompt is for a file edit?
    if re.search(r"\b(Edit|Write|MultiEdit|NotebookEdit)\b", prompt_text, re.IGNORECASE):
        return True
    if re.search(r"file.*\.(py|ts|tsx|js|jsx|yaml|yml|json|md|txt)\b", prompt_text):
        return True
    return False


def _find_master_session_for_hitl() -> str | None:
    """Return the master's session_id for HITL escalation notices."""
    try:
        from khimaira.monitor.chats import load_room, ROLE_MASTER
        room = load_room("chat-fdf7c4cbd3bd")
        member_roles: dict[str, str] = room["meta"].get("member_roles") or {}
        for sid, r in member_roles.items():
            if r == ROLE_MASTER:
                return sid
    except Exception:
        pass
    return None


def _escalate_hitl(
    window_id: int,
    session_id: str,
    role: str,
    prompt_text: str,
    reason: str,
) -> None:
    """Post a notice to master that a HITL prompt was NOT auto-answered."""
    try:
        from khimaira.monitor import sessions as sess_mod
        master_id = _find_master_session_for_hitl()
        preview = prompt_text.strip()[:200].replace("\n", " | ")
        msg = (
            f"⚠️ HITL prompt NOT auto-answered — agent {role} (window {window_id}) "
            f"reason: {reason}. "
            f"Prompt: {preview!r} — session {session_id[:8]} HOLDING; respond manually."
        )
        if master_id:
            sess_mod.post_notice(
                target_session_id=master_id,
                text=msg,
                from_session_id=session_id,
                fire_desktop_notify=True,
            )
        _log.info(
            "roster-hitl: ESCALATE window=%d role=%s session=%s reason=%r",
            window_id, role, session_id[:8], reason,
        )
    except Exception as exc:
        _log.warning("roster-hitl: escalation notice failed: %s", exc)


# --- Guard B-bypass: read-only-safe command allowlist -----------------------
#
# A HITL permission prompt for a provably non-destructive, read-only command
# (git status, ls, cat, grep without a pipe, find without -exec …) is safe to
# auto-answer regardless of task scope — it can't mutate anything. This closes
# the dominant escalation case (every benign `find`/`grep`/`git status` that
# Guard B can't match to the task body).
#
# SECURITY MODEL (per analyst-1's bug-class enumeration): a POSITIVE allowlist
# that FAILS CLOSED, never a blocklist. Auto-answer ONLY when ALL hold:
#   (a) the command extracts cleanly as a single `Bash(<cmd>)` with no nested
#       parens / newlines / box-border leakage,
#   (b) ZERO shell metacharacters (no chaining/pipe/redirect/substitution),
#   (c) the bare executable is in a tiny hardcoded read-only set
#       (git → a read-only subcommand only),
#   (d) no write-capable flags (find -exec/-delete …),
#   (e) no env-prefix (VAR=val cmd — PATH/LD_PRELOAD hijack),
#   (f) no sensitive-path read (/etc, ~/.ssh, .env, credentials …).
# Anything else → return False → fall through to the normal scope/role guards
# → escalate to human. A needless escalation is recoverable; an auto-approved
# `rm -rf` is not.
_HITL_READONLY_VERBS = frozenset({
    "ls", "cat", "head", "tail", "wc", "find", "grep", "rg", "tree", "file",
    "stat", "pwd", "echo", "which", "basename", "dirname", "realpath", "du",
})
_GIT_READONLY_SUBCMDS = frozenset({
    "status", "log", "diff", "show", "branch", "blame", "rev-parse", "ls-files",
})
# find actions that write/execute. Matched by PREFIX (not exact) so variants
# like -fprint0 / -execdir / -okdir can't slip an exact-token check.
_FIND_WRITE_PREFIXES = ("-exec", "-ok", "-fprint", "-fls")
_FIND_WRITE_EXACT = frozenset({"-delete"})
# git write-mode flags on otherwise-read subcommands: --output writes a file
# (diff/show), branch -d/-D/-m/-M/-u/-f mutate refs, -c/-C config-inject, etc.
# git is allowed ONLY when none of these (and for `branch`, no flag at all).
_GIT_WRITE_FLAGS = frozenset({
    "-o", "--output", "-d", "-D", "-m", "-M", "-u", "-f", "--force",
    "--edit-description", "--set-upstream-to", "--unset-upstream",
    "-c", "-C", "--create-reflog", "--amend",
})
# PINNED ASSUMPTIONS (analyst-1 + architect-1 adversarial review) — the
# bounded blocklist is complete-by-construction ONLY while these hold; a
# violation should re-trigger this audit, not silently widen the auto-approve:
#  1. VERSION: the allowed read-git-subcommands have `--output`/`-o` as their
#     ONLY in-flag write vector, and find's write/exec actions are the closed
#     set matched by _FIND_WRITE_PREFIXES + _FIND_WRITE_EXACT. Re-audit on a
#     git/find version bump that could add a write-flag to a read-subcommand.
#  2. REPO-TRUST: git diff/show auto-approve is safe BECAUSE the repo is
#     trusted (khimaira / jeevy_portal). In an UNTRUSTED checkout a repo-local
#     `.gitconfig` ([diff] external=evil / core.pager=cmd / GIT_EXTERNAL_DIFF)
#     executes on `git diff` — invisible to a command-string allowlist. If
#     auto-HITL ever runs over untrusted checkouts, drop git from the allowlist.
# Shell metacharacters that enable chaining / pipe / redirect / substitution /
# subshell. ANY occurrence → not provably safe → escalate. Box-border (│),
# nbsp (\xa0) and backslash mean the extraction is ambiguous → also escalate.
_HITL_UNSAFE_CHARS = ";|&<>$`(){}\\│\xa0\n\r\t"
_HITL_SENSITIVE_PATH_RE = re.compile(
    r"/etc/|/root/|\.ssh|id_rsa|id_ed25519|\.env\b|shadow|\.aws|\.bashrc|\.zshrc|"
    r"credentials|\.pgpass|secret",
    re.IGNORECASE,
)
_HITL_BASH_CMD_RE = re.compile(r"Bash\(([^()\n\r]+)\)")


def _extract_bash_command(raw_block: str) -> str | None:
    """Extract a single clean `Bash(<cmd>)` command, or None if not unambiguous.

    Requires the command to contain no nested parens / newlines (those would
    indicate substitution or a multi-line/box-wrapped render → ambiguous →
    escalate). Returns the inner command string stripped, else None.
    """
    m = _HITL_BASH_CMD_RE.search(raw_block)
    if not m:
        return None
    return m.group(1).strip()


def _is_read_only_safe(raw_block: str) -> bool:
    """Guard B-bypass: True only for a provably non-destructive read-only command.

    Fail-closed positive allowlist — see the SECURITY MODEL note above. Any
    extraction ambiguity, metacharacter, unknown executable, write-flag,
    env-prefix, or sensitive-path read returns False (→ escalate).
    """
    cmd = _extract_bash_command(raw_block)
    if not cmd:
        return False
    # (b) zero shell metacharacters / ambiguity markers
    if any(ch in cmd for ch in _HITL_UNSAFE_CHARS):
        return False
    # (f) sensitive-path read
    if _HITL_SENSITIVE_PATH_RE.search(cmd):
        return False
    tokens = cmd.split()
    if not tokens:
        return False
    exe = tokens[0]
    # (e) env-prefix (VAR=val cmd) / PATH-hijack — executable must be a bare name
    if "=" in exe or "/" in exe:
        return False
    if exe == "git":
        # (c) git allowed only with a read-only subcommand — AND flag-aware,
        # because several "read" subcommands have write modes via flags.
        if len(tokens) < 2 or tokens[1] not in _GIT_READONLY_SUBCMDS:
            return False
        sub, rest = tokens[1], tokens[2:]
        # `git branch` mutates with ANY argument (creates/deletes/renames a
        # ref); it's read-only ONLY when bare (`git branch` = list).
        if sub == "branch":
            return not rest
        # Other read subcommands: reject write-mode flags. --output / -o write a
        # file (diff/show); guard the --output=<path> form too.
        for t in rest:
            if t in _GIT_WRITE_FLAGS or t.startswith("--output="):
                return False
        return True
    if exe not in _HITL_READONLY_VERBS:
        return False
    # (d) flag-aware: find must carry no write/execute action. Prefix-matched so
    # variants (-fprint0, -execdir, -okdir) can't slip an exact-token check.
    if exe == "find":
        for t in tokens:
            if t in _FIND_WRITE_EXACT or any(t.startswith(p) for p in _FIND_WRITE_PREFIXES):
                return False
    return True


def _handle_hitl_prompt(
    window_id: int,
    session_id: str,
    role: str,
    prompt: dict[str, str],
) -> str:
    """Run Guards A/B/C/D. Inject answer or escalate. Return action taken."""
    raw = prompt["raw_block"]
    answer_key = prompt["answer_key"]
    kind = prompt["kind"]

    # Guard A: destructive denylist (hard gate — always applies)
    danger = _check_destructive(raw)
    if danger:
        _escalate_hitl(window_id, session_id, role, raw, f"destructive-marker: {danger!r}")
        return "escalated"

    # Read-only-safe allowlist: a provably non-destructive command bypasses the
    # scope (B) and role (C) checks — it can't mutate anything, so task-scope
    # and file-edit-role verification are moot. Guard A (above) and Guard D
    # (below) still apply. Fail-closed: anything not provably safe → False →
    # normal guards → escalate.
    read_only_safe = _is_read_only_safe(raw)

    # Guard B: in-task-scope (skipped for read-only-safe commands)
    if not read_only_safe:
        task_body = _get_session_active_task_body(session_id)
        if not _is_in_task_scope(raw, task_body):
            _escalate_hitl(window_id, session_id, role, raw, "out-of-scope or scope-unverifiable")
            return "escalated"

        # Guard C: role-permits (a read-only command edits nothing → moot when safe)
        if _role_blocks_file_edit(role, raw):
            _escalate_hitl(window_id, session_id, role, raw, f"role {role!r} does not permit file-edit")
            return "escalated"

    # Guard D: bias-to-escalate for unrecognised prompt kind
    if kind == "unknown":
        _escalate_hitl(window_id, session_id, role, raw, "unrecognised prompt type")
        return "escalated"

    # All guards passed — inject the answer
    submitted = _inject_text_and_submit(window_id, answer_key)
    if submitted:
        _log.info(
            "roster-hitl: ANSWERED window=%d role=%s session=%s key=%r kind=%s",
            window_id, role, session_id[:8], answer_key, kind,
        )
        return "answered"

    _log.warning(
        "roster-hitl: inject failed window=%d role=%s", window_id, role
    )
    return "skipped"


# ---------------------------------------------------------------------------
# Disk-WIP probe — hook-independent ALIVE-BUT-WORKING detection
# ---------------------------------------------------------------------------
#
# DESIGN CONSTRAINT (analyst + architect, 2026-06-03):
# All khimaira agents share ~/dev/khimaira as cwd. A bare git-status or
# workspace-scan cross-attributes — agent-A's edit shows in agent-B's WIP check.
# The ONLY per-session-precise signal is the OWED-TASK target-file mtime:
# files declared in THIS session's active chat-task → stat those mtimes.
# No owed-task or no resolvable files → no per-session signal; caller falls
# back to reachability + active-since-restart (never infer WIP from shared repo).

# Extracts file paths from a task body. Three forms:
#   1. Backtick-wrapped: `packages/foo/bar.py`
#   2. Bare package path: packages/foo/bar.py (after whitespace)
#   3. "file:" / "path:" prefix: file: packages/foo/bar.py
_TASK_FILE_PATH_RE = re.compile(
    r"`([^`\n]+\.[a-zA-Z]{1,6})`"
    r"|(?:^|\s)((?:packages?|src|apps?|tests?)/\S+\.[a-zA-Z]{1,6})"
    r"|(?:file|path)\s*[:=]\s*(\S+\.[a-zA-Z]{1,6})",
    re.IGNORECASE | re.MULTILINE,
)


def _resolve_task_target_paths(task_body: str, project_root: Path) -> list[Path]:
    """Extract candidate file paths from a task body and resolve to existing files.

    Tries each extracted path as (a) absolute and (b) relative to project_root.
    Returns only paths that exist on disk — these are the OWED-TASK target files.
    """
    found: list[Path] = []
    seen: set[str] = set()
    for m in _TASK_FILE_PATH_RE.finditer(task_body):
        raw = next(g for g in m.groups() if g)
        raw = raw.strip()  # whitespace only — preserve leading / for absolute paths
        if not raw or raw in seen or len(raw) < 4:
            continue
        seen.add(raw)
        for candidate in (Path(raw), project_root / raw):
            try:
                if candidate.is_file():
                    found.append(candidate)
                    break
            except OSError:
                continue
    return found


def _session_has_recent_wip(
    session_id: str,
    task_body: str | None,
    project_root: Path,
    threshold_s: float,
) -> bool:
    """Return True if disk evidence shows the session is actively editing.

    Checks two signals — both scoped to THIS session's owed-task target files
    (never the full shared repo):
    (a) stat mtime — catches any recent write to the target file(s).
    (b) git diff HEAD -- <target files> — catches staged/unstaged WIP on those files.

    No owed-task → returns False (no per-session signal in a shared-cwd roster).
    """
    if not task_body:
        return False

    target_files = _resolve_task_target_paths(task_body, project_root)
    if not target_files:
        _log.debug(
            "roster-recovery: disk-WIP session=%s — no resolvable target files in task body",
            session_id[:8],
        )
        return False

    cutoff = time.time() - threshold_s

    # (a) stat mtime — hook-independent, catches any write
    for p in target_files:
        try:
            if p.stat().st_mtime > cutoff:
                _log.debug(
                    "roster-recovery: disk-WIP session=%s file=%s modified %.0fs ago",
                    session_id[:8], p.name, time.time() - p.stat().st_mtime,
                )
                return True
        except OSError:
            continue

    # (b) git diff INTERSECTED with target files — catches staged/unstaged WIP on
    # these specific files without touching the full repo diff (no cross-attribution).
    try:
        result = subprocess.run(
            ["git", "diff", "--name-only", "HEAD", "--"] + [str(p) for p in target_files],
            capture_output=True, text=True, timeout=5.0, cwd=str(project_root),
        )
        if result.returncode == 0 and result.stdout.strip():
            _log.debug(
                "roster-recovery: disk-WIP session=%s git-diff shows WIP on target files",
                session_id[:8],
            )
            return True
    except Exception:
        pass

    return False


# ---------------------------------------------------------------------------
# Per-window decision logic
# ---------------------------------------------------------------------------

def _resolve_session_by_name(raw_name: str) -> str | None:
    """Resolve a window name (e.g. 'agent-2') to a session UUID.

    Uses sessions.resolve_active_session (most-recently-active, durable read)
    from Lane 3. Falls back to returning None on any failure.
    """
    try:
        from khimaira.monitor import sessions as _sess
        fn = getattr(_sess, "resolve_active_session", None)
        if fn is not None:
            return fn(raw_name)
    except Exception:
        pass
    return None


# --- Rate-limit detection (window-scanner) -----------------------------------
# A roster agent hit by a server-side rate limit (429) or the usage cap STOPS
# mid-turn and sits idle. No reliable transcript api_error record is written
# (frontend-lead's audit: the 5h cap + the "Server is temporarily limiting"
# 429 don't materialise as scannable transcript records), so the signal is the
# RENDERED error in the window. Real captured render (agent-2, 2026-06-02):
#   "⎿ API Error: Server is temporarily limiting requests (not your usage limit) · Rate limited"
# Key on CC's LITERAL error strings, NOT bare "rate limit" — that appears in
# task discussion (incl. this very feature's chatter) and would false-positive.
# \s+ (not literal spaces) between words: the window WRAPS long lines, inserting
# a newline + indent mid-phrase ("...limiting\n     requests"), so literal-space
# matching misses the real render. \s+ spans the wrap. (Caught by testing the
# actual captured render, not an assumed one — premise-vs-runtime.)
_RATE_LIMIT_RE = re.compile(
    r"Server\s+is\s+temporarily\s+limiting\s+requests"
    r"|Claude\s+usage\s+limit\s+reached"
    r"|upgrade\s+to\s+increase\s+your\s+usage\s+limit",
    re.IGNORECASE,
)


def _detect_rate_limit(text: str) -> bool:
    """True if the window scrollback shows a CC rate-limit / usage-cap error."""
    return bool(_RATE_LIMIT_RE.search(text))


# Server-side 429 specifically — distinct from the 5h personal usage cap.
# DIFFERENT CLASSES: 429 (server temporary) → stagger + retry soon;
# 5h cap → wait for reset (staggering can't help a 5h cap, wrong mitigation).
# Keys on the stable "Server is temporarily limiting requests" core substring.
# The "(not your usage limit)" tail self-discriminates from the cap but is
# treated as optional chrome — a line-wrapped render may split it.
_SERVER_429_RE = re.compile(
    r"Server\s+is\s+temporarily\s+limiting\s+requests",
    re.IGNORECASE,
)


def _detect_server_429(text: str) -> bool:
    """True if the window shows a server-side 429 (not the 5h personal usage cap).

    Use for the stagger mitigation path — keyed on the CONFIRMED render string
    from Joseph 2026-06-03: "API Error: Server is temporarily limiting requests
    (not your usage limit) · Rate limited". MUST NOT match the 5h cap render
    ("Claude usage limit reached") which needs a DIFFERENT mitigation.
    """
    return bool(_SERVER_429_RE.search(text))


def _get_screen_scrollback(window_id: int, tail_lines: int = 220) -> str | None:
    """Read a generous slice of the window's SCROLLBACK (not just the visible
    screen). A rate-limit error scrolls off-screen the moment chat messages
    arrive (observed: agent-2's error was ~180 lines above the visible tail
    after the Guard-5 flood), so the visible screen alone misses it. Returns
    the last ``tail_lines`` of the full buffer, or None on read failure."""
    full = _kitty("get-text", f"--match=id:{window_id}", "--extent=all")
    if full is None:
        return None
    lines = full.splitlines()
    return "\n".join(lines[-tail_lines:])


def _escalate_rate_limit(window_id: int, session_id: str, role: str) -> None:
    """Notify master that a roster session is rate-limited + stalled mid-turn."""
    if os.environ.get("KHIMAIRA_QUIET") == "1":
        return  # quiet mode — suppress rate-limit notices (detection still runs)
    try:
        from khimaira.monitor import sessions as sess_mod
        master_id = _find_master_session_for_hitl()
        msg = (
            f"🚦 RATE-LIMITED — {role} (window {window_id}, session {session_id[:8]}) "
            f"hit a server rate-limit / usage cap and STOPPED mid-turn (idle). "
            f"Interactive window — the daemon can't auto-resume; it RECOVERS when "
            f"the limit clears (re-prompt it) or needs a manual retry. Detected "
            f"from the window render (no reliable transcript record is written)."
        )
        if master_id:
            sess_mod.post_notice(
                target_session_id=master_id,
                text=msg,
                from_session_id=session_id,
                fire_desktop_notify=True,
            )
        _log.info(
            "roster-ratelimit: ESCALATE window=%d role=%s session=%s",
            window_id, role, session_id[:8],
        )
    except Exception as exc:
        _log.warning("roster-ratelimit: escalation notice failed: %s", exc)


async def _process_window(win: dict[str, Any]) -> None:
    """Assess a single roster window and act if appropriate."""
    window_id: int = win["window_id"]
    role: str = win["role"]
    raw_name: str = win.get("raw_name") or ""

    # Guard (c): global opt-out
    if not _env_enabled():
        return

    # Guard (a): resolve session UUID by window NAME (unique, e.g. "agent-2"),
    # not by role (ambiguous when multiple sessions share a role). This was the
    # root cause of the "ambiguous target" abort that prevented compaction/wake/HITL
    # from ever firing: with 11 sessions all role='agent', _resolve_session_for_role
    # always returned None. The window title is unique — resolve_active_session
    # resolves it to the most-recently-active UUID.
    session_id: str | None = None
    if raw_name:
        session_id = await asyncio.get_running_loop().run_in_executor(
            None, _resolve_session_by_name, raw_name
        )
        if session_id:
            _log.debug(
                "roster-recovery: resolved window %d by name %r → %s",
                window_id, raw_name, session_id[:8],
            )
    if not session_id:
        # Fallback: role-based resolution (works when only one session holds the role).
        # Log at WARNING — a silent name→role fallback is a GAP-5 regression risk:
        # if resolve_active_session returned None for a renamed/dead seat, role-resolve
        # may route to the old seat again.
        if raw_name:
            _log.warning(
                "roster-recovery: name-resolve failed for %r (window %d) — falling back "
                "to role=%r (GAP-5 regression risk if seat is dead/renamed)",
                raw_name, window_id, role,
            )
        session_id = await asyncio.get_running_loop().run_in_executor(
            None, _resolve_session_for_role, role
        )
    if not session_id:
        _log.debug(
            "roster-recovery: no session UUID for name=%r role=%s window=%d — skip",
            raw_name,
            role,
            window_id,
        )
        return

    # Guard (c): per-session opt-out
    if _session_opt_out(session_id):
        return

    # Read window screen
    text = await asyncio.get_running_loop().run_in_executor(None, _get_screen, window_id)
    if text is None:
        return

    # -----------------------------------------------------------------------
    # Rate-limit path: a rate-limited / usage-capped agent stops mid-turn and
    # sits idle. Scan the SCROLLBACK (the error scrolls off the visible screen
    # when chat messages arrive) and escalate to master. Debounced per window.
    # -----------------------------------------------------------------------
    rl_action = (window_id, "ratelimit")
    if time.time() - _DEBOUNCE.get(rl_action, 0.0) >= _RATE_LIMIT_COOLDOWN_S:
        scrollback = await asyncio.get_running_loop().run_in_executor(
            None, _get_screen_scrollback, window_id
        )
        if scrollback and _detect_rate_limit(scrollback):
            await asyncio.get_running_loop().run_in_executor(
                None, _escalate_rate_limit, window_id, session_id, role
            )
            _DEBOUNCE[rl_action] = time.time()

    # -----------------------------------------------------------------------
    # HITL path: detect permission dialog and auto-answer or escalate (first)
    # -----------------------------------------------------------------------
    if _env_auto_hitl_enabled() and not _session_hitl_opt_out(session_id):
        hitl_prompt = _detect_hitl_prompt(text)
        if hitl_prompt is not None:
            action_key = (window_id, "hitl")
            last_attempt = _DEBOUNCE.get(action_key, 0.0)
            if time.time() - last_attempt >= _COMPACT_COOLDOWN_S:
                # Escalation-dedupe: only send the master notice ONCE per unique
                # prompt per window. A changed prompt (new content) re-escalates;
                # a cleared prompt (no-HITL cycle) resets the marker.
                content_hash = _prompt_content_hash(hitl_prompt["raw_block"])
                if content_hash == _HITL_ESCALATED.get(window_id):
                    _log.debug(
                        "roster-hitl: dedupe skip window=%d role=%s (prompt unchanged, already escalated)",
                        window_id, role,
                    )
                else:
                    result = await asyncio.get_running_loop().run_in_executor(
                        None, _handle_hitl_prompt, window_id, session_id, role, hitl_prompt
                    )
                    _DEBOUNCE[action_key] = time.time()
                    if result == "escalated":
                        _HITL_ESCALATED[window_id] = content_hash
                    else:
                        _HITL_ESCALATED.pop(window_id, None)
                    _log.info(
                        "roster-hitl: %s window=%d role=%s session=%s",
                        result, window_id, role, session_id[:8],
                    )
            return  # HITL prompt present — don't compact/wake until it's cleared
        else:
            # No HITL prompt this cycle — clear dedupe marker so a future
            # re-appearance or changed prompt escalates fresh.
            _HITL_ESCALATED.pop(window_id, None)

    context_pct = await asyncio.get_running_loop().run_in_executor(
        None, _compute_context_pct, session_id
    )
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
        text_after = await asyncio.get_running_loop().run_in_executor(
            None, _get_screen, window_id
        )
        if text_after and _is_busy(text_after):
            _log.info(
                "roster-recovery: window %d became busy during distill — aborting compact",
                window_id,
            )
            return

        # Guard (b) + TOCTOU: inject /compact with buffer-verify before submit
        submitted = await asyncio.get_running_loop().run_in_executor(
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

        obligations = await asyncio.get_running_loop().run_in_executor(
            None, _get_session_obligations, session_id
        )
        has_pending_task = await asyncio.get_running_loop().run_in_executor(
            None, _session_has_pending_task, session_id
        )
        has_pending_invite = await asyncio.get_running_loop().run_in_executor(
            None, _session_has_pending_invite, session_id
        )
        if not obligations and not has_pending_task and not has_pending_invite:
            return

        rows = sessions_mod.list_sessions(use_cache=True)
        row = next((r for r in rows if r.get("session_id") == session_id), None)
        if not row:
            return
        idle_s = float(row.get("last_active_age_s") or 0)
        if idle_s < _IDLE_MIN_S:
            return  # not idle long enough

        # Disk-WIP probe — ALIVE-BUT-WORKING guard (hook-independent).
        # Checks owed-task target-file mtimes + git-diff intersection; does NOT
        # use bare git-status / workspace-scan (cross-attributes in shared-cwd roster).
        #
        # Path.cwd() is correct for THIS khimaira roster (all seats share ~/dev/khimaira).
        # CROSS-PROJECT follow-up: project_root is a per-session attribute; for a jp seat
        # in a different repo it should be resolved from the session's recorded cwd
        # (sessions.get_workspace), not the daemon's cwd — else relative task-target paths
        # resolve against the wrong repo → probe returns False → false-wake.
        # (architect msg-fa3ba046b93a, analyst criterion-4 — 2026-06-03)
        task_body_for_wip = await asyncio.get_running_loop().run_in_executor(
            None, _get_session_active_task_body, session_id
        )
        has_wip = await asyncio.get_running_loop().run_in_executor(
            None,
            _session_has_recent_wip,
            session_id,
            task_body_for_wip,
            Path.cwd(),
            _WIP_THRESHOLD_S,
        )
        if has_wip:
            # Session is editing-but-SSE-deaf: last_active is stale but task-target
            # files are fresh. Do NOT inject a wake — it would interrupt live work.
            _log.info(
                "roster-recovery: skip wake window=%d role=%s session=%s "
                "(idle=%.0fs but disk-WIP on task targets — ALIVE-BUT-WORKING)",
                window_id, role, session_id[:8], idle_s,
            )
            return

        # Rate-limited: if rate-limit escalation fired recently for this window,
        # the session can't act on a wake injection — skip to avoid a futile attempt.
        if time.time() - _DEBOUNCE.get((window_id, "ratelimit"), 0.0) < _RATE_LIMIT_COOLDOWN_S:
            _log.debug(
                "roster-recovery: skip wake window=%d role=%s — rate-limited recently",
                window_id, role,
            )
            return

        action_key = (window_id, "wake")
        last_attempt = _DEBOUNCE.get(action_key, 0.0)
        if time.time() - last_attempt < _COMPACT_COOLDOWN_S:
            return

        wake_msg = "⏰ resume: call chat_my_chats + act on your pending task"
        submitted = await asyncio.get_running_loop().run_in_executor(
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
    windows = await asyncio.get_running_loop().run_in_executor(
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
