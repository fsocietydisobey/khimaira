"""POB graph — Proactive Observation Builder pipeline.

Observes developer behavior, identifies friction points, proposes tools,
builds them in isolated branches, and opens PRs for approval.

Flow:
    watch → analyze → propose → (nothing to build → END) | forge → pr → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from chimera.config import OrchestratorConfig, get_classify_model
from chimera.core.state import OrchestratorState
from chimera.log import get_logger
from chimera.nodes.toolbuilder.forge import build_toolbuilder_forge_node
from chimera.nodes.toolbuilder.friction import build_toolbuilder_friction_node
from chimera.nodes.toolbuilder.pr_creator import build_toolbuilder_pr_creator_node
from chimera.nodes.toolbuilder.proposer import build_toolbuilder_proposer_node
from chimera.nodes.toolbuilder.watcher import build_toolbuilder_watcher_node

log = get_logger("pob")


def _after_propose(state: OrchestratorState) -> str:
    """If proposer found nothing to build, exit."""
    spec = state.get("toolbuilder_tool_spec")
    if not spec:
        return END
    return "forge"


async def build_toolbuilder_graph(config: OrchestratorConfig):
    """Build and compile the POB proactive tool-builder graph.

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
    db_path = str(data_dir / "pob_checkpoints.db")

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()
    log.info("POB checkpointer ready: %s", db_path)

    model = get_classify_model(config)
    watcher_node = build_toolbuilder_watcher_node()
    friction_node = build_toolbuilder_friction_node(model)
    proposer_node = build_toolbuilder_proposer_node(model)
    forge_node = build_toolbuilder_forge_node()
    pr_creator_node = build_toolbuilder_pr_creator_node()

    graph = StateGraph(OrchestratorState)

    graph.add_node("watch", watcher_node)
    graph.add_node("analyze", friction_node)
    graph.add_node("propose", proposer_node)
    graph.add_node("forge", forge_node)
    graph.add_node("pr", pr_creator_node)

    graph.add_edge(START, "watch")
    graph.add_edge("watch", "analyze")
    graph.add_edge("analyze", "propose")
    graph.add_conditional_edges("propose", _after_propose, {"forge": "forge", END: END})
    graph.add_edge("forge", "pr")
    graph.add_edge("pr", END)

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled, checkpointer
