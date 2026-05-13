"""`/api/usage` — read the LLM usage JSONL and produce rollups.

Returns:
  - `last_5m`, `last_1h`, `last_24h`: token + cost totals for each window
  - `by_role`, `by_model`, `by_provider`: breakdowns over the last 24h
  - `recent`: the last N call records, oldest first

Reads the tail of `~/.local/state/khimaira/usage.jsonl` lazily on each
request — fast enough at khimaira-scale (a heavy day = ~10k lines, ~2MB).
If the file grows unwieldy, rotate it manually; the dashboard cares
about recent data only.

Cost is best-effort. Unknown models record token counts but estimate
$0; the rate-anomaly check uses tokens-per-minute as the primary signal
so an unknown model still trips the alarm.
"""

from __future__ import annotations

import json
import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from khimaira.usage import log_file_path

from .._optional import require

# Tail-read window: never re-read more than this many lines from disk.
# At one line per LLM call, 50k = roughly a week of moderate activity.
_MAX_TAIL_LINES = 50_000

# Default API response trims `recent` to this many calls (oldest first).
_DEFAULT_RECENT = 50


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.get("/usage")
    async def get_usage(recent: int = _DEFAULT_RECENT) -> dict[str, Any]:
        records = _load_recent_records(_MAX_TAIL_LINES)
        if not records:
            return {
                "log_path": str(log_file_path()),
                "total_records": 0,
                "windows": {"last_5m": _empty_window(), "last_1h": _empty_window(), "last_24h": _empty_window()},
                "by_role": {},
                "by_model": {},
                "by_provider": {},
                "recent": [],
                "note": (
                    "no usage records yet — file is empty or doesn't exist. "
                    "Trigger an LLM call (chain_pipeline, swarm, etc.) and "
                    "this populates."
                ),
            }

        now = datetime.now(timezone.utc)
        return {
            "log_path": str(log_file_path()),
            "total_records": len(records),
            "windows": {
                "last_5m": _aggregate_window(records, now, minutes=5),
                "last_1h": _aggregate_window(records, now, minutes=60),
                "last_24h": _aggregate_window(records, now, minutes=24 * 60),
            },
            "by_role": _group(records, key="role", since=now - timedelta(hours=24)),
            "by_model": _group(records, key="model", since=now - timedelta(hours=24)),
            "by_provider": _group(records, key="provider", since=now - timedelta(hours=24)),
            "recent": records[-recent:],
        }

    return router


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _empty_window() -> dict[str, float]:
    return {"calls": 0, "input_tokens": 0, "output_tokens": 0, "estimated_cost_usd": 0.0}


def _load_recent_records(max_lines: int) -> list[dict[str, Any]]:
    """Read up to the last `max_lines` lines of the usage log. Skips
    malformed lines silently — a corrupt JSONL row shouldn't break the
    endpoint."""
    path = log_file_path()
    if not path.exists():
        return []

    # Cheap tail: read whole file when small (<8MB), else read last 8MB
    # and discard the partial head line. JSONL is line-delimited, so
    # any truncation only loses one line.
    size = path.stat().st_size
    if size <= 8 * 1024 * 1024:
        data = path.read_bytes()
    else:
        with path.open("rb") as f:
            f.seek(-8 * 1024 * 1024, os.SEEK_END)
            data = f.read()

    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > 8 * 1024 * 1024 and lines:
        # Discard the (likely truncated) first line.
        lines = lines[1:]
    if len(lines) > max_lines:
        lines = lines[-max_lines:]

    out: list[dict[str, Any]] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _aggregate_window(
    records: list[dict[str, Any]],
    now: datetime,
    *,
    minutes: int,
) -> dict[str, float]:
    cutoff = now - timedelta(minutes=minutes)
    win = _empty_window()
    for r in records:
        ts = _parse_ts(r.get("ts"))
        if ts is None or ts < cutoff:
            continue
        win["calls"] += 1
        win["input_tokens"] += int(r.get("input_tokens", 0) or 0)
        win["output_tokens"] += int(r.get("output_tokens", 0) or 0)
        win["estimated_cost_usd"] += float(r.get("estimated_cost_usd", 0.0) or 0.0)
    win["estimated_cost_usd"] = round(win["estimated_cost_usd"], 6)
    return win


def _group(
    records: list[dict[str, Any]],
    *,
    key: str,
    since: datetime,
) -> dict[str, dict[str, float]]:
    """Group records by `key` (role/model/provider) and sum tokens + cost.
    Only counts records with ts >= `since`."""
    out: dict[str, dict[str, float]] = defaultdict(_empty_window)
    for r in records:
        ts = _parse_ts(r.get("ts"))
        if ts is None or ts < since:
            continue
        bucket = str(r.get(key) or "unknown")
        out[bucket]["calls"] += 1
        out[bucket]["input_tokens"] += int(r.get("input_tokens", 0) or 0)
        out[bucket]["output_tokens"] += int(r.get("output_tokens", 0) or 0)
        out[bucket]["estimated_cost_usd"] += float(r.get("estimated_cost_usd", 0.0) or 0.0)
    # Round costs and convert defaultdict to plain dict for JSON.
    return {
        k: {**v, "estimated_cost_usd": round(v["estimated_cost_usd"], 6)}
        for k, v in sorted(out.items(), key=lambda kv: -kv[1]["estimated_cost_usd"])
    }


def _parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        ts = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts
