"""POB rejection memory — tracks accepted/rejected tool proposals.

Prevents rebuilding tools that the user has rejected. Tracks friction
categories that have been rejected 2+ times so the proposer can skip them.

Database lives at ~/.local/share/chimera/toolbuilder_memory.db.
"""

import os
import time
from pathlib import Path

import aiosqlite

from chimera.log import get_logger

log = get_logger("toolbuilder_memory")

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS toolbuilder_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    tool_name TEXT NOT NULL,
    friction_type TEXT NOT NULL,
    description TEXT,
    accepted INTEGER NOT NULL,
    rejection_reason TEXT
)
"""


def _get_db_path() -> str:
    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "chimera"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "toolbuilder_memory.db")


async def _get_db() -> aiosqlite.Connection:
    db_path = _get_db_path()
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(_CREATE_TABLE)
    await conn.commit()
    return conn


async def record_outcome(
    tool_name: str,
    friction_type: str,
    accepted: bool,
    description: str = "",
    rejection_reason: str = "",
) -> None:
    """Record the outcome of a tool proposal."""
    conn = await _get_db()
    try:
        await conn.execute(
            "INSERT INTO toolbuilder_memory "
            "(timestamp, tool_name, friction_type, description, accepted, rejection_reason) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), tool_name, friction_type, description, int(accepted), rejection_reason),
        )
        await conn.commit()
        log.info("recorded outcome: %s (%s) — %s", tool_name, friction_type, "accepted" if accepted else "rejected")
    finally:
        await conn.close()


async def get_rejected_types() -> list[str]:
    """Get friction types that have been rejected 2+ times."""
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT friction_type, COUNT(*) as cnt FROM toolbuilder_memory "
            "WHERE accepted = 0 "
            "GROUP BY friction_type HAVING cnt >= 2"
        )
        rows = await cursor.fetchall()
        return [r[0] for r in rows]
    finally:
        await conn.close()


async def was_rejected(friction_type: str) -> bool:
    """Check if a friction type has been rejected 2+ times."""
    rejected = await get_rejected_types()
    return friction_type in rejected


async def get_recent_proposals(limit: int = 10) -> list[dict]:
    """Get recent tool proposals for reporting."""
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT tool_name, friction_type, description, accepted, rejection_reason, timestamp "
            "FROM toolbuilder_memory ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "tool_name": r[0], "friction_type": r[1], "description": r[2],
                "accepted": bool(r[3]), "rejection_reason": r[4], "timestamp": r[5],
            }
            for r in rows
        ]
    finally:
        await conn.close()


async def last_proposal_time() -> float:
    """Get timestamp of the most recent proposal (for cool-down)."""
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT MAX(timestamp) FROM toolbuilder_memory"
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else 0.0
    finally:
        await conn.close()
