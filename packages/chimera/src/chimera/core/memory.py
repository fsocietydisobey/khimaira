"""Persistent memory for SPR-4 — cross-run context via SQLite.

Stores summaries, decisions, and outcomes from past runs so the phase
router can inject relevant context into new runs. This is cross-run
memory (what happened in previous invocations), not within-run state
(which is handled by LangGraph's checkpointer).

Database lives at ~/.local/share/chimera/spr4_memory.db.
"""

import json
import os
import time
from pathlib import Path

import aiosqlite

from chimera.log import get_logger

log = get_logger("memory")

_CREATE_TABLE = """\
CREATE TABLE IF NOT EXISTS run_memory (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    thread_id TEXT NOT NULL,
    timestamp REAL NOT NULL,
    summary TEXT NOT NULL,
    decisions TEXT,
    outcome TEXT,
    artifacts TEXT,
    task TEXT
)
"""


def _get_memory_db_path() -> str:
    """Get the path for the SPR-4 memory database."""
    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "chimera"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "spr4_memory.db")


async def _get_db() -> aiosqlite.Connection:
    """Open and initialize the memory database."""
    db_path = _get_memory_db_path()
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute(_CREATE_TABLE)
    await conn.commit()
    return conn


async def save_run(
    thread_id: str,
    summary: str,
    task: str = "",
    decisions: list[str] | None = None,
    outcome: str = "",
    artifacts: dict | None = None,
) -> None:
    """Save a run's summary and outcome to memory.

    Args:
        thread_id: The graph thread ID.
        summary: Brief summary of what happened.
        task: The original task description.
        decisions: List of key decisions made during the run.
        outcome: Final outcome (e.g. "completed", "failed at planning").
        artifacts: Optional dict of artifacts (plan hash, research topics, etc).
    """
    conn = await _get_db()
    try:
        await conn.execute(
            "INSERT INTO run_memory (thread_id, timestamp, summary, decisions, outcome, artifacts, task) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                thread_id,
                time.time(),
                summary,
                json.dumps(decisions or []),
                outcome,
                json.dumps(artifacts or {}),
                task,
            ),
        )
        await conn.commit()
        log.info("saved run memory for thread %s", thread_id)
    finally:
        await conn.close()


async def get_recent_context(thread_id: str | None = None, limit: int = 5) -> str:
    """Load recent run context as a formatted string for state injection.

    Args:
        thread_id: Optional — filter to a specific thread for continuity.
        limit: Max number of past runs to include.

    Returns:
        Formatted markdown string with past run summaries, or empty string.
    """
    conn = await _get_db()
    try:
        if thread_id:
            cursor = await conn.execute(
                "SELECT task, summary, outcome, decisions FROM run_memory "
                "WHERE thread_id = ? ORDER BY timestamp DESC LIMIT ?",
                (thread_id, limit),
            )
        else:
            cursor = await conn.execute(
                "SELECT task, summary, outcome, decisions FROM run_memory "
                "ORDER BY timestamp DESC LIMIT ?",
                (limit,),
            )
        rows = await cursor.fetchall()
    finally:
        await conn.close()

    if not rows:
        return ""

    parts = ["## Past run context\n"]
    for task, summary, outcome, decisions_json in rows:
        entry = f"- **Task:** {task}\n  **Summary:** {summary}"
        if outcome:
            entry += f"\n  **Outcome:** {outcome}"
        try:
            decisions = json.loads(decisions_json) if decisions_json else []
            if decisions:
                entry += "\n  **Decisions:** " + "; ".join(decisions)
        except json.JSONDecodeError:
            pass
        parts.append(entry)

    return "\n".join(parts)
