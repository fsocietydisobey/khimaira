r"""Process registry — track long-running processes for SSE-streamed observability.

The use case this exists for: Claude Code polls long-running processes
(test suites, dev servers, builds) via repeated `cat <log>` calls — 30+ MCP
roundtrips for what could be 1 blocking call. This module turns that into:

  agent: spawn_process("npm test", label="tests")
  agent: wait_for_process("tests", completion_signal=r"\d+ passed|FAIL")
         # blocks until pattern matches OR exit OR timeout
         # returns full output + exit code in ONE call
  agent: tests done!

The registry lives in the khimaira-monitor daemon process, not in the MCP
server, because the MCP server is restarted on every Claude Code session
while the daemon is long-lived. Spawning processes from the daemon means
they survive an MCP-server bounce (and the agent can re-attach by label).

Architecture:
  spawn → ProcessHandle (pid, stdout buffer, stderr buffer, status)
        → registry[label] = handle
        → background reader tasks fan output into ring buffers
  wait_for_process → asyncio.Event triggered by:
    - completion_signal regex match in stdout/stderr
    - process exit
    - timeout
  follow_process → async generator yielding new chunks since last cursor
  /api/processes/{label}/stream → SSE wrapping follow_process for the dashboard
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import AsyncIterator

from khimaira.log import get_logger

log = get_logger("monitor.processes")

# Per-process output ring buffer size. 4MB = enough for typical test/build
# output without blowing memory if many processes run simultaneously.
_RING_BUFFER_BYTES = 4 * 1024 * 1024


@dataclass
class ProcessHandle:
    """One tracked subprocess. Lives in the daemon's registry."""

    label: str
    pid: int
    cmd: list[str]
    cwd: str | None
    started_at: float
    ended_at: float | None = None
    exit_code: int | None = None

    # Async event triggered when process exits (for wait_for_process)
    exit_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Output buffers — append-only, capped by _RING_BUFFER_BYTES.
    # We track per-stream so callers can filter, but also a unified
    # `output_chunks` for follow_process (chronologically merged).
    _stdout_bytes: int = 0
    _stderr_bytes: int = 0
    _stdout_buf: list[str] = field(default_factory=list)
    _stderr_buf: list[str] = field(default_factory=list)

    # Unified chronological stream — list of (stream, text, ts).
    # `subscriber_events` notifies follow_process consumers of new chunks.
    output_chunks: list[tuple[str, str, float]] = field(default_factory=list)
    new_output_event: asyncio.Event = field(default_factory=asyncio.Event)

    # Internal handle to the asyncio.subprocess.Process for kill ops
    proc: asyncio.subprocess.Process | None = None

    # Reader tasks — cancellable on kill
    reader_tasks: list[asyncio.Task] = field(default_factory=list)

    def is_running(self) -> bool:
        return self.exit_code is None

    def append_stdout(self, text: str) -> None:
        self._stdout_buf.append(text)
        self._stdout_bytes += len(text)
        self._maybe_trim(self._stdout_buf, "_stdout_bytes")
        self._record_chunk("stdout", text)

    def append_stderr(self, text: str) -> None:
        self._stderr_buf.append(text)
        self._stderr_bytes += len(text)
        self._maybe_trim(self._stderr_buf, "_stderr_bytes")
        self._record_chunk("stderr", text)

    def _maybe_trim(self, buf: list[str], counter_attr: str) -> None:
        # Naive ring trim — drop oldest entries until under limit. Cheap;
        # processes producing > 4MB of output are uncommon.
        while getattr(self, counter_attr) > _RING_BUFFER_BYTES and buf:
            old = buf.pop(0)
            setattr(self, counter_attr, getattr(self, counter_attr) - len(old))

    def _record_chunk(self, stream: str, text: str) -> None:
        self.output_chunks.append((stream, text, time.time()))
        # Trim chronological list at 2x ring budget (rough; chunks vary)
        if len(self.output_chunks) > 4096:
            self.output_chunks = self.output_chunks[-2048:]
        # Wake all subscribers waiting on new output
        self.new_output_event.set()
        self.new_output_event.clear()

    def stdout_text(self) -> str:
        return "".join(self._stdout_buf)

    def stderr_text(self) -> str:
        return "".join(self._stderr_buf)

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "pid": self.pid,
            "cmd": self.cmd,
            "cwd": self.cwd,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "exit_code": self.exit_code,
            "is_running": self.is_running(),
            "duration_s": (self.ended_at or time.time()) - self.started_at,
            "stdout_bytes": self._stdout_bytes,
            "stderr_bytes": self._stderr_bytes,
        }


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

# label → handle. Module-level so the daemon's coroutines + the MCP server's
# tool handlers can both reach it.
_registry: dict[str, ProcessHandle] = {}


class ProcessExists(RuntimeError):
    """Tried to spawn with a label that's already in use."""


class ProcessNotFound(RuntimeError):
    """Lookup by label that doesn't exist."""


def get(label: str) -> ProcessHandle:
    h = _registry.get(label)
    if h is None:
        raise ProcessNotFound(
            f"No process registered with label {label!r}. "
            f"Active labels: {sorted(_registry)}"
        )
    return h


def list_all(include_finished: bool = True) -> list[ProcessHandle]:
    if include_finished:
        return list(_registry.values())
    return [h for h in _registry.values() if h.is_running()]


def cleanup_finished(older_than_s: float = 3600.0) -> int:
    """Drop finished processes older than the cutoff. Returns count removed.

    Called periodically by the daemon to keep the registry from growing
    unboundedly. Default 1h = enough that recent test runs are still queryable
    after the agent finishes its session.
    """
    cutoff = time.time() - older_than_s
    to_remove = [
        label for label, h in _registry.items()
        if not h.is_running() and (h.ended_at or 0) < cutoff
    ]
    for label in to_remove:
        del _registry[label]
    return len(to_remove)


# ---------------------------------------------------------------------------
# Spawn / kill
# ---------------------------------------------------------------------------


async def spawn(
    cmd: list[str],
    *,
    label: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    replace_existing: bool = False,
) -> ProcessHandle:
    """Spawn a process and register it under `label`.

    Background tasks read stdout/stderr asynchronously into ring buffers.
    Caller gets a handle immediately — process runs until it exits or is
    killed.
    """
    if label in _registry and _registry[label].is_running():
        if replace_existing:
            await kill(label)
        else:
            raise ProcessExists(
                f"Process {label!r} is already running (pid={_registry[label].pid}). "
                f"Pass replace_existing=True to kill the old one first."
            )

    full_env = {**os.environ, **(env or {})}
    log.info("spawn: label=%s cmd=%s cwd=%s", label, " ".join(cmd[:3]), cwd or "<inherit>")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=cwd,
        env=full_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    assert proc.stdout is not None and proc.stderr is not None  # noqa: S101

    handle = ProcessHandle(
        label=label,
        pid=proc.pid,
        cmd=list(cmd),
        cwd=cwd,
        started_at=time.time(),
        proc=proc,
    )
    _registry[label] = handle

    # Background reader tasks — fan output into ring buffers.
    handle.reader_tasks = [
        asyncio.create_task(_drain_stream(handle, proc.stdout, is_stdout=True)),
        asyncio.create_task(_drain_stream(handle, proc.stderr, is_stdout=False)),
        asyncio.create_task(_wait_for_exit(handle)),
    ]

    return handle


async def kill(label: str, *, sig: int = signal.SIGTERM, grace_s: float = 5.0) -> bool:
    """Kill a tracked process. SIGTERM first, SIGKILL after `grace_s`.

    Returns True if the process was running (and is now stopped), False if
    it had already exited.
    """
    h = get(label)
    if not h.is_running() or h.proc is None:
        return False

    try:
        h.proc.send_signal(sig)
    except ProcessLookupError:
        return False

    try:
        await asyncio.wait_for(h.exit_event.wait(), timeout=grace_s)
    except asyncio.TimeoutError:
        log.warning("kill: %s did not exit on %s, sending SIGKILL", label, sig)
        try:
            h.proc.send_signal(signal.SIGKILL)
        except ProcessLookupError:
            pass
        await h.exit_event.wait()
    return True


# ---------------------------------------------------------------------------
# Wait / follow — the polling-replacement primitives
# ---------------------------------------------------------------------------


async def wait_for_process(
    label: str,
    *,
    completion_signal: str | None = None,
    timeout_s: float = 300.0,
) -> dict:
    """**The polling-replacement primitive.** Blocks until one of:

    1. `completion_signal` regex matches new stdout OR stderr output, OR
    2. The process exits, OR
    3. The timeout elapses.

    Returns a dict with:
      - reason: "signal_match" | "exit" | "timeout"
      - stdout_text, stderr_text: full output captured so far
      - exit_code: when reason="exit"
      - matched: when reason="signal_match" — the matching substring
      - duration_s: wall time spent waiting

    Pass `completion_signal=None` to block strictly on exit/timeout.
    """
    h = get(label)
    pattern = re.compile(completion_signal) if completion_signal else None
    t0 = time.monotonic()
    deadline = t0 + timeout_s

    # Walk all PRE-EXISTING output first — pattern may already match.
    # Track a cursor (chunk index) so subsequent waits don't re-scan.
    cursor = 0

    def _check_match() -> tuple[bool, str]:
        nonlocal cursor
        if pattern is None:
            return False, ""
        # Concat new chunks since cursor
        for stream, text, _ts in h.output_chunks[cursor:]:
            cursor += 1
            m = pattern.search(text)
            if m:
                return True, m.group(0)
        return False, ""

    matched_text = ""
    while True:
        # Check exit first — a fast process may exit before we get here
        if not h.is_running():
            return _result(h, reason="exit", duration_s=time.monotonic() - t0)

        ok, matched_text = _check_match()
        if ok:
            return _result(
                h, reason="signal_match", matched=matched_text,
                duration_s=time.monotonic() - t0,
            )

        # Wait for either new output OR exit. Use whichever fires first.
        # The daemon's output writers set `new_output_event`; the exit
        # waiter sets `exit_event`. We race them with a short timeout
        # so we re-check `_check_match` even if neither fires.
        time_left = deadline - time.monotonic()
        if time_left <= 0:
            return _result(h, reason="timeout", duration_s=time.monotonic() - t0)

        try:
            await asyncio.wait_for(
                _wait_either(h.new_output_event, h.exit_event),
                timeout=min(time_left, 1.0),
            )
        except asyncio.TimeoutError:
            # Loop — re-check match against any output that arrived
            # while we were waiting (race condition tolerance)
            continue


async def follow_process(
    label: str,
    *,
    chunks_per_yield: int = 1,
    max_chunks: int | None = None,
    include_existing: bool = True,
) -> AsyncIterator[dict]:
    """Async generator yielding output chunks as they arrive.

    Used by the SSE endpoint and by `mcp__khimaira__follow_process`. Yields
    dicts: `{stream: 'stdout'|'stderr', text: str, ts: float}`. Terminates
    when the process exits OR `max_chunks` is reached.

    `include_existing=True` replays already-buffered chunks first so a
    late-attaching consumer doesn't miss the start of the run.
    """
    h = get(label)
    cursor = 0 if include_existing else len(h.output_chunks)
    yielded = 0

    while True:
        # Drain anything new
        while cursor < len(h.output_chunks):
            if max_chunks is not None and yielded >= max_chunks:
                return
            stream, text, ts = h.output_chunks[cursor]
            cursor += 1
            yielded += 1
            yield {"stream": stream, "text": text, "ts": ts}

        if not h.is_running():
            return  # exited; stream complete

        # Wait for new output OR exit
        try:
            await asyncio.wait_for(
                _wait_either(h.new_output_event, h.exit_event),
                timeout=15.0,
            )
        except asyncio.TimeoutError:
            # Heartbeat — emit nothing, just loop. Consumers (SSE) emit
            # their own keepalive at the transport layer.
            continue


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _drain_stream(
    handle: ProcessHandle,
    stream: asyncio.StreamReader,
    *,
    is_stdout: bool,
) -> None:
    """Read lines from a subprocess stream into the ring buffer."""
    while True:
        try:
            line = await stream.readline()
        except Exception as exc:
            log.warning("processes: drain failed for %s: %s", handle.label, exc)
            return
        if not line:
            return
        text = line.decode("utf-8", errors="replace")
        if is_stdout:
            handle.append_stdout(text)
        else:
            handle.append_stderr(text)


async def _wait_for_exit(handle: ProcessHandle) -> None:
    """Wait for the subprocess to exit, then update the handle."""
    if handle.proc is None:
        return
    try:
        rc = await handle.proc.wait()
    except Exception as exc:
        log.warning("processes: wait failed for %s: %s", handle.label, exc)
        rc = -1
    handle.exit_code = rc
    handle.ended_at = time.time()
    handle.exit_event.set()
    handle.new_output_event.set()  # wake any follow consumers
    handle.new_output_event.clear()
    log.info("exit: label=%s rc=%d duration=%.1fs",
             handle.label, rc, handle.ended_at - handle.started_at)


async def _wait_either(*events: asyncio.Event) -> None:
    """Return when any of the events is set."""
    futures = [asyncio.create_task(e.wait()) for e in events]
    try:
        done, pending = await asyncio.wait(
            futures, return_when=asyncio.FIRST_COMPLETED,
        )
        for p in pending:
            p.cancel()
    finally:
        for f in futures:
            if not f.done():
                f.cancel()


def _result(
    h: ProcessHandle,
    *,
    reason: str,
    duration_s: float,
    matched: str = "",
) -> dict:
    """Build the wait_for_process response.

    Two distinct timings — they look the same when wait blocks the whole
    time, but diverge when the process exited before the wait was even
    called (then `wait_duration_s` ≈ 0 but `process_runtime_s` is the real
    work duration). MCP wrapper uses `process_runtime_s` for the
    user-facing "finished in" message.
    """
    process_runtime_s = (h.ended_at or time.time()) - h.started_at
    return {
        "label": h.label,
        "pid": h.pid,
        "reason": reason,
        "matched": matched,
        "stdout_text": h.stdout_text(),
        "stderr_text": h.stderr_text(),
        "exit_code": h.exit_code,
        "is_running": h.is_running(),
        # Kept for backwards compat — the wait's own elapsed time
        "duration_s": round(duration_s, 3),
        # The actual process wall-clock duration. Prefer this in UI/logs.
        "process_runtime_s": round(process_runtime_s, 3),
        "wait_duration_s": round(duration_s, 3),
    }
