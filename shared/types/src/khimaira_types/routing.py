"""RoutingDecision — output of `khimaira.dispatch.router.route()`.

Produced AFTER the classifier runs. The classifier says "this is a trivial
classify task → use Haiku-tier"; the router maps that to a concrete runner +
model + budget given what's actually available on the user's machine.

If Ollama is unreachable, the router downgrades to the next-best available
runner. The decision artifact records both the *original recommendation* and
the *actual choice* so the dashboard can show "AMR wanted Ollama but fell
back to Claude because Ollama wasn't running."
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from .classification import TaskClassification


class RoutingDecision(BaseModel):
    """Concrete dispatch choice + audit trail."""

    classification: TaskClassification

    # The actual choice (may differ from recommended_* if a fallback fired)
    chosen_runner: str
    chosen_model: str
    chosen_thinking_budget_tokens: int = 0

    # Why the actual choice differs from the recommendation, if it does
    fallback_reason: str | None = Field(
        default=None,
        description=(
            "Explanation when actual ≠ recommended. Examples: "
            "'ollama unreachable', 'claude session quota exhausted', "
            "'requested model not in capability matrix'."
        ),
    )

    # Per-task budget enforcement input
    task_id: str
    budget_remaining_usd: float | None = Field(
        default=None,
        description="Budget left for this task. None = unlimited.",
    )

    # Set when the router refuses to dispatch (budget exceeded, all runners
    # unavailable, etc.) — caller surfaces this back to the user.
    refused: bool = False
    refusal_reason: str | None = None
