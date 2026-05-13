"""UsageRecord — one row per LLM call in the usage tracker.

Persisted as JSONL at `~/.local/state/khimaira/usage.jsonl`. Read by:
- `/api/usage` (rolling totals for dashboard)
- `/api/savings` (counterfactual cost — what you'd have spent without AMR)
- `check_usage_rate` self-watch invariant (rate-anomaly alarm)

The `task_id` field is what makes per-task budgeting possible — every record
ties back to the khimaira dispatch that triggered it.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

Provider = Literal["anthropic", "openai", "google", "local", "other"]
Source = Literal[
    "cli",  # CLI subprocess (claude, codex, gemini, ollama, llm)
    "api",  # direct API SDK call (legacy, being removed)
    "manual",  # synthetic record (testing)
]
Mode = Literal[
    "auto",  # khimaira classifier+pool_router picked the model
    "explicit-tier",  # user passed an explicit tier (haiku/flash/sonnet/local) — bypassed pool_router
    "manual",  # user picked the model directly (khimaira task --model, direct chain call)
    "unknown",  # legacy / pre-mode-tracking records
]


class UsageRecord(BaseModel):
    """One LLM call, accounted."""

    ts: str = Field(description="ISO 8601 UTC timestamp.")
    task_id: str | None = Field(
        default=None,
        description=(
            "Khimaira dispatch ID that triggered this call. None for ad-hoc "
            "calls outside of a khimaira task. Per-task budget enforcement "
            "groups records by this field."
        ),
    )

    runner: str = Field(
        description="The CLI runner that executed: claude, codex, gemini, ollama, llm.",
    )
    provider: Provider
    model: str
    role: str | None = Field(
        default=None,
        description="Logical role: architect | classify | research | implement | refactor | etc.",
    )

    input_tokens: int = 0
    output_tokens: int = 0
    latency_s: float = 0.0
    estimated_cost_usd: float = 0.0

    source: Source = "cli"

    mode: Mode = Field(
        default="unknown",
        description=(
            "How the model for this call was chosen. 'auto' = khimaira "
            "classifier + pool_router picked. 'explicit-tier' = user passed "
            "a tier hint (haiku/flash/sonnet/local). 'manual' = user picked "
            "the model directly. 'unknown' = legacy record predating mode "
            "tracking. Used by `khimaira usage savings` to attribute savings "
            "only to auto-mode dispatches."
        ),
    )

    # Budget signal: when the router escalated mid-task (cheap → expensive)
    escalation_count: int = 0
