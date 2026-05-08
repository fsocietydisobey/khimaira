"""Discover langgraph checkpointer connections per project.

Two backends supported:
  - Postgres (AsyncPostgresSaver): URLs found by scanning the project's
    .env files plus common backend subdirs.
  - SQLite (AsyncSqliteSaver): .db files found in the project's XDG_DATA
    directory, identified by the presence of a `checkpoints` table.

Projects use a zoo of names for env vars (`DATABASE_URL`, `POSTGRES_DSN`,
`LANGGRAPH_CHECKPOINT_DSN`, etc.), so we rely on URL-shape detection
instead of name matching.
"""

from __future__ import annotations

import os
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

_POSTGRES_SCHEMES = ("postgres", "postgresql", "postgresql+psycopg", "postgresql+asyncpg")


@dataclass(frozen=True)
class PostgresConnection:
    """A Postgres connection candidate parsed from a project .env."""

    var: str
    url: str
    host: str
    database: str


@dataclass(frozen=True)
class SqliteConnection:
    """A SQLite checkpoint database file."""

    label: str          # human-readable name (e.g. "spr4_checkpoints")
    path: str           # absolute filesystem path


@dataclass(frozen=True)
class Connections:
    """All checkpointer connections for one project."""

    postgres: list[PostgresConnection]
    sqlite: list[SqliteConnection]

    @property
    def primary_kind(self) -> str | None:
        if self.postgres:
            return "postgres"
        if self.sqlite:
            return "sqlite"
        return None


def discover_all(project_path: Path) -> Connections:
    """Find every checkpointer connection (Postgres + SQLite) for a project."""
    return Connections(
        postgres=discover_postgres(project_path),
        sqlite=discover_sqlite(project_path),
    )


def discover_sqlite(project_path: Path) -> list[SqliteConnection]:
    """Find SQLite checkpoint databases for a project.

    Two scan paths:
      1. XDG_DATA/<project_name>/*.db — chimera-style: graphs write
         their checkpoints to the user's data home keyed by project name.
      2. <project_path>/**/*.db (capped depth, common dirs) — projects
         that vendor their checkpoint DBs inside the repo.

    A .db file is only considered a checkpointer if it contains a
    `checkpoints` table with the expected schema. This filters out
    arbitrary SQLite databases (config DBs, fixtures, etc.).
    """
    seen: set[str] = set()
    out: list[SqliteConnection] = []

    xdg_data = Path(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")))
    candidate_paths: list[Path] = []
    project_data_dir = xdg_data / project_path.name
    if project_data_dir.is_dir():
        candidate_paths.extend(project_data_dir.glob("*.db"))

    # Local scan — only the obvious places, not a full walk.
    skip = {".venv", "venv", "node_modules", ".git", "__pycache__", "dist", "build", "site-packages", ".next"}
    for candidate in project_path.rglob("*.db"):
        if any(part in skip for part in candidate.parts):
            continue
        candidate_paths.append(candidate)

    for path in candidate_paths:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        if not _is_langgraph_checkpoint_db(path):
            continue
        label = path.stem
        out.append(SqliteConnection(label=label, path=resolved))
    return out


def _is_langgraph_checkpoint_db(path: Path) -> bool:
    """True if the .db has a `checkpoints` table with at least the
    columns langgraph-checkpoint-sqlite writes."""
    required = {"thread_id", "checkpoint_ns", "checkpoint_id", "checkpoint", "metadata"}
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=0.5) as conn:
            cur = conn.execute("PRAGMA table_info(checkpoints)")
            cols = {row[1] for row in cur.fetchall()}
    except sqlite3.Error:
        return False
    return required.issubset(cols)


def discover_postgres(project_path: Path) -> list[PostgresConnection]:
    """Return all Postgres URLs found in the project's .env files.

    Looks at the project root + common backend subdirs (`backend/`, `server/`,
    `api/`) for `.env` and `.env.local`. Returns a deduped list keyed by URL.

    Walking deeper is intentionally avoided — `.env` files in nested package
    directories almost never hold connection strings, and a recursive walk
    risks picking up dev-fixture URLs from test trees.
    """
    out: list[PostgresConnection] = []
    seen: set[str] = set()
    candidate_dirs = [
        project_path,
        project_path / "backend",
        project_path / "server",
        project_path / "api",
    ]
    for d in candidate_dirs:
        if not d.is_dir():
            continue
        for env_name in (".env", ".env.local"):
            env_path = d / env_name
            if not env_path.is_file():
                continue
            for var, url in _parse_env(env_path):
                if not _looks_like_postgres(url):
                    continue
                if url in seen:
                    continue
                seen.add(url)
                host, database = _summarize(url)
                out.append(PostgresConnection(var=var, url=url, host=host, database=database))
    return out


_LINE_RE = re.compile(r"""
    ^
    \s*
    (?:export\s+)?              # optional `export `
    ([A-Za-z_][A-Za-z0-9_]*)    # var name
    \s*=\s*
    (?:                         # value: bare, single-quoted, or double-quoted
        '([^']*)'
      | "([^"]*)"
      | (\S+)
    )
    \s*
    $
""", re.VERBOSE)


def _parse_env(path: Path) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        if not line or line.lstrip().startswith("#"):
            continue
        match = _LINE_RE.match(line)
        if not match:
            continue
        var = match.group(1)
        value = match.group(2) or match.group(3) or match.group(4) or ""
        out.append((var, value))
    return out


def _looks_like_postgres(url: str) -> bool:
    return any(url.startswith(f"{scheme}://") for scheme in _POSTGRES_SCHEMES)


def _summarize(url: str) -> tuple[str, str]:
    """Return (host, database) for display. Host masks credentials."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return ("?", "?")
    host = parsed.hostname or "?"
    if parsed.port:
        host = f"{host}:{parsed.port}"
    database = (parsed.path or "/").lstrip("/") or "?"
    return (host, database)
