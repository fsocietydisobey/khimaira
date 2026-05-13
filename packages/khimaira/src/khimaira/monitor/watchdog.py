"""Process watchdog — find zombie holders of checkpointer DB files.

The fire_swarm incident: a python process spawned a khimaira swarm,
burned through credits across 3 fan-out threads, then parked at an
HITL gate. It sat for 16 hours holding `pde_checkpoints.db` open with
zero new checkpoint writes — invisible to the state-only monitor.

This module walks `/proc/<pid>/fd` looking for python processes that
hold any discovered SQLite checkpointer DB. For each holder it pairs
process age against the DB's most-recent write and flags zombies =
process older than the configured age AND no DB write inside a
recency window.

Linux-only (uses /proc). Postgres-connected processes are out of scope
for now — those hold TCP sockets, not file handles, and the discovery
would need pg_stat_activity. Most khimaira-driven runaways are SQLite-
backed (the swarm/refiner/components/deadcode/toolbuilder graphs all
default to SQLite checkpointers), so this catches the main class.

Defaults are conservative to avoid false positives on legitimate
long-running daemons (khimaira-monitor itself, an active MCP server):

  - PROCESS_AGE_HOURS = 2.0   (process must be older than this)
  - DB_IDLE_HOURS     = 1.0   (and DB must have been quiet this long)

A daemon that's actively committing checkpoints will never trip the
DB_IDLE_HOURS check no matter how long it's been running. Only the
combination — old process, silent DB — is the zombie pattern.
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path

from khimaira.log import get_logger

from .discovery.connections import Connections, SqliteConnection
from .discovery.project import Project

log = get_logger("monitor.watchdog")


@dataclass
class HolderProcess:
    """A process found holding a checkpointer DB file open."""

    pid: int
    cmdline: str
    process_age_s: float
    db_path: str
    db_idle_s: float | None       # None when DB unreadable
    is_zombie: bool


# Default thresholds. Tunable via env vars for users with unusually
# long-running legitimate workloads.
_PROCESS_AGE_HOURS = float(os.environ.get("KHIMAIRA_WATCHDOG_PROCESS_AGE_HOURS", "2.0"))
_DB_IDLE_HOURS = float(os.environ.get("KHIMAIRA_WATCHDOG_DB_IDLE_HOURS", "1.0"))

# A handful of cmdlines we KNOW are legitimate even when long-running.
# The khimaira-monitor daemon itself shows up here whenever it has SQLite
# self-introspection happening; the khimaira MCP server is the typical
# graph host. We never zombie-flag these — they're the system, not a
# runaway.
_NEVER_ZOMBIE_PATTERNS = (
    "khimaira monitor",
    "khimaira_monitor",
    "uvicorn",
    "khimaira/.venv/bin/khimaira",  # the MCP server entry
)


def find_holders(
    connections_by_project: dict[Path, Connections],
    *,
    process_age_hours: float = _PROCESS_AGE_HOURS,
    db_idle_hours: float = _DB_IDLE_HOURS,
) -> list[HolderProcess]:
    """Walk /proc and return every process holding a checkpointer DB.

    Each entry is annotated with `is_zombie=True` when the process is
    older than `process_age_hours` AND the DB has been idle for at
    least `db_idle_hours`.
    """
    if not Path("/proc").is_dir():
        log.warning("watchdog: /proc not available, skipping")
        return []

    # All checkpointer DB paths across all projects.
    db_paths_to_project: dict[str, str] = {}
    for path, conns in connections_by_project.items():
        for s in conns.sqlite:
            db_paths_to_project[s.path] = path.name

    if not db_paths_to_project:
        return []

    db_idle_seconds: dict[str, float | None] = {
        p: _db_idle_seconds(p) for p in db_paths_to_project
    }

    now = time.time()
    age_threshold_s = process_age_hours * 3600.0
    idle_threshold_s = db_idle_hours * 3600.0
    holders: list[HolderProcess] = []

    for proc_dir in Path("/proc").iterdir():
        if not proc_dir.name.isdigit():
            continue
        pid = int(proc_dir.name)

        fd_dir = proc_dir / "fd"
        try:
            fd_entries = list(fd_dir.iterdir())
        except (PermissionError, FileNotFoundError):
            continue

        held_db: str | None = None
        for fd in fd_entries:
            try:
                target = os.readlink(fd)
            except (OSError, FileNotFoundError):
                continue
            # The actual checkpointer DB shows up as fd → /path/to/foo.db.
            # Some SQLite WAL/shm sidecars (foo.db-wal, foo.db-shm) also
            # point back; we count any of them as holding the DB.
            for db_path in db_paths_to_project:
                if target == db_path or target.startswith(db_path + "-"):
                    held_db = db_path
                    break
            if held_db:
                break

        if not held_db:
            continue

        try:
            process_age_s = now - proc_dir.stat().st_mtime
        except (OSError, FileNotFoundError):
            continue

        cmdline = _read_cmdline(proc_dir)

        # Don't flag system processes — khimaira-monitor, the MCP server,
        # and uvicorn are expected to hold checkpointer DBs.
        is_system = any(pat in cmdline for pat in _NEVER_ZOMBIE_PATTERNS)

        idle_s = db_idle_seconds.get(held_db)
        is_old = process_age_s >= age_threshold_s
        db_silent = idle_s is not None and idle_s >= idle_threshold_s
        is_zombie = is_old and db_silent and not is_system

        holders.append(HolderProcess(
            pid=pid,
            cmdline=cmdline,
            process_age_s=process_age_s,
            db_path=held_db,
            db_idle_s=idle_s,
            is_zombie=is_zombie,
        ))

    return holders


def to_dict(h: HolderProcess) -> dict:
    return {
        "pid": h.pid,
        "cmdline": h.cmdline,
        "process_age_s": round(h.process_age_s, 1),
        "process_age_h": round(h.process_age_s / 3600.0, 2),
        "db_path": h.db_path,
        "db_idle_s": round(h.db_idle_s, 1) if h.db_idle_s is not None else None,
        "db_idle_h": round(h.db_idle_s / 3600.0, 2) if h.db_idle_s is not None else None,
        "is_zombie": h.is_zombie,
    }


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _db_idle_seconds(db_path: str) -> float | None:
    """Best-effort 'how long since this DB was last written.'

    SQLite WAL mode means writes go to `.db-wal`, then checkpoint to the
    main `.db` file. The WAL's mtime is the most accurate signal of
    recent activity; fall back to the main file's mtime when WAL absent.
    """
    candidates = [db_path + "-wal", db_path]
    most_recent: float | None = None
    for cand in candidates:
        try:
            mtime = os.stat(cand).st_mtime
        except (OSError, FileNotFoundError):
            continue
        if most_recent is None or mtime > most_recent:
            most_recent = mtime
    if most_recent is None:
        return None
    return max(0.0, time.time() - most_recent)


def _read_cmdline(proc_dir: Path) -> str:
    """Read /proc/<pid>/cmdline. Args are null-delimited; collapse to a
    space-separated string for human display + pattern matching."""
    try:
        raw = (proc_dir / "cmdline").read_bytes()
    except (OSError, FileNotFoundError):
        return ""
    return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()


# ---------------------------------------------------------------------------
# Public anomaly-style entry — used by the self-watch invariant
# ---------------------------------------------------------------------------


def find_zombies(
    projects: list[Project],
    connections_by_project: dict[Path, Connections] | None = None,
) -> list[HolderProcess]:
    """Convenience wrapper: re-discover SQLite connections at call time
    (khimaira projects create per-graph DBs lazily) and return only
    zombie-flagged holders.

    The self-watch check uses this; the rest of the module is exposed
    for an eventual /api/watchdog endpoint or MCP tool.
    """
    from .discovery.connections import discover_sqlite

    if connections_by_project is None:
        # Re-glob so newly-created .db files are picked up.
        connections_by_project = {
            p.path: Connections(postgres=[], sqlite=discover_sqlite(p.path))
            for p in projects
        }

    holders = find_holders(connections_by_project)
    return [h for h in holders if h.is_zombie]
