"""Review phase subgraph — Integration Gate integration gate + human approval.

Runs the integration validator (full test suite + type checker + diff review),
then pauses for human approval via the existing human_review node.

    integration_gate → human_review (PAUSED) → exit

The human sets human_approved on approval or provides feedback on rejection.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from chimera.core.state import OrchestratorState
from chimera.nodes.human_review import build_human_review_node
from chimera.nodes.balanced.integration_gate import build_integration_gate_node


async def _set_review_handoff(state: OrchestratorState) -> dict:
    """Set handoff_type based on human review decision."""
    history = list(state.get("history", []))
    review_status = state.get("human_review_status", "")

    if review_status == "approved":
        return {
            "handoff_type": "done",
            "human_approved": True,
            "history": history + ["review: human approved — marking done"],
        }
    else:
        feedback = state.get("human_feedback", "")
        return {
            "handoff_type": "needs_impl_fix",
            "history": history + [f"review: human rejected — {feedback}"],
        }


def build_review_subgraph(validator_model: BaseChatModel):
    """Build the review phase subgraph with Integration Gate integration gate.

    Flow: integration_gate → human_review (HITL) → set_handoff → exit

    Args:
        validator_model: LangChain model (unused — Integration Gate is deterministic,
            but kept for API consistency with other subgraph builders).

    Returns:
        Compiled StateGraph (no checkpointer — parent handles that).
    """
    integration_gate_node = build_integration_gate_node()
    human_review_node = build_human_review_node()

    graph = StateGraph(OrchestratorState)

    graph.add_node("integration_gate", integration_gate_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("set_handoff", _set_review_handoff)

    graph.add_edge(START, "integration_gate")
    graph.add_edge("integration_gate", "human_review")
    graph.add_edge("human_review", "set_handoff")
    graph.add_edge("set_handoff", END)

    return graph.compile()
