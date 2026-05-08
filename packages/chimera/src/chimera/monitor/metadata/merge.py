"""Merge cached metadata into the AST topology output.

This is the function the `/api/topology/<name>` endpoint calls to upgrade
the bare AST result into a polished diagram. AST is always the source of
truth for which nodes exist; metadata adds roles, labels, hierarchy.

If no metadata is present, we pass the AST through unchanged — the
diagram just looks like the AST-only fallback.
"""

from __future__ import annotations

from ..discovery.introspector import TopologyResult
from .schema import NodeMetadata, ProjectMetadata


def enrich_topology(
    result: TopologyResult,
    metadata: ProjectMetadata | None,
) -> TopologyResult:
    """Return a copy of `result` with metadata-driven enhancements applied.

    Doesn't mutate the input. The returned TopologyResult has:
      - node_roles: dict[node_name → role] (used by the renderer for shapes/colors)
      - node_labels: dict[node_name → display label]
      - graph_label: cleaned display name for the graph
      - graph_summary: one-line description (used in the tab tooltip)
      - layout: TB/LR/etc.

    These attrs are added to the result via the existing metadata pathways
    in TopologyResult-as-dict serialization. We extend the result here, the
    serializer in api/topology.py reads them.
    """
    if metadata is None:
        return result

    graph_meta = metadata.graphs.get(result.graph_name)
    if graph_meta is None:
        return result

    # Build node enrichment maps
    node_roles: dict[str, str] = {}
    node_labels: dict[str, str] = {}
    node_summaries: dict[str, str] = {}
    for node_name, node_meta in graph_meta.nodes.items():
        if not isinstance(node_meta, NodeMetadata):
            continue
        if node_meta.role is not None:
            node_roles[node_name] = node_meta.role
        if node_meta.label is not None:
            node_labels[node_name] = node_meta.label
        if node_meta.summary is not None:
            node_summaries[node_name] = node_meta.summary

    # Attach as ad-hoc attributes — TopologyResult is a plain dataclass so
    # this is safe. The serializer checks for presence and falls back
    # cleanly when absent.
    setattr(result, "node_roles", node_roles)
    setattr(result, "node_labels", node_labels)
    setattr(result, "node_summaries", node_summaries)
    setattr(result, "graph_label", graph_meta.label or _clean_graph_name(result.graph_name))
    setattr(result, "graph_summary", graph_meta.summary or "")
    setattr(result, "graph_role", graph_meta.role or "")
    setattr(result, "layout", graph_meta.layout)
    setattr(result, "invokes", dict(graph_meta.invokes))

    return result


def _clean_graph_name(name: str) -> str:
    """Default presentational name when no metadata label is present."""
    return name.replace("_compiled_", "").replace("build_", "").replace("_graph", "").replace("_", " ")
