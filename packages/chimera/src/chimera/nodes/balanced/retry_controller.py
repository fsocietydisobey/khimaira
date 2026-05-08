"""Retry Controller — strategic retry engine.

Replaces naive retry (same prompt + feedback) with intelligent strategy selection.
When a node fails, RetryController analyzes the failure pattern and chooses the best
retry approach: simple retry, model escalation, task decomposition, or graceful exit.

Tracks retry history to avoid repeating the same failed approach.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.retry_controller")

RETRY_CONTROLLER_SYSTEM_PROMPT = """\
You are a retry strategy advisor. A node in the pipeline has failed.
Analyze the failure and choose the best retry approach.

## Available strategies

1. **retry** — Same approach with error feedback appended. Best for transient errors,
   minor prompt issues, or when the error message clearly points to the fix.
2. **escalate** — The current model can't handle this. Recommend switching to a more
   capable model or routing to gemini_assist for analysis.
3. **decompose** — The task is too complex for one shot. Break it into smaller sub-tasks
   that can be attempted individually.
4. **exit** — Max retries exhausted or the failure is fundamental (wrong architecture,
   impossible constraint). Exit gracefully with the best partial result.

## Decision rules

- Attempt 1 failure → usually "retry" with feedback
- Attempt 2 failure with SAME error → "escalate" (the model can't solve this)
- Attempt 2 failure with DIFFERENT error → "retry" (making progress, just needs more tries)
- Attempt 3+ failure → "decompose" or "exit" depending on error pattern
- Timeout errors → "retry" (transient), or "decompose" if task is too large
- Hallucination errors → "escalate" (need a different model's perspective)
"""


class RetryStrategy(BaseModel):
    """Strategic retry decision."""

    strategy: str = Field(
        description="retry | escalate | decompose | exit"
    )
    modified_instructions: str = Field(
        default="",
        description="Updated instructions for the target node on retry. Include error context.",
    )
    reasoning: str = Field(
        description="Why this strategy was chosen",
    )
    sub_tasks: list[str] = Field(
        default_factory=list,
        description="If strategy is 'decompose', the list of smaller sub-tasks",
    )


def build_retry_controller_node(model: BaseChatModel):
    """Build a strategic retry engine node.

    Args:
        model: LangChain chat model (cheap/fast — Haiku recommended).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(RetryStrategy)

    async def retry_controller_node(state: OrchestratorState) -> dict:
        """Analyze failure and choose retry strategy."""
        history = list(state.get("history", []))
        retry_history = list(state.get("retry_history") or [])
        node_failure = state.get("node_failure") or {}

        if not node_failure:
            log.info("no failure to analyze, passing")
            return {
                "retry_strategy": {"strategy": "retry", "reasoning": "no failure detected"},
                "history": history + ["retry_controller: no failure to analyze"],
            }

        failed_node = node_failure.get("node", "unknown")
        error = node_failure.get("error", "unknown error")
        attempt = node_failure.get("attempt", 1)

        # Check retry history for repeated failures
        same_error_count = sum(
            1 for r in retry_history
            if r.get("node") == failed_node and r.get("error") == error
        )

        prompt = (
            f"## Failed node\n\n{failed_node} (attempt {attempt})\n\n"
            f"## Error\n\n{error}\n\n"
            f"## Same error occurred {same_error_count} time(s) before\n\n"
            f"## Retry history\n\n"
        )

        if retry_history:
            for r in retry_history[-5:]:  # Last 5 retries
                prompt += f"- {r.get('node', '?')} attempt {r.get('attempt', '?')}: {r.get('strategy', '?')} — {r.get('error', '?')[:100]}\n"
        else:
            prompt += "No previous retries.\n"

        messages = [
            SystemMessage(content=RETRY_CONTROLLER_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        strategy_raw = await structured_model.ainvoke(messages)
        assert isinstance(strategy_raw, RetryStrategy)
        strategy = strategy_raw

        log.info(
            "[%s attempt %d] strategy: %s — %s",
            failed_node, attempt, strategy.strategy, strategy.reasoning,
        )

        strategy_dict = {
            "strategy": strategy.strategy,
            "modified_instructions": strategy.modified_instructions,
            "reasoning": strategy.reasoning,
            "sub_tasks": strategy.sub_tasks,
        }

        # Record this retry attempt in history
        retry_entry = {
            "node": failed_node,
            "attempt": attempt,
            "error": error[:200],
            "strategy": strategy.strategy,
        }

        result: dict = {
            "retry_strategy": strategy_dict,
            "retry_history": [retry_entry],
            "history": history + [
                f"retry_controller: {failed_node} attempt {attempt} → {strategy.strategy} — {strategy.reasoning}"
            ],
        }

        # If retrying, update supervisor_instructions with modified instructions
        if strategy.strategy == "retry" and strategy.modified_instructions:
            result["supervisor_instructions"] = strategy.modified_instructions

        # If exiting, set handoff to move on
        if strategy.strategy == "exit":
            result["handoff_type"] = "ready_for_review"

        return result

    return retry_controller_node
