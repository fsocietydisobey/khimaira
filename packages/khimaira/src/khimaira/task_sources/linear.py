"""Linear task source — daemon-side MCP dispatch (skeleton, 2026-05-14).

**Status**: SKELETON. `fetch_open_tasks` returns `[]` cleanly. Real
implementation pending the daemon-side MCP dispatch infrastructure
described in `tasks/linear-adapter/IMPLEMENTATION.md` (Steps 1-3).

The shape exists now so users can write `{kind: linear, enabled: true}`
in their `~/.khimaira/task_sources.yaml` without the config validator
rejecting an unknown kind. The adapter silently returns no tasks
until the daemon endpoint at `GET /api/task-sources/fetch?kind=linear`
lands.

### Why this isn't a hook-safe shell-out like the GitHub adapter

Linear has no `gh`-equivalent CLI. The canonical read path is the
Linear MCP server (`npx -y mcp-remote https://mcp.linear.app/mcp`),
which the SessionStart hook can't invoke (stdlib-only subprocess, no
MCP client).

The architectural fix:
  1. Daemon hosts an HTTP endpoint `/api/task-sources/fetch?kind=linear`
  2. Daemon-side implementation calls `mcp__linear__list_issues` via
     the daemon's own MCP client subsystem
  3. This adapter (hook-side) becomes a thin HTTP client pointing
     at the daemon endpoint
  4. `hook_safe()` flips from False to True once the endpoint exists

### What the implementation will look like (Step 4 in the spec)

```python
async def fetch_open_tasks(self) -> list[Task]:
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                f"http://127.0.0.1:{self.daemon_port}/api/task-sources/fetch",
                params={"kind": "linear"},
            )
            r.raise_for_status()
            return [Task(**t) for t in r.json().get("tasks", [])]
    except Exception:
        log.warning("linear adapter: daemon fetch failed (%s)", exc)
        return []
```

Until then, the skeleton just returns `[]` so a misconfigured-but-
forward-looking user gets a silent no-op instead of an import error.
"""

from __future__ import annotations

from dataclasses import dataclass

from khimaira.log import get_logger

from . import Task

log = get_logger("task_sources.linear")


@dataclass
class LinearTaskSource:
    """Skeleton adapter — full implementation pending daemon-side MCP dispatch.

    Args:
        daemon_port: optional override for the khimaira-monitor daemon port
            (default 8740). Used by the future fetch_open_tasks impl to
            reach `/api/task-sources/fetch?kind=linear`.
        timeout_s: HTTP timeout for the daemon round-trip (default 10s).
    """

    name: str = "linear"
    daemon_port: int = 8740
    timeout_s: float = 10.0

    def hook_safe(self) -> bool:
        """Currently False — no daemon endpoint yet, so the SessionStart
        hook can't reach this adapter. After
        `tasks/linear-adapter/IMPLEMENTATION.md` Step 3 lands, flips to
        True (HTTP from the hook to localhost:8740 is hook-safe)."""
        return False

    async def fetch_open_tasks(self) -> list[Task]:
        """Skeleton — returns [] until the daemon endpoint exists.

        After the daemon-side dispatch infrastructure ships, this method
        becomes a thin httpx GET against
        `/api/task-sources/fetch?kind=linear` and unpacks the response
        into Task objects. See module docstring for the target shape.

        Until then, a silent no-op is preferable to raising:
          - Lets users add `{kind: linear, enabled: true}` to their
            task_sources.yaml today without breakage
          - Lets fetch_all_open_tasks treat Linear as just-another-source
            once it ships, with no fan-out plumbing changes needed
        """
        log.debug(
            "linear adapter: skeleton — returning [] until "
            "tasks/linear-adapter/IMPLEMENTATION.md Step 3 ships"
        )
        return []
