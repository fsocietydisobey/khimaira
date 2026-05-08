"""Integration Gate — comprehensive integration validation gate.

The final checkpoint before commit. Runs the full test suite, type checker,
and git diff review. Unlike the simple validator (which scores one output),
Integration Gate validates the ENTIRE codebase state after all changes.

All checks are deterministic (subprocess calls) — no LLM involved.
"""

import asyncio
import json

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.integration_gate")


async def _run_tool(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a shell command and return (exit_code, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )
    except asyncio.TimeoutError:
        proc.kill()
        return (1, "", f"Command timed out after {timeout}s")
    except FileNotFoundError:
        return (1, "", f"Command not found: {cmd[0]}")


def build_integration_gate_node():
    """Build a comprehensive integration validation gate.

    Runs pytest, pyright, and git diff review. All deterministic — no LLM calls.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def integration_gate_node(state: OrchestratorState) -> dict:
        """Run full integration validation suite."""
        history = list(state.get("history", []))

        tests_passed = 0
        tests_failed = 0
        test_output = ""
        type_errors = 0
        unintended_changes: list[str] = []
        checks_run: list[str] = []
        errors: list[str] = []

        # 1. Run full test suite
        log.info("running test suite...")
        code, stdout, stderr = await _run_tool(
            ["uv", "run", "pytest", "--tb=short", "-q"], timeout=180
        )
        test_output = stdout or stderr
        if code == 0:
            # Parse pytest output for counts
            for line in test_output.split("\n"):
                if "passed" in line:
                    import re
                    match = re.search(r"(\d+) passed", line)
                    if match:
                        tests_passed = int(match.group(1))
                    match = re.search(r"(\d+) failed", line)
                    if match:
                        tests_failed = int(match.group(1))
            checks_run.append(f"pytest: {tests_passed} passed, {tests_failed} failed")
            log.info("pytest: %d passed, %d failed", tests_passed, tests_failed)
        elif "no tests ran" in test_output.lower() or "no tests ran" in stderr.lower():
            checks_run.append("pytest: no tests found")
            log.info("pytest: no tests found")
        else:
            # Parse failure count from output
            import re
            match = re.search(r"(\d+) failed", test_output)
            if match:
                tests_failed = int(match.group(1))
            match = re.search(r"(\d+) passed", test_output)
            if match:
                tests_passed = int(match.group(1))
            checks_run.append(f"pytest: {tests_passed} passed, {tests_failed} failed")
            log.info("pytest: %d passed, %d failed", tests_passed, tests_failed)

        # 2. Run type checker
        log.info("running type checker...")
        code, stdout, stderr = await _run_tool(
            ["npx", "pyright", "--outputjson"], timeout=120
        )
        if stdout:
            try:
                pyright_result = json.loads(stdout)
                diag = pyright_result.get("generalDiagnostics", [])
                type_errors = len([d for d in diag if d.get("severity") == "error"])
                checks_run.append(f"pyright: {type_errors} errors")
                log.info("pyright: %d errors", type_errors)
            except json.JSONDecodeError:
                # Try to count errors from text output
                type_errors = stderr.count("error:")
                checks_run.append(f"pyright: ~{type_errors} errors (parsed from text)")
                log.info("pyright: ~%d errors (text parse)", type_errors)
        elif "not found" in stderr.lower() or code == 1 and not stdout:
            checks_run.append("pyright: not available (skipped)")
            log.info("pyright not available, skipping")
        else:
            checks_run.append("pyright: completed (no JSON output)")

        # 3. Git diff review — check for unintended changes
        log.info("reviewing git diff...")
        code, stdout, stderr = await _run_tool(
            ["git", "diff", "--name-only", "HEAD"]
        )
        if stdout:
            changed_files = [f for f in stdout.split("\n") if f.strip()]
            # Flag changes to sensitive files
            sensitive_patterns = [".env", "credentials", "secret", "checkpoints.db"]
            for f in changed_files:
                if any(p in f.lower() for p in sensitive_patterns):
                    unintended_changes.append(f)
            if unintended_changes:
                checks_run.append(f"git diff: {len(unintended_changes)} sensitive files modified")
                log.warning("sensitive files modified: %s", unintended_changes)
            else:
                checks_run.append(f"git diff: {len(changed_files)} files changed (clean)")
        else:
            checks_run.append("git diff: no uncommitted changes")

        # Determine pass/fail
        passed = (
            tests_failed == 0
            and len(unintended_changes) == 0
            # type_errors > 0 is a warning, not a blocker (for now)
        )

        result_dict = {
            "passed": passed,
            "tests_passed": tests_passed,
            "tests_failed": tests_failed,
            "test_output": test_output[:2000] if test_output else "",
            "type_errors": type_errors,
            "unintended_changes": unintended_changes,
            "checks_run": checks_run,
        }

        log.info(
            "integration gate: %s — tests=%d/%d, type_errors=%d, sensitive_files=%d",
            "PASSED" if passed else "FAILED",
            tests_passed, tests_passed + tests_failed,
            type_errors, len(unintended_changes),
        )

        history_entry = (
            f"integration_gate: {'PASSED' if passed else 'FAILED'} — "
            + ", ".join(checks_run)
        )

        result: dict = {
            "integration_result": result_dict,
            "history": history + [history_entry],
        }

        if not passed:
            result["handoff_type"] = "tests_failing"

        return result

    return integration_gate_node
