"""ACL registry — catalog of immutable atomic primitives.

Each "component" is a tested, verified building block that agents MUST use
instead of raw implementations. The registry tracks available components,
what they replace, and their validation status.

Database lives at ~/.local/share/chimera/acl_registry.db.
"""

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import aiosqlite

from chimera.log import get_logger

log = get_logger("acl_registry")

# Built-in components extracted from existing CHIMERA code
BUILTIN_COMPONENTS: dict[str, dict[str, str | list[str]]] = {
    "database": {
        "module": "chimera.core.memory",
        "description": "Async SQLite with WAL mode via aiosqlite",
        "replaces": ["sqlite3.connect", "aiosqlite.connect"],
        "category": "data",
    },
    "logger": {
        "module": "chimera.log",
        "description": "Structured stderr logging with component names",
        "replaces": ["logging.getLogger", "print"],
        "category": "observability",
    },
    "cli_runner": {
        "module": "chimera.cli.cli",
        "description": "CLI subprocess runner with stdin isolation and timeout",
        "replaces": ["subprocess.run", "subprocess.Popen", "asyncio.create_subprocess_exec"],
        "category": "execution",
    },
    "config_loader": {
        "module": "chimera.config.loader",
        "description": "YAML config loader with XDG path resolution",
        "replaces": ["yaml.safe_load", "json.load"],
        "category": "config",
    },
    "git_ops": {
        "module": "chimera.tools.git_tools",
        "description": "Async git operations (checkpoint, revert, diff)",
        "replaces": ["git commit", "git revert"],
        "category": "vcs",
    },
    "file_io": {
        "module": "chimera.tools.filesystem",
        "description": "Safe file read/write with path validation",
        "replaces": ["open()", "Path.read_text", "Path.write_text"],
        "category": "io",
    },
}

# Imports that agents should NOT use directly — must use ACL components instead
BANNED_RAW_IMPORTS: dict[str, str] = {
    "sqlite3": "database",
    "subprocess": "cli_runner",
}

_CREATE_TABLES = """\
CREATE TABLE IF NOT EXISTS acl_components (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    module TEXT NOT NULL,
    description TEXT,
    replaces TEXT,
    category TEXT,
    added_at REAL NOT NULL,
    validated INTEGER DEFAULT 0,
    validation_output TEXT
);

CREATE TABLE IF NOT EXISTS component_validation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    component_name TEXT,
    level TEXT NOT NULL,
    passed INTEGER NOT NULL,
    output TEXT,
    pairs_tested INTEGER DEFAULT 0
);
"""


def _get_db_path() -> str:
    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "chimera"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "acl_registry.db")


async def _get_db() -> aiosqlite.Connection:
    db_path = _get_db_path()
    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.executescript(_CREATE_TABLES)
    await conn.commit()
    return conn


async def init_builtin_components() -> int:
    """Seed the registry with built-in components. Returns count added."""
    conn = await _get_db()
    added = 0
    try:
        for name, info in BUILTIN_COMPONENTS.items():
            cursor = await conn.execute(
                "SELECT id FROM acl_components WHERE name = ?", (name,)
            )
            if await cursor.fetchone():
                continue
            await conn.execute(
                "INSERT INTO acl_components (name, module, description, replaces, category, added_at, validated) "
                "VALUES (?, ?, ?, ?, ?, ?, 1)",
                (
                    name,
                    info["module"],
                    info["description"],
                    json.dumps(info.get("replaces", [])),
                    info.get("category", ""),
                    time.time(),
                ),
            )
            added += 1
        await conn.commit()
        log.info("initialized %d built-in components", added)
    finally:
        await conn.close()
    return added


async def register_component(
    name: str,
    module: str,
    description: str,
    replaces: list[str] | None = None,
    category: str = "",
) -> None:
    """Register a new component in the ACL."""
    conn = await _get_db()
    try:
        await conn.execute(
            "INSERT OR REPLACE INTO acl_components "
            "(name, module, description, replaces, category, added_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (name, module, description, json.dumps(replaces or []), category, time.time()),
        )
        await conn.commit()
        log.info("registered component: %s", name)
    finally:
        await conn.close()


async def get_all_components() -> list[dict]:
    """Get all registered components."""
    conn = await _get_db()
    try:
        cursor = await conn.execute(
            "SELECT name, module, description, replaces, category, validated "
            "FROM acl_components ORDER BY name"
        )
        rows = await cursor.fetchall()
        return [
            {
                "name": r[0],
                "module": r[1],
                "description": r[2],
                "replaces": json.loads(r[3]) if r[3] else [],
                "category": r[4],
                "validated": bool(r[5]),
            }
            for r in rows
        ]
    finally:
        await conn.close()


async def mark_validated(name: str, passed: bool, output: str = "") -> None:
    """Mark a component as validated (or failed)."""
    conn = await _get_db()
    try:
        await conn.execute(
            "UPDATE acl_components SET validated = ?, validation_output = ? WHERE name = ?",
            (int(passed), output, name),
        )
        await conn.commit()
    finally:
        await conn.close()


async def log_validation(
    level: str,
    passed: bool,
    output: str = "",
    component_name: str = "",
    pairs_tested: int = 0,
) -> None:
    """Log a validation run."""
    conn = await _get_db()
    try:
        await conn.execute(
            "INSERT INTO component_validation_log "
            "(timestamp, component_name, level, passed, output, pairs_tested) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (time.time(), component_name, level, int(passed), output, pairs_tested),
        )
        await conn.commit()
    finally:
        await conn.close()


def get_manifest() -> str:
    """Return a formatted manifest of built-in components for agent prompts.

    This is synchronous — uses the static BUILTIN_COMPONENTS dict.
    For the full dynamic registry, use get_all_components().
    """
    lines = ["## ACL — Required Atomic Components\n"]
    lines.append("You MUST use these primitives. Do NOT write raw implementations of patterns they cover.\n")
    for name, info in BUILTIN_COMPONENTS.items():
        lines.append(f"- **{name}**: `{info['module']}` — {info['description']}")
        replaces = info.get("replaces", [])
        if replaces:
            assert isinstance(replaces, list)
            lines.append(f"  - Replaces: {', '.join(f'`{r}`' for r in replaces)}")
    return "\n".join(lines)


@dataclass
class ValidationReport:
    """Results from ACL validation (combinatorial testing)."""

    isolation_results: dict[str, bool] = field(default_factory=dict)
    pair_results: dict[str, bool] = field(default_factory=dict)
    scenario_results: dict[str, bool] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        all_results = (
            list(self.isolation_results.values())
            + list(self.pair_results.values())
            + list(self.scenario_results.values())
        )
        return len(all_results) > 0 and all(all_results)

    @property
    def summary(self) -> str:
        iso_pass = sum(self.isolation_results.values())
        iso_total = len(self.isolation_results)
        pair_pass = sum(self.pair_results.values())
        pair_total = len(self.pair_results)
        return (
            f"isolation: {iso_pass}/{iso_total}, "
            f"pairs: {pair_pass}/{pair_total}, "
            f"scenarios: {len(self.scenario_results)}"
        )

    def to_dict(self) -> dict:
        return {
            "passed": self.passed,
            "summary": self.summary,
            "isolation": self.isolation_results,
            "pairs": self.pair_results,
            "scenarios": self.scenario_results,
        }
