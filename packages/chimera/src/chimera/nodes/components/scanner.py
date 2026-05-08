"""ACL scanner — discovers components from the existing codebase.

Scans Python files for common patterns (database connections, HTTP calls,
subprocess invocations) and maps them to ACL components. Identifies which
files use raw implementations that should be replaced.
"""

import ast
from pathlib import Path

from chimera.core.component_registry import (
    BANNED_RAW_IMPORTS,
    get_all_components,
    init_builtin_components,
)
from chimera.core.state import OrchestratorState
from chimera.log import get_logger

log = get_logger("node.component_scanner")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


def build_component_scanner_node():
    """Build a scanner node that discovers ACL component usage and violations.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def component_scanner_node(state: OrchestratorState) -> dict:
        """Scan the codebase for ACL component usage and violations."""
        history = list(state.get("history", []))

        # Initialize built-in components if needed
        added = await init_builtin_components()
        if added:
            log.info("seeded %d built-in ACL components", added)

        components = await get_all_components()
        log.info("ACL registry has %d components", len(components))

        # Scan source files for violations
        src_dir = _PROJECT_ROOT / "src" / "chimera"
        violations: list[dict[str, str]] = []
        component_usage: dict[str, int] = {}

        for py_file in src_dir.rglob("*.py"):
            rel_path = str(py_file.relative_to(_PROJECT_ROOT))
            # Skip the ACL code itself
            if "acl_registry" in rel_path or "nodes/acl" in rel_path:
                continue

            try:
                source = py_file.read_text()
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in BANNED_RAW_IMPORTS:
                            component_name = BANNED_RAW_IMPORTS[alias.name]
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "import": alias.name,
                                "should_use": component_name,
                                "severity": "warning",
                            })
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[0] in BANNED_RAW_IMPORTS:
                        component_name = BANNED_RAW_IMPORTS[node.module.split(".")[0]]
                        violations.append({
                            "file": rel_path,
                            "line": node.lineno,
                            "import": node.module,
                            "should_use": component_name,
                            "severity": "warning",
                        })

            # Track component usage
            for comp in components:
                module = comp["module"]
                if module in source:
                    component_usage[comp["name"]] = component_usage.get(comp["name"], 0) + 1

        scan_result = {
            "components_registered": len(components),
            "violations_found": len(violations),
            "violations": violations[:20],  # Cap at 20 for state size
            "component_usage": component_usage,
        }

        log.info(
            "ACL scan: %d components, %d violations, %d active usages",
            len(components), len(violations), sum(component_usage.values()),
        )

        return {
            "component_scan_result": scan_result,
            "history": history + [
                f"component_scanner: {len(components)} components, "
                f"{len(violations)} violations found"
            ],
        }

    return component_scanner_node
