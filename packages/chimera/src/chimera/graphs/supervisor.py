"""LangGraph StateGraph — v0.6 Dynamic Supervisor with Fan-Out, HITL, and Error Recovery.

Hub-and-spoke architecture: every node returns to the supervisor, which
inspects the full state and decides what to do next (or terminate).

v0.6 features:
    - Fan-out/fan-in: supervisor dispatches parallel research via Send()
    - Human-in-the-loop: graph pauses for human approval before implementation
    - Dynamic routing via Pydantic RouterDecision (structured LLM output)
    - Error recovery: nodes catch failures, supervisor retries with feedback
    - Max retry enforcement: select_next_node caps calls per node
    - Persistent checkpoints via AsyncSqliteSaver
    - Supervisor uses cheap API model (Haiku). Domain nodes use CLI subprocesses.

Flow with HITL:
    supervisor → architect → supervisor → human_review (PAUSED)
    → [human approves] → supervisor → implement → supervisor → END
    → [human rejects]  → supervisor → architect (with feedback) → ...
"""

from __future__ import annotations

import os
from pathlib import Path

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from chimera.config import OrchestratorConfig, get_classify_model
from chimera.core.state import OrchestratorState
from chimera.log import get_logger
from chimera.nodes import (
    build_architect_node,
    build_gemini_assist_node,
    build_human_review_node,
    build_implement_node,
    build_research_node,
    build_supervisor_node,
    build_validator_node,
)

log = get_logger("graph")

MAX_RETRIES_PER_NODE = 3


def _get_db_path() -> str:
    """Get the path for the checkpoint database."""
    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "chimera"
    data_dir.mkdir(parents=True, exist_ok=True)
    return str(data_dir / "checkpoints.db")


def select_next_node(state: OrchestratorState) -> str | list[Send]:
    """Read the supervisor's decision and route to the next node.

    Enforces max retries per node — if a node has been called MAX_RETRIES_PER_NODE
    times, force finish instead of allowing another call.

    If parallel_tasks is populated and next_node is 'research',
    fan out via Send(). Otherwise, route sequentially.
    """
    next_node = state.get("next_node", "finish")
    if next_node == "finish":
        return END

    # Enforce max retries — hard cap regardless of supervisor decision
    node_calls = state.get("node_calls", {})
    if node_calls.get(next_node, 0) >= MAX_RETRIES_PER_NODE:
        return END

    parallel_tasks = state.get("parallel_tasks", [])

    # Fan-out: supervisor chose research AND provided parallel sub-tasks
    if next_node == "research" and parallel_tasks:
        return [
            Send(
                "research",
                {
                    "task": state.get("task", ""),
                    "context": state.get("context", ""),
                    "supervisor_instructions": pt.get("instructions", ""),
                    "parallel_task_topic": pt.get("topic", ""),
                    "validation_feedback": state.get("validation_feedback", ""),
                },
            )
            for pt in parallel_tasks
        ]

    return next_node


def _research_exit(state: OrchestratorState) -> str:
    """Route research output: to merge_research if fan-out, else to supervisor."""
    if state.get("parallel_tasks", []):
        return "merge_research"
    return "supervisor"


async def _merge_research_node(state: OrchestratorState) -> dict:
    """Combine parallel research outputs into a single research_findings string.

    Reads output_versions to find entries from the current parallel batch,
    synthesizes them into sectioned markdown, then clears parallel_tasks.
    """
    output_versions = state.get("output_versions") or []
    parallel_tasks = state.get("parallel_tasks") or []
    history = list(state.get("history") or [])

    # Collect research entries matching the current parallel topics
    parallel_topics = {pt.get("topic", "") for pt in parallel_tasks}
    parallel_results = [
        entry
        for entry in output_versions
        if entry.get("node") == "research" and entry.get("topic", "") in parallel_topics
    ]

    # Synthesize into sectioned markdown
    if parallel_results:
        sections = []
        for result in parallel_results:
            topic = result.get("topic", "unknown")
            content = result.get("content", "")
            sections.append(f"### {topic}\n\n{content}")
        merged = "\n\n---\n\n".join(sections)
    else:
        # Fallback: use whatever research_findings exists
        merged = state.get("research_findings", "")

    history.append(f"merge_research: combined {len(parallel_results)} parallel findings")

    return {
        "research_findings": merged,
        "parallel_tasks": [],  # Clear to prevent re-triggering fan-out
        "history": history,
    }


async def build_orchestrator_graph(config: OrchestratorConfig):
    """Build and compile the orchestrator StateGraph with persistent checkpointing.

    The supervisor (Haiku) makes routing decisions. Domain nodes (research,
    architect, implement) use CLI subprocesses. Validator (Haiku) scores output.
    Fan-out is supported for research via Send(). Human review pauses
    the graph before implementation.

    Uses AsyncSqliteSaver for persistent checkpoints that survive server restarts.

    Args:
        config: OrchestratorConfig with provider/role definitions.

    Returns:
        Tuple of (compiled StateGraph, AsyncSqliteSaver checkpointer).
    """
    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    # Persistent checkpointer — survives server restarts
    db_path = _get_db_path()
    conn = await aiosqlite.connect(db_path)
    # WAL mode for concurrent reads (e.g. sqlite3 CLI while server runs)
    await conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()
    log.info("checkpointer ready: %s", db_path)

    # Supervisor and validator use a cheap/fast API model
    supervisor_model = get_classify_model(config)

    supervisor_node = build_supervisor_node(supervisor_model)
    validator_node = build_validator_node(supervisor_model)
    research_node = build_research_node()
    architect_node = build_architect_node()
    implement_node = build_implement_node()
    human_review_node = build_human_review_node()
    gemini_assist_node = build_gemini_assist_node()

    # --- Wire the graph ---
    graph = StateGraph(OrchestratorState)

    graph.add_node("supervisor", supervisor_node)
    graph.add_node("research", research_node)
    graph.add_node("architect", architect_node)
    graph.add_node("implement", implement_node)
    graph.add_node("validator", validator_node)
    graph.add_node("merge_research", _merge_research_node)
    graph.add_node("human_review", human_review_node)
    graph.add_node("gemini_assist", gemini_assist_node)

    # Entry: START → supervisor (supervisor makes the first decision)
    graph.add_edge(START, "supervisor")

    # Supervisor routes dynamically (may return Send() list for fan-out)
    graph.add_conditional_edges(
        "supervisor",
        select_next_node,
        {
            "research": "research",
            "architect": "architect",
            "human_review": "human_review",
            "implement": "implement",
            "validator": "validator",
            "gemini_assist": "gemini_assist",
            END: END,
        },
    )

    # Research → conditional: fan-out goes to merge, sequential goes to supervisor
    graph.add_conditional_edges(
        "research",
        _research_exit,
        {
            "merge_research": "merge_research",
            "supervisor": "supervisor",
        },
    )

    # merge_research always returns to supervisor
    graph.add_edge("merge_research", "supervisor")

    # human_review returns to supervisor (which reads approval/rejection)
    graph.add_edge("human_review", "supervisor")

    # Other nodes return to supervisor
    graph.add_edge("architect", "supervisor")
    graph.add_edge("implement", "supervisor")
    graph.add_edge("gemini_assist", "supervisor")
    graph.add_edge("validator", "supervisor")

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled, checkpointer
