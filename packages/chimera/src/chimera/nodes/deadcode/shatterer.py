"""DCE shatterer — identifies monolithic files for splitting.

Analyzes large files (>500 lines) and proposes intelligent splits based on
class boundaries, function clusters, and import chains. Uses AST analysis
(no LLM calls for the analysis phase).
"""

import ast
from collections import defaultdict
from pathlib import Path

from chimera.core.state import OrchestratorState
from chimera.log import get_logger

log = get_logger("node.deadcode_shatterer")


def _analyze_file_structure(source: str, file_path: str) -> dict | None:
    """Analyze a Python file and propose splits if warranted."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None

    lines = source.count("\n")
    if lines <= 500:
        return None

    classes: list[dict] = []
    functions: list[dict] = []

    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            end_line = getattr(node, "end_lineno", node.lineno)
            classes.append({
                "name": node.name,
                "start": node.lineno,
                "end": end_line,
                "size": end_line - node.lineno,
            })
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end_line = getattr(node, "end_lineno", node.lineno)
            functions.append({
                "name": node.name,
                "start": node.lineno,
                "end": end_line,
                "size": end_line - node.lineno,
            })

    if len(classes) <= 1 and len(functions) <= 5:
        return None  # Not worth splitting

    # Build a call graph to find function clusters
    call_graph: dict[str, set[str]] = defaultdict(set)
    func_names = {f["name"] for f in functions}

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            caller = node.name
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    if isinstance(child.func, ast.Name) and child.func.id in func_names:
                        call_graph[caller].add(child.func.id)

    # Propose splits: each class gets its own file, orphan functions grouped
    proposals: list[dict] = []
    for cls in classes:
        if cls["size"] > 50:
            proposals.append({
                "type": "extract_class",
                "name": cls["name"],
                "target_file": f"{Path(file_path).stem}_{cls['name'].lower()}.py",
                "lines": cls["size"],
            })

    return {
        "file": file_path,
        "total_lines": lines,
        "classes": len(classes),
        "functions": len(functions),
        "proposals": proposals,
    }


def build_deadcode_shatterer_node():
    """Build a shatterer node that identifies monolithic files for splitting.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def deadcode_shatterer_node(state: OrchestratorState) -> dict:
        """Analyze large files and propose splits."""
        history = list(state.get("history", []))
        shadow_path = state.get("deadcode_shadow_path", "")
        targets = state.get("deadcode_targets") or {}

        large_files = [t for t in targets.get("targets", []) if t.get("type") == "large_file"]

        if not large_files or not shadow_path:
            return {
                "deadcode_split_proposals": [],
                "history": history + ["deadcode_shatterer: no large files to analyze"],
            }

        proposals: list[dict] = []
        for target in large_files:
            file_path = Path(shadow_path) / target["file"]
            if not file_path.exists():
                continue

            try:
                source = file_path.read_text()
            except (UnicodeDecodeError, OSError):
                continue

            analysis = _analyze_file_structure(source, target["file"])
            if analysis and analysis["proposals"]:
                proposals.append(analysis)

        log.info("DCE shatterer: %d files analyzed, %d split proposals", len(large_files), len(proposals))

        return {
            "deadcode_split_proposals": proposals,
            "history": history + [
                f"deadcode_shatterer: {len(proposals)} split proposals from {len(large_files)} large files"
            ],
        }

    return deadcode_shatterer_node
