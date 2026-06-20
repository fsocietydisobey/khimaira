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
# Space consecutive wake injections so many SSE-deaf seats waking at once don't
# burst the Anthropic API into a "Server is temporarily limiting requests" 429.
# Mirrors the assign->BEGIN dispatch stagger (chats._DISPATCH_STAGGER_S). 0 disables.
_WAKE_STAGGER_S = float(os.environ.get("KHIMAIRA_WAKE_STAGGER_S", "2.5"))
_COMPACT_COOLDOWN_S = 300.0  # 5 min between compact/wake attempts per window
_RATE_LIMIT_COOLDOWN_S = 600.0  # 10 min between rate-limit escalations per window
# Disk-WIP threshold: how recently a task-target file must have been modified to
# count as ALIVE-BUT-WORKING. Errs long (recoverable-default): a false-no-wake
# delay self-heals next cycle; a false-wake interrupting active work is the harm.
_WIP_THRESHOLD_S = float(os.environ.get("KHIMAIRA_WIP_THRESHOLD_S", "900"))  # 15 min
# Clock-skew epsilon for the unconsumed-chat signal: a message ts (daemon ISO clock)
# is compared against last_active (filesystem mtime clock). Require the message to be
# at least this many seconds newer so a borderline-equal can't false-fire.
_TS_SKEW_EPSILON_S = float(os.environ.get("KHIMAIRA_TS_SKEW_EPSILON_S", "2.0"))

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

# Human-interface roles: the seats a HUMAN drives directly (intake = the user's
# entry point into the roster; master = the orchestrator the user often pilots).
# The watchdog NEVER auto-actuates these (no compact / wake / HITL inject) — the
# human manages their own context + answers their own prompts. Auto-compacting the
# user's interface window (muther-intake at 88-92%) was the 2026-06-18 incident:
# even when the user is briefly tabbed away, /compact keystrokes land in the window
# they type into. Agents stay fully auto-managed (that's muther ISSUE 2). Override
# with KHIMAIRA_ROSTER_ACTUATE_INTERFACE=1 for fully-autonomous (no-human) rosters.
_HUMAN_INTERFACE_ROLES = frozenset(["intake", "master"])


# ---------------------------------------------------------------------------
# Environment / opt-out
# ---------------------------------------------------------------------------

def _env_enabled() -> bool:
    """Return False if the global kill-switch is set."""
    return os.environ.get("KHIMAIRA_ROSTER_RECOVERY", "1") != "0"


def _env_focus_inject_allowed() -> bool:
    """Return True only if injecting into the user-FOCUSED window is explicitly
    allowed (default False). The human-presence guard skips the focused window so
    the watchdog never types under the user's cursor; set
    KHIMAIRA_ROSTER_INJECT_FOCUSED=1 to override (e.g. headless/test rosters with
    no human present)."""
    return os.environ.get("KHIMAIRA_ROSTER_INJECT_FOCUSED", "0") == "1"


def _env_actuate_interface_allowed() -> bool:
    """Return True only if auto-actuating human-interface roles (intake/master) is
    explicitly allowed (default False). Set KHIMAIRA_ROSTER_ACTUATE_INTERFACE=1 for
    fully-autonomous rosters with no human driving the intake/master seats."""
    return os.environ.get("KHIMAIRA_ROSTER_ACTUATE_INTERFACE", "0") == "1"


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
                    if infer_role_from_name is None:
                        parts = nm.rsplit("-", 1)
                        return (
                            parts[0]
                            if (len(parts) == 2 and parts[1].isdigit())
                            else nm
                        )
                    # Try the full name first (registered prefixed leads like
                    # jp-frontend-lead resolve directly via the themis registry).
                    # Then progressively strip leading "<prefix>-" segments and
                    # retry, so a prefixed-roster WORKER window (muther-critic-1,
                    # jp-agent-1) whose prefix is NOT a registry role still resolves
                    # to its base role (critic, agent). infer_role_from_name only
                    # strips the trailing -<n>, leaving "muther-critic" → None — so
                    # WITHOUT this, discovery dropped the ENTIRE prefixed roster
                    # (every window → role=None → `if not role: continue`) and the
                    # watchdog never woke a single muther-*/jp-* worker. Confirmed
                    # audit-grade 2026-06-18: _discover_roster_windows returned 0 for
                    # the live muther roster though all 10 seats were in-scope.
                    # (muther ISSUE 1/2 Path A; roster_ids filter below still scopes
                    # to THIS daemon's roster, so prefix-tolerance can't cross rosters.)
                    candidate = nm
                    while candidate:
                        resolved = infer_role_from_name(candidate)
                        if resolved:
                            return resolved
                        _head, _sep, _tail = candidate.partition("-")
                        if not _sep:
                            return None
                        candidate = _tail

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

                roster.append({
                    "window_id": wid,
                    "role": role,
                    "raw_name": raw_name,
                    "cmdline": joined,
                    # Human-presence signal: the window the user is currently
                    # focused on. NEVER inject (compact/wake/hitl) into it — doing
                    # so types keystrokes under the user's cursor + scrolls them
                    # out of place (muther-intake incident 2026-06-18, when Path A
                    # first made human-driven windows discoverable).
                    "is_focused": bool(win.get("is_focused")),
                })
    return roster


def _window_for_session_name(name: str) -> dict[str, Any] | None:
    """Find ONE kitty window for session `name`, UNSCOPED by roster.

    For TARGETED wakes where the exact target session is known. Unlike
    `_discover_roster_windows()` — which is cross-project roster-scoped and
    returns NOTHING for a session on a different roster — this searches every
    window by name. That roster-scoping is correct for "list MY roster" but
    wrong for "wake THIS specific session": with one daemon serving multiple
    rosters, a gate-complete wake for another roster's master found 0 windows
    and silently skipped (muther note-2: dual-verdict tasks never committed).

    Matches the window title (stripping kitty's leading activity/bell markers
    like "✳ " that break exact title-match) OR a `-n/-r <name>` token in the
    window cmdline (drift-proof — set at launch).
    """
    raw = _kitty("ls")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    target = (name or "").strip()
    if not target:
        return None
    for os_win in data:
        for tab in os_win.get("tabs", []):
            for win in tab.get("windows", []):
                wid = win.get("id")
                if wid is None:
                    continue
                title = (win.get("title") or "").strip()
                title_clean = title.lstrip("✳🔔★*• ").strip()
                cmdline = " ".join(str(c) for c in (win.get("cmdline") or []))
                m = re.search(r"\s-(?:r|n)\s+(\S+)", cmdline)
                cmd_name = m.group(1) if m else None
                if target in (title, title_clean, cmd_name):
                    return {"window_id": wid, "raw_name": target, "cmdline": cmdline}
    return None


def _roster_role_map() -> dict[str, Any]:
    """session_id → role across all active chat rooms (member_roles).

    The reverse of ``_resolve_session_for_role``: a forward sid→role lookup so a
    registered window can be synthesized with the role its session holds. Last
    writer wins on the rare cross-chat collision (a session in two rooms).
    """
    out: dict[str, Any] = {}
    try:
        from khimaira.monitor import chats as chats_mod

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return out
        for chat_path in chat_dir.glob("chat-*.jsonl"):
            try:
                room = chats_mod.load_room(chat_path.stem)
                member_roles: dict[str, str] = room["meta"].get("member_roles") or {}
                for sid, r in member_roles.items():
                    if r:
                        out[sid] = r
            except Exception:
                continue
    except Exception:
        pass
    return out


def _union_registered_windows(
    windows: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Add identity-registered windows the title-discovery pass missed.

    For each roster member that registered a window_id (POST /sessions/{id}/window)
    whose window is LIVE in kitty but was NOT found by title (non-role-shaped title),
    synthesize a ``win`` dict so the wake reaches it by IDENTITY. This finishes task
    #16 for the wake path: title-discovery drops any window whose title doesn't
    resolve to a role, so a legitimately-roled member named e.g. "livyatan" was
    silently unwakeable.

    ADDITIVE: title-discovered windows are untouched; only previously-invisible
    registered windows are appended (deduped by window_id) — so the live watchdog's
    behavior on every currently-wakeable window is unchanged.

    Two hard guards (kitty window_ids renumber on restart, so a registered wid can go
    stale or be reused):
      * LIVENESS — only synthesize for a wid that is present in this sweep's
        ``kitty @ ls``; a stale wid must never be woken.
      * is_focused — carried from that same ls pass so the human-presence /
        human-interface guards in ``_process_window`` apply to registered windows
        identically (never inject into Joseph's focused window).
    """
    try:
        from khimaira.monitor import sessions as _sess_mod
    except Exception:
        return windows

    roster_ids = _get_roster_member_ids()
    if not roster_ids:
        return windows  # fail-open: no canonical roster → don't synthesize

    discovered_wids = {w.get("window_id") for w in windows}

    # One kitty-ls pass → live window_id → window json (for is_focused). A registered
    # wid absent here is stale (window closed / kitty renumbered) and is skipped.
    live: dict[int, dict[str, Any]] = {}
    raw = _kitty("ls")
    if raw:
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            data = []
        for os_win in data:
            for tab in os_win.get("tabs", []):
                for w in tab.get("windows", []):
                    wid = w.get("id")
                    if wid is not None:
                        live[wid] = w

    role_by_sid = _roster_role_map()
    name_by_sid: dict[str, str] = {}
    try:
        for row in _sess_mod.list_sessions(use_cache=True):
            sid = row.get("session_id")
            nm = row.get("name")
            if sid and nm:
                name_by_sid[sid] = nm
    except Exception:
        pass

    added = 0
    for sid in roster_ids:
        wid = _sess_mod.get_session_window(sid)
        if wid is None:
            continue
        if wid in discovered_wids:
            continue  # already found by title — dedup, no double-wake
        live_win = live.get(wid)
        if live_win is None:
            _log.debug(
                "roster-recovery: registered window %s for %s is stale (not live) — skip",
                wid, sid[:8],
            )
            continue  # LIVENESS gate: stale / renumbered wid
        role = role_by_sid.get(sid)
        if not role:
            continue  # not a roled member → nothing to wake it as
        windows.append({
            "window_id": wid,
            "role": role,
            "raw_name": name_by_sid.get(sid) or sid,
            "session_id": sid,            # carried → _process_window resolves directly
            "cmdline": "",
            "is_focused": bool(live_win.get("is_focused")),
            "registered": True,
        })
        discovered_wids.add(wid)
        added += 1
    if added:
        _log.info(
            "roster-recovery: union added %d identity-registered window(s) the title "
            "pass missed", added,
        )
    return windows


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

def _title_match_arg(window_title: str) -> str:
    """Build an EXACT-match kitty ``--match=title:`` arg.

    kitty's ``title:`` query is an *unanchored regular expression* (re.search),
    so a bare ``title:agent-1`` is a SUBSTRING match — it also hits
    ``muther-agent-1`` and every other ``*-agent-1`` twin. That substring
    behavior cross-nudged a sister roster on 2026-06-07 (khimaira-0's manual
    nudge woke all 12 muther-* windows). Anchor with ``^...$`` and escape the
    title so only the exact window matches.
    """
    return f"--match=title:^{re.escape(window_title)}$"


# ---------------------------------------------------------------------------
# Duplicate-window reaping (2026-06-11) — the title-match substrate fix.
#
# The ENTIRE roster-coordination substrate is title-anchored kitty matching
# (manual nudges, busy/state reads, AND the daemon's auto-wake/dispatch-wake).
# A session restart/resume can spawn a fresh role-titled window WITHOUT closing
# the prior one, leaving TWO windows with the identical title. The anchored
# ^...$ regex (the cross-roster fix) guards substring bleed but does NOTHING
# against an EXACT duplicate — both match. Result: send-text injects into both
# (garbling the live agent), get-text reads one NON-DETERMINISTICALLY (stale
# shell vs live), nudges "don't land", busy-checks misclassify, and master can
# watch a dead shell while the live agent files its verdict elsewhere
# (compounding the commit-miss gap). muther filed this 2026-06-11; Joseph: "huge".
#
# Fix: reap the STALE duplicate so each title resolves to exactly one window.
# Live-vs-stale is read from kitty's foreground_processes: a live agent window
# has the `claude` binary running in the foreground; a stale shell (after the
# agent exited + `exec bash`) does not. Reap only when exactly one live window
# remains (safe); on ambiguity (0 or ≥2 live), loud-log and reap NOTHING.
# ---------------------------------------------------------------------------

def _window_is_live(win: dict[str, Any]) -> bool:
    """True if the kitty window has a running `claude` agent in its foreground
    processes (vs a stale shell left after the agent exited)."""
    for proc in win.get("foreground_processes") or []:
        cmd = proc.get("cmdline") or []
        if cmd and os.path.basename(str(cmd[0])) == "claude":
            return True
    return False


def _reap_duplicate_windows() -> int:
    """Reap stale duplicate-titled windows so title-match is deterministic.

    Returns the number of windows reaped. Safe-by-construction: a title's stale
    window(s) are closed ONLY when exactly one LIVE window remains for that
    title. Ambiguous cases (no live, or multiple live) are loud-logged and left
    untouched — never guess which to close. Fail-open (kitty errors → 0).
    """
    raw = _kitty("ls")
    if not raw:
        return 0
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return 0

    by_title: dict[str, list[dict[str, Any]]] = {}
    for os_win in data:
        for tab in os_win.get("tabs", []):
            for win in tab.get("windows", []):
                title = (win.get("title") or "").strip()
                if title:
                    by_title.setdefault(title, []).append(win)

    reaped = 0
    for title, wins in by_title.items():
        if len(wins) < 2:
            continue
        live = [w for w in wins if _window_is_live(w)]
        stale = [w for w in wins if not _window_is_live(w)]
        if len(live) == 1 and stale:
            for w in stale:
                if _kitty("close-window", f"--match=id:{w['id']}") is not None:
                    reaped += 1
                    _log.warning(
                        "roster-recovery: reaped STALE duplicate window id=%d "
                        "title=%r (kept live id=%d)",
                        w["id"], title, live[0]["id"],
                    )
        else:
            _log.error(
                "roster-recovery: AMBIGUOUS duplicate title %r — %d window(s), "
                "%d live; NOT reaping. Title-match is unreliable for this role "
                "until resolved (map by session_id, or close one manually).",
                title, len(wins), len(live),
            )
    return reaped


def _count_title_windows(window_title: str) -> int:
    """Count live kitty windows whose title exactly matches. -1 if kitty
    can't answer (caller treats unknown conservatively)."""
    raw = _kitty("ls", _title_match_arg(window_title))
    if raw is None:
        return -1
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return -1
    return sum(len(t.get("windows", [])) for o in data for t in o.get("tabs", []))


def _inject_text_and_submit(window_id: int, text: str, window_title: str = "") -> bool:
    """Inject ``text`` into a kitty window and submit with Enter.

    Prefers title-match (``--match=title:<window_title>``) over id-match when
    ``window_title`` is provided.  Title-match is stable across restarts (window
    IDs renumber; titles don't) and fails loudly on no-match (rc != 0) instead
    of silently no-oping like ``--match=id:<dead>`` (rc=0, undetected until the
    TOCTOU check fires).  Id-match is kept as the fallback for callers that only
    know the window id.

    Steps:
    1. Clear any pre-existing input with Ctrl-U (line-kill) so stale nudge text
       from a prior aborted cycle doesn't pollute the TOCTOU check.
    2. Send the text + a unique nonce (does NOT submit yet).
    3. POLL the buffer (up to ~1.8s) until the nonce echoes — accommodates slow
       re-render on saturated high-context windows where a single fixed-delay read
       would race the render and false-abort.
    4. Verify the nonce is present (TOCTOU guard). Abort + clear (Ctrl-C) if it
       never appears within the poll ceiling.
    5. Submit with Enter.

    Returns True if submitted, False if aborted.
    """
    # Duplicate-title guard (2026-06-11): a title-match inject into a duplicated
    # title hits BOTH windows (garbling the live agent). If >1 window shares this
    # title, self-heal once (reap the stale duplicate) then re-check; if STILL
    # ambiguous, ABORT loud rather than inject into the wrong/both windows.
    # Resolve the actuation match-arg. Prefer title-match (stable across restarts —
    # window ids renumber, role titles don't), but kitty decorates LIVE window titles
    # with dynamic activity markers (e.g. "✳ livyatan", a bell glyph, a thinking-state
    # symbol) that an anchored ^name$ title-match cannot match. When that happens the
    # title-match SILENTLY no-ops (rc=0, keystrokes go nowhere) and every wake aborts on
    # the nonce check — the livyatan window-978 failure (2026-06-20): the union path
    # passed the clean session name "livyatan" while the real kitty title was
    # "✳ livyatan". The marker can also FLICKER on/off between discovery and inject for
    # role-named windows. So branch on how many live windows the title actually matches:
    #   n==1 → title-match (precise + restart-stable; the normal case)
    #   n==0 → the (possibly decoration-prefixed/flickering) title matches no live
    #          window; fall back to the id we resolved THIS sweep (fresh, not stale). If
    #          the id is ALSO dead, the nonce TOCTOU catches it — safe either way.
    #   n>1  → ambiguous duplicate title; reap the stale twin, re-check, abort if still
    #          ambiguous rather than inject into the wrong/both windows.
    use_title = False
    if window_title:
        n = _count_title_windows(window_title)
        if n > 1:
            _reap_duplicate_windows()
            n = _count_title_windows(window_title)
        if n > 1:
            _log.error(
                "roster-recovery: REFUSING inject into %r — %d windows share "
                "this title (ambiguous after reap). Nudge/wake skipped.",
                window_title, n,
            )
            return False
        use_title = n == 1
        if n == 0:
            _log.info(
                "roster-recovery: title %r matches no live window (kitty title "
                "decoration / marker flicker?) — falling back to id-match window %d",
                window_title, window_id,
            )

    match_arg = (
        _title_match_arg(window_title) if use_title else f"--match=id:{window_id}"
    )
    id_match = f"--match=id:{window_id}"

    # Step 1: clear any stale input (Ctrl-U = Unix line-kill; clears current
    # input line in Claude Code's TUI without affecting conversation history).
    # This prevents accumulated nudge text from prior aborted cycles from causing
    # TOCTOU false-positives (observed: 6× repeat accumulation in muther roster).
    _kitty("send-key", id_match, "ctrl+u")
    time.sleep(0.05)

    # Step 2: send text WITH a unique verification nonce appended. The nonce makes the
    # TOCTOU check robust to a SATURATED window — a long-running, high-context session
    # showing the task panel + "/clear to save Nk tokens" footer (the COMMON state for
    # exactly the long-lived sessions that idle and need waking). In that layout the
    # input line (carrying the wrapped wake text) is pushed far ABOVE the last screen
    # lines, so scanning only the last 15 lines for the static wake text misses it and
    # aborts — the real cause of the 520k-token-window wake failure (2026-06-20). The
    # nonce appears ONLY in the just-injected input (a prior wake used a DIFFERENT
    # nonce, and submitted wakes in scrollback carry old nonces), so we can scan the
    # WHOLE buffer for it with no stale false-positive. Cost: a small "(sync wkNNNNN)"
    # marker rides along in the submitted prompt — the agent ignores it.
    nonce = f"wk{int(time.time()) % 100000:05d}"
    inject = f"{text}  (sync {nonce})"
    if _kitty("send-text", match_arg, "--", inject) is None:
        _log.warning(
            "roster-recovery: send-text failed for window %d (%s)", window_id, match_arg
        )
        return False

    # Step 3+4: TOCTOU — POLL the buffer until the nonce echoes, up to a bound.
    # A single fixed sleep+read fails on SATURATED windows (the 520k-token, task-panel
    # sessions that are exactly the ones idling and needing a wake): at high context the
    # Claude Code TUI re-renders slowly, so the injected nonce hasn't been painted into
    # the screen buffer within 0.15s — the single read captures the PRE-inject screen,
    # finds no nonce, and aborts. The send-text DID land (manual nudges to the same
    # window succeed); only the readback raced the render. Polling accommodates both
    # regimes: a fast normal window passes on iteration 1 (~0.1s); a slow high-context
    # window gets up to ~1.8s for the echo to appear. (2026-06-20 — the real fix for the
    # livyatan window-978 wake failures that the nonce/whole-buffer change alone didn't
    # close, because the bug was a render-vs-readback TIMING race, not a scan-scope miss.)
    _NORM_STRIP = re.compile(r"[\s>│▌❯|]+")
    buffer: str | None = None
    last_read_failed = False
    for _ in range(12):  # 12 × 0.15s ≈ 1.8s ceiling
        time.sleep(0.15)
        buffer = _get_screen(window_id)
        if buffer is None:
            last_read_failed = True
            continue
        last_read_failed = False
        # Scan the WHOLE captured buffer (whitespace + prompt-box glyphs stripped, so a
        # wrapped inject reconstructs) for the unique nonce. Whole-buffer (not
        # last-15-lines) is what makes this robust to the saturated/task-panel layout;
        # the nonce is what makes whole-buffer scanning SAFE — only the current inject
        # carries it, so no stale submitted-wake in scrollback can false-positive.
        if nonce in _NORM_STRIP.sub("", buffer):
            break
    else:
        # Nonce never echoed within the poll ceiling — abort safely.
        _kitty("send-key", id_match, "ctrl+c")
        if last_read_failed or buffer is None:
            _log.warning(
                "roster-recovery: TOCTOU verify read failed for window %d — aborted",
                window_id,
            )
        else:
            lines = [ln.rstrip() for ln in buffer.splitlines() if ln.strip()]
            _log.warning(
                "roster-recovery: TOCTOU mismatch on window %d — nonce %s not found in "
                "buffer after poll (last line %r), aborted",
                window_id,
                nonce,
                lines[-1] if lines else "",
            )
        return False

    # Step 5: submit
    if _kitty("send-key", match_arg, "enter") is None:
        _log.warning(
            "roster-recovery: send-key enter failed for window %d (%s)", window_id, match_arg
        )
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


def _iso_to_epoch(ts: str | None) -> float:
    """Parse a daemon ISO-8601 timestamp to epoch seconds; 0.0 if unparseable."""
    if not ts:
        return 0.0
    try:
        from datetime import datetime

        return datetime.fromisoformat(ts).timestamp()
    except Exception:
        return 0.0


def _session_has_unread_inbox(session_id: str) -> bool:
    """Return True if the session has >=1 unread inbox note (peek — never marks read).

    Healthy/active sessions drain their inbox each turn (pending_notes mark_read, or
    the 3-surface auto-expire), so this is empty on a quiet roster and non-zero only
    on an SSE-deaf idle session with notices / handoffs piling up unread. Covers
    session_post_notice / post_handoff. Fail-open: False on any read error.
    """
    try:
        from khimaira.monitor import sessions as sessions_mod

        return bool(sessions_mod.pending_notes(session_id, mark_read=False))
    except Exception:
        return False


def _session_has_unconsumed_chat(session_id: str) -> bool:
    """Return True if an inbound chat message arrived AFTER the session's last action.

    Premise-correct peer-reply signal. An idle session keeps its SSE subscriber
    alive, so the chat cursor (which advances on SSE DELIVERY, chats.py event
    generator) can't distinguish "delivered" from "acted on" — it lags only if the
    SSE physically disconnects. Instead compare each message's ts against the
    session's last_active mtime (the last OBSERVABLE action): chat receipt writes
    the CHAT dir (messages + cursors.jsonl), never the session dir, so last_active
    is not polluted by delivery and the signal self-clears on the session's next turn.

    Excludes self-sent + SYSTEM messages. Clock-skew safe: message ts (daemon ISO)
    and last_active (filesystem mtime) are two clocks, so require ts > last_active +
    _TS_SKEW_EPSILON_S. Fail-open: False on any read error.
    """
    try:
        from khimaira.monitor import chats as chats_mod
        from khimaira.monitor import sessions as sessions_mod

        summary = sessions_mod.summary(session_id)
        last_active_epoch = time.time() - float(summary.get("last_active_age_s") or 0.0)
        threshold = last_active_epoch + _TS_SKEW_EPSILON_S

        try:
            sid = chats_mod._resolve_or_uuid(session_id)
        except Exception:
            sid = session_id

        chat_dir = chats_mod._chat_dir()
        if not chat_dir.exists():
            return False
        for chat_path in chat_dir.glob("chat-*.jsonl"):
            try:
                room = chats_mod.load_room(chat_path.stem)
                member = room["members"].get(sid)
                if not member or member.get("state") != chats_mod.ACCEPTED:
                    continue
                for m in room.get("messages", []):
                    if m.get("kind") != chats_mod.MSG:
                        continue
                    sender = m.get("sender_id")
                    if sender == sid or sender == chats_mod.SYSTEM_SENDER_ID:
                        continue
                    if _iso_to_epoch(m.get("ts")) > threshold:
                        return True
            except Exception:
                continue
        return False
    except Exception:
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
    window_title: str = "",
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
    submitted = _inject_text_and_submit(window_id, answer_key, window_title)
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
    # Identity-registered windows (from _union_registered_windows) carry their sid
    # directly — skip the fragile name→uuid resolution for them. Title-discovered
    # windows lack the key (win.get → None), so they fall through to the existing
    # name/role resolution UNCHANGED (additive).
    session_id: str | None = win.get("session_id")
    if not session_id and raw_name:
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

    # Guard (HUMAN-PRESENCE): never inject into the window the user is focused on.
    # is_focused (from kitty @ ls, captured at discovery this sweep) means the user
    # is actively looking at / typing in this window — injecting /compact or a wake
    # prompt would land keystrokes under their cursor and scroll them out of place.
    # This skips ALL actuation (compact/wake/HITL) for the focused window; it stays
    # eligible the moment the user tabs away. (muther-intake incident 2026-06-18 —
    # Path A first made human-driven roster windows discoverable.) NOTE: do NOT key
    # this on "auto mode on" — roster agents run in auto-accept mode by design, so
    # that would disable the watchdog for every agent.
    if win.get("is_focused") and not _env_focus_inject_allowed():
        _log.debug(
            "roster-recovery: skip window %d role=%s — user-focused (human present)",
            window_id, role,
        )
        return

    # Guard (HUMAN-INTERFACE): never auto-actuate the seats a human drives directly
    # (intake = user's entry point; master = orchestrator). Unlike the focused guard,
    # this holds even when the user is briefly tabbed away — the user's interface
    # window must not be compacted/woken behind their back. (muther-intake at 92%
    # kept getting /compact'd whenever the user looked at another window.)
    if role in _HUMAN_INTERFACE_ROLES and not _env_actuate_interface_allowed():
        _log.debug(
            "roster-recovery: skip window %d role=%s — human-interface seat "
            "(user manages own context)",
            window_id, role,
        )
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
                        None, _handle_hitl_prompt, window_id, session_id, role, hitl_prompt, raw_name
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
            None, _inject_text_and_submit, window_id, "/compact", raw_name
        )
        # STORM GUARD: cool down on EVERY attempt, success or abort. A failed
        # actuation (TOCTOU mismatch / transient-busy) previously left the debounce
        # unset, so it retried every 60s sweep — the muther-intake storm 2026-06-18.
        # Cooling down on abort converts "retry every sweep" → "retry every cooldown".
        _DEBOUNCE[action_key] = time.time()
        if submitted:
            _log.info(
                "roster-recovery: /compact submitted to window %d role=%s session=%s",
                window_id,
                role,
                session_id[:8],
            )
        else:
            _log.debug(
                "roster-recovery: /compact actuation aborted (TOCTOU/busy) window %d "
                "role=%s — cooled down, will retry after cooldown not next sweep",
                window_id,
                role,
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
        has_unread_inbox = await asyncio.get_running_loop().run_in_executor(
            None, _session_has_unread_inbox, session_id
        )
        has_unconsumed_chat = await asyncio.get_running_loop().run_in_executor(
            None, _session_has_unconsumed_chat, session_id
        )
        if (
            not obligations
            and not has_pending_task
            and not has_pending_invite
            and not has_unread_inbox
            and not has_unconsumed_chat
        ):
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

        wake_msg = "⏰ resume: call chat_my_chats + act on your inbox / pending work"
        submitted = await asyncio.get_running_loop().run_in_executor(
            None, _inject_text_and_submit, window_id, wake_msg, raw_name
        )
        # STORM GUARD: cool down on EVERY attempt, success or abort (see compact
        # path). A wake that TOCTOU-aborts (e.g. the target window re-rendered)
        # must NOT retry every 60s sweep — it cools down like a successful one.
        _DEBOUNCE[action_key] = time.time()
        if submitted:
            _log.info(
                "roster-recovery: wake injected to window %d role=%s session=%s",
                window_id,
                role,
                session_id[:8],
            )
            # Stagger: pause before the sweep moves to the next window so multiple
            # SSE-deaf seats don't all hit the API simultaneously on wake (429 burst).
            # Only fires after an ACTUAL wake — skipped windows (WIP / rate-limited /
            # debounced / not-idle) return early above and never pay the delay.
            if _WAKE_STAGGER_S > 0:
                await asyncio.sleep(_WAKE_STAGGER_S)
        else:
            _log.debug(
                "roster-recovery: wake actuation aborted (TOCTOU/busy) window %d "
                "role=%s — cooled down, will retry after cooldown not next sweep",
                window_id,
                role,
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
    loop = asyncio.get_running_loop()
    # Reap stale duplicate-titled windows FIRST so every title-anchored op this
    # sweep (and every external nudge/busy-read) resolves to exactly one window.
    await loop.run_in_executor(None, _reap_duplicate_windows)
    windows = await loop.run_in_executor(None, _discover_roster_windows)
    # ADDITIVE: union in identity-registered windows the title pass missed (non-role-
    # shaped titles). Title-discovered windows above are untouched.
    windows = await loop.run_in_executor(None, _union_registered_windows, windows)
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
    piggyback = os.environ.get("KHIMAIRA_RECONCILE_VIA_ROSTER_RECOVERY") == "1"
    while True:
        await asyncio.sleep(_WATCH_INTERVAL_S)
        try:
            await check_once()
        except Exception as exc:
            _log.warning("roster-recovery: sweep error: %s", exc)
        # #18 backstop (gated, opt-in): auto_dispatch's own sleep-loop can freeze
        # on the live daemon (uvloop timer never fires; load/SSE-churn ruled out
        # as the cause). THIS loop demonstrably fires on the same daemon, so drive
        # the commit-ready reconcile from here as a churn-immune fallback. Idempotent
        # + cooldown-gated downstream, so double-firing with a healthy auto_dispatch
        # is harmless. Enable with KHIMAIRA_RECONCILE_VIA_ROSTER_RECOVERY=1.
        if piggyback:
            try:
                from khimaira.monitor import auto_dispatch as _ad

                await _ad._reconcile_commit_ready()
            except Exception as exc:
                _log.warning("roster-recovery: piggyback reconcile error: %s", exc)
