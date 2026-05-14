"""Load + dispatch enabled task sources.

User config lives at `~/.khimaira/task_sources.yaml` (or
`$XDG_CONFIG_HOME/khimaira/task_sources.yaml`). When the config file
doesn't exist, khimaira defaults to a single JSONL source pointing at
`~/.khimaira/todo.jsonl` — that file may or may not exist; if it
doesn't, fetch returns [] cleanly.

Example config:

    sources:
      - kind: jsonl
        path: ~/work/todo.jsonl    # optional; default is ~/.khimaira/todo.jsonl
        enabled: true
      # Future: linear, github, etc.
      # - kind: linear
      #   enabled: true

`fetch_all_open_tasks(hook_safe_only=False)` fans out across enabled
sources, calls each `fetch_open_tasks()` in parallel, and returns the
merged list. Pass `hook_safe_only=True` from the SessionStart hook to
exclude adapters that need MCP / network — they'll be reached via
slash command or daemon-side dispatch later.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import yaml

from khimaira.log import get_logger

from . import Task, TaskSource
from .github import GithubTaskSource
from .jsonl import JsonlTaskSource
from .linear import LinearTaskSource

log = get_logger("task_sources.config")


def _config_path() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        return Path(xdg) / "khimaira" / "task_sources.yaml"
    return Path(os.path.expanduser("~/.khimaira/task_sources.yaml"))


def _build_source(entry: dict[str, Any]) -> TaskSource | None:
    """Construct one TaskSource from a config entry. Returns None if
    the kind isn't recognized (logs a warning so misconfig surfaces)."""
    kind = str(entry.get("kind", "") or "").lower()
    if not entry.get("enabled", True):
        return None
    if kind == "jsonl":
        path_raw = entry.get("path")
        path = Path(os.path.expanduser(str(path_raw))) if path_raw else None
        return JsonlTaskSource(path=path)
    if kind == "github":
        return GithubTaskSource(
            limit=int(entry.get("limit", 30) or 30),
            cmd=str(entry.get("cmd", "gh") or "gh"),
            timeout_s=float(entry.get("timeout_s", 10.0) or 10.0),
        )
    if kind == "linear":
        # Skeleton — returns [] until daemon-side MCP dispatch ships.
        # See tasks/linear-adapter/IMPLEMENTATION.md.
        return LinearTaskSource(
            daemon_port=int(entry.get("daemon_port", 8740) or 8740),
            timeout_s=float(entry.get("timeout_s", 10.0) or 10.0),
        )
    log.warning(
        "task_sources: unknown kind %r in config — ignoring entry %r. "
        "Built-in kinds: jsonl, github, linear (linear is currently a "
        "skeleton — returns no tasks until daemon-side MCP dispatch "
        "ships, see tasks/linear-adapter/IMPLEMENTATION.md).",
        kind,
        entry,
    )
    return None


def load_configured_sources() -> list[TaskSource]:
    """Read the user's config and return enabled sources.

    Defaults to `[JsonlTaskSource()]` when no config file exists — that
    adapter resolves to `~/.khimaira/todo.jsonl` and fetches []
    cleanly when the file doesn't exist either. So a brand-new install
    "works" (returns nothing) without any user setup.
    """
    cfg_path = _config_path()
    if not cfg_path.is_file():
        return [JsonlTaskSource()]
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        log.warning("task_sources: %s is malformed YAML: %s", cfg_path, exc)
        return [JsonlTaskSource()]
    entries = data.get("sources") or []
    if not isinstance(entries, list):
        return [JsonlTaskSource()]

    out: list[TaskSource] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        src = _build_source(entry)
        if src is not None:
            out.append(src)
    return out


async def fetch_all_open_tasks(
    sources: list[TaskSource] | None = None,
    *,
    hook_safe_only: bool = False,
) -> list[Task]:
    """Fan out across enabled sources; merge results in order.

    Args:
        sources: list of TaskSource. Defaults to `load_configured_sources()`.
        hook_safe_only: if True, skip sources whose `hook_safe()` is False.
            SessionStart passes True; agent-context slash commands pass False.
    """
    if sources is None:
        sources = load_configured_sources()
    targets = [s for s in sources if not hook_safe_only or s.hook_safe()]
    if not targets:
        return []
    # Run all adapters concurrently; an exception from one doesn't
    # poison the others.
    results = await asyncio.gather(
        *(s.fetch_open_tasks() for s in targets),
        return_exceptions=True,
    )
    merged: list[Task] = []
    for src, res in zip(targets, results, strict=True):
        if isinstance(res, Exception):
            log.warning("task_sources: %s raised: %s", src.name, res)
            continue
        merged.extend(res)
    return merged
