"""Research phase subgraph — multi-round research with critic loop.

Orchestrates the existing research node (Gemini CLI) in a loop:
    research → critic → (loop if needs_more_research, exit if research_complete)

The critic scores research_findings and decides whether more research is
needed. The loop runs until the critic passes or max steps are reached.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from chimera.nodes.pipeline.critic import build_critic_node
from chimera.nodes.pipeline.research import build_research_node
from chimera.core.state import OrchestratorState


def _after_critic(state: OrchestratorState) -> str:
    """Route based on critic's handoff decision."""
    handoff = state.get("handoff_type", "research_complete")
    if handoff == "needs_more_research":
        return "research"
    return END


def build_research_subgraph(critic_model: BaseChatModel):
    """Build the research phase subgraph.

    Args:
        critic_model: LangChain model for the critic (Haiku).

    Returns:
        Compiled StateGraph (no checkpointer — parent handles that).
    """
    research_node = build_research_node()
    critic_node = build_critic_node(critic_model, "research")

    graph = StateGraph(OrchestratorState)

    graph.add_node("research", research_node)
    graph.add_node("critic", critic_node)

    graph.add_edge(START, "research")
    graph.add_edge("research", "critic")
    graph.add_conditional_edges(
        "critic",
        _after_critic,
        {"research": "research", END: END},
    )

    return graph.compile()
