"""SPR-4 parent graph — phase router with hierarchical subgraphs.

CHIMERA / SPR-4: Sequential Phase Runner. A separate graph
entry point that orchestrates four phase subgraphs (research, planning,
implementation, review) via structured handoffs.

The phase router reads handoff_type after each phase and routes to the
next phase, loops back, or terminates. Each subgraph reuses existing
node factories (build_research_node, build_architect_node, etc.) and
adds critic loops for quality-gated progression.

Flow:
    START → load_memory → phase_router → {research | planning | impl | review} → phase_router → ... → save_memory → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from chimera.config import OrchestratorConfig, get_classify_model
from chimera.core.guards import require_plan_approved
from chimera.core.memory import get_recent_context, save_run
from chimera.core.state import OrchestratorState
from chimera.log import get_logger
from chimera.subgraphs import (
    build_implementation_subgraph,
    build_planning_subgraph,
    build_research_subgraph,
    build_review_subgraph,
)

log = get_logger("spr4")

# Default max steps per phase
DEFAULT_MAX_STEPS = {
    "research": 5,
    "planning": 5,
    "implementation": 5,
    "review": 1,
}


async def _load_memory_node(state: OrchestratorState) -> dict:
    """Load past run context into state at graph entry."""
    history = list(state.get("history", []))

    # Try to load context from previous runs
    memory_context = await get_recent_context(limit=3)
    if memory_context:
        log.info("injected memory context (%d chars)", len(memory_context))
        history.append("spr4: loaded past run context from memory")
    else:
        history.append("spr4: no past run context found")

    return {
        "memory_context": memory_context,
        "phase": "research",  # Default starting phase
        "handoff_type": "",
        "phase_step": 0,
        "max_phase_steps": DEFAULT_MAX_STEPS["research"],
        "plan_approved": False,
        "human_approved": False,
        "history": history,
    }


async def _phase_router_node(state: OrchestratorState) -> dict:
    """Read handoff_type and route to the next phase.

    This is the hub of the SPR-4 graph. After each phase subgraph returns,
    it reads the handoff_type and decides:
    - Which phase to run next
    - Whether to loop back to the same phase
    - Whether to terminate

    Also resets phase_step and sets max_phase_steps for the next phase.
    """
    handoff = state.get("handoff_type", "")
    current_phase = state.get("phase", "")
    history = list(state.get("history", []))

    # Determine next phase based on handoff_type
    next_phase = _resolve_next_phase(handoff, current_phase, state)

    if next_phase == "done":
        log.info("phase router: done (handoff=%s)", handoff)
        history.append(f"phase_router: finishing (handoff={handoff})")
        return {
            "phase": "done",
            "history": history,
        }

    # Reset step counter for the new phase
    max_steps = DEFAULT_MAX_STEPS.get(next_phase, 5)

    log.info(
        "phase router: %s → %s (handoff=%s, max_steps=%d)",
        current_phase or "start", next_phase, handoff, max_steps,
    )
    history.append(f"phase_router: {current_phase or 'start'} → {next_phase} (handoff={handoff})")

    return {
        "phase": next_phase,
        "handoff_type": "",  # Clear for the next phase
        "phase_step": 0,
        "max_phase_steps": max_steps,
        "history": history,
    }


def _resolve_next_phase(
    handoff: str, current_phase: str, state: OrchestratorState
) -> str:
    """Determine the next phase based on handoff_type and current phase.

    Handoff routing table:
        "" (empty, initial)        → research (default start)
        "research_complete"        → planning
        "needs_more_research"      → research (loop)
        "plan_approved"            → implementation (if plan_approved, else planning)
        "plan_revision"            → planning (loop)
        "ready_for_review"         → review
        "tests_failing"            → implementation (loop)
        "done"                     → done (terminate)
        "needs_impl_fix"           → implementation (retry)
        "plan_not_approved"        → planning (guard blocked impl)
        "max_steps_reached"        → next logical phase
    """
    if handoff == "" and current_phase == "":
        return "research"

    if handoff == "research_complete":
        return "planning"

    if handoff == "needs_more_research":
        return "research"

    if handoff == "plan_approved":
        if require_plan_approved(state):
            return "implementation"
        return "planning"

    if handoff == "plan_revision":
        return "planning"

    if handoff == "ready_for_review":
        return "review"

    if handoff == "tests_failing":
        return "implementation"

    if handoff == "needs_impl_fix":
        return "implementation"

    if handoff == "plan_not_approved":
        return "planning"

    if handoff == "done":
        return "done"

    # Fallback: advance to next logical phase
    phase_order = ["research", "planning", "implementation", "review", "done"]
    if current_phase in phase_order:
        idx = phase_order.index(current_phase)
        if idx + 1 < len(phase_order):
            return phase_order[idx + 1]

    return "done"


def _route_to_phase(state: OrchestratorState) -> str:
    """Conditional edge: route from phase_router to the correct phase subgraph."""
    phase = state.get("phase", "done")
    if phase in ("research", "planning", "implementation", "review"):
        return f"{phase}_phase"
    return "save_memory"


async def _save_memory_node(state: OrchestratorState) -> dict:
    """Save run summary to persistent memory before exiting."""
    history = state.get("history", [])
    task = state.get("task", "")

    # Build summary from history
    summary = "; ".join(history[-5:]) if history else "No history"

    # Extract key decisions
    decisions = [h for h in history if h.startswith("phase_router:") or h.startswith("critic(")]

    # Determine outcome
    outcome = "completed"
    if state.get("handoff_type") == "plan_not_approved":
        outcome = "blocked at implementation — plan not approved"
    else:
        node_failure = state.get("node_failure") or {}
        if node_failure:
            outcome = f"failed — {node_failure.get('error', 'unknown')}"

    try:
        await save_run(
            thread_id="",  # No thread_id in SPR-4 yet — could add later
            summary=summary,
            task=task,
            decisions=decisions,
            outcome=outcome,
        )
    except Exception as e:
        log.warning("failed to save run memory: %s", e)

    return {
        "history": list(history) + [f"spr4: saved run memory (outcome={outcome})"],
    }


async def build_pipeline_graph(config: OrchestratorConfig):
    """Build and compile the SPR-4 parent graph with phase subgraphs.

    Uses the same persistent checkpointer as Option B. The four phase
    subgraphs are compiled without checkpointers (the parent handles it).

    Args:
        config: OrchestratorConfig with provider/role definitions.

    Returns:
        Tuple of (compiled StateGraph, AsyncSqliteSaver checkpointer).
    """
    import os
    from pathlib import Path

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    # Persistent checkpointer (same path as Option B)
    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "chimera"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "spr4_checkpoints.db")

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()
    log.info("SPR-4 checkpointer ready: %s", db_path)

    # Build the critic/validator model (Haiku — cheap and fast)
    critic_model = get_classify_model(config)

    # Build phase subgraphs (no checkpointer — parent handles persistence)
    # Implementation subgraph gets a separate review_model for cross-model arbitration
    research_phase = build_research_subgraph(critic_model)
    planning_phase = build_planning_subgraph(critic_model)
    implementation_phase = build_implementation_subgraph(critic_model, review_model=critic_model)
    review_phase = build_review_subgraph(critic_model)

    # --- Wire the parent graph ---
    graph = StateGraph(OrchestratorState)

    # Entry nodes
    graph.add_node("load_memory", _load_memory_node)
    graph.add_node("phase_router", _phase_router_node)

    # Phase subgraphs as nodes
    graph.add_node("research_phase", research_phase)
    graph.add_node("planning_phase", planning_phase)
    graph.add_node("implementation_phase", implementation_phase)
    graph.add_node("review_phase", review_phase)

    # Exit node
    graph.add_node("save_memory", _save_memory_node)

    # Edges
    graph.add_edge(START, "load_memory")
    graph.add_edge("load_memory", "phase_router")

    # Phase router → phase subgraph (or save_memory if done)
    graph.add_conditional_edges(
        "phase_router",
        _route_to_phase,
        {
            "research_phase": "research_phase",
            "planning_phase": "planning_phase",
            "implementation_phase": "implementation_phase",
            "review_phase": "review_phase",
            "save_memory": "save_memory",
        },
    )

    # All phases return to phase router
    graph.add_edge("research_phase", "phase_router")
    graph.add_edge("planning_phase", "phase_router")
    graph.add_edge("implementation_phase", "phase_router")
    graph.add_edge("review_phase", "phase_router")

    # save_memory → END
    graph.add_edge("save_memory", END)

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled, checkpointer
