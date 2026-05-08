"""Planning phase subgraph — architect with Stress Tester adversarial review.

Orchestrates the existing architect node (Claude CLI) in a loop:
    architect → stress_tester (adversarial review) → (loop if blockers, exit if plan_approved)

Stress Tester replaces the passive critic — it actively tries to find flaws in the
architecture plan. If blockers are found, the architect revises.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from chimera.core.state import OrchestratorState
from chimera.nodes.pipeline.architect import build_architect_node
from chimera.nodes.pipeline.critic import build_critic_node
from chimera.nodes.balanced.stress_tester import build_stress_tester_node


def _after_stress_tester(state: OrchestratorState) -> str:
    """Route based on Stress Tester's verdict."""
    verdict = state.get("stress_test_verdict") or {}
    issues = verdict.get("issues", [])
    blockers = [i for i in issues if i.get("severity") == "blocker"]

    # Check step limit
    step = state.get("phase_step", 0)
    max_steps = state.get("max_phase_steps", 5)

    if blockers and step < max_steps:
        return "architect"  # Loop back — blockers must be fixed
    return "critic"  # Pass to critic for final scoring + handoff decision


def build_planning_subgraph(critic_model: BaseChatModel):
    """Build the planning phase subgraph with Stress Tester adversarial review.

    Flow: architect → stress_tester (adversarial) → critic (score + handoff) → loop/exit

    Stress Tester attacks the plan first. If blockers exist, architect revises before
    the critic even scores. Once Stress Tester passes, the critic scores and sets
    handoff_type (plan_approved or plan_revision based on quality threshold).

    Args:
        critic_model: LangChain model for Stress Tester and critic (Haiku).

    Returns:
        Compiled StateGraph (no checkpointer — parent handles that).
    """
    architect_node = build_architect_node()
    stress_tester_node = build_stress_tester_node(critic_model)
    critic_node = build_critic_node(critic_model, "planning")

    graph = StateGraph(OrchestratorState)

    graph.add_node("architect", architect_node)
    graph.add_node("stress_tester", stress_tester_node)
    graph.add_node("critic", critic_node)

    graph.add_edge(START, "architect")
    graph.add_edge("architect", "stress_tester")

    # Stress Tester blockers → loop back to architect; otherwise → critic for scoring
    graph.add_conditional_edges(
        "stress_tester",
        _after_stress_tester,
        {"architect": "architect", "critic": "critic"},
    )

    # Critic decides final handoff (plan_approved or plan_revision)
    def _after_critic(state: OrchestratorState) -> str:
        handoff = state.get("handoff_type", "plan_approved")
        step = state.get("phase_step", 0)
        max_steps = state.get("max_phase_steps", 5)
        if handoff == "plan_revision" and step < max_steps:
            return "architect"
        return END

    graph.add_conditional_edges(
        "critic",
        _after_critic,
        {"architect": "architect", END: END},
    )

    return graph.compile()
