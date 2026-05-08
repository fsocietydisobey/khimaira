"""Fitness function — objective, deterministic health scoring.

Runs pytest, pyright, and a spec parser to produce a HealthReport.
The score is sealed — the running process cannot modify its own evaluation.
All checks run via subprocess (no LLM calls).
"""

import asyncio
import re
from dataclasses import dataclass, field
from pathlib import Path

from chimera.log import get_logger

log = get_logger("fitness")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


async def _run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    """Run a command and return (exit_code, stdout, stderr)."""
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(_PROJECT_ROOT),
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        return (
            proc.returncode or 0,
            stdout.decode("utf-8", errors="replace").strip(),
            stderr.decode("utf-8", errors="replace").strip(),
        )
    except asyncio.TimeoutError:
        proc.kill()
        return (1, "", f"Timed out after {timeout}s")
    except FileNotFoundError:
        return (1, "", f"Command not found: {cmd[0]}")


@dataclass
class HealthReport:
    """Objective health metrics for the codebase."""

    tests_passing: int = 0
    tests_failing: int = 0
    test_output: str = ""
    pyright_errors: int = 0
    lint_warnings: int = 0
    spec_features_done: int = 0
    spec_features_total: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        """Single 0.0-1.0 score. Higher = healthier."""
        total_tests = self.tests_passing + self.tests_failing
        test_ratio = self.tests_passing / max(total_tests, 1)
        type_score = 1.0 if self.pyright_errors == 0 else max(0.0, 1.0 - self.pyright_errors / 50)
        spec_progress = self.spec_features_done / max(self.spec_features_total, 1)
        lint_score = 1.0 if self.lint_warnings == 0 else max(0.0, 1.0 - self.lint_warnings / 100)

        return (
            test_ratio * 0.30
            + type_score * 0.20
            + spec_progress * 0.20
            + lint_score * 0.10
            + (0.20 if total_tests > 0 else 0.0)  # bonus for having tests at all
        )

    def to_dict(self) -> dict:
        """Serialize to dict for state storage."""
        return {
            "tests_passing": self.tests_passing,
            "tests_failing": self.tests_failing,
            "pyright_errors": self.pyright_errors,
            "lint_warnings": self.lint_warnings,
            "spec_features_done": self.spec_features_done,
            "spec_features_total": self.spec_features_total,
            "score": self.score,
        }


def parse_spec(spec_path: Path | None = None) -> tuple[int, int]:
    """Count checked [x] vs total [ ] items in SPEC.md."""
    path = spec_path or (_PROJECT_ROOT / "SPEC.md")
    if not path.exists():
        return 0, 0

    text = path.read_text()
    done = len(re.findall(r"- \[x\]", text, re.IGNORECASE))
    total = done + len(re.findall(r"- \[ \]", text))
    return done, total


async def assess_health() -> HealthReport:
    """Run all health checks and return a HealthReport."""
    report = HealthReport()

    # 1. Run pytest
    log.info("running pytest...")
    code, stdout, stderr = await _run(["uv", "run", "pytest", "--tb=short", "-q"], timeout=180)
    report.test_output = stdout or stderr
    output = stdout + "\n" + stderr

    match = re.search(r"(\d+) passed", output)
    if match:
        report.tests_passing = int(match.group(1))
    match = re.search(r"(\d+) failed", output)
    if match:
        report.tests_failing = int(match.group(1))

    if "no tests ran" in output.lower():
        log.info("pytest: no tests found")
    else:
        log.info("pytest: %d passed, %d failed", report.tests_passing, report.tests_failing)

    # 2. Run pyright
    log.info("running pyright...")
    code, stdout, stderr = await _run(["npx", "pyright", "--outputjson"], timeout=120)
    if stdout:
        try:
            import json
            result = json.loads(stdout)
            diags = result.get("generalDiagnostics", [])
            report.pyright_errors = len([d for d in diags if d.get("severity") == "error"])
        except (json.JSONDecodeError, KeyError):
            report.pyright_errors = stderr.count("error:")
    elif "not found" not in stderr.lower():
        report.pyright_errors = stderr.count("error:")
    log.info("pyright: %d errors", report.pyright_errors)

    # 3. Run ruff check
    log.info("running ruff check...")
    code, stdout, stderr = await _run(["uv", "run", "ruff", "check", "--output-format", "json", "."])
    if stdout:
        try:
            import json
            issues = json.loads(stdout)
            report.lint_warnings = len(issues)
        except json.JSONDecodeError:
            pass
    log.info("ruff: %d warnings", report.lint_warnings)

    # 4. Parse SPEC.md
    report.spec_features_done, report.spec_features_total = parse_spec()
    log.info("spec: %d/%d features done", report.spec_features_done, report.spec_features_total)

    log.info("health score: %.2f", report.score)
    return report
