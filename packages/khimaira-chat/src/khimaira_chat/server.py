"""MCP stdio server — declares `claude/channel` capability + chat tools.

Per Claude Code's channels model, this process is spawned as a stdio
subprocess by Claude Code itself (one per session). The agent calls
the chat_* tools; the subprocess holds an HTTP/SSE connection to
khimaira-monitor and forwards inbound chat events into the agent's
context as `notifications/claude/channel` events.

Lazy session_id registration: Claude Code does NOT set CLAUDE_SESSION_ID
in the subprocess env. The agent passes its session_id on the first
chat tool call; we store it for the subprocess lifetime and start the
SSE subscriber. The SessionStart hook fires `chat_my_chats(session_id)`
on session boot to force this registration before any chat events
need to be delivered.
"""

from __future__ import annotations

import asyncio
import atexit
import collections
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import mcp.types as types
from mcp.server.lowlevel import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage

from khimaira_chat import daemon_client

log = logging.getLogger("khimaira_chat.server")

SERVER_NAME = "khimaira-chat"
SERVER_VERSION = "0.1.0"

INSTRUCTIONS = (
    "Cross-session real-time chat via Claude Code channels.\n"
    "\n"
    "WHEN TO USE WHICH PRIMITIVE (read this carefully — agents often pick wrong):\n"
    "- The user says 'reply to <session>', 'send <session> a message', 'chat with "
    "<session>', or 'tell <session>': call `chat_my_chats(session_id=<my_id>)` FIRST. "
    "If you have an active chat with that session, use `chat_send(chat_id=..., body=...)`. "
    "Only fall back to `mcp__khimaira__session_post_notice` if NO shared chat exists.\n"
    '- Incoming `<channel kind="invite" ...>` block: use `chat_accept` '
    "(no chat_id arg defaults to the latest pending invite).\n"
    '- Incoming `<channel sender="..." ...>` block (no kind=invite): that\'s a chat '
    "message. Reply with `chat_send` to the same chat_id from the channel meta.\n"
    "- For acknowledgments, thanks, seen/receipt signals, or 👍, use "
    "`chat_react(chat_id=..., target_msg_id=..., emoji=...)`. This is THE "
    "acknowledgment primitive: it is visible but creates no reply obligation and "
    "does not wake another session. Do not send a reciprocal chat message merely "
    "to acknowledge receipt.\n"
    "- The user wants to leave a note for someone who's not actively chatting: "
    "use `mcp__khimaira__session_post_notice` (durable, lands in their inbox).\n"
    "- The user wants a synchronous answer from one peer: use `mcp__khimaira__"
    "session_log_question` (formal Q→A contract, blocking).\n"
    "\n"
    "session_id is your own Claude Code session id (visible in the SessionStart hook "
    "context block titled '🆔 khimaira session_id'); pass it to all chat_* tools.\n"
    "\n"
    "DON'T leak `<thinking>`, `<scratchpad>`, etc. tags into chat message bodies — "
    "the daemon strips them defensively but it's noise. Send only the message body."
)


# ---------------------------------------------------------------------------
# Session entanglement fence — globally-unique SSE subscriber per session_id
# ---------------------------------------------------------------------------
#
# The bug: two subprocesses can register the SAME khimaira session_id (e.g.
# via `claude --resume` or a duplicate window launch). Each subprocess calls
# daemon_client.subscribe_events(session_id) independently — the daemon pushes
# every chat event to that id → BOTH subprocesses emit it → both windows see
# the roster's notifications. The `_SubprocessState` guard is per-subprocess;
# it does NOT prevent two separate subprocesses from claiming the same id.
#
# The fix: a PID claim file at _SSE_CLAIM_DIR/<session_id>.pid. When this
# subprocess registers a session_id it checks for a prior claim:
#   - No file → this subprocess is the first; write our PID, subscribe.
#   - File exists, PID alive → a live peer already owns this id; fence
#     (set _state.sse_fenced=True, skip SSE subscription, log a warning).
#   - File exists, PID dead → prior subprocess is gone; reclaim the file,
#     write our PID, subscribe normally.
#
# Ambiguity policy: err toward FENCE (not ALLOW). If we cannot verify the
# prior PID is dead, we block. A false-fence is visible (this window gets no
# SSE) and retryable; a false-allow re-opens the silent dual-subscribe, which
# is the undiagnosable bug we're fixing. (Same recoverable-default discipline
# as other safety decisions this session: the direction of the default IS the
# safety property.)
#
# Non-Linux / /proc-unreadable: _pid_alive returns None (ambiguous). The fence
# treats this as FENCE (not allow) — same err-toward-fence policy. The only
# fail-open path is a filesystem error creating the claim dir or writing the
# file (an infra blip that would block every new session; failing-open there
# is the recoverable default since the process still works without the fence).

_SSE_CLAIM_DIR = Path.home() / ".local" / "state" / "khimaira" / "sse-claim"
_MY_PID = os.getpid()
# The claim path for this subprocess's session (set in _acquire_session_claim).
_my_claim_path: Path | None = None


def _get_proc_starttime(pid: int) -> str | None:
    """Read the process start-time from /proc/<pid>/stat field 22 (Linux only).

    Field 22 in /proc/<pid>/stat is the jiffies-since-boot at which the process
    started. It is unique per PID within a boot — the OS cannot reuse the same
    PID with the same start-time for a different process. This disambiguates PID
    reuse: a stale claim for PID X with starttime T versus a LIVE unrelated
    process at PID X with starttime T2 ≠ T — the claim is for the DEAD original.

    Returns None if /proc is unavailable (non-Linux) or on any read error.
    """
    try:
        stat_text = Path(f"/proc/{pid}/stat").read_text()
        # /proc/<pid>/stat format: pid (comm) state ppid ... starttime ...
        # The command name can contain spaces and parentheses, so we parse from
        # the right of the closing ')' to get reliably-indexed fields after it.
        rp = stat_text.rfind(")")
        if rp < 0:
            return None
        fields = stat_text[rp + 2 :].split()
        # After ')': state(0), ppid(1), pgrp(2), session(3), tty(4), tpgid(5),
        # flags(6), minflt(7), cminflt(8), majflt(9), cmajflt(10), utime(11),
        # stime(12), cutime(13), cstime(14), priority(15), nice(16), nthreads(17),
        # itrealvalue(18), starttime(19)
        return fields[19] if len(fields) > 19 else None
    except OSError:
        return None


def _pid_alive(pid: int) -> bool | None:
    """Return True if the process is alive, False if dead, None if unknown.

    Uses /proc on Linux. Returns None on non-Linux platforms where /proc is
    unavailable — caller should treat None as ambiguous and err toward FENCE.
    """
    proc_path = Path(f"/proc/{pid}")
    if not Path("/proc").exists():
        return None  # non-Linux; can't determine
    try:
        return proc_path.exists()
    except OSError:
        return None  # permission or other error → ambiguous


def _is_original_claimant(prior_pid: int, prior_starttime: str | None) -> bool | None:
    """Return True if the prior claim is from the ORIGINAL (not a reused) PID.

    PID reuse: a crashed session leaves a stale claim. The OS can assign the same
    PID to an unrelated process. `_pid_alive(stale_pid)` returns True for the
    unrelated process → false-fence: the real session can never reclaim.

    Fix: compare the current starttime of `prior_pid` against the starttime
    recorded at claim-time. If they differ, the PID was reused → original is dead
    → reclaim is safe. If they match, the original process is still alive → fence.

    Returns:
        True  — the original process is still alive (fence)
        False — the process is dead or the PID was reused (reclaim)
        None  — liveness cannot be determined (err toward fence)
    """
    if prior_starttime is None:
        # No starttime was recorded at claim-time (e.g. written by an older version
        # of this code). Fall back to PID-only alive check — reuse-ambiguous but the
        # best we can do without the reference starttime.
        return _pid_alive(prior_pid)
    current_starttime = _get_proc_starttime(prior_pid)
    if current_starttime is None:
        # Can't read current starttime — process may not exist or /proc unavailable.
        alive = _pid_alive(prior_pid)
        if alive is False:
            return False  # /proc/<pid>/ doesn't exist → dead → reclaim
        return None  # ambiguous
    if current_starttime != prior_starttime:
        # PID was reused by a different process → original is dead → reclaim.
        return False
    # Same PID, same starttime → the original process is still alive → fence.
    return True


def _acquire_session_claim(session_id: str) -> bool:
    """Attempt to acquire the SSE-subscriber claim for session_id.

    Returns True if this subprocess should proceed to subscribe (either no
    prior claim, or the prior claimant's process is dead / PID-reused).
    Returns False if a live prior claimant already owns this session_id —
    this subprocess must NOT subscribe (entanglement fence).

    The claim file stores PID:starttime so PID reuse is detectable: if the
    OS assigns the same PID to a different process after a crash, the
    start-times differ → original is dead → reclaim is allowed.
    """
    global _my_claim_path
    try:
        _SSE_CLAIM_DIR.mkdir(parents=True, exist_ok=True)
        claim_path = _SSE_CLAIM_DIR / f"{session_id}.pid"
        my_starttime = _get_proc_starttime(_MY_PID)
        my_claim_text = f"{_MY_PID}:{my_starttime or ''}"

        if claim_path.exists():
            try:
                raw = claim_path.read_text().strip()
                parts = raw.split(":", 1)
                prior_pid = int(parts[0])
                # prior_starttime is None for claims written by older code (PID-only
                # format). Reuse-safety (starttime discrimination) applies only to
                # new-format claims; old-format falls back to PID-only liveness.
                # Old claims are transitional — they age out as new ones overwrite.
                prior_starttime = parts[1] if len(parts) > 1 and parts[1] else None
            except (ValueError, OSError, IndexError):
                prior_pid = None
                prior_starttime = None

            if prior_pid is not None and prior_pid != _MY_PID:
                original_alive = _is_original_claimant(prior_pid, prior_starttime)
                if original_alive is True:
                    # Original process is live and owns this session_id — fence.
                    log.warning(
                        "khimaira-chat: session-entanglement fence: session_id=%s already "
                        "claimed by live PID %d (starttime=%s); this subprocess will NOT "
                        "subscribe to SSE. Two subprocesses share this session_id — close "
                        "the duplicate window to clear the entanglement.",
                        session_id,
                        prior_pid,
                        prior_starttime,
                    )
                    return False
                elif original_alive is None:
                    # Liveness ambiguous (non-Linux, /proc-unreadable, etc.) → err FENCE.
                    log.warning(
                        "khimaira-chat: session-entanglement fence: session_id=%s has a "
                        "prior claim (PID %d) but process liveness cannot be verified "
                        "(non-Linux or /proc unavailable). Fencing as a precaution. "
                        "To force-reclaim: delete %s",
                        session_id,
                        prior_pid,
                        claim_path,
                    )
                    return False
                else:
                    # Prior PID dead or reused — reclaim.
                    log.info(
                        "khimaira-chat: session-entanglement: reclaiming session_id=%s "
                        "from dead/reused PID %d (starttime=%s)",
                        session_id,
                        prior_pid,
                        prior_starttime,
                    )

        # Write our claim (PID:starttime for reuse detection).
        claim_path.write_text(my_claim_text)
        _my_claim_path = claim_path

        def _release_claim() -> None:
            try:
                if claim_path.exists() and claim_path.read_text().strip() == my_claim_text:
                    claim_path.unlink(missing_ok=True)
            except OSError:
                pass

        atexit.register(_release_claim)
        return True

    except OSError as exc:
        # Filesystem error creating the claim dir or writing the file.
        # Fail-open: allow subscription (don't block on claim-infra failure).
        log.warning(
            "khimaira-chat: session-entanglement: could not write claim file for "
            "session_id=%s (%s); proceeding without fence (fail-open)",
            session_id,
            exc,
        )
        return True


def _release_session_claim(session_id: str) -> None:
    """Release this subprocess's SSE claim for session_id, if we own it.

    `_acquire_session_claim` only releases its own claim at process exit
    (via the `atexit`-registered closure captured at claim time). That's
    fine for the normal one-id-per-lifetime case, but on a /clear re-bind
    (`_SubprocessState._rebind`) the OLD session_id is dead well before
    process exit — Claude Code minted a new one and re-fired SessionStart.
    Leaving the old claim file behind would keep it "live-claimed by our
    PID" indefinitely: harmless in the common case (the old session_id is
    never reused), but it's an unbounded leak of claim files and, if a
    session_id were ever reused, a permanent false-fence against whichever
    subprocess legitimately claims it next. Best-effort cleanup — never
    raises, since releasing a claim is never load-bearing enough to fail
    the re-bind over.
    """
    try:
        claim_path = _SSE_CLAIM_DIR / f"{session_id}.pid"
        if not claim_path.exists():
            return
        raw = claim_path.read_text().strip()
        prior_pid = int(raw.split(":", 1)[0])
        if prior_pid == _MY_PID:
            claim_path.unlink(missing_ok=True)
            log.info(
                "khimaira-chat: session-entanglement: released stale claim for "
                "session_id=%s (superseded by /clear re-bind)",
                session_id,
            )
    except (OSError, ValueError, IndexError):
        pass


# ---------------------------------------------------------------------------
# Per-subprocess state — bound on first tool call
# ---------------------------------------------------------------------------


class _SubprocessState:
    """Holds the session_id (lazy-registered) + SSE subscriber task.

    **One subprocess = one session, for its lifetime — MODULO a legitimate
    /clear re-bind.** This is load-bearing: Claude Code's channel-notification
    routing is per-stdio-pipe (the notification only reaches the agent that
    spawned this subprocess). Sharing a subprocess across UNRELATED sessions
    would break that routing — the daemon would push events for session B's
    chats to a subprocess that actually serves session A, and the agent
    never sees them.

    `register()` enforces this by raising if a tool call arrives bearing a
    different session_id, UNLESS that session_id is the daemon-confirmed
    /clear identity for THIS physical process (see `_rebind` +
    `_find_legitimate_rebind_ppid`). Claude Code re-fires SessionStart with a
    freshly-minted session_id after `/clear` but REUSES the same MCP
    subprocess — without the re-bind path, the subprocess stays frozen on the
    pre-/clear id forever and every chat tool call from the new id is
    refused. The ppid-registry check is what keeps this safe: a genuinely
    foreign session runs in a DIFFERENT process (different ancestor ppid
    chain), so it can never satisfy the check — only Claude Code re-firing
    SessionStart for OUR OWN ancestor chain can. Any other mismatch still
    raises, visible in the agent's tool-call result, so a misconfigured
    caller fails loudly rather than silently rewriting our identity.
    """

    # Capacity of the seen-event dedup set. LRU OrderedDict pops oldest on overflow.
    _DEDUP_MAX = 1000

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.last_event_id: str | None = None
        self.subscriber_task: asyncio.Task | None = None
        # Bounded dedup set — prevents reprocessing the same event_id after
        # a subscriber reconnect during the cursor-advance race window.
        self.seen_event_ids: collections.OrderedDict[str, None] = collections.OrderedDict()
        # 2026-06-06: background boot task (ppid-bridge + subscriber start) —
        # held here so it isn't garbage-collected mid-flight. See _serve().
        self.boot_task: asyncio.Task | None = None
        # Phase B v1.3: watchdog supervises subscriber_task. Restarts it
        # if it crashes; logs the crash reason via task.exception(). One
        # watchdog per subprocess lifetime, spawned in _serve().
        self.watchdog_task: asyncio.Task | None = None
        # Restart counter — bumped each time the watchdog reincarnates a
        # crashed subscriber. Logged on every restart so the steady-state
        # "subscriber restarted N times" pattern is grep-able. Hot-restart
        # loops (persistent daemon-down case) become visible without
        # special-casing exponential backoff.
        self.subscriber_restart_count: int = 0
        # write_stream captured at stdio_server() time so the SSE
        # subscriber can emit notifications/claude/channel directly,
        # without needing the session object from request_context.
        self.write_stream: Any = None
        # True if the entanglement fence blocked this subprocess from
        # subscribing to SSE (a live prior claimant already owns this
        # session_id). When fenced, _ensure_subscriber and _serve skip
        # spawning _proactive_sse_loop.
        self.sse_fenced: bool = False

    def register(self, session_id: str) -> None:
        if self.session_id is None:
            self.session_id = session_id
            # Ensure the daemon-auth header uses the khimaira session ID, not
            # CLAUDE_CODE_SESSION_ID (which may differ after a session restart).
            daemon_client.set_caller_session_id(session_id)
            # Entanglement fence: only one live subprocess may subscribe SSE
            # for a given session_id. Checks process-liveness of any prior
            # claimant; errs toward FENCE on ambiguity (silent dual-subscribe
            # is the worse, undiagnosable failure vs a visible/retryable block).
            if not _acquire_session_claim(session_id):
                self.sse_fenced = True
            log.info(
                "khimaira-chat: registered session_id=%s (sse_fenced=%s)",
                session_id,
                self.sse_fenced,
            )
            # Re-slot: if compaction recycled this subprocess and assigned a new
            # session_id, re-bind the window's stable KHIMAIRA_ROSTER_SLOT to the
            # new sid so slot_resolve can bridge it. Gated: skip if fenced (a
            # duplicate must not hijack the live owner's slot).
            _maybe_reslot(session_id)
            # Bridge Claude Code's `-n <name>` flag → khimaira friendly name.
            _maybe_register_display_name(session_id)
        elif self.session_id != session_id:
            via_ppid = _find_legitimate_rebind_ppid(session_id)
            if via_ppid is not None:
                self._rebind(session_id, via_ppid)
            else:
                raise ValueError(
                    f"This subprocess is bound to session {self.session_id!r}; "
                    f"refusing tool call from session {session_id!r}. "
                    f"One subprocess = one session for its lifetime."
                )

    def _rebind(self, new_session_id: str, via_ppid: int) -> None:
        """Follow this subprocess's binding to new_session_id after a /clear.

        Caller (`register`) has already confirmed via `_find_legitimate_rebind_ppid`
        that new_session_id is the daemon-confirmed identity for one of this
        process's ancestors — SessionStart re-posted the {ppid: session_id}
        mapping for OUR OWN ancestor chain, which only happens on a genuine
        /clear re-fire, never a foreign session.

        Advancing `session_id` alone is not enough: Claude Code's channel
        notifications are routed by the CURRENT session_id, so the SSE
        subscriber must also be torn down and restarted under the new
        identity, or the agent goes SSE-deaf immediately after /clear even
        though tool calls succeed. This mirrors the ordering `register()`
        uses for first-bind (claim → reslot → display-name) plus the
        subscriber-restart step, reusing the same watchdog
        cancel/reincarnate pattern as `_subscriber_watchdog` /
        `_dispatch_tool`'s force-resubscribe (bump `subscriber_restart_count`,
        `create_task(_proactive_sse_loop())`) rather than inventing a new one.
        """
        old_session_id = self.session_id
        log.warning(
            "khimaira-chat: /clear re-bind: session_id %s -> %s (confirmed via ancestor ppid=%d)",
            old_session_id,
            new_session_id,
            via_ppid,
        )
        self.session_id = new_session_id
        # Ensure the daemon-auth header uses the khimaira session ID, not
        # CLAUDE_CODE_SESSION_ID (which may differ after a session restart).
        daemon_client.set_caller_session_id(new_session_id)

        # Entanglement claim handoff: the OLD id is dead (Claude Code minted
        # a new one on /clear) — release its claim so it doesn't linger and
        # falsely appear "live-claimed" forever. Then acquire the new id's
        # claim exactly as first-bind does; sse_fenced is re-derived (not
        # just set-on-failure) since a prior fenced state must be able to
        # clear on rebind, and vice versa.
        if old_session_id is not None:
            _release_session_claim(old_session_id)
        self.sse_fenced = not _acquire_session_claim(new_session_id)
        log.info(
            "khimaira-chat: /clear re-bind claim handoff complete for "
            "session_id=%s (sse_fenced=%s)",
            new_session_id,
            self.sse_fenced,
        )

        # Re-slot + display-name bridge, same as first-bind. Gated on
        # sse_fenced internally (_maybe_reslot checks it itself).
        _maybe_reslot(new_session_id)
        _maybe_register_display_name(new_session_id)

        # Restart the SSE subscriber under new_session_id. The cursor state
        # (last_event_id, seen_event_ids) belonged to the OLD session's
        # stream — reset it so the new subscriber starts clean rather than
        # replaying a Last-Event-ID the daemon has no record of for this id.
        old_task = self.subscriber_task
        self.subscriber_task = None
        self.last_event_id = None
        self.seen_event_ids.clear()
        if old_task is not None and not old_task.done():
            old_task.cancel()
        if not self.sse_fenced and self.write_stream is not None:
            self.subscriber_restart_count += 1
            self.subscriber_task = asyncio.create_task(_proactive_sse_loop())
            log.info(
                "khimaira-chat: /clear re-bind — subscriber restarted for "
                "session_id=%s (restart #%d)",
                new_session_id,
                self.subscriber_restart_count,
            )
        else:
            log.info(
                "khimaira-chat: /clear re-bind — subscriber restart skipped "
                "(sse_fenced=%s, write_stream_up=%s)",
                self.sse_fenced,
                self.write_stream is not None,
            )


_state = _SubprocessState()


def _find_legitimate_rebind_ppid(new_session_id: str) -> int | None:
    """The /clear re-bind legitimacy guard — ppid-based, not trust-the-caller.

    Walks the same ancestor chain the boot-time ppid-bridge uses
    (`_ancestor_pids`) and asks the daemon what session_id each ancestor ppid
    currently maps to. SessionStart posts the {ppid: session_id} mapping on
    every boot, INCLUDING a /clear re-fire — so if Claude Code genuinely
    re-fired SessionStart for THIS process's ancestor chain with
    new_session_id, the daemon's registry reflects it. No backoff/retry here
    (unlike `_async_try_auto_register_from_ppid`'s boot-time budget): by the
    time an agent issues a tool call bearing new_session_id, SessionStart
    has already run synchronously and posted the mapping — if it hasn't,
    this is correctly treated as not-yet-legitimate and the caller raises
    (the agent's next tool call, after SessionStart lands, succeeds).

    A genuinely foreign session runs in a DIFFERENT process — its ppid chain
    can never contain one of OUR ancestors — so it can never satisfy this by
    construction. That's the safety property the whole re-bind rests on.

    Returns the matching ancestor ppid (for logging) or None if no ancestor
    maps to new_session_id.
    """
    for ppid in _ancestor_pids(max_depth=6):
        try:
            sid = daemon_client.lookup_session_by_ppid(ppid)
        except Exception:
            continue
        if sid == new_session_id:
            return ppid
    return None


def _maybe_reslot(session_id: str) -> None:
    """Re-bind the slot when the subprocess (re)registers a session_id.

    On context compaction, Claude Code recycles the MCP subprocess and assigns
    a new session uuid, but does NOT re-fire SessionStart (the sole slot
    registrar). As a result, the new uuid is never slot-registered and
    slot_resolve cannot bridge it → the roster link breaks.

    This function re-posts the slot-bind on every register() so compaction
    composes correctly: KHIMAIRA_ROSTER_SLOT is preserved in the window env
    across compaction (set once at kitty window launch), so the bind stays
    stable even as the uuid rotates.

    GATE — only re-slot if NOT fenced:
        A fenced subprocess (K3 entanglement — duplicate of a LIVE session)
        must NOT re-slot. If it did, `_update_slot_registry` would make the
        duplicate's sid the slot's current_sid, displacing the REAL owner and
        hijacking its identity. The fence stops dual-subscribe; this gate
        stops slot-hijack. The two fixes must compose, not fight.

    Idempotency: the daemon's /slot endpoint treats a re-POST with the same
    slot + unchanged current_sid as a no-op — no prior_sids rotation, no churn.
    Only a new sid triggers the rotate. Fail-open: never block registration on
    slot-bind failure.
    """
    if _state.sse_fenced:
        # Fenced = duplicate of a live session. Do NOT re-slot — that would
        # hijack the live owner's slot binding.
        log.info(
            "khimaira-chat: re-slot skipped for session_id=%s (sse_fenced — "
            "duplicate subprocess must not own the slot)",
            session_id,
        )
        return
    roster_slot = os.environ.get("KHIMAIRA_ROSTER_SLOT", "").strip()
    kitty_wid_str = os.environ.get("KITTY_WINDOW_ID", "").strip()
    if not roster_slot or not kitty_wid_str:
        return
    try:
        kitty_wid = int(kitty_wid_str)
    except ValueError:
        log.warning(
            "khimaira-chat: re-slot skipped — KITTY_WINDOW_ID=%r is not an int",
            kitty_wid_str,
        )
        return
    try:
        daemon_client.bind_slot(session_id, roster_slot, kitty_wid)
        log.info(
            "khimaira-chat: re-slotted session_id=%s → slot=%s wid=%d",
            session_id,
            roster_slot,
            kitty_wid,
        )
    except Exception as exc:
        # Fail-open: a missing slot-bind is bad but must not block registration.
        log.warning(
            "khimaira-chat: re-slot POST failed for session_id=%s slot=%r wid=%d: %r "
            "— session un-slotted, slot_resolve will not bridge this sid",
            session_id,
            roster_slot,
            kitty_wid,
            exc,
        )


def _detect_claude_display_name() -> str | None:
    """Walk the ancestor chain looking for `-n <name>` / `--name <name>`
    in any parent's cmdline.

    Claude Code's `-n NAME` sets a display name. We bridge that to
    khimaira's friendly name by setting it server-side. But our
    DIRECT parent is usually `uv` (since Claude Code spawns us via
    `bash -lc 'uv run khimaira-chat'`), so we have to walk ancestors
    until we find Claude Code's invocation argv.

    Linux-only via /proc; returns None on other platforms or if no
    ancestor's cmdline contains the flag.
    """
    for ppid in _ancestor_pids(max_depth=6):
        try:
            with open(f"/proc/{ppid}/cmdline", "rb") as f:
                argv = f.read().decode("utf-8", errors="replace").split("\x00")
            for i, arg in enumerate(argv):
                if arg in ("-n", "--name") and i + 1 < len(argv):
                    name = argv[i + 1].strip()
                    if name:
                        return name
        except (OSError, IndexError, UnicodeDecodeError):
            continue
    return None


def _maybe_register_display_name(session_id: str) -> None:
    """If Claude Code launched with `-n <name>`, propagate that to the
    daemon as a friendly session name. Best-effort: silent on failure
    (the chat tools still work without the name; user just has to
    `/rename` manually if they want it)."""
    name = _detect_claude_display_name()
    if not name:
        return
    try:
        daemon_client.set_session_name(session_id, name)
        log.info("khimaira-chat: auto-registered name=%s for session %s", name, session_id)
    except Exception as exc:
        # Likely: name already taken, or daemon unreachable. Either way,
        # don't fail the chat tool call — fallback is /rename.
        log.warning("khimaira-chat: name auto-register failed for %r — %s", name, exc)
        return
    # Apply any persistent by-name auto-accept allowlist now that the
    # session has its durable identity. No-op if no allowlist file exists.
    try:
        result = daemon_client.apply_auto_accept_by_name(session_id, name)
        if result.get("applied"):
            log.info(
                "khimaira-chat: applied by-name auto-accept allowlist (%d peers) for %s",
                len(result.get("allow", [])),
                name,
            )
    except Exception as exc:
        log.warning("khimaira-chat: by-name auto-accept apply failed for %r — %s", name, exc)


# ---------------------------------------------------------------------------
# Routing — pure function deciding whether/how to emit a channel block
# ---------------------------------------------------------------------------


def _route_record(record: dict[str, Any], my_session_id: str) -> tuple[str, dict[str, str]] | None:
    """Decide whether this subprocess should emit a channel notification
    for an incoming SSE record, and if so, the (content, meta) to send.

    Returns None to skip. Pure function for testability — neither SSE
    loop should embed routing logic directly. Phase B v1.1 extended this
    from msg-only to also cover task creations and task_update transitions.

    Routing rules:
    - kind=member, state=pending, session_id == me → invite notification
    - kind=msg, sender != me → message notification
    - kind=reaction, sender != me → non-obligating reaction notification
    - kind=task, (assignee == me) OR (unassigned AND sender != me) → task created
    - kind=task_update, by_session_id != me → task transition (the actor
      doesn't see their own action echoed; everyone else in the chat does,
      which covers the master-sees-agent-done and agent-sees-master-approve
      cases without an extra lookup)
    - All other kinds → skip
    """
    kind = record.get("kind")

    if (
        kind == "member"
        and record.get("state") == "pending"
        and record.get("session_id") == my_session_id
    ):
        chat_id = record.get("chat_id", "")
        inviter = record.get("invited_by", "someone")
        content = (
            f"{inviter} invited you to chat {chat_id}. "
            f"Accept with `/khimaira-chat-accept` or decline with "
            f"`/khimaira-chat-reject` (no chat_id needed — defaults "
            f"to this invite)."
        )
        meta = {"chat_id": str(chat_id), "kind": "invite", "from": str(inviter)}
        return content, meta

    if kind == "msg":
        sender_id = record.get("sender_id")
        if sender_id == my_session_id:
            return None
        content = record.get("body", "")
        meta = {
            "chat_id": str(record.get("chat_id", "")),
            "sender": str(record.get("sender_name") or sender_id or ""),
            "msg_id": str(record.get("id", "")),
        }
        return content, meta

    if kind == "reaction":
        sender_id = record.get("sender_id")
        if sender_id == my_session_id:
            return None
        sender_name = record.get("sender_name") or sender_id or ""
        emoji = record.get("emoji", "")
        target_id = record.get("target_id", "")
        content = f"{sender_name} reacted {emoji} to {target_id}"
        meta = {
            "chat_id": str(record.get("chat_id", "")),
            "kind": "reaction",
            "sender": str(sender_name),
            "target_id": str(target_id),
            "emoji": str(emoji),
        }
        return content, meta

    if kind == "task":
        sender_id = record.get("sender_id")
        assignee_id = record.get("assignee_id")
        if assignee_id == my_session_id:
            pass  # I'm the assignee — emit
        elif assignee_id is None and sender_id != my_session_id:
            pass  # unassigned, not my own — emit (broadcast-to-accepted shape)
        else:
            return None
        task_id = record.get("id", "")
        sender_name = record.get("sender_name") or sender_id or ""
        body = record.get("body", "")
        content = f"📋 task {task_id} [pending] from {sender_name}: {body}"
        meta = {
            "chat_id": str(record.get("chat_id", "")),
            "kind": "task",
            "task_id": str(task_id),
            "sender": str(sender_name),
            "status": "pending",
        }
        return content, meta

    if kind == "task_update":
        by_session_id = record.get("by_session_id")
        if by_session_id == my_session_id:
            return None  # don't echo own transition
        task_id = record.get("task_id", "")
        by_name = record.get("by_name") or by_session_id or ""
        status = record.get("status", "")
        note = record.get("note")
        suffix = f": {note}" if note else ""
        content = f"📋 task {task_id} [{status}] from {by_name}{suffix}"
        meta = {
            "chat_id": str(record.get("chat_id", "")),
            "kind": "task_update",
            "task_id": str(task_id),
            "sender": str(by_name),
            "status": str(status),
        }
        return content, meta

    if kind == "task_verdict":
        # B3 gate verdicts (critic approve/changes, verifier ship/hold). The
        # daemon broadcasts these to every member, but until now this router
        # dropped them ("all other kinds → skip") — so the MASTER never saw a
        # verdict land in-context and dual-verdict-complete commits stalled until
        # a manual poll (muther 2026-06-12). Emit like task_update: everyone but
        # the reviewer who filed it sees it (covers master-sees-dual-verdict).
        by_session_id = record.get("by_session_id")
        if by_session_id == my_session_id:
            return None  # the reviewer doesn't need their own verdict echoed
        task_id = record.get("task_id", "")
        by_name = record.get("by_name") or by_session_id or ""
        verdict = record.get("verdict", "")
        content = f"⚖️ verdict on task {task_id}: {verdict} (by {by_name})"
        meta = {
            "chat_id": str(record.get("chat_id", "")),
            "kind": "task_verdict",
            "task_id": str(task_id),
            "verdict": str(verdict),
            "sender": str(by_name),
        }
        return content, meta

    if kind == "task_signal":
        by_session_id = record.get("by_session_id")
        if by_session_id == my_session_id:
            return None  # master sent it; don't echo back to master
        assignee_id = record.get("assignee_id")
        if assignee_id is not None and assignee_id != my_session_id:
            return None  # task has a specific assignee; non-assignees skip
        # else: I'm the assignee, OR task is unassigned (broadcast).
        task_id = record.get("task_id", "")
        by_name = record.get("by_name") or by_session_id or ""
        note = record.get("note")
        suffix = f": {note}" if note else ""
        content = f"🟢 task {task_id} [ready to start] from {by_name}{suffix}"
        meta = {
            "chat_id": str(record.get("chat_id", "")),
            "kind": "task_signal",
            "task_id": str(task_id),
            "sender": str(by_name),
            "signal": str(record.get("signal", "start")),
        }
        return content, meta

    return None


# ---------------------------------------------------------------------------
# SSE subscriber lazy-start
# ---------------------------------------------------------------------------


def _ensure_subscriber() -> None:
    """Start the proactive SSE subscriber if it isn't running yet.

    Uses `_proactive_sse_loop` (emits via the stable `write_stream` captured
    at stdio boot), NOT the request-context session — so inbound delivery
    survives context compaction and turn boundaries.

    History (the bug this fixes): the lazy-start path used to spawn a
    session-bound `_sse_loop(ctx.session)`. That captured the MCP
    request-context session from the FIRST tool call and kept emitting
    through it; after a context compaction the session handle went stale,
    so the subscriber stayed 'alive' (never crashed → the watchdog never
    replaced it) but every channel notification went nowhere — the agent
    was silently SSE-deaf. Phase B v1.3 introduced the write_stream-based
    `_proactive_sse_loop` and migrated the watchdog (Lane A) and
    force-resubscribe (Lane B) onto it, but this lazy-start path was
    missed. When the boot-time ppid-bridge misses (common — SessionStart
    hook timing), this path wins the boot race, so the stale session-bound
    loop ran for the whole session. Repointing it here closes the gap.
    """
    if _state.sse_fenced:
        # Entanglement fence: a live peer already owns this session_id.
        # This subprocess must not subscribe to SSE.
        return
    if _state.write_stream is None:
        # stdio transport not up yet (write_stream is captured in _serve);
        # the watchdog will start the subscriber on its first tick.
        return
    if _state.subscriber_task is not None and not _state.subscriber_task.done():
        return
    _state.subscriber_task = asyncio.create_task(_proactive_sse_loop())


# ---------------------------------------------------------------------------
# MCP Server + tool handlers
# ---------------------------------------------------------------------------


def _build_server() -> Server:
    server: Server = Server(SERVER_NAME, version=SERVER_VERSION, instructions=INSTRUCTIONS)

    @server.list_tools()
    async def list_tools() -> list[types.Tool]:
        return [
            types.Tool(
                name="chat_create_room",
                description=(
                    "Create a new chat room with the given members. Creator is "
                    "auto-accepted; invitees go through handshake (chat_accept). "
                    "If the same members already have a chat, returns the existing "
                    "one — pass fresh=True for a new transcript. "
                    "v1.9.5: pass topology='hierarchical' to make targeted messages "
                    "(chat_send_to) default to private=True automatically."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Your session id (creator)",
                        },
                        "members": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Other session ids/names to invite",
                        },
                        "title": {"type": "string", "description": "Optional room title"},
                        "fresh": {"type": "boolean", "default": False},
                        "topology": {
                            "type": "string",
                            "enum": ["flat", "hierarchical", "custom"],
                            "description": (
                                "Privacy semantics for targeted messages. "
                                "'flat' (default): history visible to all. "
                                "'hierarchical': send_to defaults to private=True. "
                                "'custom': no automatic defaults."
                            ),
                            "default": "flat",
                        },
                        "member_roles": {
                            "type": "object",
                            "description": (
                                "Optional session_id→role mapping written to room meta at "
                                "creation. Pass when you know the role each member will hold "
                                "(e.g. master spawning a roster). Enables Themis enforcement "
                                "from day one without a separate chat_grant_role call."
                            ),
                            "additionalProperties": {"type": "string"},
                        },
                    },
                    "required": ["session_id", "members"],
                },
            ),
            types.Tool(
                name="chat_invite",
                description=(
                    "Invite another session into an existing chat. Caller must be an accepted member. "
                    "Pass `role` to atomically bind the invitee's role at invite-time (no separate "
                    "chat_grant_role needed). Assigning master or *-lead roles requires the caller "
                    "to be the chat master; non-privileged roles (agent, observer, etc.) may be "
                    "assigned by any accepted member."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "invitee": {"type": "string", "description": "Session id or name"},
                        "role": {
                            "type": "string",
                            "description": (
                                "Optional role to bind atomically at invite-time. "
                                "Privileged roles (master, *-lead) require caller to be master."
                            ),
                        },
                    },
                    "required": ["session_id", "chat_id", "invitee"],
                },
            ),
            types.Tool(
                name="chat_grant_role",
                description=(
                    "Master-only: bind a role onto an existing accepted chat member. "
                    "Use to fix role-less members added before atomic invite-role support, "
                    "or to promote/demote mid-session. The caller must be the chat master — "
                    "the daemon enforces this against the authenticated session identity."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "target_session_id": {
                            "type": "string",
                            "description": "Session id or name of the member to assign the role to",
                        },
                        "role": {"type": "string", "description": "Role to assign"},
                        "demote_to": {
                            "type": "string",
                            "description": (
                                "Role to assign the current master when promoting a new master "
                                "(default: agent). Ignored for non-master role assignments."
                            ),
                        },
                    },
                    "required": ["session_id", "chat_id", "target_session_id", "role"],
                },
            ),
            types.Tool(
                name="chat_reseat_master",
                description=(
                    "Dead-master recovery: seat a NEW session as master of an "
                    "orphaned roster after the prior master session died (window/"
                    "process exited; registry-GC'd). Fills the gap where "
                    "chat_grant_role (master-only) and chat_transfer_membership "
                    "(needs the dead session as live donor) both fail. REFUSES if "
                    "the incumbent master is still live — use those tools for a "
                    "live handoff. Adds the new master as an accepted member if "
                    "needed, promotes it, demotes the dead incumbent, and emits "
                    "the master role-directive."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "new_master_session_id": {
                            "type": "string",
                            "description": "Session id or name of the new master to seat.",
                        },
                    },
                    "required": ["session_id", "chat_id", "new_master_session_id"],
                },
            ),
            types.Tool(
                name="chat_accept",
                description=(
                    "Accept an invite into a chat. If chat_id is omitted, "
                    "accepts the most recent pending invite (the common case — "
                    "you don't need to know the chat_id)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {
                            "type": "string",
                            "description": "Optional; defaults to latest pending",
                        },
                    },
                    "required": ["session_id"],
                },
            ),
            types.Tool(
                name="chat_reject",
                description=(
                    "Decline an invite. If chat_id is omitted, rejects the most "
                    "recent pending invite. Creator can re-invite later."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {
                            "type": "string",
                            "description": "Optional; defaults to latest pending",
                        },
                    },
                    "required": ["session_id"],
                },
            ),
            types.Tool(
                name="chat_send",
                description="Send a message to a chat. You must be an accepted member.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "body": {"type": "string"},
                        "private": {
                            "type": "boolean",
                            "description": (
                                "When True, message is hidden from non-recipients in "
                                "chat_history. Requires `to` to be set."
                            ),
                        },
                    },
                    "required": ["session_id", "chat_id", "body"],
                },
            ),
            types.Tool(
                name="chat_react",
                description=(
                    "THE primitive for acknowledgments, thanks, seen/receipt signals, "
                    "and emoji reactions. Reacts to an existing chat event without "
                    "creating a reply obligation or waking another session."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "target_msg_id": {
                            "type": "string",
                            "description": "The id of the chat event being acknowledged",
                        },
                        "emoji": {
                            "type": "string",
                            "description": "Reaction marker, for example 👍 or ✅",
                        },
                    },
                    "required": ["session_id", "chat_id", "target_msg_id", "emoji"],
                },
            ),
            types.Tool(
                name="chat_history",
                description="Read recent messages from a chat. Caller must be an accepted member.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "limit": {"type": "integer", "default": 50},
                        "since": {
                            "type": "string",
                            "description": "Optional event_id to start after (for /khimaira-chat-poll)",
                        },
                    },
                    "required": ["session_id", "chat_id"],
                },
            ),
            types.Tool(
                name="chat_my_chats",
                description=(
                    "List chats you're a member of (pending or accepted). "
                    "Also serves as the registration ping for this subprocess — "
                    "the SessionStart hook calls this on boot to force lazy "
                    "session_id registration before any messages need to be delivered."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {"session_id": {"type": "string"}},
                    "required": ["session_id"],
                },
            ),
            types.Tool(
                name="chat_leave",
                description="Leave a chat. You stop receiving messages but the chat continues for others.",
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                    },
                    "required": ["session_id", "chat_id"],
                },
            ),
            types.Tool(
                name="chat_delete",
                description=(
                    "Archive a chat (move JSONL to chats/archive/). "
                    "Only the creator can call this — non-creators should use chat_leave."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                    },
                    "required": ["session_id", "chat_id"],
                },
            ),
            types.Tool(
                name="chat_transfer_membership",
                description=(
                    "Transfer your chat membership to a different session (for "
                    "session handoff). The receiving session lands accepted "
                    "immediately, no handshake. By default you become "
                    "transferred-out (no further pushes, no send rights — your "
                    "chat_history rights persist via the JSONL). Other accepted "
                    "members see a 📦 system message in the transcript. Pairs "
                    "with /khimaira-transfer-session — use it for context-"
                    "handoff to a fresh session. "
                    "Phase B v1.6: pass `as_deputize=true` for the "
                    "/khimaira-deputize variant — atomically writes "
                    "`meta.deputized_original_master = from_session_id` AND "
                    "skips the donor's TRANSFERRED_OUT write so the donor "
                    "stays ACCEPTED throughout the pause-and-handoff cycle. "
                    "Use `chat_resume_master` to reverse."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Your session id (the donor — also the value of from_session_id)",
                        },
                        "chat_id": {"type": "string"},
                        "from_session_id": {
                            "type": "string",
                            "description": "Donor session (must equal session_id; explicit for clarity)",
                        },
                        "to_session_id": {
                            "type": "string",
                            "description": "Recipient session (must be a known registered session, must not already be an accepted member)",
                        },
                        "as_deputize": {
                            "type": "boolean",
                            "description": (
                                "Phase B v1.6 deputize variant. When true, "
                                "writes meta.deputized_original_master AND "
                                "skips donor's TRANSFERRED_OUT write. "
                                "Default false (terminal-handoff)."
                            ),
                            "default": False,
                        },
                    },
                    "required": [
                        "session_id",
                        "chat_id",
                        "from_session_id",
                        "to_session_id",
                    ],
                },
            ),
            types.Tool(
                name="chat_resume_master",
                description=(
                    "Phase B v1.6: caller (original master per meta marker) "
                    "reclaims master role from the current vice that's "
                    "holding it. Pairs with /khimaira-resume. Reverses a "
                    "deliberate `chat_transfer_membership(..., "
                    "as_deputize=true)` swap. Admin-style: vice cooperation "
                    "not required. Validates caller matches "
                    "`meta.deputized_original_master`; atomically swaps "
                    "master role back via v1.5 role-directive emits to "
                    "both sides; clears the meta marker."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {
                            "type": "string",
                            "description": "Your session id (the original master reclaiming)",
                        },
                        "chat_id": {"type": "string"},
                        "demote_to": {
                            "type": "string",
                            "description": (
                                "Role the vice gets demoted to on resume. "
                                "Default 'agent'. Cannot be 'master' (closes "
                                "quorum loophole)."
                            ),
                            "default": "agent",
                        },
                    },
                    "required": ["session_id", "chat_id"],
                },
            ),
            # ---- Phase B tools ----
            types.Tool(
                name="chat_send_to",
                description=(
                    "Send a message to a subset of chat members. Like "
                    "chat_send but only the sessions in `to` receive the channel "
                    "push. Use when you want to coordinate with a specific peer "
                    "inside a multi-party chat (e.g. master sidebars an agent on a "
                    "task without broadcasting to siblings). Set `private=True` to "
                    "also hide the message from non-recipients in chat_history "
                    "(default: visible to all members in history, push-only to `to`)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "body": {"type": "string"},
                        "to": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Session ids/names that should receive the push",
                        },
                        "private": {
                            "type": "boolean",
                            "description": (
                                "When True, message is also hidden from non-recipients "
                                "in chat_history (not just push-only)."
                            ),
                        },
                    },
                    "required": ["session_id", "chat_id", "body", "to"],
                },
            ),
            types.Tool(
                name="chat_task_create",
                description=(
                    "Create a structured task in a chat with status lifecycle "
                    "(pending → in_progress → done → approved | changes_requested). "
                    "Use this INSTEAD of a free-form chat_send when the work needs "
                    "explicit tracking — e.g. master/agent delegation where the "
                    "master will later approve or send back for rework. The chat "
                    "creator is the implicit master; only they can approve / "
                    "request changes. Optional `assignee` pre-claims the task for "
                    "a specific session; omit to leave unassigned for whoever picks "
                    "it up first."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "body": {"type": "string", "description": "Task description / spec"},
                        "assignee": {
                            "type": "string",
                            "description": "Optional session id or name to pre-assign",
                        },
                        "private": {
                            "type": "boolean",
                            "description": (
                                "When True, task is hidden from non-assignee members "
                                "in chat_history. Requires assignee to be set."
                            ),
                        },
                        "domain": {
                            "type": "string",
                            "enum": [
                                "backend",
                                "frontend",
                                "data",
                                "devops",
                                "orchestration",
                            ],
                            "description": (
                                "Optional knowledge domain — the daemon appends "
                                "PROVISIONAL mnemosyne context for <project>:<domain> "
                                "to the task body so the assignee gets specialist "
                                "context with the brief. Set on clearly domain-scoped "
                                "implementation tasks."
                            ),
                        },
                    },
                    "required": ["session_id", "chat_id", "body"],
                },
            ),
            types.Tool(
                name="chat_task_update",
                description=(
                    "Move a task between lifecycle states. Valid transitions: "
                    "pending→in_progress→done→approved|changes_requested. Master "
                    "(chat creator) is the only one who can approve / request "
                    "changes or cancel a task; the assignee (or any accepted member "
                    "if unassigned) moves it through pending→in_progress→done. Use "
                    "`note` to attach context — especially on approve/changes_requested "
                    "where the master should explain the verdict."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "new_status": {
                            "type": "string",
                            "enum": [
                                "in_progress",
                                "done",
                                "approved",
                                "changes_requested",
                                "cancelled",
                            ],
                            "description": "Target state",
                        },
                        "note": {
                            "type": "string",
                            "description": "Optional human-readable context for the transition",
                        },
                        "private": {
                            "type": "boolean",
                            "description": (
                                "When True, status update is hidden from non-assignee "
                                "members in chat_history. Task must have an assignee."
                            ),
                        },
                    },
                    "required": ["session_id", "chat_id", "task_id", "new_status"],
                },
            ),
            types.Tool(
                name="chat_task_signal_start",
                description=(
                    "Master-only 'go' signal on a pending task. Use when you've "
                    "created a task and want to explicitly tell the assignee they "
                    "can start (closes the friction where v1 only had free-form "
                    "chat_send for this). Doesn't change task status — the assignee "
                    "still drives pending → in_progress when they pick it up. Valid "
                    "only on pending tasks; the chat creator (master) is the only "
                    "role allowed to signal. Surfaces as a `🟢 task ... [ready to "
                    "start]` channel block on the assignee's side."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "note": {
                            "type": "string",
                            "description": "Optional context (e.g. 'all blockers resolved, you can start')",
                        },
                    },
                    "required": ["session_id", "chat_id", "task_id"],
                },
            ),
            types.Tool(
                name="chat_task_verdict",
                description=(
                    "Write a structured gate-verdict for a task (B3 enforcement). "
                    "critic writes verdict='approve' or 'changes'; verifier writes "
                    "verdict='ship' or 'hold'. IN-AGENT-6 GATE_BEFORE_COMMIT and "
                    "IN-MASTER-9 APPROVE_WITHOUT_REVIEW_VERDICTS both read these "
                    "structured events — do NOT use prose in chat messages to signal "
                    "verdicts for block-level gates; only this tool counts."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                        "task_id": {"type": "string"},
                        "verdict": {
                            "type": "string",
                            "enum": ["approve", "changes", "ship", "hold"],
                            "description": "critic: approve|changes; verifier: ship|hold",
                        },
                    },
                    "required": ["session_id", "chat_id", "task_id", "verdict"],
                },
            ),
            types.Tool(
                name="chat_task_status",
                description=(
                    "List all tasks in a chat with current status. Returns "
                    "[{task_id, body, assignee, status, last_update_ts, last_note}]. "
                    "Use this to check 'what's pending review' or 'what's been "
                    "approved' without scanning the full message transcript."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "chat_id": {"type": "string"},
                    },
                    "required": ["session_id", "chat_id"],
                },
            ),
            types.Tool(
                name="chat_auto_accept_from",
                description=(
                    "Set this session's auto-accept allowlist. Invites from any "
                    "peer in `allow` (matched by session name OR uuid) skip the "
                    "pending state and go directly to accepted — no need for the "
                    "agent to call chat_accept. Use for trusted master sessions "
                    "that frequently spin up worker chats with this session. Pass "
                    "an empty list to clear. REPLACES the prior list (not additive)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "session_id": {"type": "string"},
                        "allow": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Session names or uuids to auto-accept invites from",
                        },
                    },
                    "required": ["session_id", "allow"],
                },
            ),
        ]

    @server.call_tool()
    async def call_tool(name: str, args: dict[str, Any]) -> list[types.ContentBlock]:
        session_id = args.get("session_id")
        if not session_id:
            return [types.TextContent(type="text", text="Error: session_id is required")]
        try:
            _state.register(session_id)
        except ValueError as exc:
            return [types.TextContent(type="text", text=f"Error: {exc}")]

        # Lazy-start the proactive SSE subscriber. It emits via the stable
        # write_stream (captured at stdio boot), NOT the request-context
        # session — so inbound delivery survives context compaction. (This
        # path historically captured ctx.session, which went stale on
        # compaction and silently broke inbound delivery.)
        _ensure_subscriber()

        try:
            result = await _dispatch_tool(name, args)
        except daemon_client.DaemonError as exc:
            return [
                types.TextContent(
                    type="text",
                    text=f"Daemon error (HTTP {exc.status_code}): {exc.detail}",
                )
            ]
        except Exception as exc:
            log.exception("khimaira-chat: tool %s failed", name)
            return [types.TextContent(type="text", text=f"Tool error: {exc}")]

        return [
            types.TextContent(
                type="text", text=json.dumps(result, separators=(",", ":"), default=str)
            )
        ]

    return server


async def _dispatch_tool(name: str, args: dict[str, Any]) -> Any:
    sid = args["session_id"]
    # Phase B v1.3 Lane B: force-resubscribe on dead subscriber.
    # Active-path complement to Lane A's 30s passive watchdog — when an
    # agent issues a chat tool call against a stale subscriber, restart
    # before dispatch so subsequent messages flow. Fire-and-forget: the
    # new task starts scheduling but isn't awaited, so this first call's
    # response may race the subscriber's first connect. That's acceptable
    # — next chat call onwards is healthy, and a 30s gap is the worst
    # case before Lane A's watchdog would have caught it anyway.
    if (
        name.startswith("chat_")
        and _state.session_id is not None
        and _state.write_stream is not None
    ):
        task = _state.subscriber_task
        if task is None or task.done():
            log.warning(
                "khimaira-chat: subscriber task %s on tool dispatch — force-resubscribe "
                "(restart_count was %d)",
                "missing" if task is None else "done",
                _state.subscriber_restart_count,
            )
            _state.subscriber_restart_count += 1
            _state.subscriber_task = asyncio.create_task(_proactive_sse_loop())
    if name == "chat_create_room":
        return daemon_client.create_room(
            sid,
            args["members"],
            title=args.get("title"),
            fresh=bool(args.get("fresh", False)),
            topology=args.get("topology", "flat"),
            member_roles=args.get("member_roles"),
        )
    if name == "chat_invite":
        return daemon_client.invite(args["chat_id"], sid, args["invitee"], role=args.get("role"))
    if name == "chat_grant_role":
        return daemon_client.grant_role(
            args["chat_id"],
            sid,  # authenticated caller — never a user-supplied by_session_id
            args["target_session_id"],
            args["role"],
            demote_to=args.get("demote_to", "agent"),
        )
    if name == "chat_reseat_master":
        return daemon_client.reseat_master(
            args["chat_id"], args["new_master_session_id"]
        )
    if name == "chat_accept":
        chat_id = args.get("chat_id")
        if not chat_id:
            chat_id = daemon_client.latest_pending(sid)
            if not chat_id:
                return {"error": "no pending invites to accept"}
        return daemon_client.accept(chat_id, sid)
    if name == "chat_reject":
        chat_id = args.get("chat_id")
        if not chat_id:
            chat_id = daemon_client.latest_pending(sid)
            if not chat_id:
                return {"error": "no pending invites to reject"}
        return daemon_client.reject(chat_id, sid)
    if name == "chat_send":
        return daemon_client.send_message(
            args["chat_id"], sid, args["body"], private=args.get("private")
        )
    if name == "chat_react":
        return daemon_client.add_reaction(
            args["chat_id"], sid, args["target_msg_id"], args["emoji"]
        )
    if name == "chat_history":
        return daemon_client.history(
            args["chat_id"], sid, limit=args.get("limit", 50), since=args.get("since")
        )
    if name == "chat_my_chats":
        return daemon_client.my_chats(sid)
    if name == "chat_leave":
        return daemon_client.leave(args["chat_id"], sid)
    if name == "chat_delete":
        return daemon_client.delete_chat(args["chat_id"], sid)
    if name == "chat_transfer_membership":
        # `from_session_id` must equal `sid` — the subprocess identity
        # is the source of truth; the explicit `from_session_id` arg
        # exists for readability at the call site. Reject mismatch
        # loudly rather than silently overriding.
        from_sid = args["from_session_id"]
        if from_sid != sid:
            return {
                "error": (
                    f"from_session_id ({from_sid!r}) must equal this subprocess's "
                    f"session ({sid!r}). You can only transfer your own membership."
                )
            }
        return daemon_client.transfer_membership(
            args["chat_id"],
            sid,
            args["to_session_id"],
            as_deputize=bool(args.get("as_deputize", False)),
        )
    if name == "chat_resume_master":
        # Phase B v1.6: caller reclaims master role from the current vice.
        # The daemon validates that `sid` matches the chat's recorded
        # meta.deputized_original_master — no client-side check needed here.
        return daemon_client.resume_master(
            args["chat_id"],
            sid,
            demote_to=args.get("demote_to", "agent"),
        )
    # ---- Phase B ----
    if name == "chat_send_to":
        return daemon_client.send_message(
            args["chat_id"],
            sid,
            args["body"],
            to=args["to"],
            private=args.get("private"),
        )
    if name == "chat_task_create":
        return daemon_client.create_task(
            args["chat_id"],
            sid,
            args["body"],
            assignee_session_id=args.get("assignee"),
            private=args.get("private", False),
            domain=args.get("domain"),
        )
    if name == "chat_task_update":
        return daemon_client.update_task_status(
            args["chat_id"],
            args["task_id"],
            sid,
            args["new_status"],
            note=args.get("note"),
            private=args.get("private", False),
        )
    if name == "chat_task_signal_start":
        return daemon_client.signal_task_start(
            args["chat_id"], args["task_id"], sid, note=args.get("note")
        )
    if name == "chat_task_verdict":
        return daemon_client.record_gate_verdict(
            args["chat_id"], sid, args["task_id"], args["verdict"]
        )
    if name == "chat_task_status":
        return daemon_client.task_status(args["chat_id"], sid)
    if name == "chat_auto_accept_from":
        return daemon_client.set_auto_accept(sid, args["allow"])
    raise ValueError(f"Unknown tool: {name!r}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def _emit_tools_list_changed() -> None:
    """Emit notifications/tools/list_changed to Claude Code so it re-fetches
    the tool list. Forward-compat hook: today our tools are statically
    registered via @server.list_tools() and never change at runtime, so
    this isn't called from the codebase yet. But declaring listChanged=True
    in capabilities means we CAN emit it later (e.g. if daemon-side state
    ever drives a dynamic tool registry, or if a future Phase exposes
    runtime-registerable tools). Without the capability declaration,
    Claude Code wouldn't know to handle the notification when it arrives.

    Note for the original test-agent friction this commit responds to
    (subprocess running pre-Phase-B code didn't see new chat_task_* tools):
    that's a SUBPROCESS-STALE-CODE problem, not a runtime-tool-list-change
    problem. tools/list_changed from the OLD subprocess wouldn't help —
    it would re-announce its OLD list. The actual fix for that case is
    subprocess restart (close+reopen Claude Code window). This capability
    is groundwork for the orthogonal "dynamic tool registry" future.
    """
    if _state.write_stream is None:
        return
    notif = types.JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/tools/list_changed",
        params=None,
    )
    msg = SessionMessage(message=types.JSONRPCMessage(root=notif))
    try:
        await _state.write_stream.send(msg)
        log.info("khimaira-chat: emitted notifications/tools/list_changed")
    except Exception as exc:
        log.warning("khimaira-chat: tools/list_changed emit failed — %s", exc)


async def _serve() -> None:
    server = _build_server()
    init_opts = InitializationOptions(
        server_name=SERVER_NAME,
        server_version=SERVER_VERSION,
        capabilities=types.ServerCapabilities(
            experimental={"claude/channel": {}},
            # listChanged=True advertises that we MAY emit
            # notifications/tools/list_changed at runtime. Today our tools
            # are statically registered via @server.list_tools() and don't
            # actually change, but the capability declaration is required
            # for Claude Code to handle the notification IF we ever do
            # dynamic registration (Phase B v1.2+ groundwork). See
            # _emit_tools_list_changed for the helper that fires it.
            tools=types.ToolsCapability(listChanged=True),
        ),
        instructions=INSTRUCTIONS,
    )
    async with stdio_server() as (read_stream, write_stream):
        # Capture the write_stream globally so the SSE subscriber can
        # emit notifications/claude/channel directly — no session
        # object, no tool-call gating. This is the unlock for true
        # auto-delivery: subscriber starts at boot, pushes events
        # the moment they arrive, agent sees them on next turn.
        _state.write_stream = write_stream

        # Phase B v1.3 Lane D (rev 2026-06-06): eager registration via the async
        # ppid-bridge — as a BACKGROUND task, never awaited before server.run().
        # The bridge does daemon HTTP (ancestor walk + retries). Awaiting it here
        # held the MCP initialize handshake hostage to daemon load: under a
        # multi-roster boot storm (~30 sessions) per-call latency stretched the
        # bridge far past Claude Code's connect timeout and EVERY session stuck
        # at "still connecting" (ROSTER-LAUNCH-INCIDENT-2026-06-06.md). The
        # handshake must start immediately; registration + subscriber start land
        # in the background, and lazy-reg (first tool call) backstops a missed
        # bridge exactly as before.
        async def _background_boot() -> None:
            if _state.session_id is None:
                try:
                    await _async_try_auto_register_from_ppid()
                except Exception:
                    log.exception(
                        "khimaira-chat: async ppid-bridge raised; falling back to lazy-reg"
                    )
            if (
                _state.session_id
                and _state.subscriber_task is None
                and not _state.sse_fenced
            ):
                _state.subscriber_task = asyncio.create_task(_proactive_sse_loop())

        _state.boot_task = asyncio.create_task(_background_boot())
        # Phase B v1.3: watchdog supervises subscriber_task for its
        # subprocess lifetime. Spawned BEFORE the subscriber (or instead
        # of it, in lazy-reg case) — that way even if _proactive_sse_loop
        # fails to start, the watchdog reincarnates it on first tick.
        if _state.watchdog_task is None:
            _state.watchdog_task = asyncio.create_task(_subscriber_watchdog())
        await server.run(read_stream, write_stream, init_opts)


# Phase B v1.3 Lane D: async ppid-bridge total budget. Module-level so
# tests can shrink it for fast-running assertions. ~5s gives a real
# slow-hook scenario room to land while still bounded.
_ASYNC_PPID_BUDGET_S: float = 5.0


async def _async_try_auto_register_from_ppid() -> None:
    """The ppid-bridge: ancestor-walk daemon lookup to self-register at boot.
    (The former sync sibling in main() was deleted 2026-06-06 — this async
    version, run as a background task from _serve(), is now the only bridge.)

    main() runs the sync version (3s budget, time.sleep). If the
    SessionStart hook is slow to post the {ppid, session_id} mapping
    (which we observed in v1.2 dogfood — subprocess booted before the
    hook landed), the sync attempt misses entirely. This async version
    runs from inside the event loop after stdio_server is up, giving a
    longer total budget (~5s default) via asyncio.sleep without blocking
    interactive use — agent tool calls can dispatch concurrently and
    lazy-reg still serves as the fallback if even this attempt misses.

    On match: sets `_state.session_id` and registers display name.
    Subscriber spawn happens in the caller (`_serve`) so the spawn site
    stays single-source-of-truth.

    Always best-effort: catches exceptions per-attempt and on persistent
    failure logs + returns. Never raises to the caller.
    """
    if _state.session_id is not None:
        return  # main() already succeeded; nothing to do
    ancestors = _ancestor_pids(max_depth=6)
    if not ancestors:
        log.info("khimaira-chat: async ppid-bridge — no ancestors, skipping")
        return
    deadline = asyncio.get_event_loop().time() + _ASYNC_PPID_BUDGET_S
    attempt = 0
    while asyncio.get_event_loop().time() < deadline:
        attempt += 1
        for ppid in ancestors:
            try:
                sid = daemon_client.lookup_session_by_ppid(ppid)
            except Exception:
                sid = None
            if sid:
                _state.session_id = sid
                # Apply entanglement fence — the ppid-bridge path also needs
                # to claim before any subscriber starts (same as register()).
                if not _acquire_session_claim(sid):
                    _state.sse_fenced = True
                log.info(
                    "khimaira-chat: async ppid-bridge succeeded on attempt %d "
                    "via ancestor ppid=%s → session_id=%s (sse_fenced=%s)",
                    attempt,
                    ppid,
                    sid,
                    _state.sse_fenced,
                )
                _maybe_reslot(sid)
                _maybe_register_display_name(sid)
                return
        # Backoff: 0.5s, 1s, 1.5s, 2s. Total ~5s matches the deadline.
        await asyncio.sleep(min(0.5 * attempt, 2.0))
    log.info(
        "khimaira-chat: async ppid-bridge gave up after %d attempts for ancestors %s; "
        "lazy-reg takes over",
        attempt,
        ancestors,
    )


# Phase B v1.3: watchdog tick interval. Module-level so tests can
# monkeypatch it (~0.5s) and exercise restart logic in seconds.
_WATCHDOG_INTERVAL_S: float = 30.0


async def _proactive_sse_loop() -> None:
    """Subscribe to the daemon's SSE stream and emit channel
    notifications directly to write_stream — bypassing the session
    object, so the subscriber runs WITHOUT waiting for any agent
    tool call. The whole point of channels.

    Routing lives in `_route_record`.

    **Phase B v1.3 layered exception handling:**
    - INNER try/except wraps per-record processing — one bad message
      (malformed payload, transient emit failure) is logged + skipped,
      stream loop continues. Single message failures must not kill the
      subscriber.
    - OUTER try/except wraps the entire `async for` + `subscribe_events`
      invocation — stream-killing failures (httpx blowup past reconnect,
      malformed framing that breaks iteration) are logged with traceback
      then RE-RAISED so the watchdog sees `task.done()` + `task.exception()`
      and can reincarnate the subscriber.
    - `asyncio.CancelledError` always propagates uncaught in both layers
      so orderly shutdown isn't broken.
    """
    assert _state.session_id is not None
    assert _state.write_stream is not None
    log.info(
        "khimaira-chat: proactive SSE subscriber starting for session_id=%s",
        _state.session_id,
    )
    try:
        async for record in daemon_client.subscribe_events(
            _state.session_id, last_event_id=_state.last_event_id
        ):
            try:
                evt_id = record.get("event_id")
                if evt_id:
                    # Dedup: skip events already processed (race window on reconnect).
                    if evt_id in _state.seen_event_ids:
                        log.debug("khimaira-chat: dedup skip event_id=%s", evt_id)
                        continue
                    _state.seen_event_ids[evt_id] = None  # new key → appended at end
                    if len(_state.seen_event_ids) > _SubprocessState._DEDUP_MAX:
                        _state.seen_event_ids.popitem(last=False)
                    _state.last_event_id = evt_id
                decision = _route_record(record, _state.session_id)
                if decision is None:
                    continue
                content, meta = decision
                await _direct_channel_notify(content, meta)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Inner: one bad record must not break the stream. Log
                # with traceback so we can diagnose; continue with the
                # next record. evt_id of the bad record is in last_event_id
                # at this point, so a watchdog restart resumes AFTER it.
                log.exception(
                    "khimaira-chat: per-record processing failed (event_id=%s); skipping",
                    record.get("event_id"),
                )
                continue
    except asyncio.CancelledError:
        raise
    except Exception:
        # Outer: stream-killing failure (subscribe_events generator died,
        # or any uncaught surprise). Log with traceback; re-raise so the
        # watchdog catches it via task.exception() and restarts. The
        # restart picks up via Last-Event-ID; no message loss for the
        # gap modulo the daemon's SSE backfill window.
        log.exception("khimaira-chat: _proactive_sse_loop crashed; re-raising for watchdog")
        raise


async def _subscriber_watchdog() -> None:
    """Phase B v1.3: supervise `_state.subscriber_task`, restart on crash.

    Sleeps `_WATCHDOG_INTERVAL_S` between ticks. On each tick:
    - If `subscriber_task` exists and is done → log the crash reason via
      `task.exception()` (which captures the traceback) and reincarnate
      it. `_state.last_event_id` persists across restarts so the new
      task resumes via Last-Event-ID backfill.
    - If `subscriber_task` is None BUT `session_id` is set → the ppid
      bridge didn't fire at boot (rare but real); start the subscriber.
    - If `subscriber_task` is None AND `session_id` is None → still in
      lazy-registration window; nothing to do yet, sleep again.

    `asyncio.CancelledError` propagates (orderly shutdown). Any other
    exception inside the watchdog itself is logged + swallowed so a
    watchdog crash doesn't lose subscriber supervision.
    """
    assert _state.write_stream is not None
    log.info("khimaira-chat: subscriber watchdog starting (interval=%ss)", _WATCHDOG_INTERVAL_S)
    while True:
        try:
            await asyncio.sleep(_WATCHDOG_INTERVAL_S)
            if _state.session_id is None:
                continue  # still lazy-reg phase, nothing to supervise
            if _state.sse_fenced:
                # Entanglement fence: this subprocess must not subscribe SSE.
                # Watchdog stays running (suppresses the "no subscriber" warn)
                # but never restarts the subscriber.
                continue
            task = _state.subscriber_task
            if task is None:
                # Session is registered but subscriber never started —
                # likely ppid bridge missed. Bootstrap it now.
                log.warning(
                    "khimaira-chat: watchdog found no subscriber_task for "
                    "session_id=%s; starting one",
                    _state.session_id,
                )
                _state.subscriber_task = asyncio.create_task(_proactive_sse_loop())
                continue
            if task.done():
                exc = task.exception() if not task.cancelled() else None
                _state.subscriber_restart_count += 1
                if exc is not None:
                    log.error(
                        "khimaira-chat: subscriber_task crashed (restart #%d) — %s: %s",
                        _state.subscriber_restart_count,
                        type(exc).__name__,
                        exc,
                        exc_info=exc,
                    )
                else:
                    # Returned normally (or was cancelled). Restart anyway —
                    # the subscriber should never exit cleanly during normal
                    # operation; a clean exit is itself a bug worth restarting.
                    log.warning(
                        "khimaira-chat: subscriber_task ended without "
                        "exception (restart #%d); reincarnating",
                        _state.subscriber_restart_count,
                    )
                _state.subscriber_task = asyncio.create_task(_proactive_sse_loop())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("khimaira-chat: watchdog tick failed; continuing")
            continue


async def _direct_channel_notify(content: str, meta: dict[str, str]) -> None:
    """Write a notifications/claude/channel message directly to the
    captured write_stream — equivalent to what session.send_message
    would do but without needing the session object."""
    if _state.write_stream is None:
        return
    notif = types.JSONRPCNotification(
        jsonrpc="2.0",
        method="notifications/claude/channel",
        params={"content": content, "meta": meta},
    )
    msg = SessionMessage(message=types.JSONRPCMessage(root=notif))
    try:
        await _state.write_stream.send(msg)
    except Exception as exc:
        log.warning("khimaira-chat: direct channel notify failed — %s", exc)


def _ancestor_pids(max_depth: int = 5) -> list[int]:
    """Walk the parent chain via /proc/<pid>/status, return ancestor PIDs.

    Claude Code spawns chat MCP via `bash -lc 'uv run khimaira-chat ...'`,
    so the actual chain is Claude Code → bash → uv → khimaira-chat.
    The SessionStart hook (also spawned by Claude Code) posts its OWN
    ppid (= Claude Code's PID). This subprocess's getppid() returns uv's
    PID, not Claude Code's — so a single ppid lookup misses. Walking
    up to grandparents (and beyond) until we find the registered ppid
    bridges that gap.

    Linux-only via /proc; returns [] on other platforms.
    """
    import os as _os

    out: list[int] = []
    cur = _os.getppid()
    for _ in range(max_depth):
        if cur <= 1:
            break
        out.append(cur)
        try:
            with open(f"/proc/{cur}/status", encoding="utf-8") as f:
                next_pid = None
                for line in f:
                    if line.startswith("PPid:"):
                        next_pid = int(line.split()[1])
                        break
            if next_pid is None or next_pid == cur:
                break
            cur = next_pid
        except (OSError, ValueError):
            break
    return out


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # 2026-06-06: the sync ppid-bridge that used to run here was DELETED. It blocked
    # ~3s of daemon HTTP (far longer under load) before stdio even opened, stacking on
    # top of the awaited async bridge in _serve() — together they held the MCP
    # initialize handshake past Claude Code's connect timeout during multi-roster boot
    # storms ("still connecting" roster-wide; ROSTER-LAUNCH-INCIDENT-2026-06-06.md).
    # Registration now runs ONLY as the background task in _serve(); the async bridge
    # (_async_try_auto_register_from_ppid) covers the same ancestor walk with a longer
    # budget, and lazy-reg on first tool call remains the final backstop.
    asyncio.run(_serve())


__all__ = ["main"]
