"""Invariant extraction — heuristic scan for things worth documenting.

Walks a feature folder and surfaces:
  - "Don't do X" comments (DON'T, DO NOT, NEVER, WARNING, FIXME)
  - Comments explaining magic numbers ("600ms because...")
  - useEffect hooks with explanatory comments
  - Functions with explicit "intentional" or "deliberate" callouts
  - TODO/HACK markers

These are SUGGESTIONS for the AI/human to consider documenting in the
feature's CLAUDE.md gotchas section. Scarlet doesn't decide what's
important — it surfaces candidates.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

# Patterns that signal "this is intentional, don't change it"
WARNING_PATTERNS = [
    re.compile(r"//\s*(DON'?T|DO\s+NOT|NEVER|WARNING|HACK|FIXME)[^\n]*", re.IGNORECASE),
    re.compile(r"#\s*(DON'?T|DO\s+NOT|NEVER|WARNING|HACK|FIXME)[^\n]*", re.IGNORECASE),
    re.compile(r"/\*\s*(DON'?T|DO\s+NOT|NEVER|WARNING|HACK|FIXME)[^*]*\*/", re.IGNORECASE | re.DOTALL),
]

# Patterns that signal "this is intentional"
INTENTIONAL_PATTERNS = [
    re.compile(r"//[^\n]*\b(intentional|deliberate|on purpose|by design)\b[^\n]*", re.IGNORECASE),
    re.compile(r"#[^\n]*\b(intentional|deliberate|on purpose|by design)\b[^\n]*", re.IGNORECASE),
]

# Magic number patterns: a numeric literal followed by a comment
MAGIC_NUMBER_PATTERN = re.compile(
    r"(\d+(?:ms|s|px|em|rem)?)\s*[,;)\s]\s*//\s*([^\n]+)", re.IGNORECASE
)

# TODO markers
TODO_PATTERN = re.compile(r"//\s*TODO[:\s][^\n]*|#\s*TODO[:\s][^\n]*", re.IGNORECASE)


@dataclass(frozen=True)
class Invariant:
    """A single invariant candidate found in source code."""

    file: str
    line: int
    kind: str  # warning, intentional, magic_number, todo
    text: str

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "kind": self.kind,
            "text": self.text,
        }


@dataclass(frozen=True)
class InvariantReport:
    """All invariant candidates found in a feature."""

    feature: str
    warnings: list[Invariant] = field(default_factory=list)
    intentional: list[Invariant] = field(default_factory=list)
    magic_numbers: list[Invariant] = field(default_factory=list)
    todos: list[Invariant] = field(default_factory=list)

    @property
    def total_count(self) -> int:
        return (
            len(self.warnings)
            + len(self.intentional)
            + len(self.magic_numbers)
            + len(self.todos)
        )

    def to_dict(self) -> dict:
        return {
            "feature": self.feature,
            "total_count": self.total_count,
            "warnings": [i.to_dict() for i in self.warnings],
            "intentional": [i.to_dict() for i in self.intentional],
            "magic_numbers": [i.to_dict() for i in self.magic_numbers],
            "todos": [i.to_dict() for i in self.todos],
        }


def extract_invariants(feature_path: Path) -> InvariantReport:
    """Scan a feature folder for invariant candidates.

    These are candidates only — Scarlet does not decide what's important.
    The AI/human reviews them and decides which deserve a place in the
    feature's CLAUDE.md gotchas section.

    Args:
        feature_path: Absolute path to the feature directory.

    Returns:
        InvariantReport with all candidates categorized.
    """
    feature_path = feature_path.resolve()

    warnings: list[Invariant] = []
    intentional: list[Invariant] = []
    magic_numbers: list[Invariant] = []
    todos: list[Invariant] = []

    for source_file in _discover_source_files(feature_path):
        try:
            text = source_file.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        rel_path = str(source_file.relative_to(feature_path))

        for line_num, line in enumerate(text.splitlines(), start=1):
            # Warning patterns
            for pattern in WARNING_PATTERNS:
                if pattern.search(line):
                    warnings.append(
                        Invariant(file=rel_path, line=line_num, kind="warning", text=line.strip())
                    )
                    break

            # Intentional patterns
            for pattern in INTENTIONAL_PATTERNS:
                if pattern.search(line):
                    intentional.append(
                        Invariant(
                            file=rel_path, line=line_num, kind="intentional", text=line.strip()
                        )
                    )
                    break

            # Magic number patterns
            if MAGIC_NUMBER_PATTERN.search(line):
                magic_numbers.append(
                    Invariant(
                        file=rel_path, line=line_num, kind="magic_number", text=line.strip()
                    )
                )

            # TODO patterns
            if TODO_PATTERN.search(line):
                todos.append(Invariant(file=rel_path, line=line_num, kind="todo", text=line.strip()))

    return InvariantReport(
        feature=feature_path.name,
        warnings=warnings,
        intentional=intentional,
        magic_numbers=magic_numbers,
        todos=todos,
    )


def _discover_source_files(feature_path: Path) -> list[Path]:
    """Walk a feature folder and yield source files for invariant scanning."""
    files: list[Path] = []
    for path in feature_path.rglob("*"):
        if not path.is_file():
            continue
        if path.suffix not in {".ts", ".tsx", ".js", ".jsx", ".mjs", ".py"}:
            continue
        if any(part.startswith(".") or part == "node_modules" for part in path.parts):
            continue
        files.append(path)
    return sorted(files)
