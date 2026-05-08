"""Directive enforcement — immutable policy rules (Special Order 937).

Checks git diffs against DIRECTIVES.md rules. Violations trigger revert.
The directive file itself is not modifiable by agents.

Two types of checks:
    - Deterministic: regex patterns for known dangerous patterns
    - Content-based: simple keyword matching (no LLM for speed)
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from chimera.log import get_logger

log = get_logger("directives")

_PROJECT_ROOT = Path(__file__).parent.parent.parent.parent.parent


@dataclass
class Violation:
    """A single directive violation."""

    directive: str
    description: str
    severity: str  # "critical" | "warning"
    file: str = ""


@dataclass
class DirectiveResult:
    """Result of checking directives against a diff."""

    passed: bool
    violations: list[Violation] = field(default_factory=list)


# Built-in deterministic checks (always enforced even without DIRECTIVES.md)
_BUILTIN_PATTERNS: list[tuple[str, str, str]] = [
    (r"\beval\s*\(", "eval() usage detected — potential code injection", "critical"),
    (r"\bexec\s*\(", "exec() usage detected — potential code injection", "critical"),
    (r"(password|secret|api_key|token)\s*=\s*['\"][^'\"]+['\"]", "Hardcoded secret detected", "critical"),
    (r"\.env\b", "Possible .env file modification", "warning"),
    (r"subprocess\.(?!DEVNULL)", "Subprocess without DEVNULL stdin", "warning"),
]


def load_directives(path: Path | None = None) -> list[str]:
    """Load directives from DIRECTIVES.md."""
    directives_path = path or (_PROJECT_ROOT / "DIRECTIVES.md")
    if not directives_path.exists():
        return []
    text = directives_path.read_text()
    # Extract bullet points as directives
    directives = re.findall(r"^- (.+)$", text, re.MULTILINE)
    return directives


def check_directives(diff: str, directives_path: Path | None = None) -> DirectiveResult:
    """Check a git diff against all directives.

    Args:
        diff: The git diff text (output of `git diff`).
        directives_path: Optional path to DIRECTIVES.md.

    Returns:
        DirectiveResult with pass/fail and any violations.
    """
    violations: list[Violation] = []

    # 1. Run built-in pattern checks on added lines
    added_lines = [
        line[1:] for line in diff.split("\n")
        if line.startswith("+") and not line.startswith("+++")
    ]
    added_text = "\n".join(added_lines)

    for pattern, description, severity in _BUILTIN_PATTERNS:
        matches = re.findall(pattern, added_text)
        if matches:
            violations.append(Violation(
                directive=f"Built-in: {description}",
                description=f"Found {len(matches)} match(es): {pattern}",
                severity=severity,
            ))

    # 2. Check for DIRECTIVES.md modification (meta-directive)
    if "DIRECTIVES.md" in diff:
        violations.append(Violation(
            directive="Meta: DIRECTIVES.md is immutable",
            description="Agent attempted to modify the directive file itself",
            severity="critical",
            file="DIRECTIVES.md",
        ))

    # 3. Check for fitness.py modification (sealed evaluation)
    if "core/fitness.py" in diff:
        violations.append(Violation(
            directive="Meta: fitness.py is sealed",
            description="Agent attempted to modify the fitness evaluation function",
            severity="critical",
            file="core/fitness.py",
        ))

    # 4. Load custom directives and do keyword matching
    custom_directives = load_directives(directives_path)
    for directive in custom_directives:
        # Simple keyword extraction — check if the diff contains forbidden patterns
        lower_directive = directive.lower()
        if "never" in lower_directive or "do not" in lower_directive:
            # Extract the key action/target from the directive
            # This is a simple heuristic — a full implementation would use an LLM
            if "unauthenticated" in lower_directive and "endpoint" in lower_directive:
                if re.search(r"@app\.(get|post|put|delete)", added_text) and "auth" not in added_text.lower():
                    violations.append(Violation(
                        directive=directive,
                        description="New endpoint may lack authentication",
                        severity="warning",
                    ))

    has_critical = any(v.severity == "critical" for v in violations)
    passed = not has_critical

    if violations:
        log.warning("directive check: %d violations (%s)", len(violations), "FAILED" if not passed else "warnings only")
    else:
        log.info("directive check: clean")

    return DirectiveResult(passed=passed, violations=violations)
