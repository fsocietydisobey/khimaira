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
    try:
        from khimaira.monitor import roster_recovery as rr

        raw = rr._kitty("ls")
    except Exception:
        return None
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
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


def reap_windowless_sessions() -> dict:
    """One GC pass. Reap registry records whose window is gone AND idle past
    the threshold. Returns a summary dict (no raise — fail-open).

    A record is reaped iff ALL hold:
      - live window titles could be enumerated (else NO-OP),
      - the session's name is NOT among live window titles,
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
        # window gone + idle past threshold → reap
        try:
            res = sessions_mod.delete_session(sid, force=True, reap=True)
            if res.get("deleted"):
                reaped += 1
                log.info(
                    "registry_gc: reaped windowless session %s (name=%r, idle=%.0fs)",
                    sid[:8], name or "(unnamed)", age,
                )
        except Exception as exc:
            log.debug("registry_gc: delete %s failed: %s", sid[:8], exc)

    if reaped:
        log.info("registry_gc: reaped %d windowless session(s); %d live titles",
                 reaped, len(live))
    return {"reaped": reaped, "live_titles": len(live)}


def reap_stale_window_duplicates() -> dict:
    """Reap /clear-orphans: 2+ sessions sharing one kitty window_id where the
    older one is a leftover.

    The trigger: `/clear` in a roster window mints a FRESH session (new UUID,
    nameless) in the SAME kitty window; the operator renames it back to the old
    name, and the previous record lingers — same name AND same window_id. The
    name-based `reap_windowless_sessions` KEEPS that orphan, because its name IS
    a live window title (the live session shares it). This window_id pass is what
    catches it. Bitten twice (griffin-agent-2, then griffin-agent-1, 2026-07-01).

    Discriminator = TURN-MARKER freshness. The live occupant was created AT the
    /clear moment and takes turns afterward, so its `turn_start/turn_end` marker
    is ALWAYS newer than the orphan's (frozen at /clear). We keep the freshest and
    reap the clearly-staler ones.

    SAFETY (a false-reap here cascades chat-leaves — the BUG3 class; see
    reap_windowless_sessions' guards). We reap a duplicate ONLY when all hold:
      - it is NOT the freshest-turn session on that window,
      - the kept session has a marker and this one is CLEARLY older (skip the
        whole group if NObody has a turn marker — can't tell → reap nothing),
      - it is NOT mid-turn (`is_mid_turn`),
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
        groups.setdefault(int(wid), []).append(
            {
                "sid": sid,
                "name": (s.get("name") or "").strip(),
                "fresh": max(marks) if marks else None,
                "age": float(s.get("last_active_age_s") or 0.0),
            }
        )

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
            if m["age"] < _DUP_REAP_IDLE_MIN_S:
                continue  # too fresh to be a settled orphan
            try:
                if sessions_mod.is_mid_turn(m["sid"]):
                    continue  # never reap an actively-working session
            except Exception:
                continue  # can't confirm not-mid-turn → skip (conservative)
            try:
                res = sessions_mod.delete_session(m["sid"], force=True, reap=True)
                if res.get("deleted"):
                    reaped += 1
                    log.info(
                        "registry_gc: reaped /clear-orphan %s (name=%r, idle=%.0fs) "
                        "— window %d now owned by live session %s",
                        m["sid"][:8], m["name"] or "(unnamed)", m["age"], wid,
                        keep["sid"][:8],
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
        int(_REAP_IDLE_MIN_S), int(_GC_INTERVAL_S),
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
