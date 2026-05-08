"""DCE archive — stores records of deleted dead code.

Prevents resurrecting patterns that were intentionally removed.
Acts as institutional memory for code deletions.

Database lives at ~/.local/share/chimera/dce_archive.db.
"""

import os
import time
from pathlib import Path

import aiosqlite

from chimera.log import get_logger

log = get_logger("dce_archive")

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS dce_archive (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    path TEXT NOT NULL,
    name TEXT NOT NULL,
    code_snippet TEXT,
    reason TEXT NOT NULL,
    evidence TEXT,
    category TEXT NOT NULL,
    lines_removed INTEGER DEFAULT 0,
    risk_level TEXT DEFAULT 'safe'
)
"""


def _get_db_path() -> str:
    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "chimera"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "dce_archive.db")


async def _get_db() -> aiosqlite.Connection:
    db_path = _get_db_path()
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(_CREATE_TABLE)
    await conn.commit()
    return conn


async def archive_deletion(
    path: str,
    name: str,
    reason: str,
    category: str,
    code_snippet: str = "",
    evidence: str = "",
    lines_removed: int = 0,
    risk_level: str = "safe",
) -> None:
    """Record a deletion in the archive."""
    conn = await _get_db()
    try:
        await conn.execute(
            "INSERT INTO dce_archive "
            "(timestamp, path, name, code_snippet, reason, evidence, category, lines_removed, risk_level) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), path, name, code_snippet[:2000], reason, evidence, category, lines_removed, risk_level),
        )
        await conn.commit()
        log.info("archived deletion: %s (%s) — %s", name, path, reason)
    finally:
        await conn.close()


async def check_archive(name: str) -> list[dict]:
    """Check if a name/pattern was previously deleted.

    Use this before agents create new code to prevent
    resurrecting patterns that were intentionally removed.
    """
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT path, name, reason, category, timestamp "
            "FROM dce_archive WHERE name LIKE ?",
            (f"%{name}%",),
        )
        rows = await cursor.fetchall()
        return [
            {
                "path": r[0], "name": r[1], "reason": r[2],
                "category": r[3], "deleted_at": r[4],
            }
            for r in rows
        ]
    finally:
        await conn.close()


async def get_recent_deletions(limit: int = 20) -> list[dict]:
    """Get recent deletions for reporting."""
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT path, name, reason, category, lines_removed, risk_level, timestamp "
            "FROM dce_archive ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [
            {
                "path": r[0], "name": r[1], "reason": r[2],
                "category": r[3], "lines_removed": r[4],
                "risk_level": r[5], "timestamp": r[6],
            }
            for r in rows
        ]
    finally:
        await conn.close()


async def get_total_lines_removed() -> int:
    """Get total lines removed across all deletions."""
    conn = await _get_db()
    try:
        cursor = await conn.execute("SELECT COALESCE(SUM(lines_removed), 0) FROM dce_archive")
        row = await cursor.fetchone()
        return row[0] if row else 0
    finally:
        await conn.close()
