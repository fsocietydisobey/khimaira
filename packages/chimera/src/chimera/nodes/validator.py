"""Validator node — scores the most recent output for quality.

Uses a cheap model (Haiku) to evaluate whether research findings or
architecture plans are complete, specific, and actionable. The score
and feedback are written to state so the supervisor can decide whether
to retry or move forward.
"""

import json

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.validator")

VALIDATOR_SYSTEM_PROMPT = """\
You are a quality validator. Score the most recent output on a 0.0-1.0 scale.

## Scoring criteria

- **Completeness** (0.25): Does it address the full scope of the task?
- **Specificity** (0.25): Does it reference concrete details (file paths, function names, patterns)?
- **Actionability** (0.25): Could someone act on this without asking follow-up questions?
- **Accuracy** (0.25): Does it avoid hallucinations, vague hand-waving, or generic advice?

## Response format

Respond with ONLY a JSON object:

{
  "score": 0.0-1.0,
  "feedback": "Brief, actionable feedback. What's missing or weak? Empty if score >= 0.7."
}
"""


def build_validator_node(model: BaseChatModel):
    """Build a validator node that scores output quality.

    Args:
        model: LangChain chat model (cheap/fast — Haiku recommended).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def validator_node(state: OrchestratorState) -> dict:
        """Score the most recent output and provide feedback."""
        task = state.get("task", "")
        history = state.get("history", [])

        # Determine what to validate — most recent substantial output
        plan = state.get("architecture_plan", "")
        findings = state.get("research_findings", "")

        if plan:
            output_type = "architecture plan"
            output_content = plan
        elif findings:
            output_type = "research findings"
            output_content = findings
        else:
            # Nothing to validate
            return {
                "validation_score": 1.0,
                "validation_feedback": "",
                "history": history + ["validator: nothing to validate, passing"],
            }

        prompt = (
            f"## Task\n\n{task}\n\n"
            f"## Output to evaluate ({output_type})\n\n{output_content}"
        )

        messages = [
            SystemMessage(content=VALIDATOR_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]
        response = await model.ainvoke(messages)
        raw = response.content.strip()

        # Parse JSON
        if raw.startswith("```"):
            raw = raw.split("\n", 1)[1].rsplit("```", 1)[0].strip()

        try:
            result = json.loads(raw)
            score = float(result.get("score", 0.5))
            feedback = result.get("feedback", "")
        except (json.JSONDecodeError, ValueError):
            score = 0.5
            feedback = "Failed to parse validator response."
            log.warning("failed to parse response: %s", raw[:200])

        log.info("scored %s: %.2f%s", output_type, score, f" — {feedback}" if feedback else "")

        return {
            "validation_score": score,
            "validation_feedback": feedback,
            "history": history
            + [f"validator: {output_type} scored {score:.2f}" + (f" — {feedback}" if feedback else "")],
        }

    return validator_node
