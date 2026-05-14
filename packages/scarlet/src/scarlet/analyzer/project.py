"""Project-level analysis: framework detection, structural overview.

Inspects a project root and returns a manifest of what kind of project
this is — framework, state management, build tooling, folder strategy.
This grounds all subsequent analysis.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from scarlet.config import ScarletConfig, load_config


@dataclass(frozen=True)
class ProjectManifest:
    """Structural overview of a project."""

    path: str
    project_type: str  # nextjs, vite, cra, python, generic
    has_typescript: bool
    has_tests: bool
    state_management: str | None  # redux-toolkit, zustand, mobx, none
    test_framework: str | None  # jest, vitest, pytest, none
    package_manager: str | None  # npm, yarn, pnpm, uv, pip
    features_root: str  # relative path to features dir
    feature_count: int
    has_scarlet_config: bool

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "project_type": self.project_type,
            "has_typescript": self.has_typescript,
            "has_tests": self.has_tests,
            "state_management": self.state_management,
            "test_framework": self.test_framework,
            "package_manager": self.package_manager,
            "features_root": self.features_root,
            "feature_count": self.feature_count,
            "has_scarlet_config": self.has_scarlet_config,
        }


def analyze_project(project_path: Path) -> ProjectManifest:
    """Analyze a project root and return a structural manifest.

    Detection is heuristic: looks at package.json, pyproject.toml, file
    extensions, and folder layout. Falls back to "generic" when uncertain.

    Args:
        project_path: Absolute path to the project root.

    Returns:
        ProjectManifest describing the project.
    """
    project_path = project_path.resolve()
    config = load_config(project_path)

    package_json = _read_package_json(project_path)
    pyproject = _read_pyproject(project_path)

    project_type = _detect_project_type(project_path, package_json, pyproject)
    has_typescript = _has_typescript(project_path, package_json)
    state_management = _detect_state_management(package_json) or config.state_management
    test_framework = _detect_test_framework(package_json, pyproject) or config.test_framework
    package_manager = _detect_package_manager(project_path)
    features_root = config.features_root
    feature_count = _count_features(project_path, features_root)
    has_tests = _has_tests(project_path)

    return ProjectManifest(
        path=str(project_path),
        project_type=project_type,
        has_typescript=has_typescript,
        has_tests=has_tests,
        state_management=state_management,
        test_framework=test_framework,
        package_manager=package_manager,
        features_root=features_root,
        feature_count=feature_count,
        has_scarlet_config=(project_path / ".scarlet.yml").exists(),
    )


def _read_package_json(project_path: Path) -> dict | None:
    """Read and merge all package.jsons in a project (handles workspace setups).

    Walks up to 2 levels deep, skipping node_modules. Merges dependencies
    and devDependencies from every package.json found. This handles:
      - Single-package projects (one package.json at root)
      - Workspace setups (root package.json + frontend/package.json + ...)
      - Monorepos (multiple sibling package.jsons)

    The merged result lets detection logic see the union of all deps,
    so a project with `next` in `frontend/package.json` is correctly
    identified as a Next.js project even if the root has other things.
    """
    package_jsons = _find_all_package_jsons(project_path)
    if not package_jsons:
        return None

    merged: dict = {"dependencies": {}, "devDependencies": {}, "scripts": {}}

    for pj in package_jsons:
        try:
            data = json.loads(pj.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue

        merged["dependencies"].update(data.get("dependencies", {}))
        merged["devDependencies"].update(data.get("devDependencies", {}))
        merged["scripts"].update(data.get("scripts", {}))

        # Preserve top-level fields from the first one we see
        for key in ("name", "version", "type", "workspaces"):
            if key in data and key not in merged:
                merged[key] = data[key]

    return merged


def _find_all_package_jsons(project_path: Path, max_depth: int = 2) -> list[Path]:
    """Find all package.json files in a project, skipping node_modules.

    Walks up to max_depth directories deep. The root package.json is
    always included if present.
    """
    found: list[Path] = []

    root_pj = project_path / "package.json"
    if root_pj.exists():
        found.append(root_pj)

    # Walk subdirectories at depth 1 and 2
    for child in project_path.iterdir():
        if not child.is_dir():
            continue
        if child.name in {"node_modules", ".git", ".next", "dist", "build"}:
            continue
        if child.name.startswith("."):
            continue

        candidate = child / "package.json"
        if candidate.exists():
            found.append(candidate)

        if max_depth > 1:
            try:
                for grandchild in child.iterdir():
                    if not grandchild.is_dir():
                        continue
                    if grandchild.name in {"node_modules", ".git"}:
                        continue
                    if grandchild.name.startswith("."):
                        continue
                    nested = grandchild / "package.json"
                    if nested.exists():
                        found.append(nested)
            except (OSError, PermissionError):
                continue

    return found


def _read_pyproject(project_path: Path) -> str | None:
    """Read pyproject.toml as raw text (we only need to grep for libs)."""
    pyproject = project_path / "pyproject.toml"
    if pyproject.exists():
        try:
            return pyproject.read_text(encoding="utf-8")
        except OSError:
            return None
    return None


def _detect_project_type(
    project_path: Path, package_json: dict | None, pyproject: str | None
) -> str:
    """Heuristic project type detection."""
    if package_json:
        deps = {**package_json.get("dependencies", {}), **package_json.get("devDependencies", {})}
        if "next" in deps:
            return "nextjs"
        if "vite" in deps:
            return "vite"
        if "react-scripts" in deps:
            return "cra"
        if "react" in deps:
            return "react"
        if "vue" in deps:
            return "vue"
        if "svelte" in deps:
            return "svelte"

    if pyproject:
        if "fastapi" in pyproject:
            return "fastapi"
        if "django" in pyproject:
            return "django"
        if "flask" in pyproject:
            return "flask"
        return "python"

    return "generic"


def _has_typescript(project_path: Path, package_json: dict | None) -> bool:
    """Check if the project uses TypeScript."""
    if package_json:
        deps = {**package_json.get("dependencies", {}), **package_json.get("devDependencies", {})}
        if "typescript" in deps:
            return True
    return any(project_path.rglob("tsconfig.json"))


def _detect_state_management(package_json: dict | None) -> str | None:
    """Detect frontend state management library."""
    if not package_json:
        return None
    deps = {**package_json.get("dependencies", {}), **package_json.get("devDependencies", {})}

    if "@reduxjs/toolkit" in deps:
        return "redux-toolkit"
    if "redux" in deps:
        return "redux"
    if "zustand" in deps:
        return "zustand"
    if "mobx" in deps:
        return "mobx"
    if "jotai" in deps:
        return "jotai"
    if "valtio" in deps:
        return "valtio"
    return None


def _detect_test_framework(package_json: dict | None, pyproject: str | None) -> str | None:
    """Detect test framework from package.json or pyproject.toml."""
    if package_json:
        deps = {**package_json.get("dependencies", {}), **package_json.get("devDependencies", {})}
        if "vitest" in deps:
            return "vitest"
        if "jest" in deps:
            return "jest"
        if "@playwright/test" in deps:
            return "playwright"
        if "mocha" in deps:
            return "mocha"

    if pyproject:
        if "pytest" in pyproject:
            return "pytest"
        if "unittest" in pyproject:
            return "unittest"

    return None


def _detect_package_manager(project_path: Path) -> str | None:
    """Detect package manager from lockfile presence."""
    if (project_path / "uv.lock").exists():
        return "uv"
    if (project_path / "pnpm-lock.yaml").exists():
        return "pnpm"
    if (project_path / "yarn.lock").exists():
        return "yarn"
    if (project_path / "package-lock.json").exists():
        return "npm"
    if (project_path / "Pipfile.lock").exists():
        return "pipenv"
    if (project_path / "poetry.lock").exists():
        return "poetry"
    return None


def _count_features(project_path: Path, features_root: str) -> int:
    """Count feature folders under the configured features_root."""
    features_dir = project_path / features_root
    if not features_dir.exists() or not features_dir.is_dir():
        # Try the common alternatives
        for alt in ["frontend/src/features", "src/features", "app/features"]:
            candidate = project_path / alt
            if candidate.exists() and candidate.is_dir():
                features_dir = candidate
                break
        else:
            return 0

    return sum(1 for child in features_dir.iterdir() if child.is_dir())


def _has_tests(project_path: Path) -> bool:
    """Heuristic check for the presence of any test files."""
    test_patterns = ["*.test.js", "*.test.ts", "*.test.tsx", "*.spec.js", "*.spec.ts", "test_*.py"]
    for pattern in test_patterns:
        if any(project_path.rglob(pattern)):
            return True
    return False
