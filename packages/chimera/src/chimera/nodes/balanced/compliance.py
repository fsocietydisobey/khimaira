"""Compliance — deterministic formatting, linting, and documentation.

Pure restriction. No creative decisions. Runs ruff format, ruff check --fix,
and reports what was changed. This is the "enforce the repo's laws" node.

Deterministic tools run via subprocess (zero LLM cost for formatting/linting).
"""

import asyncio
import json

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.compliance")


async def _run_tool(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr)."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
    )
    stdout, stderr = await proc.communicate()
    return (
        proc.returncode or 0,
        stdout.decode("utf-8", errors="replace").strip(),
        stderr.decode("utf-8", errors="replace").strip(),
    )


def build_compliance_node():
    """Build a deterministic compliance enforcement node.

    Runs ruff format and ruff check --fix on the project. No LLM calls —
    pure deterministic tooling.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def compliance_node(state: OrchestratorState) -> dict:
        """Run formatting and linting tools, report changes."""
        history = list(state.get("history", []))
        report: dict = {
            "files_formatted": [],
            "lint_fixes": [],
            "errors": [],
        }

        # Run ruff format
        try:
            code, stdout, stderr = await _run_tool(
                ["uv", "run", "ruff", "format", "--check", "--diff", "."]
            )
            if code != 0:
                # Files need formatting — run the actual format
                code2, stdout2, stderr2 = await _run_tool(
                    ["uv", "run", "ruff", "format", "."]
                )
                if code2 == 0:
                    # Count formatted files from the diff output
                    formatted = [
                        line.split(" ")[-1]
                        for line in stdout.split("\n")
                        if line.startswith("Would reformat:")
                    ] if stdout else ["(files formatted)"]
                    report["files_formatted"] = formatted
                    log.info("formatted %d files", len(formatted))
                else:
                    report["errors"].append(f"ruff format failed: {stderr2}")
                    log.warning("ruff format failed: %s", stderr2)
            else:
                log.info("no formatting needed")
        except FileNotFoundError:
            report["errors"].append("ruff not found — skipping format")
            log.warning("ruff not found, skipping format")

        # Run ruff check --fix
        try:
            code, stdout, stderr = await _run_tool(
                ["uv", "run", "ruff", "check", "--fix", "--output-format", "json", "."]
            )
            if stdout:
                try:
                    lint_results = json.loads(stdout)
                    if lint_results:
                        fixes = [
                            f"{r.get('filename', '?')}:{r.get('location', {}).get('row', '?')} — {r.get('code', '?')}: {r.get('message', '?')}"
                            for r in lint_results[:20]  # Cap output
                        ]
                        report["lint_fixes"] = fixes
                        log.info("found %d lint issues (%d auto-fixed)", len(lint_results), len(fixes))
                except json.JSONDecodeError:
                    pass
            if code == 0:
                log.info("lint check passed")
            else:
                # Some issues may not be auto-fixable
                log.info("lint check found issues (some may not be auto-fixable)")
        except FileNotFoundError:
            report["errors"].append("ruff not found — skipping lint")
            log.warning("ruff not found, skipping lint")

        # Build history entry
        formatted_count = len(report["files_formatted"])
        lint_count = len(report["lint_fixes"])
        error_count = len(report["errors"])

        history_entry = f"hod: {formatted_count} files formatted, {lint_count} lint fixes"
        if error_count:
            history_entry += f", {error_count} tool errors"

        return {
            "compliance_report": report,
            "history": history + [history_entry],
        }

    return compliance_node
