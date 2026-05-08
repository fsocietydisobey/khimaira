"""PDE graph — parallel dispatch dispatch.

Central task decomposer decomposes a goal into N independent tasks,
fans out via Send(), merges results, and validates atomically.

Supports two dispatch modes:
    - Flat: all tasks dispatched at once (for independent tasks)
    - PDE-F: graduated generations (for tasks with dependencies)
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from chimera.config import OrchestratorConfig, get_classify_model
from chimera.core.state import OrchestratorState
from chimera.log import get_logger
from chimera.nodes.swarm.aggregator import build_swarm_aggregator_node
from chimera.nodes.swarm.task_decomposer import build_swarm_decomposer_node, pdef_budget, sort_into_generations
from chimera.nodes.swarm.worker import build_swarm_worker_node

log = get_logger("pde")


def _fan_out(state: OrchestratorState) -> list[Send]:
    """Dispatch tasks based on the task decomposer's manifest.

    Flat mode: Send() all tasks at once.
    PDE-F mode: Sort tasks into generations by dependency depth,
    then dispatch all generations. Each generation's tasks get context from
    previous generations via accumulated state.

    Note: True sequential generation dispatch (wait for Gen 1 before dispatching
    Gen 2) requires a graph-level loop. For now, all tasks are dispatched at once
    but with generation metadata attached so the decomposer's dependency ordering
    is preserved in the execution order.
    """
    manifest = state.get("swarm_manifest") or {}
    tasks = manifest.get("tasks", [])
    mode = manifest.get("dispatch_mode", "flat")

    if not tasks:
        log.warning("no tasks in manifest — nothing to dispatch")
        return [Send("merge", {})]

    if mode == "pdef" and any(t.get("dependencies") for t in tasks):
        # PDE-F mode: sort into generations
        generations = sort_into_generations(tasks)
        log.info("pdef dispatch: %d generations", len(generations))

        sends = []
        accumulated_context = ""
        for gen_idx, gen_tasks in enumerate(generations):
            budget = pdef_budget(gen_idx)
            for task in gen_tasks:
                payload = {
                    "task": task.get("id", "unknown"),
                    "context": (
                        task.get("description", "")
                        + (f"\n\n## Prior generations' context\n\n{accumulated_context}" if accumulated_context else "")
                    ),
                    "supervisor_instructions": (
                        f"Generation {gen_idx + 1}. "
                        f"Files in scope: {', '.join(task.get('files', []))}. "
                        f"Token budget: {budget}."
                    ),
                }
                sends.append(Send("worker", payload))

            # Build accumulated context for next generation
            for task in gen_tasks:
                accumulated_context += f"\n- {task.get('id', '?')}: {task.get('description', '')[:100]}"

        log.info("dispatching %d agents across %d generations", len(sends), len(generations))
        return sends

    # Flat mode: dispatch all at once
    sends = []
    for task in tasks:
        payload = {
            "task": task.get("id", "unknown"),
            "context": task.get("description", ""),
            "supervisor_instructions": f"Files in scope: {', '.join(task.get('files', []))}",
        }
        sends.append(Send("worker", payload))

    log.info("flat dispatch: %d agents", len(sends))
    return sends


async def build_swarm_graph(config: OrchestratorConfig):
    """Build and compile the PDE parallel dispatch graph.

    Args:
        config: OrchestratorConfig with provider/role definitions.

    Returns:
        Tuple of (compiled StateGraph, AsyncSqliteSaver checkpointer).
    """
    import os
    from pathlib import Path

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    data_dir = Path(
        os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
    ) / "chimera"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "pde_checkpoints.db")

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()
    log.info("PDE checkpointer ready: %s", db_path)

    model = get_classify_model(config)
    task_decomposer_node = build_swarm_decomposer_node(model)
    pde_worker_node = build_swarm_worker_node()
    merge_node = build_swarm_aggregator_node()

    graph = StateGraph(OrchestratorState)

    graph.add_node("decomposer", task_decomposer_node)
    graph.add_node("worker", pde_worker_node)
    graph.add_node("merge", merge_node)

    graph.add_edge(START, "decomposer")
    graph.add_conditional_edges("decomposer", _fan_out)
    graph.add_edge("worker", "merge")
    graph.add_edge("merge", END)

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled, checkpointer
