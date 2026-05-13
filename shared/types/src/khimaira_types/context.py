"""FileContext + ContextBundle — output of the context resolver pillar.

The resolver answers *"what files actually matter for this task?"* before
anything hits the LLM. Reduces prompt tokens 5–10× compared to "paste the
whole codebase" workflows. Consumers: `khimaira task` (assembles the prompt),
the AMR classifier (sees how many files were resolved as a complexity signal),
and the dashboard (audit log of resolution decisions).
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ResolutionSource = Literal[
    "seance",     # vector / semantic search
    "scarlet",    # static cartography (CLAUDE.md, dep graphs)
    "serena",     # LSP — symbol references and definitions
    "explicit",   # user pointed at this file directly
    "filesystem", # heuristic walk (recently modified, README, etc.)
]


class FileContext(BaseModel):
    """One file the resolver decided is task-relevant."""

    path: str = Field(description="Absolute or project-relative file path.")
    relevance: float = Field(
        ge=0.0, le=1.0,
        description="Aggregate relevance score across resolution sources.",
    )
    sources: list[ResolutionSource] = Field(
        description=(
            "Which resolvers flagged this file. Multi-source matches "
            "outrank single-source so the merge weights cross-corroboration."
        ),
    )
    snippet: str | None = Field(
        default=None,
        description=(
            "Optional excerpt — the lines/symbols specifically matched. "
            "When set, the prompt builder may use just this excerpt "
            "instead of the whole file (token saver)."
        ),
    )
    line_range: tuple[int, int] | None = Field(
        default=None,
        description="(start, end) line numbers covered by `snippet`, if any.",
    )
    reasoning: str = Field(
        default="",
        description="Short human-readable explanation. Shown in the dashboard.",
    )


class ContextBundle(BaseModel):
    """The complete context-resolver output for a single task.

    Passed to the prompt builder. Also serialized to the dashboard's
    `/api/context_log` so devs can see exactly what got included.
    """

    task_description: str
    files: list[FileContext]

    # Aggregate metrics for dashboard rendering / budget enforcement
    total_estimated_tokens: int = Field(
        description="Sum of all included snippets, used by budget gate.",
    )
    sources_consulted: list[ResolutionSource]
    elapsed_ms: int = Field(
        description="Resolution wall time. Targets: ≤ 2s for live tail UX.",
    )

    # Set when the resolver had to truncate to fit the budget
    budget_truncated: bool = False
    budget_dropped_files: list[str] = Field(
        default_factory=list,
        description="Files that scored below the cutoff. Audit signal — too many drops = budget too tight.",
    )
