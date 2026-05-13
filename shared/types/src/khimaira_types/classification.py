"""TaskClassification — output of the AMR (Automatic Model Router) classifier.

The classifier is a cheap-runner call that takes a task description and returns
a routing recommendation. The router consumes this to pick which CLI runner +
model gets the actual work.

Cost model: the classifier itself runs at ~$0.0004/call on Haiku-tier. Saved
cost from routing trivial tasks down-tier is far larger than the classifier's
own bill.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

TaskType = Literal[
    "research",      # codebase exploration / understanding
    "architect",     # design decisions, multi-file planning
    "implement",     # write new code
    "refactor",      # rewrite existing code
    "debug",         # find + fix a bug
    "classify",      # short categorical decision
    "format",        # mechanical formatting / cleanup
    "explain",       # describe code behavior to user
    "chat",          # conversational, no code change
    "other",
]

ComplexityTier = Literal[
    "trivial",   # rename, format, single-line change
    "simple",    # single-file change, ≤ 30 lines
    "medium",    # 1-3 file change, modest reasoning
    "complex",   # multi-file, architectural reasoning needed
    "extreme",   # whole-system, deep cross-cutting
]

ThinkingLevel = Literal["none", "low", "medium", "high"]


class TaskClassification(BaseModel):
    """Routing recommendation for a single dev task.

    Produced by `khimaira.dispatch.classifier`; consumed by
    `khimaira.dispatch.router`. Stable over the wire — khimaira-monitor's
    `/api/routing` endpoint serializes this for the dashboard's
    decision-log view.
    """

    task_type: TaskType
    complexity_tier: ComplexityTier
    thinking_level: ThinkingLevel

    recommended_runner: str = Field(
        description=(
            "Which CLI runner to dispatch to. Values: 'claude', 'codex', "
            "'gemini', 'ollama', 'llm'. Pure-CLI substrate — no API SDK names."
        ),
    )
    recommended_model: str = Field(
        description=(
            "Model identifier the runner should use. Format depends on the "
            "runner: 'claude-opus-4-7' for claude/codex/gemini; 'llama-3.3-70b' "
            "for ollama; 'openrouter/anthropic/claude-3.5-sonnet' for llm."
        ),
    )
    thinking_budget_tokens: int = Field(
        default=0,
        ge=0,
        description=(
            "Extended-thinking budget for runners that support it (Claude). "
            "0 = no extended thinking. Higher = more reasoning, more $."
        ),
    )

    estimated_cost_usd_max: float = Field(
        ge=0.0,
        description="Worst-case cost ceiling. Used by per-task budget enforcement.",
    )

    reasoning: str = Field(
        description=(
            "1-2 sentence explanation. Surfaced in the dashboard so devs "
            "learn the routing logic by watching it work."
        ),
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="Classifier's confidence. Low confidence → router may escalate.",
    )

    # Optional caller-provided override hint that the classifier respected.
    forced_by: str | None = Field(
        default=None,
        description="Set when the user passed --model or similar. None when classifier chose freely.",
    )
