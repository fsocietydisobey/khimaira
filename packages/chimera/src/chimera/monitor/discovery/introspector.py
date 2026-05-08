"""Runtime topology introspection — primary path.

Imports a project's graph factory module and reads the compiled graph's
`get_graph()` to extract authoritative node + edge data. This is faster
and more accurate than AST walking when it works.

When import fails (the project's deps aren't in chimera's venv) or the
factory uses dynamic node names (chimera's own factories!), the caller
falls back to `ast_walker.extract`. The fallback marks results as
`approximate: true` so the UI can badge them.
"""

from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Heuristic markers for "this graph factory uses dynamic node names".
# Free-form regex on graph.nodes — if any node name contains these tokens,
# we treat the introspection as suspect and recommend AST fallback.
_DYNAMIC_MARKERS = ("dynamic", "<", ">", "{", "}")


@dataclass
class TopologyResult:
    """Outcome of a topology extraction attempt."""

    nodes: list[str] = field(default_factory=list)
    edges: list[tuple[str, str]] = field(default_factory=list)
    source: str = "introspection"  # "introspection" | "ast"
    approximate: bool = False
    error: str | None = None
    graph_name: str = ""


def introspect_module(module_path: str, project_path: Path | None = None) -> list[TopologyResult]:
    """Import `module_path` and extract every CompiledStateGraph it exposes.

    Args:
        module_path: dotted module path like "chimera.graphs.pipeline".
        project_path: optional project root added to sys.path during import.

    Returns:
        One TopologyResult per discovered compiled graph. If import fails,
        a single TopologyResult with `error` set is returned so the API
        can surface the failure cleanly.
    """
    sys_path_added = None
    if project_path is not None:
        path_str = str(project_path)
        if path_str not in sys.path:
            sys.path.insert(0, path_str)
            sys_path_added = path_str
    try:
        try:
            module = importlib.import_module(module_path)
        except Exception as exc:
            return [TopologyResult(error=f"import failed: {exc!r}", graph_name=module_path)]

        results: list[TopologyResult] = []
        for name, obj in vars(module).items():
            result = _extract_from_object(obj, name)
            if result is not None:
                results.append(result)
        if not results:
            return [TopologyResult(error="no compiled graphs found in module", graph_name=module_path)]
        return results
    finally:
        if sys_path_added is not None and sys_path_added in sys.path:
            sys.path.remove(sys_path_added)


def _extract_from_object(obj: object, name: str) -> TopologyResult | None:
    """Try to read topology from `obj`. Returns None if it isn't a graph."""
    # CompiledStateGraph exposes get_graph() returning a Graph with nodes/edges.
    get_graph = getattr(obj, "get_graph", None)
    if not callable(get_graph):
        return None
    try:
        graph = get_graph()
    except Exception as exc:
        return TopologyResult(error=f"get_graph() raised: {exc!r}", graph_name=name)

    nodes_raw = getattr(graph, "nodes", None)
    if nodes_raw is None:
        return None

    try:
        node_names = [str(n) for n in nodes_raw]
    except Exception as exc:
        return TopologyResult(error=f"nodes iteration failed: {exc!r}", graph_name=name)

    edges_raw = getattr(graph, "edges", None)
    edges: list[tuple[str, str]] = []
    if edges_raw is not None:
        try:
            for edge in edges_raw:
                src, dst = _edge_endpoints(edge)
                if src is not None and dst is not None:
                    edges.append((src, dst))
        except Exception as exc:
            return TopologyResult(error=f"edges iteration failed: {exc!r}", graph_name=name)

    approximate = any(
        any(marker in node_name for marker in _DYNAMIC_MARKERS) for node_name in node_names
    )

    return TopologyResult(
        nodes=node_names,
        edges=edges,
        source="introspection",
        approximate=approximate,
        graph_name=name,
    )


def _edge_endpoints(edge: object) -> tuple[str | None, str | None]:
    """Tolerantly extract (source, target) from various edge shapes."""
    for src_attr, dst_attr in (("source", "target"), ("from_", "to"), ("u", "v")):
        src = getattr(edge, src_attr, None)
        dst = getattr(edge, dst_attr, None)
        if src is not None and dst is not None:
            return (str(src), str(dst))
    if isinstance(edge, tuple) and len(edge) >= 2:
        return (str(edge[0]), str(edge[1]))
    return (None, None)
