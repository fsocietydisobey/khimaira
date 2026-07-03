"""Registry auto-GC — reap session records whose kitty windows are gone.

The buildup problem (2026-06-08): every roster relaunch mints fresh session
records (resume churn + new UUIDs), and nothing removes the old ones. The
registry climbed 16 → 60+ across a day of relaunches; every `session_list()`
the master calls then dumps ALL of them (~21KB at 60 sessions) straight into
its context — a measurable boot tax (the fresh master hit 79% partly from one
60-session list dump).

The manual fix was a `reap=True` delete sweep keyed on "name not in any live
kitty window title". This module makes that STRUCTURAL: a periodic daemon
sweep reaps records whose window is gone AND that have been idle past a
threshold, so the registry self-cleans and `session_list` stays small.

SAFETY (this sweep can DELETE records, so it is conservative by construction):
  - Kitty-unavailable → NO-OP. If we can't enumerate live windows (headless
    daemon, kitty down, IPC error) we reap NOTHING — never assume "no windows
    means all dead". This is the single most important guard.
  - Idle threshold — only reap sessions idle longer than _REAP_IDLE_MIN_S, so
    a freshly-launched session that hasn't bound its window title yet is safe.
  - reap=True path — delete_session archives decisions before removing and
    marks the session LEFT in its chats (skipping chats where it's master).
  - Self-protection — delete_session already refuses to delete the daemon's
    own CLAUDE_CODE_SESSION_ID.

WINDOW-ID LIVENESS (2026-07-03, id-split fix): the name check above has a gap —
a Claude Code `/clear` mints a fresh, UNNAMED session bound to the SAME kitty
window as its (differently-named) agent. An unnamed session can never satisfy
"name in live titles", so the name-only sweep reaped it even though its window
was very much alive (journal-observed: `reaped WINDOWLESS session 3edda8d8
(name='(unnamed)', idle=2355s)` immediately followed by a chat-leave cascade).
`_live_window_ids()` cross-checks the session's registered `window_id` (see
`sessions.get_session_window`) against the live kitty window ids from the same
`kitty @ ls` snapshot — a session is reapable only if BOTH the name check AND
the window-id check say "not live".

ACCEPTED-CHAT-MEMBER GUARD (2026-07-03, defense-in-depth): independent of the
window checks, a session that is an ACCEPTED member of a chat and has shown
recent tool-call activity is never reap-cascaded, even if the window/name
checks above somehow missed it. See `_accepted_member_skip_reason`. Fail-open
toward SKIP — a false-reap cascades `chats.leave()` across every chat the
session is in, which is far worse than leaving one dead record in the registry
for one more sweep.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time

log = logging.getLogger("monitor.registry_gc")

# Tunables (env-overridable).
_REAP_IDLE_MIN_S = float(os.environ.get("KHIMAIRA_REGISTRY_REAP_IDLE_S", "1800"))  # 30 min
_GC_INTERVAL_S = float(os.environ.get("KHIMAIRA_REGISTRY_GC_INTERVAL_S", "600"))  # 10 min
# RE-ENABLED 2026-06-12 after the muther-symptom-2 false-positive reaps were
# fixed at the source: liveness now matches the drift-proof launch `-n` name
# (not just the mutable window title), and a transient empty kitty result is
# treated as can't-tell rather than reap-everything. Opt out with
# KHIMAIRA_REGISTRY_GC=0 if a new false-positive class surfaces.
_GC_ENABLED = os.environ.get("KHIMAIRA_REGISTRY_GC", "1") != "0"
# /clear-orphan dedup: how long a same-window duplicate must be idle before it's
# reaped. Shorter than the windowless threshold — a co-located session with fresher
# turns is strong evidence the older one is orphaned — but non-zero to avoid reaping
# a session that's merely between turns. Env-overridable.
_DUP_REAP_IDLE_MIN_S = float(os.environ.get("KHIMAIRA_REGISTRY_DUP_REAP_IDLE_S", "300"))  # 5 min
# Audit-grade activity veto (2026-07-02): a same-window duplicate that made a TOOL
# CALL within this window is treated as live and NEVER reaped, regardless of how its
# turn markers compare. Turn-marker freshness is inspection-grade (a live worker
# mid-long-build stamps a stale turn_end and looks frozen); tool-call recency is the
# side-effect signal that outranks it. Generous by design — a settled orphan hasn't
# issued a tool call since /clear. Env-overridable.
_DUP_REAP_ACTIVITY_VETO_S = float(
    os.environ.get("KHIMAIRA_REGISTRY_DUP_ACTIVITY_VETO_S", "900")
)  # 15 min
# Accepted-chat-member guard (2026-07-03): a session that is an ACCEPTED member
# of a chat and made a tool call within this window is treated as live and
# never reap-cascaded, regardless of which reaper (windowless or dup) reached
# it. Same rationale as _DUP_REAP_ACTIVITY_VETO_S — tool-call recency is the
# audit-grade liveness signal — but applied as an independent guard on TOP of
# the name/window checks, not a replacement for them. Env-overridable.
_REAP_MEMBER_ACTIVITY_S = float(os.environ.get("KHIMAIRA_REAP_MEMBER_ACTIVITY_S", "1800"))  # 30 min


def _name_from_cmdline(cmdline: list[str]) -> str | None:
    """Extract the Claude Code session name from a `-n <name>` launch flag.
    This is the STABLE identity: set at window launch, it never drifts the way
    the window TITLE does. Matching on it (not just title) is what stops a live
    agent whose title drifted from being mistaken for windowless."""
    for i, tok in enumerate(cmdline):
        if tok == "-n" and i + 1 < len(cmdline):
            return (cmdline[i + 1] or "").strip() or None
        if tok.startswith("-n") and len(tok) > 2:
            return tok[2:].strip() or None
        if tok.startswith("--session-name="):
            return tok.split("=", 1)[1].strip() or None
    return None


def _iso_to_epoch(raw: str | None) -> float | None:
    """Parse an ISO-8601 tool-call timestamp → epoch seconds, or None on any
    failure. Mirrors sessions._read_marker_ts' parse for a raw string (not a file)."""
    if not raw:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(raw.replace("Z", "+00:00")).timestamp()
    except (ValueError, AttributeError):
        return None


def _kitty_ls_data() -> list | None:
    """Fetch + parse `kitty @ ls` JSON once. Returns None if kitty is
    unavailable or the response can't be parsed.

    Shared by `_live_window_identities` (name-based liveness) and
    `_live_window_ids` (id-based liveness, 2026-07-03) so both checks read the
    same snapshot instead of shelling out to kitty twice per sweep.
    """
    try:
        from khimaira.monitor import roster_recovery as rr

        raw = rr._kitty("ls")
    except Exception:
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None


def _live_window_identities() -> set[str] | None:
    """Return the set of identities (names) that prove a LIVE window, or None if
    kitty is UNAVAILABLE (the no-op signal — caller must reap nothing).

    Each live window contributes BOTH its current title AND its launch `-n` name
    (from the foreground process cmdline). Title-only matching false-reaped live
    agents whose title drifted from their session name (muther symptom 2); the
    launch name is drift-proof.

    None vs empty-set is load-bearing: None = "can't tell" (skip). An empty set
    ("kitty answered, zero windows") is treated as suspicious by the caller and
    also skips — the daemon's own tooling keeps ≥1 window, so empty almost always
    means a transient kitty hiccup, not a genuinely empty desktop.
    """
    data = _kitty_ls_data()
    if data is None:
        return None

    names: set[str] = set()
    for os_win in data:
        for tab in os_win.get("tabs", []):
            for win in tab.get("windows", []):
                t = (win.get("title") or "").strip()
                if t:
                    names.add(t)
                    # Kitty decorates window titles with activity/bell markers
                    # (✳ idle, ⠂ thinking, * bell, etc.) that break exact
                    # name-match. Add the de-decorated form too, so a live
                    # "✳ muther" window still proves the "muther" session is
                    # alive. Without this, the reaper false-deletes the session
                    # AND cascades a chat-membership leave (delete_session marks
                    # the reaped session LEFT in every chat) — muther was dropped
                    # from her jeevy roster chat twice on 2026-06-21 this way.
                    # Mirrors roster_recovery's title-match normalizer.
                    cleaned = t.lstrip("✳🔔★*•⠂ ").strip()
                    if cleaned and cleaned != t:
                        names.add(cleaned)
                for proc in win.get("foreground_processes", []):
                    nm = _name_from_cmdline(proc.get("cmdline") or [])
                    if nm:
                        names.add(nm)
    return names


def _live_window_ids() -> set[int] | None:
    """Return the set of kitty window IDs currently live, or None if kitty is
    UNAVAILABLE (same no-op signal as `_live_window_identities`).

    2026-07-03 id-split fix: a Claude Code `/clear` mints a fresh, UNNAMED
    session bound to the SAME kitty window as its (differently-named) agent.
    Name-based liveness can never protect that session — it has no name to
    match — but its window is very much alive. This is the cross-check:
    `reap_windowless_sessions` keeps a session whose registered `window_id`
    (`sessions.get_session_window`) is in this set, even when its name matches
    nothing live.

    None here means "can't tell" — callers must treat it exactly like a
    `_live_window_identities` None (degrade gracefully, don't reap on it
    alone). Unlike the name check, an empty set from THIS function is not
    independently treated as suspicious — `reap_windowless_sessions` only
    calls this after the name check already passed its own empty/None guard,
    so an empty id set here just means "no additional id-based protection",
    not "kitty is lying".
    """
    data = _kitty_ls_data()
    if data is None:
        return None

    ids: set[int] = set()
    for os_win in data:
        for tab in os_win.get("tabs", []):
            for win in tab.get("windows", []):
                wid = win.get("id")
                if wid is None:
                    continue
                try:
                    ids.add(int(wid))
                except (TypeError, ValueError):
                    continue
    return ids


def _accepted_member_skip_reason(session_id: str) -> str | None:
    """Defense-in-depth guard shared by both reapers (2026-07-03): a session
    that is an ACCEPTED member of a chat and has shown recent tool-call
    activity must never be reap-cascaded, even if the caller's window/name
    liveness checks somehow missed it.

    Returns a short, grep-able skip-reason string if the reap should be
    SKIPPED, or None if the caller may proceed (not an accepted member of any
    chat, or accepted but genuinely stale — no recent tool call, safe to reap
    as legitimate cleanup of a dead roster member).

    Fail-open toward SKIP: a false-reap here cascades `chats.leave()` across
    every chat the session is in (the BUG3 class), which is far worse than
    leaving one dead record in the registry for one more sweep. ANY error
    while checking membership or activity is treated as "can't tell" → skip.
    """
    try:
        from khimaira.monitor import chats as chats_mod

        rooms = chats_mod.my_chats(session_id)
    except Exception as exc:
        log.debug(
            "registry_gc: membership check failed for %s (%s) — skipping reap (conservative)",
            session_id[:8],
            exc,
        )
        return "membership-check-failed"

    if not any(c.get("my_state") == chats_mod.ACCEPTED for c in rooms):
        return None  # not an accepted chat member — no extra protection needed

    try:
        from khimaira.monitor import sessions as sessions_mod

        calls = sessions_mod.recent_tool_calls(session_id, limit=1)
        last_tool = _iso_to_epoch(calls[0].get("ts")) if calls else None
    except Exception as exc:
        log.debug(
            "registry_gc: activity check failed for %s (%s) — skipping reap (conservative)",
            session_id[:8],
            exc,
        )
        return "activity-check-failed"

    if last_tool is not None and (time.time() - last_tool) < _REAP_MEMBER_ACTIVITY_S:
        return "accepted-chat-member-recent-activity"

    return None  # accepted member but idle past the activity window → genuinely dead


def reap_windowless_sessions() -> dict:
    """One GC pass. Reap registry records whose window is gone AND idle past
    the threshold. Returns a summary dict (no raise — fail-open).

    A record is reaped iff ALL hold:
      - live window titles could be enumerated (else NO-OP),
      - the session's name is NOT among live window titles,
      - the session's registered window_id is NOT among live window ids
        (2026-07-03 id-split fix — protects an unnamed session sharing a live
        agent's window; see `_live_window_ids`),
      - it is not an ACCEPTED chat member showing recent tool-call activity
        (defense-in-depth; see `_accepted_member_skip_reason`),
      - it has been idle >= _REAP_IDLE_MIN_S,
      - it is not the daemon's own session (delete_session enforces this too).
    """
    live = _live_window_identities()
    if live is None:
        return {"reaped": 0, "skipped": "kitty-unavailable"}
    if not live:
        # "Zero windows" is almost always a transient kitty hiccup, not a real
        # empty desktop (the daemon's own tooling keeps ≥1 window). Reaping the
        # whole registry on a transient empty was a mass false-positive path —
        # treat empty as can't-tell and skip.
        return {"reaped": 0, "skipped": "kitty-empty-suspicious"}

    # id-split fix: cross-check against live window IDS too, so an unnamed
    # session sharing a live agent's window survives even though its name (or
    # lack thereof) never matches. A failed id enumeration degrades gracefully
    # to the pre-fix name-only behavior instead of blocking the whole sweep —
    # it's additive protection, not a new no-op gate.
    live_window_ids = _live_window_ids()
    if live_window_ids is None:
        log.debug(
            "registry_gc: live window-id enumeration failed — falling back to name-only liveness"
        )
        live_window_ids = set()

    try:
        from khimaira.monitor import sessions as sessions_mod

        rows = sessions_mod.list_sessions(use_cache=False)
    except Exception as exc:
        log.debug("registry_gc: list_sessions failed: %s", exc)
        return {"reaped": 0, "skipped": "list-failed"}

    self_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    reaped = 0
    for s in rows:
        sid = s.get("session_id") or ""
        name = (s.get("name") or "").strip()
        age = s.get("last_active_age_s", 0.0) or 0.0
        if not sid or sid == self_id:
            continue
        if age < _REAP_IDLE_MIN_S:
            continue  # too fresh — its window may not be titled/bound yet
        if name and name in live:
            continue  # a live window holds this name → keep
        try:
            wid = sessions_mod.get_session_window(sid)
        except Exception:
            wid = None
        if wid is not None and wid in live_window_ids:
            log.info(
                "registry_gc: keeping id-split session %s (name=%r) — window %s is live "
                "though the name doesn't match any live title",
                sid[:8],
                name or "(unnamed)",
                wid,
            )
            continue  # window is live even though name isn't → keep
        skip_reason = _accepted_member_skip_reason(sid)
        if skip_reason:
            log.info(
                "registry_gc: keeping windowless session %s (name=%r) — %s",
                sid[:8],
                name or "(unnamed)",
                skip_reason,
            )
            continue
        # window gone + idle past threshold → reap
        try:
            res = sessions_mod.delete_session(sid, force=True, reap=True)
            if res.get("deleted"):
                reaped += 1
                log.info(
                    "registry_gc: reaped windowless session %s (name=%r, idle=%.0fs)",
                    sid[:8],
                    name or "(unnamed)",
                    age,
                )
        except Exception as exc:
            log.debug("registry_gc: delete %s failed: %s", sid[:8], exc)

    if reaped:
        log.info("registry_gc: reaped %d windowless session(s); %d live titles", reaped, len(live))
    return {"reaped": reaped, "live_titles": len(live)}


def _migrate_chat_memberships(orphan_sid: str, heir_sid: str) -> list[str]:
    """Hand the orphan's ACCEPTED chat memberships to the heir (the live co-window
    session) BEFORE the orphan is reaped.

    Root fix for the chat↔registry id desync (griffin-agent-1, 2026-07-02). `/clear`
    mints a fresh session (heir) in the same kitty window; the operator renames it
    back, and the previous record (orphan) lingers. The chat store still keys the
    roster membership on the ORPHAN's id while the monitor registry now names the
    HEIR — the two stores disagree. When the orphan is reaped, delete_session marks
    it LEFT in every chat, so the heir — the session the operator actually /cleared
    into — inherits NOTHING and silently loses roster-chat access.

    Migrating the memberships first closes the desync at its root: the heir becomes
    the ACCEPTED chat member (and inherits the master role if the orphan was creator,
    via transfer_membership's creator-propagation), so both id-stores agree post-reap.
    After a successful transfer the orphan is TRANSFERRED_OUT, so delete_session's
    leave-cascade skips that chat — no double-handling.

    Per-chat failures are non-fatal and skipped (the reap's leave-cascade cleans up
    whatever didn't transfer):
      - heir already ACCEPTED in the chat → 409 (nothing to migrate; heir's already in)
      - orphan not ACCEPTED (pending/left) → 403 (nothing load-bearing to move)
      - heir unresolvable → 404 (shouldn't happen — heir is a live registry record)

    Returns the chat_ids successfully migrated (for the reap log). Fail-open: any
    setup error (chats import, my_chats read) returns an empty list — migration is
    best-effort and must NEVER block the reap it precedes.
    """
    migrated: list[str] = []
    try:
        from khimaira.monitor import chats as chats_mod
    except Exception:
        return migrated
    try:
        rooms = chats_mod.my_chats(orphan_sid)
    except Exception:
        return migrated
    for c in rooms:
        if c.get("my_state") != chats_mod.ACCEPTED:
            continue  # only ACCEPTED memberships transfer (transfer_membership 403s otherwise)
        chat_id = c.get("chat_id")
        if not chat_id:
            continue
        try:
            chats_mod.transfer_membership(chat_id, orphan_sid, heir_sid)
            migrated.append(chat_id)
        except Exception as exc:
            log.debug(
                "registry_gc: membership migrate skip %s (%s→%s): %s",
                chat_id,
                orphan_sid[:8],
                heir_sid[:8],
                exc,
            )
    return migrated


def reap_stale_window_duplicates() -> dict:
    """Reap /clear-orphans: 2+ sessions sharing one kitty window_id where the
    older one is a leftover.

    The trigger: `/clear` in a roster window mints a FRESH session (new UUID,
    nameless) in the SAME kitty window; the operator renames it back to the old
    name, and the previous record lingers — same name AND same window_id. The
    name-based `reap_windowless_sessions` KEEPS that orphan, because its name IS
    a live window title (the live session shares it). This window_id pass is what
    catches it. Bitten twice (griffin-agent-2, then griffin-agent-1, 2026-07-01).

    Discriminator = TURN-MARKER freshness, GUARDED by tool-call activity. The live
    occupant was created AT the /clear moment and takes turns afterward, so its
    `turn_start/turn_end` marker is usually newer than the orphan's (frozen at
    /clear). We keep the freshest and reap the clearly-staler ones — BUT turn-marker
    freshness is inspection-grade and can point the wrong way: a live worker mid-
    long-build stamps a stale `turn_end` (looks frozen) while making tool calls the
    whole time, so a marker-only reap can evict the REAL worker and cascade its
    chat-leave. griffin-agent-1 (2026-07-02) was reaped this way. So the survivor
    decision is cross-checked against tool-call recency — the AUDIT-GRADE side-effect
    signal (a session issuing tool calls, incl. chat_send/task_update, is alive).

    SAFETY (a false-reap here cascades chat-leaves — the BUG3 class; see
    reap_windowless_sessions' guards). We reap a duplicate ONLY when all hold:
      - it is NOT the freshest-turn session on that window,
      - the kept session has a marker and this one is CLEARLY older (skip the
        whole group if NObody has a turn marker — can't tell → reap nothing),
      - it has NOT made a tool call within _DUP_REAP_ACTIVITY_VETO_S (absolute
        liveness veto), AND is not at-least-as-tool-active as the kept session
        (relative veto — audit-grade activity outranks marker freshness),
      - it is NOT mid-turn (`is_mid_turn`),
      - it is NOT an ACCEPTED chat member showing recent tool-call activity
        (2026-07-03 defense-in-depth guard; see `_accepted_member_skip_reason`),
      - it has been idle >= _DUP_REAP_IDLE_MIN_S.
    Registry-only (no kitty dependency), so it runs even when the windowless
    sweep no-ops on kitty-unavailable.
    """
    try:
        from khimaira.monitor import sessions as sessions_mod

        rows = sessions_mod.list_sessions(use_cache=False)
    except Exception as exc:
        log.debug("registry_gc: dup-reap list_sessions failed: %s", exc)
        return {"reaped": 0, "skipped": "list-failed"}

    self_id = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    groups: dict[int, list[dict]] = {}
    for s in rows:
        sid = s.get("session_id") or ""
        if not sid or sid == self_id:
            continue
        try:
            wid = sessions_mod.get_session_window(sid)
        except Exception:
            wid = None
        if not wid:
            continue
        sdir = sessions_mod._session_dir(sid)
        ts = sessions_mod._read_marker_ts(sdir / "turn_start.txt")
        te = sessions_mod._read_marker_ts(sdir / "turn_end.txt")
        marks = [m for m in (ts, te) if m is not None]
        # Audit-grade liveness: the most recent TOOL CALL (incl. chat_send /
        # task_update). Outranks turn-marker freshness in the reap decision below.
        last_tool: float | None = None
        try:
            calls = sessions_mod.recent_tool_calls(sid, limit=1)
            if calls:
                last_tool = _iso_to_epoch(calls[0].get("ts"))
        except Exception:
            last_tool = None
        groups.setdefault(int(wid), []).append(
            {
                "sid": sid,
                "name": (s.get("name") or "").strip(),
                "fresh": max(marks) if marks else None,
                "last_tool": last_tool,
                "age": float(s.get("last_active_age_s") or 0.0),
            }
        )

    now = time.time()
    reaped = 0
    for wid, members in groups.items():
        if len(members) < 2:
            continue
        with_marker = [m for m in members if m["fresh"] is not None]
        if not with_marker:
            continue  # nobody has a turn marker → can't pick the live one → skip
        keep = max(with_marker, key=lambda m: m["fresh"])
        for m in members:
            if m["sid"] == keep["sid"]:
                continue
            if m["fresh"] is not None and m["fresh"] >= keep["fresh"]:
                continue  # not clearly staler than the kept session → skip
            # AUDIT-GRADE VETO: tool-call activity outranks turn-marker freshness.
            # A session issuing tool calls is definitionally alive; reaping it would
            # evict a live worker and cascade its chat-leave (BUG3 class). This is
            # what marker-only freshness got wrong for griffin-agent-1 (2026-07-02).
            m_tool = m["last_tool"]
            if m_tool is not None:
                if (now - m_tool) < _DUP_REAP_ACTIVITY_VETO_S:
                    continue  # made a tool call recently → alive (absolute veto)
                keep_tool = keep["last_tool"]
                if keep_tool is not None and m_tool >= keep_tool:
                    continue  # ≥ as tool-active as the kept session → don't reap
            if m["age"] < _DUP_REAP_IDLE_MIN_S:
                continue  # too fresh to be a settled orphan
            try:
                if sessions_mod.is_mid_turn(m["sid"]):
                    continue  # never reap an actively-working session
            except Exception:
                continue  # can't confirm not-mid-turn → skip (conservative)
            # ACCEPTED-CHAT-MEMBER GUARD (2026-07-03, defense-in-depth): applies to
            # both reapers — see _accepted_member_skip_reason. Catches the case
            # where a duplicate is an accepted roster member with recent tool
            # activity that the marker/tool-veto logic above didn't already skip.
            skip_reason = _accepted_member_skip_reason(m["sid"])
            if skip_reason:
                log.info(
                    "registry_gc: keeping /clear-orphan candidate %s (name=%r, window %d) — %s",
                    m["sid"][:8],
                    m["name"] or "(unnamed)",
                    wid,
                    skip_reason,
                )
                continue
            # MEMBERSHIP MIGRATION (2026-07-02): hand the orphan's chat memberships
            # to the live heir BEFORE the reap, so the session the operator /cleared
            # into inherits roster-chat access instead of the reap silently dropping
            # it. Closes the chat↔registry id desync at its root. Best-effort — never
            # blocks the reap.
            migrated = _migrate_chat_memberships(m["sid"], keep["sid"])
            try:
                res = sessions_mod.delete_session(m["sid"], force=True, reap=True)
                if res.get("deleted"):
                    reaped += 1
                    log.info(
                        "registry_gc: reaped /clear-orphan %s (name=%r, idle=%.0fs) "
                        "— window %d now owned by live session %s; migrated %d chat(s): %s",
                        m["sid"][:8],
                        m["name"] or "(unnamed)",
                        m["age"],
                        wid,
                        keep["sid"][:8],
                        len(migrated),
                        migrated or "none",
                    )
            except Exception as exc:
                log.debug("registry_gc: dup-reap %s failed: %s", m["sid"][:8], exc)

    if reaped:
        log.info("registry_gc: reaped %d /clear-orphan duplicate(s)", reaped)
    return {"reaped": reaped}


async def registry_gc_loop() -> None:
    """Background loop: reap windowless records + /clear-orphan duplicates every
    _GC_INTERVAL_S."""
    if not _GC_ENABLED:
        log.info("registry_gc: disabled via KHIMAIRA_REGISTRY_GC=0")
        return
    log.info(
        "registry_gc: started (idle_threshold=%ds, interval=%ds)",
        int(_REAP_IDLE_MIN_S),
        int(_GC_INTERVAL_S),
    )
    while True:
        try:
            reap_windowless_sessions()
        except Exception:
            log.exception("registry_gc: sweep error")
        try:
            reap_stale_window_duplicates()
        except Exception:
            log.exception("registry_gc: dup-reap sweep error")
        await asyncio.sleep(_GC_INTERVAL_S)
