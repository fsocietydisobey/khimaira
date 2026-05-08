"""Implementation phase subgraph — TFB balanced forces.

Full flow with expansion/restriction/synthesis:
    guard → implement → stress_tester (attack) → scope_analyzer (propose) → arbitrator (arbitrate) → compliance (format)
    → arbitrator decides: loop back to implement, or exit as ready_for_review

The guard node enforces the plan_approved invariant — implementation
cannot proceed without an approved architecture plan.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from chimera.core.guards import require_plan_approved
from chimera.core.state import OrchestratorState
from chimera.nodes.balanced.scope_analyzer import build_scope_analyzer_node
from chimera.nodes.balanced.stress_tester import build_stress_tester_node
from chimera.nodes.balanced.compliance import build_compliance_node
from chimera.nodes.pipeline.implement import build_implement_node
from chimera.nodes.balanced.arbitrator import build_arbitrator_node


async def _guard_node(state: OrchestratorState) -> dict:
    """Enforce plan_approved invariant before implementation.

    Also resets the subgraph-local `implementation_loop_step` to 0 on
    every subgraph entry. The parent pipeline may invoke this subgraph
    multiple times across a single run; without this reset the loop
    counter would accumulate and trigger an early "max iterations"
    exit on the second invocation.
    """
    if not require_plan_approved(state):
        history = list(state.get("history", []))
        return {
            "handoff_type": "plan_not_approved",
            "history": history + ["guard: blocked implementation — plan not approved"],
        }
    return {"implementation_loop_step": 0}


def _after_guard(state: OrchestratorState) -> str:
    """Block implementation if plan is not approved."""
    if state.get("handoff_type") == "plan_not_approved":
        return END
    return "implement"


def _after_arbitrator(state: OrchestratorState) -> str:
    """Route based on Arbitrator's arbitration decision.

    Returns one of: "implement" (rework loop), "compliance" (proceed).
    Both must be keys in the conditional-edges mapping below.
    """
    arb = state.get("arbitration_decision") or {}
    handoff = state.get("handoff_type", "")

    # Stress Tester blocker or Arbitrator says rework needed.
    # Note: arbitrator clears handoff_type to "ready_for_review" when
    # needs_rework=False, so the OR clause is informational redundancy
    # rather than a separate signal — but keeping it as a defense in
    # depth in case some other node sets tests_failing in the future.
    if arb.get("needs_rework") or handoff == "tests_failing":
        # Self-bound the loop using the subgraph-local counter. Without
        # this cap the loop runs until LangGraph's recursion_limit
        # kills it — which is what bug-report 2026-05-07 surfaced.
        step = state.get("implementation_loop_step", 0)
        max_steps = state.get("max_phase_steps", 5)
        if step >= max_steps:
            return "compliance"  # Max iterations — ship what we have
        return "implement"  # Loop back for rework

    return "compliance"  # Proceed to formatting


def build_implementation_subgraph(
    critic_model: BaseChatModel,
    review_model: BaseChatModel | None = None,
):
    """Build the implementation phase subgraph with TFB balanced forces.

    Flow: guard → implement → stress_tester → scope_analyzer → arbitrator → compliance → exit
    With loop: if arbitrator says needs_rework → back to implement

    Args:
        critic_model: LangChain model for Stress Tester and ScopeAnalyzer (Haiku).
        review_model: LangChain model for Arbitrator (should be different from
            the builder for cross-model review). Falls back to critic_model.

    Returns:
        Compiled StateGraph (no checkpointer — parent handles that).
    """
    implement_node = build_implement_node()
    stress_tester_node = build_stress_tester_node(critic_model)
    scope_analyzer_node = build_scope_analyzer_node(critic_model)
    arbitrator_node = build_arbitrator_node(review_model or critic_model)
    compliance_node = build_compliance_node()

    graph = StateGraph(OrchestratorState)

    graph.add_node("guard", _guard_node)
    graph.add_node("implement", implement_node)
    graph.add_node("stress_tester", stress_tester_node)
    graph.add_node("scope_analyzer", scope_analyzer_node)
    graph.add_node("arbitrator", arbitrator_node)
    graph.add_node("compliance", compliance_node)

    # Entry
    graph.add_edge(START, "guard")
    graph.add_conditional_edges(
        "guard",
        _after_guard,
        {"implement": "implement", END: END},
    )

    # Implementation → Stress Tester (attack) → ScopeAnalyzer (propose) → Arbitrator (arbitrate)
    graph.add_edge("implement", "stress_tester")
    graph.add_edge("stress_tester", "scope_analyzer")
    graph.add_edge("scope_analyzer", "arbitrator")

    # Arbitrator decides: rework or proceed to Compliance
    graph.add_conditional_edges(
        "arbitrator",
        _after_arbitrator,
        {"implement": "implement", "compliance": "compliance"},
    )

    # Compliance (format) → exit
    graph.add_edge("compliance", END)

    return graph.compile()
