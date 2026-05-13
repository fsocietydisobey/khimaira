"""CLR graph — continuous refinement loop.

A self-sustaining closed loop:
    assess → triage → execute (via SPR-4) → validate → commit/rollback → assess

Runs until convergence (5 cycles with no improvement), budget exhaustion,
or idle (spec complete + healthy).
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from khimaira.config import OrchestratorConfig
from khimaira.core.fitness import assess_health
from khimaira.core.refiner_memory import get_last_cycle_number, get_recent_cycles, log_cycle
from khimaira.core.state import OrchestratorState
from khimaira.log import get_logger
from khimaira.nodes.refiner.classifier import build_classifier_node
from khimaira.nodes.refiner.health_scanner import build_health_scanner_node
from khimaira.tools.git_tools import git_checkpoint, git_diff_files, git_revert, is_self_modification

log = get_logger("clr")


async def _init_node(state: OrchestratorState) -> dict:
    """Initialize the refinement loop — set baseline and load resume state."""
    history = list(state.get("history", []))
    max_cycles = state.get("max_cycles", 50)

    # Resume from last cycle if restarting
    last_cycle = await get_last_cycle_number()
    if last_cycle > 0:
        log.info("resuming from cycle %d", last_cycle)
        history.append(f"clr: resuming from cycle {last_cycle}")

    # Load recent refinement context
    recent = await get_recent_cycles(limit=5)
    memory_parts = []
    for r in recent:
        memory_parts.append(
            f"cycle {r['cycle']}: {r['action']} — {r['description']}"
            + (" (reverted)" if r['reverted'] else "")
        )
    memory = "\n".join(memory_parts) if memory_parts else ""

    return {
        "cycle_count": last_cycle,
        "max_cycles": max_cycles,
        "consecutive_no_improvement": 0,
        "health_baseline": 0.0,
        "requires_restart": False,
        "memory_context": memory,
        "history": history + ["clr: initialized"],
    }


async def _set_baseline_node(state: OrchestratorState) -> dict:
    """Record the current health score as baseline before executing a task."""
    score = state.get("health_score", 0.0)
    cycle = state.get("cycle_count", 0) + 1

    # Git checkpoint before making changes
    await git_checkpoint(f"baseline before cycle {cycle}")

    return {
        "health_baseline": score,
        "cycle_count": cycle,
    }


async def _execute_node(state: OrchestratorState) -> dict:
    """Execute the triage decision by invoking SPR-4 with the generated task.

    Note: In a full implementation, this would embed the SPR-4 graph as a subgraph.
    For now, it delegates to the existing CLI runners directly for simplicity.
    """
    from khimaira.prompts.builder import build_prompt
    from khimaira.dispatch.runners import run_claude

    action = state.get("refiner_action", "idle")
    task = state.get("refiner_task", "")
    history = list(state.get("history", []))
    cycle = state.get("cycle_count", 0)

    if action == "idle":
        return {"history": history + [f"execute(cycle {cycle}): idle — nothing to do"]}

    log.info("cycle %d: executing %s — %s", cycle, action, task[:80])

    # Use Claude CLI to execute the task
    prompt = build_prompt(
        "You are a senior software engineer. Execute this task precisely.",
        f"## Task\n\n{task}",
        f"## Action type: {action}",
        "Read the relevant files, make the changes, and verify they work.",
    )

    try:
        result = await run_claude(prompt, timeout=600, permission_mode="acceptEdits")
        return {
            "implementation_result": result,
            "history": history + [f"execute(cycle {cycle}): completed {action}"],
        }
    except Exception as e:
        log.error("cycle %d: execution failed — %s", cycle, e)
        return {
            "history": history + [f"execute(cycle {cycle}): failed — {e}"],
        }


async def _validate_node(state: OrchestratorState) -> dict:
    """Re-assess health and compare to baseline."""
    history = list(state.get("history", []))
    cycle = state.get("cycle_count", 0)
    baseline = state.get("health_baseline", 0.0)
    consecutive = state.get("consecutive_no_improvement", 0)

    report = await assess_health()
    new_score = report.score
    delta = new_score - baseline

    if delta > 0:
        decision = "commit"
        consecutive = 0
        log.info("cycle %d: improved %.2f → %.2f (+%.2f) → commit", cycle, baseline, new_score, delta)
    elif delta == 0:
        decision = "commit"  # Neutral change — may be prep for future improvement
        consecutive += 1
        log.info("cycle %d: unchanged %.2f → commit (consecutive=%d)", cycle, new_score, consecutive)
    else:
        decision = "rollback"
        consecutive += 1
        log.info("cycle %d: regressed %.2f → %.2f (%.2f) → rollback", cycle, baseline, new_score, delta)

    return {
        "health_score": new_score,
        "health_report": report.to_dict(),
        "consecutive_no_improvement": consecutive,
        "refiner_action": decision,
        "history": history + [
            f"validate(cycle {cycle}): {baseline:.2f} → {new_score:.2f} ({delta:+.2f}) → {decision}"
        ],
    }


async def _commit_or_rollback_node(state: OrchestratorState) -> dict:
    """Commit or revert based on validation result, log to CLR memory."""
    history = list(state.get("history", []))
    cycle = state.get("cycle_count", 0)
    action = state.get("refiner_action", "commit")
    task = state.get("refiner_task", "")
    baseline = state.get("health_baseline", 0.0)
    score = state.get("health_score", 0.0)

    reverted = False
    if action == "rollback":
        success = await git_revert()
        reverted = True
        if success:
            log.info("cycle %d: reverted", cycle)
        else:
            log.warning("cycle %d: revert failed", cycle)
    else:
        changed_files = await git_diff_files()
        await git_checkpoint(f"cycle {cycle}: {task[:60]}")

        # Check for self-modification
        if await is_self_modification(changed_files):
            log.warning("cycle %d: self-modification detected!", cycle)
            return {
                "requires_restart": True,
                "history": history + [f"commit(cycle {cycle}): self-modification detected — restart required"],
            }

    # Log to CLR memory
    await log_cycle(
        cycle=cycle,
        action=state.get("refiner_action", "unknown"),
        description=task,
        health_before=baseline,
        health_after=score,
        reverted=reverted,
    )

    return {
        "history": history + [
            f"{'rollback' if reverted else 'commit'}(cycle {cycle}): "
            f"health {baseline:.2f} → {score:.2f}"
        ],
    }


def _after_triage(state: OrchestratorState) -> str:
    """Route based on triage decision."""
    action = state.get("refiner_action", "idle")
    if action == "idle":
        return END
    return "set_baseline"


def _after_commit(state: OrchestratorState) -> str:
    """Check if we should continue looping or exit."""
    if state.get("requires_restart"):
        return END  # Outer daemon will restart
    if state.get("refiner_action") == "idle":
        return END
    return "assess"  # Loop back


async def build_refiner_graph(config: OrchestratorConfig):
    """Build and compile the CLR refinement loop graph.

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
    ) / "khimaira"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "clr_checkpoints.db")

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()
    log.info("CLR checkpointer ready: %s", db_path)
    health_scanner_node = build_health_scanner_node()
    classifier_node = build_classifier_node()

    graph = StateGraph(OrchestratorState)

    graph.add_node("init", _init_node)
    graph.add_node("assess", health_scanner_node)
    graph.add_node("triage", classifier_node)
    graph.add_node("set_baseline", _set_baseline_node)
    graph.add_node("execute", _execute_node)
    graph.add_node("validate", _validate_node)
    graph.add_node("commit_or_rollback", _commit_or_rollback_node)

    # Wiring
    graph.add_edge(START, "init")
    graph.add_edge("init", "assess")
    graph.add_edge("assess", "triage")
    graph.add_conditional_edges("triage", _after_triage, {"set_baseline": "set_baseline", END: END})
    graph.add_edge("set_baseline", "execute")
    graph.add_edge("execute", "validate")
    graph.add_edge("validate", "commit_or_rollback")
    graph.add_conditional_edges("commit_or_rollback", _after_commit, {"assess": "assess", END: END})

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled, checkpointer
