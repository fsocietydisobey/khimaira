"""Import-graph analysis.

Walks every source file in a project, parses imports, and builds a graph
of feature → feature dependencies. Used by:
  - generate_dep_graph: outputs Mermaid/JSON dependency visualization
  - extract_feature_metadata: identifies cross-feature consumers per feature
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path

from scarlet.analyzer.features import scan_features
from scarlet.analyzer.project import analyze_project

# Match ES module imports/requires; we only care about the source path
IMPORT_RE = re.compile(
    r"""
    (?:
        import\s+(?:[^'"]+\s+from\s+)?['"]([^'"]+)['"]   # import ... from 'X'
        |
        import\s*\(\s*['"]([^'"]+)['"]\s*\)              # dynamic import('X')
        |
        require\s*\(\s*['"]([^'"]+)['"]\s*\)              # require('X')
    )
    """,
    re.VERBOSE,
)


@dataclass(frozen=True)
class ImportEdge:
    """One import edge: file A imports module B."""

    from_file: str
    to_module: str  # the import path string

    def to_dict(self) -> dict:
        return {"from_file": self.from_file, "to_module": self.to_module}


@dataclass(frozen=True)
class FeatureGraph:
    """Feature-level import graph."""

    features: list[str] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)  # (from_feature, to_feature)
    deep_imports: list[tuple[str, str, str]] = field(default_factory=list)  # (from, to, file)
    edge_files: dict[tuple[str, str], list[str]] = field(default_factory=dict)
    """Per-edge file mapping: (from_feature, to_feature) → list of files in
    `from_feature` that import from `to_feature`. Used by the CLAUDE.md
    consumers section to show file-level granularity."""

    def to_dict(self) -> dict:
        return {
            "features": self.features,
            "edges": [{"from": f, "to": t} for f, t in self.edges],
            "deep_imports": [
                {"from": f, "to": t, "file": file} for f, t, file in self.deep_imports
            ],
        }


def build_feature_graph(project_path: Path) -> FeatureGraph:
    """Build a feature-level dependency graph for a project.

    For each source file under each feature, parse its imports. If an
    import targets another feature (via `@/features/X` or relative paths
    that resolve into another feature folder), record the edge.

    Args:
        project_path: Absolute path to the project root.

    Returns:
        FeatureGraph with features, edges, and deep-import warnings.
    """
    project_path = project_path.resolve()
    manifest = analyze_project(project_path)
    summaries = scan_features(project_path)

    feature_names = [s.name for s in summaries]
    feature_paths: dict[str, Path] = {s.name: Path(s.path) for s in summaries}

    edges: set[tuple[str, str]] = set()
    deep_imports: list[tuple[str, str, str]] = []
    edge_files: dict[tuple[str, str], list[str]] = {}

    for summary in summaries:
        feature_dir = Path(summary.path)
        for source_file in feature_dir.rglob("*"):
            if not source_file.is_file():
                continue
            if source_file.suffix not in {".ts", ".tsx", ".js", ".jsx", ".mjs"}:
                continue

            try:
                imports = _parse_imports(source_file)
            except OSError:
                continue

            for import_path in imports:
                target_feature = _resolve_to_feature(
                    import_path, source_file, feature_names, feature_paths, manifest.features_root
                )
                if target_feature and target_feature != summary.name:
                    edge = (summary.name, target_feature)
                    edges.add(edge)

                    # Track which files in the source feature import from the target
                    file_str = str(source_file)
                    edge_files.setdefault(edge, [])
                    if file_str not in edge_files[edge]:
                        edge_files[edge].append(file_str)

                    # Detect deep imports — paths that go past the feature barrel
                    if _is_deep_import(import_path, target_feature):
                        deep_imports.append(
                            (summary.name, target_feature, file_str)
                        )

    return FeatureGraph(
        features=feature_names,
        edges=sorted(edges),
        deep_imports=deep_imports,
        edge_files=edge_files,
    )


def _parse_imports(file_path: Path) -> list[str]:
    """Extract import path strings from a JS/TS file.

    Uses a regex rather than tree-sitter for speed — we only need the
    string literal, not the full AST. The regex handles `import`,
    dynamic `import()`, and CommonJS `require()`.
    """
    text = file_path.read_text(encoding="utf-8", errors="ignore")
    paths: list[str] = []
    for match in IMPORT_RE.finditer(text):
        # Each match has three optional groups; pick whichever fired
        path = match.group(1) or match.group(2) or match.group(3)
        if path:
            paths.append(path)
    return paths


def _resolve_to_feature(
    import_path: str,
    source_file: Path,
    feature_names: list[str],
    feature_paths: dict[str, Path],
    features_root: str,
) -> str | None:
    """Resolve an import path to a feature name, if it targets one.

    Handles:
      - `@/features/drawings`           → "drawings"
      - `@/features/drawings/components/X` → "drawings" (deep import)
      - `../../features/drawings`       → "drawings"
      - relative paths inside the same feature → None (not cross-feature)
    """
    # Alias-based imports: @/features/X or features/X
    for prefix in ("@/features/", "features/", "@features/", "src/features/"):
        if import_path.startswith(prefix):
            remainder = import_path[len(prefix):]
            feature = remainder.split("/")[0]
            if feature in feature_names:
                return feature

    # Relative imports: try to resolve to an absolute path and check if it lands in a feature
    if import_path.startswith("."):
        try:
            resolved = (source_file.parent / import_path).resolve()
        except (OSError, ValueError):
            return None

        for feature_name, feature_dir in feature_paths.items():
            try:
                resolved.relative_to(feature_dir)
                return feature_name
            except ValueError:
                continue

    return None


def _is_deep_import(import_path: str, feature_name: str) -> bool:
    """A deep import is one that goes *past* the feature root barrel."""
    # @/features/drawings           → not deep
    # @/features/drawings/          → not deep
    # @/features/drawings/components/X → deep
    if feature_name not in import_path:
        return False
    after_feature = import_path.split(feature_name, 1)[1]
    return after_feature.strip("/") != ""
