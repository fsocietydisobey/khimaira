"""Health scanner — runs the fitness function and writes health metrics to state.

Pure deterministic evaluation. No LLM calls. Runs pytest, pyright, ruff,
and parses SPEC.md to produce a HealthReport.
"""

from chimera.log import get_logger
from chimera.core.fitness import assess_health
from chimera.core.state import OrchestratorState

log = get_logger("node.assess")


def build_health_scanner_node():
    """Build an assess node that runs the fitness function.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def health_scanner_node(state: OrchestratorState) -> dict:
        """Run health assessment and write results to state."""
        history = list(state.get("history", []))
        cycle = state.get("cycle_count", 0)

        log.info("cycle %d: running health assessment...", cycle)
        report = await assess_health()

        log.info(
            "cycle %d: health %.2f (tests=%d/%d, pyright=%d, lint=%d, spec=%d/%d)",
            cycle, report.score,
            report.tests_passing, report.tests_passing + report.tests_failing,
            report.pyright_errors, report.lint_warnings,
            report.spec_features_done, report.spec_features_total,
        )

        return {
            "health_report": report.to_dict(),
            "health_score": report.score,
            "history": history + [
                f"assess(cycle {cycle}): health {report.score:.2f} — "
                f"tests {report.tests_passing}/{report.tests_passing + report.tests_failing}, "
                f"pyright {report.pyright_errors}, lint {report.lint_warnings}, "
                f"spec {report.spec_features_done}/{report.spec_features_total}"
            ],
        }

    return health_scanner_node
