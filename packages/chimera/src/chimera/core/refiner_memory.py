"""Refinement memory for CLR — tracks what worked and what failed per cycle.

Separate from SPR-4 memory.py (which tracks cross-run task context).
This tracks the refinement history: which cycles improved health, which
were reverted, which spec items failed repeatedly.
"""

import json
import os
import time
from pathlib import Path

import aiosqlite

from chimera.log import get_logger

log = get_logger("clr_memory")

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS clr_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cycle INTEGER NOT NULL,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,
    description TEXT,
    health_before REAL,
    health_after REAL,
    reverted INTEGER DEFAULT 0,
    spec_item TEXT,
    files_changed TEXT,
    error_log TEXT
)
"""


def _get_db_path() -> str:
    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "chimera"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "clr.db")


async def _get_db() -> aiosqlite.Connection:
    db_path = _get_db_path()
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(_CREATE_TABLE)
    await conn.commit()
    return conn


async def log_cycle(
    cycle: int,
    action: str,
    description: str = "",
    health_before: float = 0.0,
    health_after: float = 0.0,
    reverted: bool = False,
    spec_item: str = "",
    files_changed: list[str] | None = None,
    error_log: str = "",
) -> None:
    """Record one refinement cycle."""
    conn = await _get_db()
    try:
        await conn.execute(
            "INSERT INTO clr_log "
            "(cycle, timestamp, action, description, health_before, health_after, "
            "reverted, spec_item, files_changed, error_log) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                cycle, time.time(), action, description,
                health_before, health_after, int(reverted),
                spec_item, json.dumps(files_changed or []), error_log,
            ),
        )
        await conn.commit()
    finally:
        await conn.close()


async def get_recent_cycles(limit: int = 10) -> list[dict]:
    """Get the last N refinement cycles."""
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT cycle, action, description, health_before, health_after, "
            "reverted, spec_item FROM clr_log "
            "ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "cycle": r[0], "action": r[1], "description": r[2],
                "health_before": r[3], "health_after": r[4],
                "reverted": bool(r[5]), "spec_item": r[6],
            }
            for r in rows
        ]
    finally:
        await conn.close()


async def get_failed_attempts(spec_item: str) -> int:
    """Count how many times a spec item was attempted and reverted."""
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT COUNT(*) FROM clr_log "
            "WHERE spec_item = ? AND reverted = 1",
            (spec_item,),
        )
        row = await cursor.fetchone()
        return row[0] if row else 0
    finally:
        await conn.close()


async def get_last_cycle_number() -> int:
    """Get the last cycle number (for resuming after restart)."""
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT MAX(cycle) FROM clr_log"
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0
    finally:
        await conn.close()
