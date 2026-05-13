"""Validator node — scores the most recent output for quality.

Phase 10 migrated: uses CLI runner via run_structured, NOT langchain.
Default runner=claude, model=claude-haiku-4-5 (cheap+fast). Caller can
override via build_validator_node(runner=..., model=...).

The score and feedback are written to state so the supervisor can decide
whether to retry or move forward.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from khimaira.core.state import OrchestratorState
from khimaira.dispatch.structured import StructuredCallError, run_structured
from khimaira.log import get_logger

log = get_logger("node.validator")

VALIDATOR_SYSTEM_PROMPT = """\
You are a quality validator. Score the most recent output on a 0.0-1.0 scale.

## Scoring criteria

- **Completeness** (0.25): Does it address the full scope of the task?
- **Specificity** (0.25): Does it reference concrete details (file paths, function names, patterns)?
- **Actionability** (0.25): Could someone act on this without asking follow-up questions?
- **Accuracy** (0.25): Does it avoid hallucinations, vague hand-waving, or generic advice?
"""


class ValidationResult(BaseModel):
    score: float = Field(ge=0.0, le=1.0, description="Quality score 0..1")
    feedback: str = Field(
        default="",
        description="Brief, actionable feedback on what's missing or weak. Empty when score >= 0.7.",
    )


def build_validator_node(
    runner: str = "claude",
    model: str = "claude-haiku-4-5",
):
    """Build a validator node that scores output quality.

    Args:
        runner: CLI runner — 'claude' (default), 'codex', 'gemini', 'ollama', 'llm'.
        model: model identifier passed to the runner. Default haiku 4.5 — cheap+fast.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def validator_node(state: OrchestratorState) -> dict:
        """Score the most recent output and provide feedback."""
        task = state.get("task", "")
        history = list(state.get("history", []))

        plan = state.get("architecture_plan", "")
        findings = state.get("research_findings", "")

        if plan:
            output_type, output_content = "architecture plan", plan
        elif findings:
            output_type, output_content = "research findings", findings
        else:
            return {
                "validation_score": 1.0,
                "validation_feedback": "",
                "history": history + ["validator: nothing to validate, passing"],
            }

        prompt = (
            f"{VALIDATOR_SYSTEM_PROMPT}\n\n"
            f"## Task\n\n{task}\n\n"
            f"## Output to evaluate ({output_type})\n\n{output_content}"
        )

        try:
            result, _ = await run_structured(
                runner,
                prompt,
                ValidationResult,
                model=model,
                max_retries=2,
            )
            score = result.score
            feedback = result.feedback
        except StructuredCallError as exc:
            log.warning("validator: structured call failed (%s) — defaulting to 0.5", exc)
            score = 0.5
            feedback = "Validator call failed; defaulting to neutral."
        except Exception as exc:
            log.warning("validator: unexpected error (%s) — defaulting to 0.5", exc)
            score = 0.5
            feedback = f"Validator error: {exc}"

        log.info("scored %s: %.2f%s", output_type, score, f" — {feedback}" if feedback else "")

        return {
            "validation_score": score,
            "validation_feedback": feedback,
            "history": history
            + [f"validator: {output_type} scored {score:.2f}" + (f" — {feedback}" if feedback else "")],
        }

    return validator_node
