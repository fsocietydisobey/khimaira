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

from khimaira.log import get_logger

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


# Rough USD per million tokens (input, output). Updated from public
# pricing pages — order of magnitude correct, may drift; the goal is
# a "$/session/day" estimate, not invoice accounting. Add models as
# the observer encounters them.
_COST_PER_MTOK = {
    # Anthropic
    "claude-opus-4": (15.0, 75.0),
    "claude-opus-4.7": (15.0, 75.0),
    "claude-sonnet-4": (3.0, 15.0),
    "claude-sonnet-4.6": (3.0, 15.0),
    "claude-haiku-4.5": (1.0, 5.0),
    # OpenAI
    "gpt-4o": (2.5, 10.0),
    "gpt-4o-mini": (0.15, 0.6),
    "gpt-4.1": (2.0, 8.0),
    # Google
    "gemini-2.5-pro": (1.25, 5.0),
    "gemini-2.5-flash": (0.075, 0.3),
    "gemini-2.0-flash": (0.075, 0.3),
}


def _model_cost_per_mtok(model: str | None) -> tuple[float, float]:
    """Best-effort price lookup. Falls back to (0, 0) if unrecognized.

    Matches by prefix so versioned model names ('claude-sonnet-4-6-20251101')
    resolve to the family rate. The goal is a useful estimate, not exact
    billing — invoice accounting comes from each provider's API.
    """
    if not model:
        return (0.0, 0.0)
    m = model.lower()
    for key, rate in _COST_PER_MTOK.items():
        if m.startswith(key.lower()):
            return rate
    return (0.0, 0.0)


def cost_summary(project: str) -> dict:
    """Aggregate llm_end events into per-model cost + telemetry overhead.

    Telemetry overhead = count of external_* events to api.smith.langchain.com.
    Useful signal because LangSmith calls are often 50-100 per LangGraph run
    and add up to real bandwidth even without API costs.

    Returns:
        {
            project, run_count, total_input_tokens, total_output_tokens,
            total_cost_usd, by_model: {model: {input_tokens, output_tokens,
                cost_usd, calls}}, telemetry_calls: int
        }

    Note: cost is best-effort estimate. Cost rate table is rough (pinned
    to public list prices). Negotiated / batch rates not reflected. For
    invoice accounting, query the provider directly.
    """
    by_model: dict[str, dict] = {}
    telemetry_calls = 0
    total_in = 0
    total_out = 0
    total_cost = 0.0
    runs_seen: set[str] = set()

    for entry in _runs.values():
        if entry.project != project:
            continue
        for ev in entry.events:
            ekind = ev.get("event") or ""
            runs_seen.add(ev.get("run_id") or "")
            if ekind == "llm_end":
                extra = ev.get("extra") or {}
                if not isinstance(extra, dict):
                    continue
                in_tok = int(extra.get("input_tokens") or 0)
                out_tok = int(extra.get("output_tokens") or 0)
                model = extra.get("model") or "unknown"
                in_rate, out_rate = _model_cost_per_mtok(model)
                cost = (in_tok * in_rate + out_tok * out_rate) / 1_000_000.0
                total_in += in_tok
                total_out += out_tok
                total_cost += cost
                bm = by_model.setdefault(
                    model,
                    {
                        "input_tokens": 0,
                        "output_tokens": 0,
                        "cost_usd": 0.0,
                        "calls": 0,
                    },
                )
                bm["input_tokens"] += in_tok
                bm["output_tokens"] += out_tok
                bm["cost_usd"] += cost
                bm["calls"] += 1
            elif ekind == "external_start":
                if (ev.get("name") or "").lower() == "api.smith.langchain.com":
                    telemetry_calls += 1

    return {
        "project": project,
        "run_count": len(runs_seen),
        "total_input_tokens": total_in,
        "total_output_tokens": total_out,
        "total_cost_usd": round(total_cost, 4),
        "by_model": {
            m: {**v, "cost_usd": round(v["cost_usd"], 4)}
            for m, v in sorted(by_model.items(), key=lambda kv: -kv[1]["cost_usd"])
        },
        "telemetry_calls_langsmith": telemetry_calls,
        "note": "estimate based on public list prices; not invoice-accurate",
    }


def cost_timeseries(
    project: str,
    *,
    bucket_minutes: int = 5,
    window_minutes: int = 60,
) -> dict:
    """Cost binned into time buckets — backs the sparkline chart in the UI.

    Iterates the same llm_end events as `cost_summary`, but groups by
    bucket instead of by model. Returns one entry per bucket within the
    window, ALWAYS including empty buckets (cost=0, calls=0) so the
    sparkline's x-axis is uniform.

    Defaults match the dashboard's "what's happened recently" framing:
    60-minute window in 5-minute buckets = 12 points. Buckets are
    aligned to wall-clock boundaries (e.g. :00, :05, :10) so two
    polls a moment apart show the same buckets.

    Returns:
        {
            project, bucket_minutes, window_minutes,
            buckets: [{ts_start, ts_end, cost_usd, llm_calls}, ...],
        }
        Bucket order: oldest → newest (left to right on the chart).
    """
    bucket_seconds = max(60, bucket_minutes * 60)
    window_seconds = max(bucket_seconds, window_minutes * 60)
    now = time.time()
    # Align the latest bucket to the wall-clock boundary so consecutive
    # polls produce the same x-axis (avoids the "sparkline jitters"
    # bug where every refresh shifts the bars by a few seconds).
    latest_bucket_start = (int(now) // bucket_seconds) * bucket_seconds
    oldest_bucket_start = latest_bucket_start - (window_seconds - bucket_seconds)

    n_buckets = ((latest_bucket_start - oldest_bucket_start) // bucket_seconds) + 1
    buckets: list[dict[str, Any]] = [
        {
            "ts_start": oldest_bucket_start + i * bucket_seconds,
            "ts_end": oldest_bucket_start + (i + 1) * bucket_seconds,
            "cost_usd": 0.0,
            "llm_calls": 0,
        }
        for i in range(int(n_buckets))
    ]

    for entry in _runs.values():
        if entry.project != project:
            continue
        for ev in entry.events:
            if ev.get("event") != "llm_end":
                continue
            ts = ev.get("ts")
            if not isinstance(ts, (int, float)):
                continue
            if ts < oldest_bucket_start or ts >= latest_bucket_start + bucket_seconds:
                continue
            idx = int((ts - oldest_bucket_start) // bucket_seconds)
            if idx < 0 or idx >= len(buckets):
                continue
            extra = ev.get("extra") or {}
            if not isinstance(extra, dict):
                continue
            in_tok = int(extra.get("input_tokens") or 0)
            out_tok = int(extra.get("output_tokens") or 0)
            model = extra.get("model") or "unknown"
            in_rate, out_rate = _model_cost_per_mtok(model)
            buckets[idx]["cost_usd"] += (
                in_tok * in_rate + out_tok * out_rate
            ) / 1_000_000.0
            buckets[idx]["llm_calls"] += 1

    for b in buckets:
        b["cost_usd"] = round(b["cost_usd"], 6)

    return {
        "project": project,
        "bucket_minutes": bucket_minutes,
        "window_minutes": window_minutes,
        "buckets": buckets,
    }


# Default thresholds (seconds) for slow-call detection. Tunable via the
# /slow endpoint's ?chain=&llm=&tool=&external= query params. These
# defaults match common app patterns: chains/graphs are allowed to take
# tens of seconds (multi-step), LLM individual calls beyond 10s are
# usually worth investigating, tools should be sub-5s, external HTTP
# beyond 30s is unusual outside of long-running ML inference.
_SLOW_DEFAULTS = {
    "chain": 30.0,
    "llm": 10.0,
    "tool": 5.0,
    "external": 30.0,
}


def find_slow_calls(
    project: str,
    thresholds: dict[str, float] | None = None,
) -> list[dict]:
    """Scan recent events; return start-events whose paired end exceeded
    the per-kind threshold (or are still in flight beyond it).

    Pairs by run_id (LangChain's per-callback id, not correlation_id) +
    matching event prefix (chain_/llm_/tool_/external_). For events
    still in flight, the duration is "wall_time so far" — surfaces
    stuck calls too.

    Returns one record per slow event with:
      kind, run_id, name, started_ts, ended_ts (or None if in flight),
      duration_ms, threshold_ms, project, correlation_id (if tagged),
      extra (the original event's extra payload from the END event).
    """
    thresh = {**_SLOW_DEFAULTS, **(thresholds or {})}
    now = time.time()
    out: list[dict] = []

    for entry in _runs.values():
        if entry.project != project:
            continue
        # Index events by (kind, run_id) and pair start+end
        starts: dict[tuple[str, str], dict] = {}
        ends: dict[tuple[str, str], dict] = {}
        for ev in entry.events:
            ekind = ev.get("event") or ""
            for k in ("chain", "llm", "tool", "external"):
                if ekind.startswith(k):
                    rid = ev.get("run_id") or ""
                    suffix = ekind[len(k) + 1 :]  # 'start', 'end', 'error'
                    if suffix == "start":
                        starts[(k, rid)] = ev
                    elif suffix in ("end", "error"):
                        ends[(k, rid)] = ev
                    break

        for (kind, rid), start in starts.items():
            end = ends.get((kind, rid))
            started_ts = start.get("ts") or 0.0
            if end:
                ended_ts = end.get("ts") or started_ts
                duration_s = ended_ts - started_ts
            else:
                ended_ts = None
                duration_s = now - started_ts

            t = thresh.get(kind, float("inf"))
            if duration_s < t:
                continue
            out.append(
                {
                    "kind": kind,
                    "run_id": rid,
                    "name": start.get("name"),
                    "started_ts": started_ts,
                    "ended_ts": ended_ts,
                    "in_flight": end is None,
                    "duration_ms": int(duration_s * 1000),
                    "threshold_ms": int(t * 1000),
                    "project": project,
                    "correlation_id": start.get("correlation_id"),
                    "extra": (end or {}).get("extra"),
                }
            )

    out.sort(key=lambda r: r["duration_ms"], reverse=True)
    return out


def events_by_correlation(project: str, correlation_id: str) -> list[dict]:
    """All events across all runs in `project` tagged with `correlation_id`.

    Closes the gap where one app-level run spawns N LangChain per-callback
    runs, each with its own UUID. Querying "what happened during my run X"
    used to require scanning every run in the project. With observer v0.4.0+
    setting correlation_id via tag_run(), this returns the full ordered
    timeline for one logical app run.

    Sorted by ts ascending (chronological — natural reading order for
    timeline reconstruction). Includes the run_id alongside each event so
    callers can drill into specific sub-runs if needed.
    """
    out: list[dict] = []
    for entry in _runs.values():
        if entry.project != project:
            continue
        for ev in entry.events:
            if ev.get("correlation_id") == correlation_id:
                out.append(ev)
    out.sort(key=lambda e: e.get("ts", 0))
    return out


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
