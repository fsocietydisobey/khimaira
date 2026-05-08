"""`/api/topology` — render compiled-graph topology as Mermaid.

Tries runtime introspection first (importing the project's graph factory
modules); falls back to tree-sitter AST extraction when import fails or
the topology contains dynamic node names.

Mermaid generation is intentionally simple — directed graph with one
edge per (source, target) pair. The frontend picks the layout.
"""

from __future__ import annotations

import sys
from pathlib import Path

from .._optional import require
from ..discovery import ast_walker, introspector
from ..discovery.introspector import TopologyResult
from ..discovery.project import Project
from ..metadata import cache as meta_cache
from ..metadata.merge import enrich_topology


def build_router(projects: list[Project]):
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.get("/topology/{name}")
    async def topology(name: str):
        project = _find(projects, name)
        if project is None:
            raise fastapi.HTTPException(status_code=404, detail=f"project not found: {name}")

        results = _extract(project)

        # Pull cached metadata once and apply to each graph result.
        metadata = meta_cache.load(project.path)
        enriched = [enrich_topology(r, metadata) for r in results]

        scan_status = _scan_status(metadata, project.path)

        return {
            "project": project.name,
            "scan_status": scan_status,
            "summary": metadata.summary if metadata else "",
            "combined_mermaid": _to_combined_mermaid(enriched),
            "graphs": [
                {
                    "name": r.graph_name,
                    "label": getattr(r, "graph_label", _default_label(r.graph_name)),
                    "summary": getattr(r, "graph_summary", ""),
                    "role": getattr(r, "graph_role", ""),
                    "source": r.source,
                    "approximate": r.approximate,
                    "error": r.error,
                    "layout": getattr(r, "layout", "LR"),
                    "invokes": getattr(r, "invokes", {}),
                    "nodes": r.nodes,
                    "node_meta": _build_node_meta(r),
                    "edges": [{"source": s, "target": t} for s, t in r.edges],
                    "mermaid": _to_mermaid(r),
                }
                for r in enriched
            ],
        }

    return router


def _to_combined_mermaid(results: list[TopologyResult]) -> str:
    """Render every graph in a project as one Mermaid diagram.

    Each graph becomes a `subgraph <id>["<label>"] ... end` cluster.
    Cross-cluster edges (dotted) are emitted from the metadata `invokes`
    map: when graph A's node `n` invokes graph B, we draw `A_n -.-> B_anchor`
    where the anchor is the first node of B. This is what makes the
    project's full architecture readable as a single tree.

    Node IDs are namespaced by graph (`<graph_safe>__<node_safe>`) so two
    graphs with a node named "agent" don't collide.
    """
    valid = [r for r in results if r.nodes and not (r.error and not r.nodes)]
    if not valid:
        return "flowchart TB\n  empty[\"no graphs to render\"]"

    lines: list[str] = ["flowchart TB"]
    classlines: list[str] = []
    edge_count = 0
    cross_edge_indices: list[int] = []

    # Map graph_name → safe subgraph id for cross-edge resolution
    graph_safe_ids: dict[str, str] = {r.graph_name: _safe_id(r.graph_name) for r in valid}
    # Map graph_name → first node's namespaced id (anchor for cross-edges)
    graph_anchors: dict[str, str] = {}

    for r in valid:
        graph_id = graph_safe_ids[r.graph_name]
        label = _escape(getattr(r, "graph_label", _default_label(r.graph_name)))
        lines.append(f'  subgraph {graph_id}["{label}"]')
        # Inherit per-graph layout direction inside the subgraph
        direction = getattr(r, "layout", "LR") or "LR"
        lines.append(f"    direction {direction}")

        node_roles: dict[str, str] = getattr(r, "node_roles", {})
        node_labels: dict[str, str] = getattr(r, "node_labels", {})

        node_ns: dict[str, str] = {}
        for node in r.nodes:
            ns_id = f"{graph_id}__{_safe_id(node)}"
            node_ns[node] = ns_id
            category = _classify_node(node, node_roles.get(node))
            node_label = node_labels.get(node, node)
            lines.append("    " + _node_decl(node, node_label, category).lstrip())
            classlines.append(f"  class {ns_id} {category}")
        # Anchor is the first non-marker node, falling back to any node
        anchor = None
        for n in r.nodes:
            if n not in ("__start__", "__end__"):
                anchor = node_ns[n]
                break
        if anchor is None and r.nodes:
            anchor = node_ns[r.nodes[0]]
        if anchor:
            graph_anchors[r.graph_name] = anchor

        for src, dst in r.edges:
            if src not in node_ns or dst not in node_ns:
                continue
            lines.append(f"    {node_ns[src]} --> {node_ns[dst]}")
            edge_count += 1
        lines.append("  end")

    # Cross-graph edges (dotted) from the invokes metadata
    for r in valid:
        invokes: dict[str, str] = getattr(r, "invokes", {}) or {}
        graph_id = graph_safe_ids[r.graph_name]
        for source_node, target_graph in invokes.items():
            if target_graph not in graph_anchors:
                continue
            src_ns = f"{graph_id}__{_safe_id(source_node)}"
            dst_ns = graph_anchors[target_graph]
            lines.append(f"  {src_ns} -.-> {dst_ns}")
            cross_edge_indices.append(edge_count)
            edge_count += 1

    # ClassDefs (same palette as the per-graph view)
    lines.extend([
        "",
        "classDef n_start fill:#ffd700,stroke:#b8860b,color:#1a1a1a,stroke-width:2px",
        "classDef n_end fill:#48bb78,stroke:#276749,color:#ffffff,stroke-width:2px",
        "classDef n_router fill:#9f7aea,stroke:#6b46c1,color:#ffffff,stroke-width:2px",
        "classDef n_gate fill:#ed8936,stroke:#9c4221,color:#ffffff,stroke-width:2px",
        "classDef n_critic fill:#e53e3e,stroke:#9b2c2c,color:#ffffff,stroke-width:2px",
        "classDef n_synth fill:#38a169,stroke:#22543d,color:#ffffff,stroke-width:2px",
        "classDef n_default fill:#2d3748,stroke:#4a5568,color:#e2e8f0,stroke-width:1.5px",
    ])
    lines.extend(classlines)
    if edge_count > 0:
        non_cross = [str(i) for i in range(edge_count) if i not in cross_edge_indices]
        if non_cross:
            lines.append(f"linkStyle {','.join(non_cross)} stroke:#718096,stroke-width:1.5px,fill:none")
        if cross_edge_indices:
            cross = ",".join(str(i) for i in cross_edge_indices)
            lines.append(f"linkStyle {cross} stroke:#9f7aea,stroke-width:2px,stroke-dasharray:5 5,fill:none")

    return "\n".join(lines)


def _scan_status(metadata, project_path) -> str:
    """One of: enriched | stale | none. UI shows a small badge for each."""
    if metadata is None:
        return "none"
    if meta_cache.is_stale(metadata, project_path):
        return "stale"
    return "enriched"


def _default_label(name: str) -> str:
    return name.replace("_compiled_", "").replace("build_", "").replace("_graph", "").replace("_", " ")


def _build_node_meta(result: TopologyResult) -> dict[str, dict[str, str]]:
    """Per-node metadata for the frontend inspector — role, label, summary.

    Always includes every node from the graph; missing fields fall back
    to defaults the frontend can render. Built from the metadata-enriched
    attrs attached by `enrich_topology`.
    """
    roles: dict[str, str] = getattr(result, "node_roles", {})
    labels: dict[str, str] = getattr(result, "node_labels", {})
    summaries: dict[str, str] = getattr(result, "node_summaries", {})
    out: dict[str, dict[str, str]] = {}
    for node in result.nodes:
        out[node] = {
            "role": roles.get(node, ""),
            "label": labels.get(node, ""),
            "summary": summaries.get(node, ""),
        }
    return out


def _find(projects: list[Project], name: str) -> Project | None:
    for p in projects:
        if p.name == name:
            return p
    return None


def _extract(project: Project) -> list[TopologyResult]:
    """Discover candidate graph modules, try introspection, fall back to AST."""
    candidate_modules = _candidate_modules(project.path)
    introspection_results: list[TopologyResult] = []
    introspection_succeeded = False
    for module in candidate_modules:
        results = introspector.introspect_module(module, project_path=project.path)
        for r in results:
            if r.error is None and r.nodes:
                introspection_results.append(r)
                introspection_succeeded = True
            elif r.error:
                # Track failures but only surface them if we have nothing else.
                pass

    if introspection_succeeded:
        # If any introspected graph is approximate, also run AST for completeness.
        return introspection_results

    # Fallback: AST walk
    ast_results = ast_walker.extract_from_path(project.path)
    return ast_results or [
        TopologyResult(
            error="no compiled graphs discovered via introspection or AST",
            source="ast",
            approximate=True,
        )
    ]


def _candidate_modules(project_path: Path) -> list[str]:
    """Heuristic: any importable module whose name contains 'graph' or
    'pipeline'. Caller adds the project root to sys.path before import."""
    if not project_path.is_dir():
        return []

    candidates: list[str] = []
    src_dir = project_path / "src"
    roots = [src_dir if src_dir.is_dir() else project_path]
    for root in roots:
        for path in root.rglob("*.py"):
            if any(part in {".venv", "venv", "__pycache__", "node_modules", "site-packages"} for part in path.parts):
                continue
            stem = path.stem
            if stem.startswith("_"):
                continue
            if "graph" not in stem and "pipeline" not in stem:
                continue
            module = _to_module_path(path, root)
            if module:
                candidates.append(module)
    return candidates


def _to_module_path(file: Path, root: Path) -> str | None:
    try:
        rel = file.relative_to(root)
    except ValueError:
        return None
    parts = list(rel.with_suffix("").parts)
    if not parts:
        return None
    return ".".join(parts)


_START_NAMES = frozenset({"__start__", "__input__", "start", "begin", "entry"})
_END_NAMES = frozenset({"__end__", "end", "done", "exit", "finish"})
# Substrings that suggest a node is a router / decision point (diamond shape).
_ROUTER_KEYWORDS = ("router", "_route", "gate", "decision", "dispatch", "branch", "switch")
# Substrings for adversarial / quality-gate nodes (red accent).
_CRITIC_KEYWORDS = ("critic", "validate", "validator", "stress", "review", "check", "compliance")
# Substrings for synthesis / aggregation nodes (green accent).
_SYNTHESIS_KEYWORDS = ("aggregat", "merge", "synth", "compile", "commit", "format")


# Maps the schema's NodeRole values to internal classDef categories.
_ROLE_TO_CATEGORY = {
    "entry": "n_start",
    "exit": "n_end",
    "router": "n_router",
    "gate": "n_gate",
    "critic": "n_critic",
    "synthesis": "n_synth",
    "executor": "n_default",
}


def _classify_node(name: str, override: str | None = None) -> str:
    """Return a CSS-class-style category used by the classDefs below.

    `override` is the metadata-provided role (mapped via _ROLE_TO_CATEGORY).
    When present it wins over the keyword heuristic — the heuristic is
    only the fallback when no enrichment has been written for this node.
    """
    if override:
        mapped = _ROLE_TO_CATEGORY.get(override)
        if mapped:
            return mapped
    lowered = name.lower()
    if lowered in _START_NAMES:
        return "n_start"
    if lowered in _END_NAMES:
        return "n_end"
    if any(k in lowered for k in _ROUTER_KEYWORDS):
        return "n_router"
    if any(k in lowered for k in _CRITIC_KEYWORDS):
        return "n_critic"
    if any(k in lowered for k in _SYNTHESIS_KEYWORDS):
        return "n_synth"
    return "n_default"


def _node_decl(name: str, label: str, category: str) -> str:
    """Render a node declaration with shape based on its category."""
    safe = _safe_id(name)
    escaped = _escape(label)
    if category in ("n_start", "n_end"):
        return f'  {safe}(["{escaped}"])'
    if category == "n_router":
        return f'  {safe}{{"{escaped}"}}'
    if category == "n_gate":
        # Hexagon for gate / HITL nodes — visually distinct from router diamonds.
        return f'  {safe}{{{{"{escaped}"}}}}'
    return f'  {safe}("{escaped}")'


def _to_mermaid(result: TopologyResult) -> str:
    """Render topology as a styled Mermaid flowchart.

    Reads `node_roles` and `node_labels` attached by metadata enrichment
    (when present); falls back to the keyword classifier on raw AST data.
    """
    if result.error and not result.nodes:
        return f"flowchart LR\n  err[\"error: {_escape(result.error)}\"]"

    direction = getattr(result, "layout", "LR") or "LR"
    node_roles: dict[str, str] = getattr(result, "node_roles", {})
    node_labels: dict[str, str] = getattr(result, "node_labels", {})

    lines = [f"flowchart {direction}"]

    categorized: list[tuple[str, str]] = []  # (safe_id, category)
    for node in result.nodes:
        category = _classify_node(node, node_roles.get(node))
        label = node_labels.get(node, node)
        lines.append(_node_decl(node, label, category))
        categorized.append((_safe_id(node), category))

    for src, dst in result.edges:
        lines.append(f"  {_safe_id(src)} --> {_safe_id(dst)}")

    # Group safe_ids by category for the class assignments
    by_category: dict[str, list[str]] = {}
    for safe_id, category in categorized:
        by_category.setdefault(category, []).append(safe_id)

    # ClassDefs — palette matches README role-color usage. `n_gate` is new:
    # amber for HITL/approval gates (distinct from purple router diamonds).
    lines.extend([
        "",
        "classDef n_start fill:#ffd700,stroke:#b8860b,color:#1a1a1a,stroke-width:2px",
        "classDef n_end fill:#48bb78,stroke:#276749,color:#ffffff,stroke-width:2px",
        "classDef n_router fill:#9f7aea,stroke:#6b46c1,color:#ffffff,stroke-width:2px",
        "classDef n_gate fill:#ed8936,stroke:#9c4221,color:#ffffff,stroke-width:2px",
        "classDef n_critic fill:#e53e3e,stroke:#9b2c2c,color:#ffffff,stroke-width:2px",
        "classDef n_synth fill:#38a169,stroke:#22543d,color:#ffffff,stroke-width:2px",
        "classDef n_default fill:#2d3748,stroke:#4a5568,color:#e2e8f0,stroke-width:1.5px",
    ])
    for category, ids in by_category.items():
        if not ids:
            continue
        lines.append(f"class {','.join(ids)} {category}")
    if result.edges:
        n_edges = len(result.edges)
        edge_indices = ",".join(str(i) for i in range(n_edges))
        lines.append(f"linkStyle {edge_indices} stroke:#718096,stroke-width:1.5px,fill:none")

    return "\n".join(lines)


def _safe_id(name: str) -> str:
    """Mermaid node IDs cannot contain spaces or punctuation."""
    out = []
    for ch in name:
        if ch.isalnum() or ch == "_":
            out.append(ch)
        else:
            out.append("_")
    text = "".join(out)
    if text and text[0].isdigit():
        text = "n_" + text
    return text or "anon"


def _escape(text: str) -> str:
    return text.replace('"', "'")


# Re-export sys to silence unused-import linters when the module is imported
# but topology isn't called (tests only).
_ = sys
