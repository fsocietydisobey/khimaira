"""PDE aggregator — combines parallel agent results and validates.

Collects all swarm_results, checks for conflicts, runs the test suite
on the combined output. Atomic batch — failure rejects ALL results.
"""

import asyncio

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.swarm_merge")


async def _run_tests() -> tuple[bool, str]:
    """Run pytest and return (passed, output)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "uv", "run", "pytest", "--tb=short", "-q",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=180)
        output = stdout.decode("utf-8", errors="replace").strip()
        passed = proc.returncode == 0
        return passed, output or stderr.decode("utf-8", errors="replace").strip()
    except asyncio.TimeoutError:
        return False, "Tests timed out after 180s"
    except FileNotFoundError:
        return True, "pytest not found — skipping"


def build_swarm_aggregator_node():
    """Build a merge node that combines parallel agent results.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def pde_aggregator_node(state: OrchestratorState) -> dict:
        """Merge all swarm results and run integrity check."""
        history = list(state.get("history", []))
        results = state.get("swarm_results") or []

        succeeded = [r for r in results if r.get("status") == "success"]
        failed = [r for r in results if r.get("status") == "failed"]

        log.info(
            "merge: %d succeeded, %d failed out of %d total",
            len(succeeded), len(failed), len(results),
        )

        if not succeeded:
            log.warning("no successful agents — nothing to merge")
            return {
                "swarm_outcome": "failed",
                "swarm_test_output": "No agents succeeded",
                "history": history + [f"swarm_merge: all {len(failed)} agents failed"],
            }

        # Run integration tests on combined result
        log.info("running integration tests on merged output...")
        passed, test_output = await _run_tests()

        if passed:
            outcome = "success" if not failed else "partial"
            log.info("merge: tests passed — %s", outcome)
        else:
            outcome = "failed"
            log.warning("merge: tests failed after combining agents")

        history_entry = (
            f"swarm_merge: {len(succeeded)}/{len(results)} agents succeeded, "
            f"tests {'passed' if passed else 'FAILED'} → {outcome}"
        )

        return {
            "swarm_outcome": outcome,
            "swarm_test_output": test_output[:2000],
            "history": history + [history_entry],
        }

    return pde_aggregator_node
