"""Detect LangGraph projects from chimera's roots registry.

A project counts as "langgraph" if either:
  - its pyproject.toml lists `langgraph` as a dependency, or
  - any .py file under it imports `langgraph` and references `StateGraph`.

The pyproject path is fast and authoritative when present; the source
scan is a fallback for projects that pin via requirements.txt or vendor
the dep elsewhere.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

# Cap the source scan so a giant repo doesn't turn discovery into a tar pit.
_SCAN_FILE_CAP = 2000
# Skip these dirs entirely — they almost never hold first-party graphs.
_SKIP_DIRS = frozenset({
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    "dist",
    "build",
    ".mypy_cache",
    ".ruff_cache",
    ".pytest_cache",
    ".next",
    "site-packages",
})


@dataclass(frozen=True)
class Project:
    """A discovered LangGraph project."""

    name: str
    path: Path
    has_pyproject: bool
    detected_via: str  # "pyproject" or "source-scan"


def discover(roots: list[str]) -> list[Project]:
    """Return all LangGraph projects in `roots`, deduped by absolute path."""
    seen: set[Path] = set()
    out: list[Project] = []
    for root in roots:
        path = Path(root).resolve()
        if path in seen:
            continue
        seen.add(path)
        project = _classify(path)
        if project is not None:
            out.append(project)
    return out


def _classify(root: Path) -> Project | None:
    """Return a Project if `root` looks like a langgraph project, else None."""
    pyproject = root / "pyproject.toml"
    if pyproject.is_file() and _pyproject_uses_langgraph(pyproject):
        return Project(
            name=root.name,
            path=root,
            has_pyproject=True,
            detected_via="pyproject",
        )
    if _source_uses_langgraph(root):
        return Project(
            name=root.name,
            path=root,
            has_pyproject=pyproject.is_file(),
            detected_via="source-scan",
        )
    return None


def _pyproject_uses_langgraph(pyproject: Path) -> bool:
    try:
        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False

    project = data.get("project", {})
    deps: list[str] = []
    deps.extend(project.get("dependencies", []) or [])
    optional = project.get("optional-dependencies", {}) or {}
    for group in optional.values():
        deps.extend(group)
    # Poetry layout
    poetry = data.get("tool", {}).get("poetry", {})
    poetry_deps = poetry.get("dependencies", {}) or {}
    deps.extend(poetry_deps.keys())
    poetry_dev = poetry.get("dev-dependencies", {}) or {}
    deps.extend(poetry_dev.keys())

    for dep in deps:
        head = re.split(r"[<>=!~\s\[]", dep, maxsplit=1)[0].strip().lower()
        if head == "langgraph" or head.startswith("langgraph-"):
            return True
    return False


_IMPORT_RE = re.compile(r"^\s*(?:from\s+langgraph|import\s+langgraph)", re.MULTILINE)
_STATEGRAPH_RE = re.compile(r"\bStateGraph\b")


def _source_uses_langgraph(root: Path) -> bool:
    if not root.is_dir():
        return False
    scanned = 0
    for path in root.rglob("*.py"):
        # Skip vendored / cached trees by checking each path component
        if any(part in _SKIP_DIRS for part in path.parts):
            continue
        scanned += 1
        if scanned > _SCAN_FILE_CAP:
            return False
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if _IMPORT_RE.search(text) and _STATEGRAPH_RE.search(text):
            return True
    return False
