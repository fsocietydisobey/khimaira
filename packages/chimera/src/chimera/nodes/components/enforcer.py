"""ACL enforcer — checks code changes for ACL compliance.

Scans changed files for raw imports that should use ACL components.
Used as a post-implementation gate (like TFB's integration_gate).
"""

import ast
from pathlib import Path

from chimera.core.component_registry import BANNED_RAW_IMPORTS, get_manifest
from chimera.core.state import OrchestratorState
from chimera.log import get_logger
from chimera.tools.git_tools import git_diff_files

log = get_logger("node.component_enforcer")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


def build_component_enforcer_node():
    """Build an enforcer node that checks code changes for ACL compliance.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def component_enforcer_node(state: OrchestratorState) -> dict:
        """Check recently changed files for ACL violations."""
        history = list(state.get("history", []))

        changed_files = await git_diff_files()
        py_files = [f for f in changed_files if f.endswith(".py")]

        if not py_files:
            return {
                "component_enforcement_result": {"passed": True, "violations": [], "files_checked": 0},
                "history": history + ["component_enforcer: no Python files changed"],
            }

        violations: list[dict[str, str | int]] = []
        for rel_path in py_files:
            full_path = _PROJECT_ROOT / rel_path
            if not full_path.exists():
                continue

            # Skip ACL code itself
            if "acl_registry" in rel_path or "nodes/acl" in rel_path:
                continue

            try:
                source = full_path.read_text()
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                continue

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name in BANNED_RAW_IMPORTS:
                            violations.append({
                                "file": rel_path,
                                "line": node.lineno,
                                "import": alias.name,
                                "should_use": BANNED_RAW_IMPORTS[alias.name],
                            })
                elif isinstance(node, ast.ImportFrom):
                    if node.module and node.module.split(".")[0] in BANNED_RAW_IMPORTS:
                        violations.append({
                            "file": rel_path,
                            "line": node.lineno,
                            "import": node.module,
                            "should_use": BANNED_RAW_IMPORTS[node.module.split(".")[0]],
                        })

        passed = len(violations) == 0
        result = {
            "passed": passed,
            "violations": violations,
            "files_checked": len(py_files),
            "manifest": get_manifest() if not passed else "",
        }

        if violations:
            log.warning("ACL enforcement: %d violations in %d files", len(violations), len(py_files))
        else:
            log.info("ACL enforcement: clean (%d files checked)", len(py_files))

        return {
            "component_enforcement_result": result,
            "history": history + [
                f"component_enforcer: {'PASS' if passed else f'FAIL — {len(violations)} violations'} "
                f"({len(py_files)} files)"
            ],
        }

    return component_enforcer_node
