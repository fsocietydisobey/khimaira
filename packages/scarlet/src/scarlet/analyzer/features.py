"""Feature-level scanning: list features, count entities, check state.

For each feature folder, report:
  - whether it has a CLAUDE.md
  - whether it has a barrel export (index.js / index.ts)
  - counts of components, hooks, slices, API endpoints
  - subfolder structure
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from scarlet.analyzer.project import analyze_project

# Common feature subfolder names
COMPONENT_DIRS = {"components", "ui", "views"}
HOOK_DIRS = {"hooks"}
SLICE_DIRS = {"slices", "store"}
API_DIRS = {"api", "endpoints"}


@dataclass(frozen=True)
class FeatureSummary:
    """Summary of a single feature folder's state."""

    name: str
    path: str
    has_claude_md: bool
    has_barrel: bool
    barrel_path: str | None
    component_count: int
    hook_count: int
    slice_count: int
    api_endpoint_count: int
    subfolders: list[str] = field(default_factory=list)
    file_count: int = 0

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "path": self.path,
            "has_claude_md": self.has_claude_md,
            "has_barrel": self.has_barrel,
            "barrel_path": self.barrel_path,
            "component_count": self.component_count,
            "hook_count": self.hook_count,
            "slice_count": self.slice_count,
            "api_endpoint_count": self.api_endpoint_count,
            "subfolders": self.subfolders,
            "file_count": self.file_count,
        }


def scan_features(project_path: Path) -> list[FeatureSummary]:
    """Scan all features in a project and return summaries.

    Args:
        project_path: Absolute path to the project root.

    Returns:
        List of FeatureSummary, one per feature folder.
    """
    project_path = project_path.resolve()
    manifest = analyze_project(project_path)

    features_dir = project_path / manifest.features_root
    if not features_dir.exists():
        # Try common alternatives
        for alt in ["frontend/src/features", "src/features", "app/features"]:
            candidate = project_path / alt
            if candidate.exists() and candidate.is_dir():
                features_dir = candidate
                break
        else:
            return []

    summaries: list[FeatureSummary] = []
    for feature_dir in sorted(features_dir.iterdir()):
        if not feature_dir.is_dir():
            continue
        summaries.append(_summarize_feature(feature_dir))

    return summaries


def _summarize_feature(feature_dir: Path) -> FeatureSummary:
    """Build a FeatureSummary for one feature folder."""
    has_claude_md = (feature_dir / "CLAUDE.md").exists()

    barrel_path = _find_barrel(feature_dir)
    has_barrel = barrel_path is not None

    component_count = _count_matching_files(feature_dir, _is_component_file)
    hook_count = _count_matching_files(feature_dir, _is_hook_file)
    slice_count = _count_matching_files(feature_dir, _is_slice_file)
    api_endpoint_count = _count_in_subdirs(feature_dir, API_DIRS, _is_api_file)

    subfolders = sorted(
        [child.name for child in feature_dir.iterdir() if child.is_dir() and not child.name.startswith(".")]
    )

    file_count = sum(1 for _ in feature_dir.rglob("*") if _.is_file())

    return FeatureSummary(
        name=feature_dir.name,
        path=str(feature_dir),
        has_claude_md=has_claude_md,
        has_barrel=has_barrel,
        barrel_path=str(barrel_path) if barrel_path else None,
        component_count=component_count,
        hook_count=hook_count,
        slice_count=slice_count,
        api_endpoint_count=api_endpoint_count,
        subfolders=subfolders,
        file_count=file_count,
    )


def _find_barrel(feature_dir: Path) -> Path | None:
    """Find the feature's barrel export file (index.js, index.ts, etc.)."""
    for name in ("index.ts", "index.tsx", "index.js", "index.jsx", "index.mjs"):
        candidate = feature_dir / name
        if candidate.exists():
            return candidate
    return None


def _count_in_subdirs(
    feature_dir: Path, dir_names: set[str], file_filter
) -> int:
    """Count files matching a filter inside any subdirectory whose name is in dir_names."""
    count = 0
    for subdir_name in dir_names:
        subdir = feature_dir / subdir_name
        if not subdir.exists() or not subdir.is_dir():
            continue
        for child in subdir.rglob("*"):
            if child.is_file() and file_filter(child):
                count += 1
    return count


def _count_matching_files(feature_dir: Path, file_filter) -> int:
    """Count all files matching a filter anywhere in a feature folder.

    Skips test files, story files, type definition files, and dotfile dirs.
    """
    count = 0
    for child in feature_dir.rglob("*"):
        if not child.is_file():
            continue
        # Skip generated, vendored, and dotfile-prefixed paths
        try:
            rel_parts = child.relative_to(feature_dir).parts
        except ValueError:
            continue
        if any(part.startswith(".") or part == "node_modules" for part in rel_parts):
            continue
        # Skip test, spec, story, and declaration files
        name = child.name
        if (
            ".test." in name
            or ".spec." in name
            or ".stories." in name
            or name.endswith(".d.ts")
        ):
            continue
        if file_filter(child):
            count += 1
    return count


def _is_component_file(path: Path) -> bool:
    """Heuristic: a component file ends in .tsx/.jsx or starts with PascalCase."""
    if path.suffix in (".tsx", ".jsx"):
        return True
    if path.suffix in (".ts", ".js") and path.stem and path.stem[0].isupper():
        return True
    return False


def _is_hook_file(path: Path) -> bool:
    """Heuristic: a hook file is a .ts/.js whose name starts with `use`."""
    if path.suffix not in (".ts", ".tsx", ".js", ".jsx"):
        return False
    return path.stem.startswith("use") and len(path.stem) > 3 and path.stem[3].isupper()


def _is_slice_file(path: Path) -> bool:
    """Heuristic: a slice file is a .ts/.js whose name contains 'slice' or 'reducer'."""
    if path.suffix not in (".ts", ".js"):
        return False
    name = path.stem.lower()
    return "slice" in name or "reducer" in name


def _is_api_file(path: Path) -> bool:
    """Heuristic: an API file is a .ts/.js inside an api/ or endpoints/ directory."""
    return path.suffix in (".ts", ".js")
