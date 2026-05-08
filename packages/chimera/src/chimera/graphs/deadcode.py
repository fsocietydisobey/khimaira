"""DCE graph — Dead Code Eliminator pipeline.

Operates in a shadow worktree for safety. Finds dead code via static
analysis, proposes file splits for monoliths, and deletes dead code in
risk-ordered batches with test validation.

Flow:
    create_shadow → seek → shatter → reap → test_and_merge → cleanup → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from chimera.config import OrchestratorConfig
from chimera.core.state import OrchestratorState
from chimera.log import get_logger
from chimera.nodes.deadcode.reaper import build_deadcode_reaper_node
from chimera.nodes.deadcode.seeker import build_deadcode_seeker_node
from chimera.nodes.deadcode.shatterer import build_deadcode_shatterer_node
from chimera.tools.worktree import create_shadow, destroy_shadow, merge_shadow

log = get_logger("dce")


async def _create_shadow_node(state: OrchestratorState) -> dict:
    """Create a shadow worktree for safe DCE operations."""
    history = list(state.get("history", []))

    shadow_path = await create_shadow("dce-shadow")
    if not shadow_path:
        return {
            "deadcode_shadow_path": "",
            "history": history + ["dce: failed to create shadow worktree"],
        }

    log.info("DCE shadow worktree created: %s", shadow_path)
    return {
        "deadcode_shadow_path": shadow_path,
        "history": history + [f"dce: shadow worktree created at {shadow_path}"],
    }


async def _merge_node(state: OrchestratorState) -> dict:
    """Merge shadow changes back to main if any deletions were made."""
    history = list(state.get("history", []))
    shadow_path = state.get("deadcode_shadow_path", "")
    reap_result = state.get("deadcode_reap_result") or {}

    if not shadow_path:
        return {
            "history": history + ["dce_merge: no shadow path — skipping"],
        }

    deleted = reap_result.get("deleted", 0)
    if deleted == 0:
        log.info("DCE: nothing was deleted — skipping merge")
        return {
            "history": history + ["dce_merge: nothing deleted — skipping merge"],
        }

    success, message = await merge_shadow(shadow_path)
    if success:
        log.info("DCE: shadow merged successfully")
        return {
            "history": history + [f"dce_merge: {message}"],
        }
    else:
        log.error("DCE: merge failed — %s", message)
        return {
            "history": history + [f"dce_merge: FAILED — {message}"],
        }


async def _cleanup_node(state: OrchestratorState) -> dict:
    """Destroy the shadow worktree and generate final report."""
    history = list(state.get("history", []))
    shadow_path = state.get("deadcode_shadow_path", "")
    reap_result = state.get("deadcode_reap_result") or {}
    targets = state.get("deadcode_targets") or {}
    split_proposals = state.get("deadcode_split_proposals") or []

    # Cleanup worktree
    if shadow_path:
        await destroy_shadow(shadow_path)
        log.info("DCE shadow worktree destroyed")

    # Build report
    parts = ["## DCE Report\n"]
    parts.append(f"**Targets found:** {len(targets.get('targets', []))}")
    parts.append(f"  - Safe: {targets.get('safe_count', 0)}")
    parts.append(f"  - Moderate: {targets.get('moderate_count', 0)}")
    parts.append(f"  - Risky: {targets.get('risky_count', 0)}")
    parts.append(f"\n**Deletions:** {reap_result.get('deleted', 0)}")
    parts.append(f"**Lines removed:** {reap_result.get('lines_removed', 0)}")
    parts.append(f"**Reverted:** {reap_result.get('reverted', 0)}")
    parts.append(f"**Skipped:** {reap_result.get('skipped', 0)}")

    if split_proposals:
        parts.append(f"\n**Split proposals:** {len(split_proposals)} files")
        for prop in split_proposals[:5]:
            parts.append(f"  - `{prop['file']}` ({prop['total_lines']} lines, {len(prop.get('proposals', []))} splits)")

    risky = reap_result.get("risky_pending_review", 0)
    if risky:
        parts.append(f"\n**Pending human review:** {risky} risky targets")

    report_text = "\n".join(parts)

    return {
        "implementation_result": report_text,
        "deadcode_shadow_path": "",
        "history": history + ["dce: cleanup complete, report generated"],
    }


def _after_create_shadow(state: OrchestratorState) -> str:
    """If shadow creation failed, go straight to cleanup."""
    if not state.get("deadcode_shadow_path"):
        return "cleanup"
    return "seek"


async def build_deadcode_graph(config: OrchestratorConfig):
    """Build and compile the DCE dead code elimination graph.

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
    db_path = str(data_dir / "dce_checkpoints.db")

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()
    log.info("DCE checkpointer ready: %s", db_path)

    seeker_node = build_deadcode_seeker_node()
    shatterer_node = build_deadcode_shatterer_node()
    reaper_node = build_deadcode_reaper_node()

    graph = StateGraph(OrchestratorState)

    graph.add_node("create_shadow", _create_shadow_node)
    graph.add_node("seek", seeker_node)
    graph.add_node("shatter", shatterer_node)
    graph.add_node("reap", reaper_node)
    graph.add_node("merge", _merge_node)
    graph.add_node("cleanup", _cleanup_node)

    graph.add_edge(START, "create_shadow")
    graph.add_conditional_edges(
        "create_shadow", _after_create_shadow,
        {"seek": "seek", "cleanup": "cleanup"},
    )
    graph.add_edge("seek", "shatter")
    graph.add_edge("shatter", "reap")
    graph.add_edge("reap", "merge")
    graph.add_edge("merge", "cleanup")
    graph.add_edge("cleanup", END)

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled, checkpointer
