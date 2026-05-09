"""In-memory heartbeat store.

Keyed by `(project, run_id)`. Each entry records a small ring buffer of
recent events for that run plus the latest current event. Written by the
heartbeat REST endpoint; read by the SSE stream + dashboard.

Why in-memory and not SQLite: heartbeats are high-volume (every node
start/end/llm event from every active app) but ephemeral. They describe
"what's happening RIGHT NOW" — not historical state. Persistence would
add latency for no value; restarting the daemon flushes the world, which
is fine because subsequent app heartbeats refill it within seconds.

Long-term storage is the LangGraph checkpointer (already-persisted) +
the future trace-archival layer (Phase 15+). This module is the
real-time live channel.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Any

from chimera.log import get_logger

log = get_logger("monitor.heartbeats")

# Per-(project, run_id) ring buffer size. 256 events covers a typical
# graph run with hierarchical sub-events; deeper graphs may evict older
# events (acceptable — we keep the most recent activity).
_BUFFER_SIZE = 256

# How long an inactive run is kept before GC. 1h = enough for "what was
# this thread doing 30 min ago?" without growing memory unboundedly.
_RUN_TTL_S = 3600.0

# How often the GC pass runs.
_GC_INTERVAL_S = 300.0


@dataclass
class RunEntry:
    """Per-run state — buffer of recent events + summary fields."""

    project: str
    run_id: str
    events: deque = field(default_factory=lambda: deque(maxlen=_BUFFER_SIZE))
    last_event_ts: float = 0.0
    current_node: str | None = None
    # Per-event new-data signal — async consumers (SSE) wait on this
    new_event: asyncio.Event = field(default_factory=asyncio.Event)


# (project, run_id) → RunEntry
_runs: dict[tuple[str, str], RunEntry] = {}

# Per-project broadcast: any update on any of project's runs wakes the
# project-level new_event so dashboard list views can re-poll.
_project_signals: dict[str, asyncio.Event] = defaultdict(asyncio.Event)

_lock = asyncio.Lock()


def _key(project: str, run_id: str) -> tuple[str, str]:
    return (project, run_id)


async def record(event: dict[str, Any]) -> None:
    """Append an event to the appropriate run's buffer.

    Required keys: project, run_id, event, ts. Other keys preserved
    verbatim — the schema is intentionally open so we can extend the
    observer without changing daemon code.
    """
    project = event.get("project") or "unknown"
    run_id = event.get("run_id")
    if not run_id:
        return  # malformed; drop silently

    key = _key(project, run_id)
    async with _lock:
        entry = _runs.get(key)
        if entry is None:
            entry = RunEntry(project=project, run_id=run_id)
            _runs[key] = entry
        entry.events.append(event)
        entry.last_event_ts = event.get("ts") or time.time()

        # Track current node — heuristic: chain_start without an end is
        # currently active. In practice, the deepest active chain wins.
        evt_kind = event.get("event")
        if evt_kind == "chain_start":
            entry.current_node = event.get("name")
        elif evt_kind == "chain_end" and entry.current_node == event.get("name"):
            entry.current_node = None

        # Wake all consumers
        entry.new_event.set()
        entry.new_event.clear()
        sig = _project_signals[project]
        sig.set()
        sig.clear()


def get(project: str, run_id: str) -> RunEntry | None:
    return _runs.get(_key(project, run_id))


def list_runs(project: str | None = None) -> list[RunEntry]:
    """All known runs, newest activity first. Filterable by project."""
    if project is None:
        items = list(_runs.values())
    else:
        items = [e for e in _runs.values() if e.project == project]
    items.sort(key=lambda e: e.last_event_ts, reverse=True)
    return items


def project_signal(project: str) -> asyncio.Event:
    return _project_signals[project]


async def gc_loop() -> None:
    """Background task: drop runs idle longer than _RUN_TTL_S."""
    while True:
        await asyncio.sleep(_GC_INTERVAL_S)
        cutoff = time.time() - _RUN_TTL_S
        async with _lock:
            stale = [k for k, e in _runs.items() if e.last_event_ts < cutoff]
            for k in stale:
                del _runs[k]
        if stale:
            log.info("heartbeats: gc'd %d idle run(s)", len(stale))


def stats() -> dict:
    return {
        "total_runs": len(_runs),
        "projects": sorted({e.project for e in _runs.values()}),
        "buffer_size_per_run": _BUFFER_SIZE,
        "run_ttl_s": _RUN_TTL_S,
    }
