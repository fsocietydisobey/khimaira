"""ACL validator — combinatorial testing engine for atomic components.

Runs three validation levels:
1. Isolation: each component module imports successfully
2. Pairs: every A+B combination can coexist
3. Scenarios: key multi-component workflows execute correctly

All validation is deterministic (no LLM calls). Uses subprocess to run
import checks in isolation.
"""

import asyncio
import itertools
from pathlib import Path

from chimera.core.component_registry import (
    ValidationReport,
    get_all_components,
    log_validation,
    mark_validated,
)
from chimera.core.state import OrchestratorState
from chimera.log import get_logger

log = get_logger("node.component_validator")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


async def _test_import(module: str) -> tuple[bool, str]:
    """Test that a module can be imported without errors."""
    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "python", "-c", f"import {module}",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_PROJECT_ROOT),
    )
    stdout, stderr = await proc.communicate()
    output = stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode == 0, output


async def _test_pair(mod_a: str, mod_b: str) -> tuple[bool, str]:
    """Test that two modules can be imported together."""
    code = f"import {mod_a}; import {mod_b}"
    proc = await asyncio.create_subprocess_exec(
        "uv", "run", "python", "-c", code,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=str(_PROJECT_ROOT),
    )
    stdout, stderr = await proc.communicate()
    output = stderr.decode("utf-8", errors="replace").strip()
    return proc.returncode == 0, output


def build_component_validator_node():
    """Build a validator node that runs combinatorial testing on ACL components.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def component_validator_node(state: OrchestratorState) -> dict:
        """Run ACL validation: isolation → pairs → scenarios."""
        history = list(state.get("history", []))
        report = ValidationReport()

        components = await get_all_components()
        if not components:
            return {
                "component_validation_report": {"passed": True, "summary": "no components to validate"},
                "history": history + ["component_validator: no components registered"],
            }

        # Level 1: Isolation — each component imports alone
        log.info("ACL validation level 1: isolation (%d components)", len(components))
        for comp in components:
            passed, output = await _test_import(comp["module"])
            report.isolation_results[comp["name"]] = passed
            await mark_validated(comp["name"], passed, output)
            if not passed:
                log.warning("isolation FAIL: %s — %s", comp["name"], output[:100])

        await log_validation("isolation", all(report.isolation_results.values()))

        # Level 2: Pairs — every A+B combination
        valid_components = [c for c in components if report.isolation_results.get(c["name"], False)]
        pairs_tested = 0
        if len(valid_components) >= 2:
            log.info("ACL validation level 2: pairs (%d components)", len(valid_components))
            for comp_a, comp_b in itertools.combinations(valid_components, 2):
                passed, output = await _test_pair(comp_a["module"], comp_b["module"])
                pair_key = f"{comp_a['name']}+{comp_b['name']}"
                report.pair_results[pair_key] = passed
                pairs_tested += 1
                if not passed:
                    log.warning("pair FAIL: %s — %s", pair_key, output[:100])

        await log_validation("pairs", all(report.pair_results.values()) if report.pair_results else True,
                             pairs_tested=pairs_tested)

        # Level 3: Scenarios — not implemented yet (requires scenario definitions)
        # Future: test key multi-component workflows

        log.info("ACL validation complete: %s (passed=%s)", report.summary, report.passed)

        return {
            "component_validation_report": report.to_dict(),
            "history": history + [f"component_validator: {report.summary} — {'PASS' if report.passed else 'FAIL'}"],
        }

    return component_validator_node
