"""Classifier — classifies the highest-priority action for CLR.

Reads the health report and decides: fix (defects), refactor (code smells),
feature (next spec item), or idle (converged). Uses Haiku for fast classification.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.triage")

TRIAGE_SYSTEM_PROMPT = """\
You are a triage classifier for an autonomous engineering system. Given a health
report about a codebase, decide the single most important action.

## Priority order (strict — follow this exactly)

1. **fix** — Tests are failing. ALWAYS fix broken tests first.
2. **fix** — Pyright type errors > 5. Type safety before features.
3. **refactor** — Lint warnings > 20. Code quality before features.
4. **feature** — All tests pass AND pyright clean → pick the next unchecked spec item.
5. **idle** — All tests pass AND spec is complete. Nothing to do.

## Rules

- Never propose a feature when tests are failing.
- If health score > 0.9 and spec_features_done == spec_features_total → idle.
- The task_description should be specific enough to hand to an implementation agent.
- For "fix", describe WHAT is broken (e.g., "3 tests failing in test_auth.py — assertion error on line 42").
- For "feature", name the specific spec item to implement.
"""


class TriageDecision(BaseModel):
    """Structured triage decision."""

    action: str = Field(description="fix | refactor | feature | idle")
    task_description: str = Field(description="Specific task for the execution agent")
    reasoning: str = Field(description="Why this action was chosen")
    spec_item: str = Field(default="", description="Which SPEC.md item (if action is feature)")


def build_classifier_node(model: BaseChatModel):
    """Build a triage node for CLR's refinement loop.

    Args:
        model: LangChain chat model (Haiku — fast, cheap).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(TriageDecision)

    async def classifier_node(state: OrchestratorState) -> dict:
        """Classify the highest-priority action."""
        history = list(state.get("history", []))
        health = state.get("health_report") or {}
        cycle = state.get("cycle_count", 0)
        consecutive = state.get("consecutive_no_improvement", 0)
        max_cycles = state.get("max_cycles", 50)

        # Check convergence / budget first
        if consecutive >= 5:
            log.info("cycle %d: converged (5 cycles with no improvement)", cycle)
            return {
                "refiner_action": "idle",
                "refiner_task": "Converged — no improvement in 5 cycles",
                "history": history + [f"triage(cycle {cycle}): idle — converged"],
            }

        if cycle >= max_cycles:
            log.info("cycle %d: budget exhausted (max %d)", cycle, max_cycles)
            return {
                "refiner_action": "idle",
                "refiner_task": "Budget exhausted",
                "history": history + [f"triage(cycle {cycle}): idle — budget exhausted"],
            }

        # Build prompt with health report
        prompt = (
            f"## Health Report\n\n"
            f"- Tests passing: {health.get('tests_passing', 0)}\n"
            f"- Tests failing: {health.get('tests_failing', 0)}\n"
            f"- Pyright errors: {health.get('pyright_errors', 0)}\n"
            f"- Lint warnings: {health.get('lint_warnings', 0)}\n"
            f"- Spec progress: {health.get('spec_features_done', 0)}/{health.get('spec_features_total', 0)}\n"
            f"- Health score: {health.get('score', 0):.2f}\n"
            f"- Cycle: {cycle}\n"
            f"- Consecutive no-improvement: {consecutive}\n"
        )

        # Add recent history context
        if history:
            recent = history[-5:]
            prompt += "\n## Recent history\n\n" + "\n".join(f"- {h}" for h in recent)

        messages = [
            SystemMessage(content=TRIAGE_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        decision = await structured_model.ainvoke(messages)
        assert isinstance(decision, TriageDecision)

        log.info("cycle %d: %s — %s", cycle, decision.action, decision.reasoning)

        return {
            "refiner_action": decision.action,
            "refiner_task": decision.task_description,
            "history": history + [
                f"triage(cycle {cycle}): {decision.action} — {decision.task_description}"
            ],
        }

    return classifier_node
