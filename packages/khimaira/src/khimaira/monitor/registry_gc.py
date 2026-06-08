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
_GC_ENABLED = os.environ.get("KHIMAIRA_REGISTRY_GC", "1") != "0"


def _live_window_titles() -> set[str] | None:
    """Return the set of live kitty window titles, or None if kitty is
    UNAVAILABLE (the no-op signal — caller must reap nothing).

    None vs empty-set is load-bearing: None = "can't tell" (skip the sweep);
    empty set = "kitty answered, zero windows" (everything is genuinely
    reapable). In practice the daemon's own observation tooling keeps at
    least one window, but the distinction is what makes the headless case safe.
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

    titles: set[str] = set()
    for os_win in data:
        for tab in os_win.get("tabs", []):
            for win in tab.get("windows", []):
                t = (win.get("title") or "").strip()
                if t:
                    titles.add(t)
    return titles


def reap_windowless_sessions() -> dict:
    """One GC pass. Reap registry records whose window is gone AND idle past
    the threshold. Returns a summary dict (no raise — fail-open).

    A record is reaped iff ALL hold:
      - live window titles could be enumerated (else NO-OP),
      - the session's name is NOT among live window titles,
      - it has been idle >= _REAP_IDLE_MIN_S,
      - it is not the daemon's own session (delete_session enforces this too).
    """
    live = _live_window_titles()
    if live is None:
        return {"reaped": 0, "skipped": "kitty-unavailable"}

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


async def registry_gc_loop() -> None:
    """Background loop: reap windowless session records every _GC_INTERVAL_S."""
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
        await asyncio.sleep(_GC_INTERVAL_S)
