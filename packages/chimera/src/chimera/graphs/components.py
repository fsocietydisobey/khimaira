"""ACL graph — Atomic Component Library validation pipeline.

Scans the codebase for component usage and violations, runs combinatorial
validation on registered components, and enforces compliance on recent changes.

Flow:
    init → scan → validate → enforce → report → END
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from chimera.config import OrchestratorConfig
from chimera.core.component_registry import init_builtin_components
from chimera.core.state import OrchestratorState
from chimera.log import get_logger
from chimera.nodes.components.enforcer import build_component_enforcer_node
from chimera.nodes.components.scanner import build_component_scanner_node
from chimera.nodes.components.validator import build_component_validator_node

log = get_logger("acl")


async def _init_node(state: OrchestratorState) -> dict:
    """Initialize ACL — seed built-in components."""
    history = list(state.get("history", []))
    added = await init_builtin_components()

    return {
        "history": history + [f"acl: initialized ({added} built-in components seeded)"],
    }


async def _report_node(state: OrchestratorState) -> dict:
    """Generate a summary report of ACL status."""
    history = list(state.get("history", []))
    scan = state.get("component_scan_result") or {}
    validation = state.get("component_validation_report") or {}
    enforcement = state.get("component_enforcement_result") or {}

    parts = ["## ACL Report\n"]

    # Scan results
    parts.append(f"**Components registered:** {scan.get('components_registered', 0)}")
    parts.append(f"**Violations found:** {scan.get('violations_found', 0)}")

    # Validation results
    if validation:
        parts.append(f"\n**Validation:** {'PASS' if validation.get('passed') else 'FAIL'}")
        parts.append(f"**Summary:** {validation.get('summary', 'n/a')}")

    # Enforcement results
    if enforcement:
        parts.append(f"\n**Enforcement:** {'PASS' if enforcement.get('passed') else 'FAIL'}")
        parts.append(f"**Files checked:** {enforcement.get('files_checked', 0)}")
        violations = enforcement.get("violations", [])
        if violations:
            parts.append("\n**Violations:**")
            for v in violations[:10]:
                f = v.get('file')
                ln = v.get('line')
                raw = v.get('import')
                use = v.get('should_use')
                parts.append(f"- `{f}:{ln}` — raw `{raw}`, use `{use}`")

    report_text = "\n".join(parts)

    return {
        "implementation_result": report_text,
        "history": history + ["acl: report generated"],
    }


def _after_validate(state: OrchestratorState) -> str:
    """If validation failed, skip enforcement (components are broken)."""
    validation = state.get("component_validation_report") or {}
    if not validation.get("passed", True):
        return "report"
    return "enforce"


async def build_components_graph(config: OrchestratorConfig):
    """Build and compile the ACL validation graph.

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
    db_path = str(data_dir / "acl_checkpoints.db")

    conn = await aiosqlite.connect(db_path)
    await conn.execute("PRAGMA journal_mode=WAL")
    checkpointer = AsyncSqliteSaver(conn)
    await checkpointer.setup()
    log.info("ACL checkpointer ready: %s", db_path)

    scanner_node = build_component_scanner_node()
    validator_node = build_component_validator_node()
    enforcer_node = build_component_enforcer_node()

    graph = StateGraph(OrchestratorState)

    graph.add_node("init", _init_node)
    graph.add_node("scan", scanner_node)
    graph.add_node("validate", validator_node)
    graph.add_node("enforce", enforcer_node)
    graph.add_node("report", _report_node)

    graph.add_edge(START, "init")
    graph.add_edge("init", "scan")
    graph.add_edge("scan", "validate")
    graph.add_conditional_edges("validate", _after_validate, {"enforce": "enforce", "report": "report"})
    graph.add_edge("enforce", "report")
    graph.add_edge("report", END)

    compiled = graph.compile(checkpointer=checkpointer)
    return compiled, checkpointer
