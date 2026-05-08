"""POB friction analyzer — ranks signals by impact and classifies tool categories.

Groups behavioral signals, ranks by impact × frequency, and classifies
what kind of tool would eliminate each friction point.
Uses Haiku for classification.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.core.state import OrchestratorState
from chimera.log import get_logger

log = get_logger("node.toolbuilder_friction")

FRICTION_SYSTEM_PROMPT = """\
You are a developer productivity analyzer. Given behavioral signals observed from
a developer's workflow (shell history, git patterns, file metrics), identify the
top friction points and classify what kind of tool would fix each one.

## Categories

- **cli_automation** — Repeated terminal commands that could be a script
- **build_optimization** — Slow builds, CI bottlenecks, Dockerfile improvements
- **test_tooling** — Slow tests, missing fixtures, test seeding
- **code_quality** — Lint rules, pre-commit hooks, custom checkers
- **observability** — Logging gaps, profiling, telemetry

## Rules

- Focus on the highest-impact friction (saves the most time or prevents most errors)
- Be specific about what tool to build
- Estimate time saved per week
- Rank by priority (1 = highest)
- Return at most 5 friction points
"""


class FrictionPoint(BaseModel):
    """A ranked friction point with proposed solution."""

    category: str = Field(
        description="cli_automation | build_optimization | test_tooling | code_quality | observability"
    )
    description: str = Field(description="What friction was observed")
    proposed_solution: str = Field(description="What tool to build")
    estimated_time_saved: str = Field(description="Time saved per week (e.g., '15 minutes')")
    priority: int = Field(description="Priority rank (1 = highest)")


class FrictionAnalysis(BaseModel):
    """Structured output from friction analysis."""

    friction_points: list[FrictionPoint] = Field(description="Ranked friction points (max 5)")
    total_signals_analyzed: int = Field(description="Number of signals that were analyzed")


def build_toolbuilder_friction_node(model: BaseChatModel):
    """Build a friction analyzer node.

    Args:
        model: LangChain chat model (Haiku — fast, cheap).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(FrictionAnalysis)

    async def toolbuilder_friction_node(state: OrchestratorState) -> dict:
        """Analyze behavioral signals and identify friction points."""
        history = list(state.get("history", []))
        signals = state.get("toolbuilder_signals") or []

        if not signals:
            return {
                "toolbuilder_friction_points": [],
                "history": history + ["toolbuilder_friction: no signals to analyze"],
            }

        # Build prompt with signals
        signal_lines = []
        for s in signals[:30]:  # Cap at 30 to keep prompt small
            signal_lines.append(
                f"- [{s.get('type')}] {s.get('description')} "
                f"(freq={s.get('frequency')}, impact={s.get('impact')})"
            )

        prompt = (
            f"## Behavioral Signals ({len(signals)} observed)\n\n"
            + "\n".join(signal_lines)
        )

        messages = [
            SystemMessage(content=FRICTION_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        analysis = await structured_model.ainvoke(messages)
        assert isinstance(analysis, FrictionAnalysis)

        friction_dicts = [
            {
                "category": fp.category,
                "description": fp.description,
                "proposed_solution": fp.proposed_solution,
                "estimated_time_saved": fp.estimated_time_saved,
                "priority": fp.priority,
            }
            for fp in analysis.friction_points
        ]

        log.info("POB friction: %d friction points identified", len(friction_dicts))

        return {
            "toolbuilder_friction_points": friction_dicts,
            "history": history + [
                f"toolbuilder_friction: {len(friction_dicts)} friction points from {len(signals)} signals"
            ],
        }

    return toolbuilder_friction_node
