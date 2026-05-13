"""MCP tool invocation telemetry.

Records every khimaira MCP tool call to JSONL: timestamp, tool name, args
(truncated), elapsed, success, output size, error.

Answers questions like:
  - Which khimaira tools is this agent actually using?
  - Are sessions calling `wait_for_process` (good) or polling (bad)?
  - Which tools fail most often, with what errors?
  - Are some tools dead weight that nobody invokes?
  - How many polls did `wait_for_process` save this week?

Storage: ~/.local/state/khimaira/mcp-calls.jsonl. One line per call.
Sync writes — khimaira-monitor daemon process serializes via asyncio.Lock.
Read by /api/mcp-calls and the `usage_report` MCP tool.

Privacy: args are truncated to 500 chars and JSON-stringified, so a tool
called with a giant code blob doesn't dump the whole thing into the log.
The `tool` and `error` fields are kept full — those are the signal.
"""

from __future__ import annotations

import asyncio
import functools
import inspect
import json
import os
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar

from khimaira.log import get_logger

log = get_logger("monitor.mcp_calls")

_LOG_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "khimaira"
_LOG_FILE = _LOG_DIR / "mcp-calls.jsonl"

_MAX_ARG_CHARS = 500
_MAX_ERROR_CHARS = 500

# Single async lock for line-level write atomicity. Records are tiny so
# contention is negligible.
_lock: asyncio.Lock | None = None


def _get_lock() -> asyncio.Lock:
    global _lock
    if _lock is None:
        _lock = asyncio.Lock()
    return _lock


def log_file_path() -> Path:
    return _LOG_FILE


def _truncate_args(args: dict[str, Any]) -> str:
    """JSON-stringify call args for logging, truncating large values."""
    try:
        # Replace big strings with placeholders before serializing
        cleaned: dict[str, Any] = {}
        for k, v in args.items():
            if isinstance(v, str) and len(v) > 80:
                cleaned[k] = f"<{len(v)} char string>"
            elif isinstance(v, list) and len(v) > 5:
                cleaned[k] = f"<list of {len(v)} items>"
            else:
                cleaned[k] = v
        s = json.dumps(cleaned, default=str)
        return s if len(s) <= _MAX_ARG_CHARS else s[: _MAX_ARG_CHARS - 4] + "..."
    except Exception:
        return f"<{len(args)} args, repr failed>"


async def _record_async(record: dict[str, Any]) -> None:
    try:
        async with _get_lock():
            await asyncio.to_thread(_append_sync, record)
    except Exception as exc:
        log.warning("mcp_calls: failed to record %s: %s", record.get("tool"), exc)


def _append_sync(record: dict[str, Any]) -> None:
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    with _LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, separators=(",", ":")) + "\n")


T = TypeVar("T")


def logged_tool(name: str) -> Callable[[Callable[..., Awaitable[T]]], Callable[..., Awaitable[T]]]:
    """Decorator — wrap an async MCP tool to record invocations.

    Usage:
        @mcp.tool()
        @logged_tool("my_tool")
        async def my_tool(x: int) -> str:
            ...

    The order matters: `logged_tool` wraps the function FIRST, then
    `mcp.tool()` registers the wrapped version. FastMCP introspects the
    wrapper's signature (preserved via functools.wraps), so tool schemas
    are unchanged.
    """
    def deco(func: Callable[..., Awaitable[T]]) -> Callable[..., Awaitable[T]]:
        @functools.wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> T:
            t0 = time.monotonic()
            success = True
            error: str | None = None
            output_size = 0
            try:
                result = await func(*args, **kwargs)
                if isinstance(result, str):
                    output_size = len(result)
                elif isinstance(result, (dict, list)):
                    output_size = len(json.dumps(result, default=str))
                return result
            except Exception as e:
                success = False
                error = (str(e) or repr(e))[:_MAX_ERROR_CHARS]
                raise
            finally:
                # Build call record. We use kwargs primarily; positional args
                # are unusual for MCP tools (they're keyword-driven by spec).
                call_args_for_log = dict(kwargs)
                # Fold positional args by their parameter name when possible
                if args:
                    try:
                        sig = inspect.signature(func)
                        names = list(sig.parameters.keys())
                        for i, v in enumerate(args):
                            if i < len(names):
                                call_args_for_log[names[i]] = v
                    except (TypeError, ValueError):
                        pass

                record = {
                    "ts": datetime.now(timezone.utc).isoformat(),
                    "tool": name,
                    "args": _truncate_args(call_args_for_log),
                    "elapsed_ms": int((time.monotonic() - t0) * 1000),
                    "success": success,
                    "output_size": output_size,
                    "error": error,
                }
                # Fire-and-forget — don't block the tool's return on logging
                try:
                    asyncio.create_task(_record_async(record))
                except RuntimeError:
                    # No running loop (rare in MCP context) — sync fallback
                    try:
                        _append_sync(record)
                    except Exception:
                        pass

        return wrapper

    return deco


# ---------------------------------------------------------------------------
# Read helpers — used by /api/mcp-calls and usage_report
# ---------------------------------------------------------------------------


def _read_recent(max_lines: int = 50_000) -> list[dict[str, Any]]:
    """Tail-read the JSONL log up to max_lines. Skips malformed rows."""
    if not _LOG_FILE.exists():
        return []
    size = _LOG_FILE.stat().st_size
    if size <= 8 * 1024 * 1024:
        data = _LOG_FILE.read_bytes()
    else:
        with _LOG_FILE.open("rb") as f:
            f.seek(-8 * 1024 * 1024, os.SEEK_END)
            data = f.read()
    text = data.decode("utf-8", errors="replace")
    lines = text.splitlines()
    if size > 8 * 1024 * 1024 and lines:
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


def query_calls(
    *,
    window_minutes: int | None = None,
    tool_filter: str | None = None,
    only_failures: bool = False,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read + filter recent calls. Returns most-recent first."""
    records = _read_recent()
    cutoff = (
        datetime.now(timezone.utc) - timedelta(minutes=window_minutes)
        if window_minutes
        else None
    )
    out: list[dict[str, Any]] = []
    for r in reversed(records):
        if cutoff:
            ts = _parse_ts(r.get("ts"))
            if ts is None or ts < cutoff:
                continue
        if tool_filter and r.get("tool") != tool_filter:
            continue
        if only_failures and r.get("success"):
            continue
        out.append(r)
        if len(out) >= limit:
            break
    return out


def summarize(window_minutes: int = 60 * 24) -> dict[str, Any]:
    """Aggregate stats over the window. The 'is khimaira being used effectively?'
    answer in one structured response.
    """
    records = _read_recent()
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=window_minutes)

    total = 0
    failures = 0
    by_tool: Counter[str] = Counter()
    by_tool_failures: Counter[str] = Counter()
    by_tool_latency: dict[str, list[int]] = defaultdict(list)
    error_samples: dict[str, list[str]] = defaultdict(list)

    polling_replacement_count = 0
    polling_replacement_blocked_seconds = 0.0

    for r in records:
        ts = _parse_ts(r.get("ts"))
        if ts is None or ts < cutoff:
            continue
        total += 1
        tool = r.get("tool", "?")
        by_tool[tool] += 1
        latency = int(r.get("elapsed_ms", 0) or 0)
        by_tool_latency[tool].append(latency)
        if not r.get("success"):
            failures += 1
            by_tool_failures[tool] += 1
            err = r.get("error") or ""
            if err and len(error_samples[tool]) < 3:
                error_samples[tool].append(err[:200])

        # Polling-replacement metric: each wait_for_process call saved
        # ~N polls (estimating one poll per 5s of blocking time).
        if tool == "wait_for_process" and r.get("success"):
            blocked_s = latency / 1000.0
            polling_replacement_count += 1
            polling_replacement_blocked_seconds += blocked_s

    # Synthesize a "polls saved" estimate: 1 wait call ≈ blocked_s/5 polls
    estimated_polls_saved = int(polling_replacement_blocked_seconds / 5)

    by_tool_summary = []
    for tool, count in by_tool.most_common():
        latencies = by_tool_latency[tool]
        latencies.sort()
        n = len(latencies)
        p50 = latencies[n // 2] if n else 0
        p95 = latencies[max(0, int(n * 0.95) - 1)] if n else 0
        by_tool_summary.append({
            "tool": tool,
            "calls": count,
            "failures": by_tool_failures.get(tool, 0),
            "p50_ms": p50,
            "p95_ms": p95,
            "errors_sampled": error_samples.get(tool, [])[:3],
        })

    return {
        "window_minutes": window_minutes,
        "total_calls": total,
        "total_failures": failures,
        "failure_rate": round(failures / total, 3) if total else 0.0,
        "by_tool": by_tool_summary,
        "polling_replacement": {
            "wait_calls": polling_replacement_count,
            "total_blocked_seconds": round(polling_replacement_blocked_seconds, 1),
            "estimated_polls_saved": estimated_polls_saved,
        },
    }
