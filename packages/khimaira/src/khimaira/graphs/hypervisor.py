"""HVD graph — the meta-orchestrator.

Monitors the repository, performs contraction (
budget, directives), and spawns the right pattern: CLR (refinement),
PDE (parallel dispatch), or SPR-4 (single task).

Flow:
    assess → dispatch → execute_pattern → directive_check → absorb → assess (loop)
    or: assess → dispatch → idle → END
"""

from __future__ import annotations

import asyncio

from langgraph.graph import END, START, StateGraph

from khimaira.config import OrchestratorConfig
from khimaira.core.directives import check_directives
from khimaira.core.resource_control import GlobalBudget
from khimaira.core.state import OrchestratorState
from khimaira.log import get_logger
from khimaira.nodes.hypervisor_dispatcher import build_hypervisor_dispatcher_node
from khimaira.nodes.refiner.health_scanner import build_health_scanner_node
from khimaira.tools.git_tools import git_checkpoint, git_revert

log = get_logger("hvd")


async def _init_node(state: OrchestratorState) -> dict:
    """Initialize HVD — set up budget and directives."""
    history = list(state.get("history", []))
    budget = GlobalBudget(
        max_daily_cost_usd=state.get("global_budget", {}).get(
            "max_daily_cost_usd", 10.0
        ),
    )

    return {
        "global_budget": budget.to_dict(),
        "hypervisor_cycle": 0,
        "active_entities": [],
        "history": history + ["hvd: initialized — contraction (contraction) applied"],
    }


async def _execute_pattern_node(state: OrchestratorState) -> dict:
    """Execute the dispatched pattern.

    This node invokes the chosen graph (CLR, PDE, or SPR-4) directly.
    In a full implementation, this would spawn background jobs. For now, it
    does a direct invocation for simplicity.
    """
    from khimaira.prompts.builder import build_prompt
    from khimaira.dispatch.runners import run_claude

    history = list(state.get("history", []))
    decision = state.get("dispatch_decision") or {}
    pattern = decision.get("pattern", "idle")
    task_desc = decision.get("task_description", "")
    cycle = state.get("hypervisor_cycle", 0) + 1

    if pattern == "idle":
        return {
            "hypervisor_cycle": cycle,
            "history": history + [f"hvd(cycle {cycle}): idle — nothing to do"],
        }

    # Git checkpoint before making changes
    await git_checkpoint(f"hvd baseline cycle {cycle}")

    log.info("cycle %d: executing %s — %s", cycle, pattern, task_desc[:60])

    # For now, execute via Claude CLI directly
    # Future: spawn the actual CLR/PDE/SPR-4 graph as a background job
    prompt = build_prompt(
        "You are a senior software engineer. Execute this task precisely.",
        (
            f"## Task\n\n{task_desc}"
            if task_desc
            else "## Task\n\nImprove the codebase based on health assessment."
        ),
        f"## Pattern: {pattern}",
    )

    try:
        result = await run_claude(prompt, timeout=600, permission_mode="acceptEdits")
        return {
            "implementation_result": result,
            "hypervisor_cycle": cycle,
            "history": history + [f"hvd(cycle {cycle}): {pattern} completed"],
        }
    except Exception as e:
        log.error("cycle %d: %s execution failed — %s", cycle, pattern, e)
        return {
            "hypervisor_cycle": cycle,
            "history": history + [f"hvd(cycle {cycle}): {pattern} failed — {e}"],
        }


async def _directive_check_node(state: OrchestratorState) -> dict:
    """Check the changes against DIRECTIVES.md (Special Order 937)."""
    history = list(state.get("history", []))
    cycle = state.get("hypervisor_cycle", 0)

    # Get the diff
    proc = await asyncio.create_subprocess_exec(
        "git",
        "diff",
        "HEAD~1",
        "HEAD",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    diff = stdout.decode("utf-8", errors="replace")

    if not diff.strip():
        return {
            "directive_result": {"passed": True, "violations": []},
            "history": history
            + [f"directive_check(cycle {cycle}): no changes to check"],
        }

    result = check_directives(diff)

    result_dict = {
        "passed": result.passed,
        "violations": [
            {
                "directive": v.directive,
                "description": v.description,
                "severity": v.severity,
                "file": v.file,
            }
            for v in result.violations
        ],
    }

    if not result.passed:
        # Revert the changes
        log.warning("cycle %d: directive violation — reverting!", cycle)
        await git_revert()
        return {
            "directive_result": result_dict,
            "history": history
            + [
                f"directive_check(cycle {cycle}): VIOLATION — "
                + "; ".join(f"{v.severity}: {v.description}" for v in result.violations)
                + " — changes reverted"
            ],
        }

    return {
        "directive_result": result_dict,
        "history": history + [f"directive_check(cycle {cycle}): clean"],
    }


def _after_dispatch(state: OrchestratorState) -> str:
    """Route based on dispatch decision."""
    decision = state.get("dispatch_decision") or {}
    pattern = decision.get("pattern", "idle")
    if pattern == "idle":
        return END
    return "execute_pattern"


def _after_directive(state: OrchestratorState) -> str:
    """Continue looping or exit."""
    cycle = state.get("hypervisor_cycle", 0)
    max_cycles = 20  # HVD's own cycle limit

    if cycle >= max_cycles:
        return END

    budget = state.get("global_budget") or {}
    if budget.get("budget_remaining", 10.0) <= 0:
        return END

    return "assess"  # Loop back


async def build_hypervisor_graph(config: OrchestratorConfig):
    """Build and compile the HVD meta-orchestrator graph.

    Args:
        config: OrchestratorConfig with provider/role definitions.

    Returns:
        Tuple of (compiled StateGraph, AsyncSqliteSaver checkpointer).
    """
    import os
    from pathlib import Path

    import aiosqlite
    from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

    data_dir = (
        Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        / "khimaira"
    )
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "hvd_checkpoints.db")

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()
    log.info("HVD checkpointer ready: %s", db_path)
    health_scanner_node = build_health_scanner_node()
    hvd_dispatch_node = build_hypervisor_dispatcher_node()

    graph = StateGraph(OrchestratorState)

    graph.add_node("init", _init_node)
    graph.add_node("assess", health_scanner_node)
    graph.add_node("dispatch", hvd_dispatch_node)
    graph.add_node("execute_pattern", _execute_pattern_node)
    graph.add_node("directive_check", _directive_check_node)

    graph.add_edge(START, "init")
    graph.add_edge("init", "assess")
    graph.add_edge("assess", "dispatch")
    graph.add_conditional_edges(
        "dispatch", _after_dispatch, {"execute_pattern": "execute_pattern", END: END}
    )
    graph.add_edge("execute_pattern", "directive_check")
    graph.add_conditional_edges(
        "directive_check", _after_directive, {"assess": "assess", END: END}
    )

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled, checkpointer
