"""Critic node — validates output quality and decides handoff routing.

Combines the validator's scoring logic with a handoff decision. Each phase
gets a critic that scores the relevant output and sets handoff_type based
on the score and step count.

Phase-specific behavior:
    research: scores research_findings → "needs_more_research" or "research_complete"
    planning: scores architecture_plan → "plan_revision" or "plan_approved"
    implementation: scores implementation_result → "tests_failing" or "ready_for_review"
"""

from pydantic import BaseModel, Field

from khimaira.core.state import OrchestratorState
from khimaira.dispatch.structured import StructuredCallError, run_structured
from khimaira.log import get_logger

log = get_logger("node.critic")


class CriticResult(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    feedback: str = Field(default="")

CRITIC_SYSTEM_PROMPT = """\
You are a quality critic evaluating the output of a {phase} phase.
Score the output on a 0.0-1.0 scale.

## Scoring criteria

- **Completeness** (0.25): Does it address the full scope of the task?
- **Specificity** (0.25): Does it reference concrete details (file paths, function names, patterns)?
- **Actionability** (0.25): Could someone act on this without asking follow-up questions?
- **Accuracy** (0.25): Does it avoid hallucinations, vague hand-waving, or generic advice?

## Response format

Respond with ONLY a JSON object:

{{
  "score": 0.0-1.0,
  "feedback": "Brief, actionable feedback. What's missing or weak? Empty if score >= 0.7."
}}
"""

# Handoff types per phase, keyed by (phase, pass/fail)
_HANDOFF_MAP: dict[str, tuple[str, str]] = {
    "research": ("research_complete", "needs_more_research"),
    "planning": ("plan_approved", "plan_revision"),
    "implementation": ("ready_for_review", "tests_failing"),
}

QUALITY_THRESHOLD = 0.7


def build_critic_node(
    phase: str,
    runner: str = "claude",
    model: str = "claude-haiku-4-5",
):
    """Build a critic node for a specific SPR-4 phase.

    Phase 10 migrated: uses CLI runner via run_structured. Default
    runner=claude, model=haiku-4-5 (cheap+fast).

    Args:
        phase: One of "research", "planning", "implementation".
        runner: CLI runner.
        model: model identifier for the runner.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    pass_handoff, fail_handoff = _HANDOFF_MAP[phase]

    # Determine which state field to evaluate
    _output_fields = {
        "research": ("research_findings", "research findings"),
        "planning": ("architecture_plan", "architecture plan"),
        "implementation": ("implementation_result", "implementation result"),
    }
    field_key, field_label = _output_fields[phase]

    async def critic_node(state: OrchestratorState) -> dict:
        """Score phase output and decide whether to loop or exit."""
        task = state.get("task", "")
        history = list(state.get("history", []))
        phase_step = state.get("phase_step", 0) + 1
        max_steps = state.get("max_phase_steps", 5)

        output_content = state.get(field_key, "")

        if not output_content:
            log.info("[%s] nothing to evaluate, passing", phase)
            return {
                "validation_score": 1.0,
                "validation_feedback": "",
                "handoff_type": pass_handoff,
                "phase_step": phase_step,
                "history": history + [f"critic({phase}): nothing to evaluate, passing"],
            }

        # Score the output
        system_prompt = CRITIC_SYSTEM_PROMPT.format(phase=phase)
        prompt = (
            f"## Task\n\n{task}\n\n"
            f"## Output to evaluate ({field_label})\n\n{output_content}"
        )

        full_prompt = f"{system_prompt}\n\n{prompt}"
        try:
            critic_result, _ = await run_structured(
                runner, full_prompt, CriticResult,
                model=model, max_retries=2,
            )
            score = critic_result.score
            feedback = critic_result.feedback
        except StructuredCallError as exc:
            log.warning("[%s] critic call failed (%s) — defaulting to 0.5", phase, exc)
            score = 0.5
            feedback = "Critic call failed; defaulting to neutral."
        except Exception as exc:
            log.warning("[%s] critic unexpected error (%s)", phase, exc)
            score = 0.5
            feedback = f"Critic error: {exc}"

        # Decide handoff
        if score >= QUALITY_THRESHOLD or phase_step >= max_steps:
            handoff = pass_handoff
            reason = "quality passed" if score >= QUALITY_THRESHOLD else "max steps reached"
        else:
            handoff = fail_handoff
            reason = "below threshold"

        log.info(
            "[%s] step %d/%d, score %.2f → %s (%s)",
            phase, phase_step, max_steps, score, handoff, reason,
        )

        # Set plan_approved when planning critic passes
        extra: dict = {}
        if phase == "planning" and handoff == "plan_approved":
            extra["plan_approved"] = True

        return {
            "validation_score": score,
            "validation_feedback": feedback,
            "critique": feedback,
            "handoff_type": handoff,
            "phase_step": phase_step,
            "history": history + [
                f"critic({phase}): {field_label} scored {score:.2f} → {handoff}"
                + (f" — {feedback}" if feedback else "")
            ],
            **extra,
        }

    return critic_node
