"""Background scanner queue.

The daemon enqueues projects whose metadata cache is missing or stale.
A single worker drains the queue serially (Gemini calls are expensive
and rate-limited; one at a time is fine for a personal-tool monitor).

Entry points:
  - `start_worker(loop)` — kick off the worker task; idempotent.
  - `enqueue(project_name, project_path)` — schedule one project.
  - `enqueue_all(projects, project_paths)` — batch enqueue everything
    that needs scanning right now.

The worker is best-effort. If a scan raises, it logs and moves on;
queue stays drained.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from chimera.log import get_logger

from . import cache as meta_cache
from .scan import scan_project

log = get_logger("monitor.metadata.scanner")

_queue: asyncio.Queue[tuple[str, Path]] | None = None
_worker_task: asyncio.Task | None = None
_in_flight: set[str] = set()


def _ensure_queue() -> asyncio.Queue:
    global _queue
    if _queue is None:
        _queue = asyncio.Queue()
    return _queue


def start_worker() -> None:
    """Spawn the worker task if it isn't already running."""
    global _worker_task
    if _worker_task is not None and not _worker_task.done():
        return
    queue = _ensure_queue()
    _worker_task = asyncio.create_task(_drain(queue), name="monitor-scanner")
    log.info("scanner: worker started")


async def _drain(queue: asyncio.Queue) -> None:
    while True:
        project_name, project_path = await queue.get()
        try:
            log.info("scanner: scanning %s", project_name)
            await scan_project(project_name, project_path)
        except Exception as exc:
            log.warning("scanner: scan failed for %s: %s", project_name, exc)
        finally:
            _in_flight.discard(str(project_path))
            queue.task_done()


def enqueue(project_name: str, project_path: Path) -> bool:
    """Add a project to the scan queue, deduped against in-flight scans.

    Returns True if enqueued, False if a scan for this path is already
    in flight or pending.
    """
    key = str(project_path)
    if key in _in_flight:
        return False
    _in_flight.add(key)
    _ensure_queue().put_nowait((project_name, project_path))
    log.info("scanner: enqueued %s", project_name)
    return True


def enqueue_stale(projects: list[tuple[str, Path]]) -> int:
    """Enqueue every project whose cache is missing or stale. Returns the
    number actually enqueued."""
    n = 0
    for project_name, project_path in projects:
        metadata = meta_cache.load(project_path)
        if meta_cache.is_stale(metadata, project_path):
            if enqueue(project_name, project_path):
                n += 1
    return n
