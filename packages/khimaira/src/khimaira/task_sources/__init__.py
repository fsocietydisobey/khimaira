"""External task source integration — Phase 1.5.

khimaira surfaces "what's assigned to you" at SessionStart, but it does
NOT prescribe where those tasks live. Users plug in their own task
tracker via the `TaskSource` Protocol. khimaira ships reference
adapters; users / community add more.

This module defines:
  - `Task` — the unified shape every adapter returns
  - `TaskSource` Protocol — what any adapter must implement
  - `load_configured_sources()` — read enabled adapters from
    ~/.khimaira/task_sources.yaml (or defaults)
  - `fetch_all_tasks()` — fan out across enabled sources, merge results

Reference adapters (in this package):
  - `jsonl` — plain JSONL at ~/.khimaira/todo.jsonl (no deps, always works)

Community / follow-up adapters (not in core khimaira, may live in
contrib or be community-maintained):
  - Linear (requires the Linear MCP server registered + daemon-side
    MCP dispatch — see `tasks/task-sources/IMPLEMENTATION.md` for the
    architectural sketch)
  - GitHub Issues (shell out to `gh issue list`)
  - Jira, Asana, plain TODO.md, etc.

The point is genericness: khimaira's roadmap doesn't name any vendor.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class Task:
    """One task assigned to the current user.

    Unified shape — every adapter normalizes its source-specific fields
    into this. UI / hooks render against this shape; they don't need
    to know which source produced a given Task.
    """

    id: str                          # source-stable id, e.g. "KHI-12" or "todo:42"
    title: str                       # short summary, displayed inline
    state: str = ""                  # source-defined state ("in progress", "todo", etc.)
    source: str = ""                 # adapter name ("jsonl", "linear", "github", ...)
    project: str = ""                # optional project / workspace label
    url: str = ""                    # optional clickable link to the task
    tags: list[str] = field(default_factory=list)


class TaskSource(Protocol):
    """One pluggable adapter for fetching the current user's open tasks.

    Implementations are async (most real task trackers are network-bound)
    and MUST NOT raise on missing data — return an empty list instead.
    A failing source should never break the SessionStart hook for
    sources that are working.
    """

    name: str
    """Stable identifier — 'jsonl', 'linear', 'github', etc."""

    async def fetch_open_tasks(self) -> list[Task]:
        """Return open tasks assigned to the current user.

        "Open" means: not done, not cancelled, not archived. Each adapter
        defines what that means against its source's state model.

        MUST return [] (never raise) when:
          - the source is unreachable
          - the source has no items
          - the source isn't configured for this user

        Errors should be logged (so the operator can debug a misconfig)
        but never propagated.
        """
        ...

    def hook_safe(self) -> bool:
        """True iff this adapter can be called from inside the SessionStart
        hook (a stdlib-only subprocess with no MCP client).

        Adapters that talk to MCP servers (Linear, etc.) return False —
        they need to be called from agent context or via a daemon-side
        dispatch layer that we haven't built yet.

        Adapters that hit local files or shell out to a CLI return True.
        """
        ...
