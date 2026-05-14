"""Dependency graph generator.

Outputs feature → feature dependency graphs in Mermaid (default) or JSON.
Used by both the CLI and the MCP tool. The Mermaid output is meant to be
committed to `shared-docs/` so AI and humans can read the graph directly.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from scarlet.analyzer.imports import FeatureGraph, build_feature_graph


@dataclass(frozen=True)
class DepGraphResult:
    """Result of a dependency graph generation pass."""

    format: str  # "mermaid" or "json"
    content: str
    feature_count: int
    edge_count: int
    deep_import_count: int

    def to_dict(self) -> dict:
        return {
            "format": self.format,
            "content": self.content,
            "feature_count": self.feature_count,
            "edge_count": self.edge_count,
            "deep_import_count": self.deep_import_count,
        }


def generate_dep_graph(project_path: Path, format: str = "mermaid") -> DepGraphResult:
    """Generate a dependency graph for a project.

    Args:
        project_path: Absolute path to the project root.
        format: Output format ("mermaid" or "json").

    Returns:
        DepGraphResult with the rendered content.
    """
    graph = build_feature_graph(project_path.resolve())

    match format:
        case "mermaid":
            content = _render_mermaid(graph)
        case "json":
            content = json.dumps(graph.to_dict(), indent=2)
        case _:
            raise ValueError(f"Unsupported format: {format}. Use 'mermaid' or 'json'.")

    return DepGraphResult(
        format=format,
        content=content,
        feature_count=len(graph.features),
        edge_count=len(graph.edges),
        deep_import_count=len(graph.deep_imports),
    )


def _render_mermaid(graph: FeatureGraph) -> str:
    """Render a FeatureGraph as a Mermaid flowchart."""
    lines: list[str] = ["flowchart LR"]

    # Declare all features as nodes (so isolated features still appear)
    for feature in graph.features:
        node_id = _safe_id(feature)
        lines.append(f"  {node_id}[{feature}]")

    lines.append("")

    # Edges
    for from_feature, to_feature in graph.edges:
        from_id = _safe_id(from_feature)
        to_id = _safe_id(to_feature)
        lines.append(f"  {from_id} --> {to_id}")

    if graph.deep_imports:
        lines.append("")
        lines.append("  %% Deep imports (bypass feature barrel — flagged as tech debt):")
        seen: set[tuple[str, str]] = set()
        for from_feature, to_feature, _ in graph.deep_imports:
            pair = (from_feature, to_feature)
            if pair in seen:
                continue
            seen.add(pair)
            from_id = _safe_id(from_feature)
            to_id = _safe_id(to_feature)
            lines.append(f"  {from_id} -.->|deep| {to_id}")

    return "\n".join(lines) + "\n"


def _safe_id(name: str) -> str:
    """Convert a feature name into a Mermaid-safe node id."""
    return name.replace("-", "_").replace(".", "_")
