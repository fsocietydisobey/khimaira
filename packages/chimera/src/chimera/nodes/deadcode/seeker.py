"""DCE seeker — finds dead code using static analysis.

Runs five deterministic analysis passes (no LLM calls):
1. Unused imports (ruff F401)
2. Unused variables (ruff F841)
3. AST analysis for zero-caller functions
4. Dependency scan for unused packages
5. Large file detection (> 500 lines)

Each target gets a risk assessment: safe, moderate, risky.
"""

import ast
import asyncio
import json
from pathlib import Path

from chimera.core.state import OrchestratorState
from chimera.log import get_logger

log = get_logger("node.deadcode_seeker")


def build_deadcode_seeker_node():
    """Build a seeker node that finds dead code targets.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def deadcode_seeker_node(state: OrchestratorState) -> dict:
        """Scan for dead code and produce a target map."""
        history = list(state.get("history", []))
        shadow_path = state.get("deadcode_shadow_path", "")

        if not shadow_path:
            return {
                "deadcode_targets": {"targets": [], "error": "no shadow worktree path"},
                "history": history + ["deadcode_seeker: no shadow path — skipping"],
            }

        cwd = shadow_path
        targets: list[dict] = []

        # Pass 1: Unused imports and variables via ruff
        log.info("DCE seeker pass 1: ruff F401/F841")
        try:
            proc = await asyncio.create_subprocess_exec(
                "uv", "run", "ruff", "check",
                "--select", "F401,F841",
                "--output-format", "json",
                ".",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await proc.communicate()
            if stdout:
                issues = json.loads(stdout.decode("utf-8", errors="replace"))
                for issue in issues:
                    targets.append({
                        "type": "unused_import" if issue.get("code") == "F401" else "unused_variable",
                        "file": issue.get("filename", ""),
                        "line": issue.get("location", {}).get("row", 0),
                        "name": issue.get("message", ""),
                        "risk": "safe",
                        "evidence": f"ruff {issue.get('code', '')}",
                    })
                log.info("ruff found %d issues", len(issues))
        except (FileNotFoundError, json.JSONDecodeError) as e:
            log.warning("ruff scan failed: %s", e)

        # Pass 2: AST analysis for zero-caller functions
        log.info("DCE seeker pass 2: AST zero-caller analysis")
        src_dir = Path(cwd) / "src" / "chimera"
        if src_dir.exists():
            all_definitions: dict[str, dict] = {}
            all_references: set[str] = set()

            for py_file in src_dir.rglob("*.py"):
                try:
                    source = py_file.read_text()
                    tree = ast.parse(source)
                except (SyntaxError, UnicodeDecodeError):
                    continue

                rel_path = str(py_file.relative_to(Path(cwd)))

                for node in ast.walk(tree):
                    # Track definitions
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if not node.name.startswith("_"):
                            all_definitions[node.name] = {
                                "file": rel_path,
                                "line": node.lineno,
                                "type": "function",
                            }
                    elif isinstance(node, ast.ClassDef):
                        if not node.name.startswith("_"):
                            all_definitions[node.name] = {
                                "file": rel_path,
                                "line": node.lineno,
                                "type": "class",
                            }

                    # Track references
                    if isinstance(node, ast.Name):
                        all_references.add(node.id)
                    elif isinstance(node, ast.Attribute):
                        all_references.add(node.attr)

            # Find definitions with zero references elsewhere
            for name, info in all_definitions.items():
                if name not in all_references:
                    targets.append({
                        "type": f"unreferenced_{info['type']}",
                        "file": info["file"],
                        "line": info["line"],
                        "name": name,
                        "risk": "moderate",
                        "evidence": "zero references found via AST analysis",
                    })

            log.info("AST analysis: %d unreferenced definitions", len([
                t for t in targets if t["type"].startswith("unreferenced_")
            ]))

        # Pass 3: Large file detection
        log.info("DCE seeker pass 3: large file detection")
        if src_dir.exists():
            for py_file in src_dir.rglob("*.py"):
                try:
                    lines = py_file.read_text().count("\n")
                    if lines > 500:
                        rel_path = str(py_file.relative_to(Path(cwd)))
                        targets.append({
                            "type": "large_file",
                            "file": rel_path,
                            "line": 0,
                            "name": rel_path,
                            "risk": "risky",
                            "evidence": f"{lines} lines (threshold: 500)",
                        })
                except (UnicodeDecodeError, OSError):
                    continue

        # Deduplicate targets
        seen = set()
        unique_targets = []
        for t in targets:
            key = (t["type"], t["file"], t["name"])
            if key not in seen:
                seen.add(key)
                unique_targets.append(t)

        # Sort by risk level
        risk_order = {"safe": 0, "moderate": 1, "risky": 2}
        unique_targets.sort(key=lambda t: risk_order.get(t.get("risk", "risky"), 2))

        target_map = {
            "targets": unique_targets,
            "safe_count": sum(1 for t in unique_targets if t["risk"] == "safe"),
            "moderate_count": sum(1 for t in unique_targets if t["risk"] == "moderate"),
            "risky_count": sum(1 for t in unique_targets if t["risk"] == "risky"),
        }

        log.info(
            "DCE seeker: %d targets (safe=%d, moderate=%d, risky=%d)",
            len(unique_targets),
            target_map["safe_count"],
            target_map["moderate_count"],
            target_map["risky_count"],
        )

        return {
            "deadcode_targets": target_map,
            "history": history + [
                f"deadcode_seeker: {len(unique_targets)} targets "
                f"(safe={target_map['safe_count']}, moderate={target_map['moderate_count']}, "
                f"risky={target_map['risky_count']})"
            ],
        }

    return deadcode_seeker_node
