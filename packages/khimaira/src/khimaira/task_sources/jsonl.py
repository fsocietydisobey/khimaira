"""JSONL task source — the no-deps reference adapter.

Reads tasks from a JSONL file (one task per line). Default path is
`~/.khimaira/todo.jsonl`; configurable per-source.

Why JSONL: every other task tracker requires either an MCP server,
an API key, a shell tool, or a network connection. JSONL works with
none of that — just append lines. Makes khimaira's task surface
useful from day one for users who don't (yet) want to wire up
Linear / GitHub / etc.

Schema per line (additive — unknown fields ignored):

    {"id": "TODO-1", "title": "fix the auth bug", "state": "open"}
    {"id": "TODO-2", "title": "write the README", "state": "in-progress"}
    {"id": "TODO-3", "title": "ship v0.5", "state": "done"}

`state` is free-form; "done" / "completed" / "cancelled" / "closed"
(case-insensitive) are treated as closed and excluded from
`fetch_open_tasks`. Anything else counts as open.

This adapter is hook-safe — it reads a local file with no network
or MCP dependency.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from khimaira.log import get_logger

from . import Task

log = get_logger("task_sources.jsonl")


_DEFAULT_PATH = (
    Path(os.environ.get("KHIMAIRA_TASKS_JSONL"))
    if os.environ.get("KHIMAIRA_TASKS_JSONL")
    else Path(os.path.expanduser("~/.khimaira/todo.jsonl"))
)

_CLOSED_STATES = {"done", "completed", "closed", "cancelled", "canceled", "archived"}


@dataclass
class JsonlTaskSource:
    """Adapter that reads tasks from a JSONL file.

    Path resolution order (first match wins):
      1. `path` constructor arg
      2. `KHIMAIRA_TASKS_JSONL` env var
      3. `~/.khimaira/todo.jsonl`
    """

    name: str = "jsonl"
    path: Path | None = None

    def hook_safe(self) -> bool:
        return True

    def _resolved_path(self) -> Path:
        return self.path or _DEFAULT_PATH

    async def fetch_open_tasks(self) -> list[Task]:
        path = self._resolved_path()
        if not path.is_file():
            return []

        out: list[Task] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError as exc:
                    log.warning("jsonl: skipping malformed line in %s: %s", path, exc)
                    continue
                if not isinstance(raw, dict):
                    continue
                state = str(raw.get("state", "") or "").lower()
                if state in _CLOSED_STATES:
                    continue
                task_id = str(raw.get("id", "") or "")
                title = str(raw.get("title", "") or "")
                if not task_id and not title:
                    continue
                tags_raw = raw.get("tags") or []
                tags = [str(t) for t in tags_raw if isinstance(t, (str, int))]
                out.append(
                    Task(
                        id=task_id or "(no-id)",
                        title=title or "(no title)",
                        state=state,
                        source=self.name,
                        project=str(raw.get("project", "") or ""),
                        url=str(raw.get("url", "") or ""),
                        tags=tags,
                    )
                )
        except OSError as exc:
            log.warning("jsonl: failed to read %s: %s", path, exc)
            return []

        return out
