"""HVD dispatcher — selects which pattern to spawn.

Reads the health report and spec progress, decides whether to spawn
CLR (refinement), PDE (parallel dispatch), or SPR-4 (single task).
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.hvd_dispatcher")

DISPATCH_SYSTEM_PROMPT = """\
You are the dispatch controller for an autonomous engineering system.
Given the repository health state, decide which execution pattern to spawn.

## Available patterns

- **clr**: Continuous refinement loop. Best for steady improvement when
  health is declining and spec items remain. Runs assess → triage → execute → validate → loop.
- **pde**: Parallel dispatch. Best for batch remediation — many independent issues
  across different files (e.g. 20 pyright errors, 10 missing tests).
  Decomposes into N parallel agents.
- **spr4**: Single-task pipeline. Best for a specific complex feature or focused fix.
  Runs research → plan → implement → review.
- **idle**: Nothing to do. Health is good, spec is complete, or budget is exhausted.

## Decision rules

1. If health has critical failures (tests failing) → spr4 (focused fix)
2. If > 5 independent issues across disjoint files → pde (batch parallel)
3. If health declining + spec items remaining → clr (steady refinement)
4. If all healthy + spec complete → idle
5. If budget exhausted → idle (regardless of state)
"""


class DispatchDecision(BaseModel):
    """Structured dispatch decision."""

    pattern: str = Field(description="clr | pde | spr4 | idle")
    reasoning: str = Field(description="Why this pattern was chosen")
    task_description: str = Field(
        default="",
        description="Task for spr4, or goal for pde. Empty for clr/idle.",
    )


def build_hypervisor_dispatcher_node(model: BaseChatModel):
    """Build HVD's pattern dispatch node.

    Args:
        model: LangChain chat model (Haiku — fast decision).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(DispatchDecision)

    async def dispatch_node(state: OrchestratorState) -> dict:
        """Decide which pattern to spawn."""
        history = list(state.get("history", []))
        health = state.get("health_report") or {}
        budget = state.get("global_budget") or {}
        task = state.get("task", "")

        # If human provided a specific task, always use spr4
        if task:
            log.info("human task provided — dispatching spr4")
            return {
                "dispatch_decision": {
                    "pattern": "spr4",
                    "reasoning": "Human provided a specific task",
                    "task_description": task,
                },
                "history": history + [f"hvd: dispatching spr4 — human task: {task[:60]}"],
            }

        # Check budget
        remaining = budget.get("budget_remaining", 10.0)
        if remaining <= 0:
            log.info("budget exhausted — idle")
            return {
                "dispatch_decision": {
                    "pattern": "idle",
                    "reasoning": "Budget exhausted",
                    "task_description": "",
                },
                "history": history + ["hvd: idle — budget exhausted"],
            }

        prompt = (
            f"## Health Report\n\n"
            f"- Tests passing: {health.get('tests_passing', 0)}\n"
            f"- Tests failing: {health.get('tests_failing', 0)}\n"
            f"- Pyright errors: {health.get('pyright_errors', 0)}\n"
            f"- Lint warnings: {health.get('lint_warnings', 0)}\n"
            f"- Spec progress: {health.get('spec_features_done', 0)}/{health.get('spec_features_total', 0)}\n"
            f"- Health score: {health.get('score', 0):.2f}\n"
            f"- Budget remaining: ${remaining:.2f}\n"
        )

        messages = [
            SystemMessage(content=DISPATCH_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        decision = await structured_model.ainvoke(messages)
        assert isinstance(decision, DispatchDecision)

        log.info("dispatch: %s — %s", decision.pattern, decision.reasoning)

        return {
            "dispatch_decision": {
                "pattern": decision.pattern,
                "reasoning": decision.reasoning,
                "task_description": decision.task_description,
            },
            "history": history + [
                f"hvd: dispatching {decision.pattern} — {decision.reasoning}"
            ],
        }

    return dispatch_node
