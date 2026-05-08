"""Scope Analyzer — scope expansion proposer.

The creative/generative force. Reads the implementation output and proposes
improvements beyond the original plan — error handling, tests, related fixes.

ScopeAnalyzer does NOT implement. It proposes. Arbitrator decides which proposals to accept.
Capped at 3 proposals per cycle to prevent infinite expansion.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.scope_analyzer")

SCOPE_ANALYZER_SYSTEM_PROMPT = """\
You are a creative engineering advisor. Your job is to spot opportunities for
improvement that the original plan missed.

Look at the implementation output and propose additions that would make it better:

1. **Error handling** — missing try/except, unhandled edge cases, missing input validation
2. **Tests** — if the change has no accompanying tests, propose them
3. **Related improvements** — adjacent code that has the same bug or could benefit from the same pattern
4. **Documentation** — if public API changed but docstrings weren't updated

Rules:
- Propose at most 3 improvements. Quality over quantity.
- Each proposal must be specific — name the file, function, and what to change.
- Do NOT propose architectural redesigns or scope expansions beyond the immediate area.
- Do NOT propose things already covered by the implementation.
- If the implementation is solid and complete, return zero proposals. That's fine.
- Estimate effort: "trivial" (< 5 min), "small" (5-15 min), "moderate" (15-30 min).
"""


class Proposal(BaseModel):
    """A single improvement proposal."""

    description: str = Field(description="What to improve — be specific")
    rationale: str = Field(description="Why this improvement matters")
    files: list[str] = Field(default_factory=list, description="Files that would be modified")
    estimated_effort: str = Field(description="trivial | small | moderate")


class ScopeAnalyzerProposal(BaseModel):
    """Structured proposals from the scope expansion analyzer."""

    proposals: list[Proposal] = Field(
        default_factory=list,
        description="Improvement proposals (max 3). Empty if implementation is solid.",
    )


def build_scope_analyzer_node(model: BaseChatModel):
    """Build a scope expansion proposer node.

    Args:
        model: LangChain chat model (creative model preferred — Gemini or Sonnet).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(ScopeAnalyzerProposal)

    async def scope_analyzer_node(state: OrchestratorState) -> dict:
        """Propose improvements beyond the plan."""
        task = state.get("task", "")
        history = list(state.get("history", []))

        plan = state.get("architecture_plan", "")
        impl = state.get("implementation_result", "")

        if not impl and not plan:
            log.info("nothing to analyze, skipping")
            return {
                "scope_proposals": [],
                "history": history + ["scope_analyzer: nothing to analyze, skipping"],
            }

        prompt_parts = [f"## Original task\n\n{task}"]
        if plan:
            prompt_parts.append(f"## Architecture plan\n\n{plan}")
        if impl:
            prompt_parts.append(f"## Implementation output\n\n{impl}")

        messages = [
            SystemMessage(content=SCOPE_ANALYZER_SYSTEM_PROMPT),
            HumanMessage(content="\n\n".join(prompt_parts)),
        ]

        result_raw = await structured_model.ainvoke(messages)
        assert isinstance(result_raw, ScopeAnalyzerProposal)
        result = result_raw

        # Enforce max 3 proposals
        proposals = result.proposals[:3]

        log.info("proposed %d improvements", len(proposals))

        proposals_dict = [p.model_dump() for p in proposals]

        if proposals:
            summaries = "; ".join(p.description[:60] for p in proposals)
            history_entry = f"scope_analyzer: proposed {len(proposals)} improvements — {summaries}"
        else:
            history_entry = "scope_analyzer: implementation is solid, no proposals"

        return {
            "scope_proposals": proposals_dict,
            "history": history + [history_entry],
        }

    return scope_analyzer_node
