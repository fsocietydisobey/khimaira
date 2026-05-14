"""MCP server exposing Scarlet's analysis and generation tools.

Tools:
  - analyze_project: Detect framework, structure, conventions
  - scan_features: List features with their state (CLAUDE.md, barrels, counts)
  - extract_feature_metadata: Parse a feature, return structured exports/hooks/slices
  - generate_barrel: Create the barrel export (index.js) for a feature
  - build_claude_md: Generate or refresh a feature's CLAUDE.md
  - generate_dep_graph: Output Mermaid or JSON dependency graph
  - lint_claude_md: Validate a feature's CLAUDE.md for staleness
"""

from __future__ import annotations

from pathlib import Path

from mcp.server.fastmcp import FastMCP

from scarlet.analyzer.features import scan_features as _scan_features
from scarlet.analyzer.imports import build_feature_graph
from scarlet.analyzer.invariants import extract_invariants as _extract_invariants
from scarlet.analyzer.metadata import extract_feature_metadata as _extract_metadata
from scarlet.analyzer.project import analyze_project as _analyze_project
from scarlet.generator.barrel import generate_barrel as _generate_barrel
from scarlet.generator.claude_md import build_claude_md as _build_claude_md
from scarlet.generator.dep_graph import generate_dep_graph as _generate_dep_graph
from scarlet.validator.linter import lint_feature_claude_md

mcp = FastMCP(
    "scarlet",
    instructions=(
        "Scarlet is a codebase cartographer. She walks any project, extracts "
        "structural metadata via tree-sitter, and generates the documentation "
        "scaffolding AI assistants need: per-feature CLAUDE.md files, barrel exports, "
        "dependency graphs, and symbol manifests.\n\n"
        "## Core principle — deterministic vs. judgment\n\n"
        "Scarlet generates **structure**; you generate **meaning**.\n"
        "- Deterministic (Scarlet's tools): parsing, metadata extraction, barrel "
        "generation, dep graphs, lint checks. Anything an AST can answer.\n"
        "- Judgment (you, via these tools): synthesizing vocabulary, identifying "
        "which gotchas matter, writing prose explanations.\n\n"
        "On `build_claude_md` updates, auto-derivable sections refresh and sections "
        "between `<!-- BEGIN MANUAL -->` / `<!-- END MANUAL -->` markers are preserved.\n\n"
        "## When to reach for Scarlet\n\n"
        "- **Onboarding a new feature**: `scan_features` → `extract_feature_metadata` "
        "→ `build_claude_md` to generate the skeleton, then fill in manual sections.\n"
        "- **After a refactor**: `lint_claude_md` to find stale docs, then "
        "`build_claude_md` to refresh auto-sections.\n"
        "- **Architecture visibility**: `generate_dep_graph` (Mermaid) to see "
        "feature → feature dependencies and flag deep imports that bypass barrels.\n"
        "- **Surfacing gotchas**: `extract_invariants` surfaces warning comments, "
        "magic numbers, and TODOs that might belong in CLAUDE.md's Gotchas section.\n"
        "- **Finding consumers**: `list_consumers` shows which features import from "
        "the given feature — useful before refactoring a shared module.\n\n"
        "## Workflow for generating a feature's CLAUDE.md\n\n"
        "1. `analyze_project(project_root)` → confirm framework, state mgmt, paths.\n"
        "2. `scan_features(project_root)` → pick the target feature.\n"
        "3. `extract_feature_metadata(feature_path)` → inspect exports.\n"
        "4. `extract_invariants(feature_path)` → candidate gotchas.\n"
        "5. `build_claude_md(project_root, feature_path)` → generate skeleton.\n"
        "6. Fill in Vocabulary / Conventions / Common tasks / Gotchas by reading the "
        "code — don't guess. Use the invariant candidates as raw material.\n\n"
        "## Tools that WRITE to disk (set `write=False` for dry run)\n\n"
        "- `generate_barrel` — writes `index.js`/`.ts`\n"
        "- `build_claude_md` — writes feature's `CLAUDE.md`\n\n"
        "Always use `write=False` first when unsure; review the content, then re-run "
        "with `write=True`."
    ),
)


@mcp.tool()
def analyze_project(path: str) -> dict:
    """Analyze a project root and return structural overview.

    Detects: framework (Next.js, Vite, FastAPI, etc.), TypeScript usage,
    state management library, test framework, package manager, features
    folder, total feature count.

    Args:
        path: Absolute path to the project root.

    Returns:
        Project manifest with detected attributes.
    """
    project_path = Path(path).resolve()
    if not project_path.is_dir():
        return {"error": f"Directory not found: {path}"}

    manifest = _analyze_project(project_path)
    return manifest.to_dict()


@mcp.tool()
def scan_features(path: str) -> list[dict]:
    """List all features in a project with their current state.

    For each feature: whether it has a CLAUDE.md, whether it has a barrel
    export, counts of components/hooks/slices/api endpoints, subfolder
    structure.

    Args:
        path: Absolute path to the project root.

    Returns:
        List of feature summaries.
    """
    project_path = Path(path).resolve()
    if not project_path.is_dir():
        return [{"error": f"Directory not found: {path}"}]

    summaries = _scan_features(project_path)
    return [s.to_dict() for s in summaries]


@mcp.tool()
def extract_feature_metadata(feature_path: str) -> dict:
    """Parse a feature folder and return structured metadata.

    Walks all source files in the feature, parses with tree-sitter, and
    extracts every exported component, hook, class, interface, type, and
    constant. Each export carries name, kind, file path, default-export
    status, and line number.

    Args:
        feature_path: Absolute path to the feature directory.

    Returns:
        Feature metadata with all exported symbols categorized.
    """
    fp = Path(feature_path).resolve()
    if not fp.is_dir():
        return {"error": f"Directory not found: {feature_path}"}

    metadata = _extract_metadata(fp)
    return metadata.to_dict()


@mcp.tool()
def generate_barrel(feature_path: str, extension: str = "js", write: bool = True) -> dict:
    """Generate a barrel export file (index.js or index.ts) for a feature.

    Walks the feature, identifies what should be exported (default-exported
    components, hooks, slices, types), and writes a re-export file.

    Args:
        feature_path: Absolute path to the feature directory.
        extension: File extension ("js", "ts", or "tsx").
        write: If true, write the file. If false, return content only (dry run).

    Returns:
        Result with the generated content and metadata.
    """
    fp = Path(feature_path).resolve()
    if not fp.is_dir():
        return {"error": f"Directory not found: {feature_path}"}

    result = _generate_barrel(fp, extension=extension, write=write)
    return result.to_dict()


@mcp.tool()
def build_claude_md(
    project_path: str,
    feature_path: str,
    import_alias: str | None = None,
    write: bool = True,
) -> dict:
    """Generate or refresh a feature's CLAUDE.md.

    Auto sections (Public API, Key files, Consumers, See also) are
    regenerated from current metadata. Manual sections (Vocabulary,
    Conventions, Common tasks, Gotchas) are preserved across updates by
    keeping <!-- BEGIN MANUAL --> ... <!-- END MANUAL --> blocks intact.

    Args:
        project_path: Absolute path to the project root.
        feature_path: Absolute path to the feature directory.
        import_alias: Canonical import path (e.g. "@/features/drawings").
                      Defaults to "@/features/<feature_name>".
        write: If true, write the file. If false, return content only.

    Returns:
        Result with the new content and whether the file was new.
    """
    pp = Path(project_path).resolve()
    fp = Path(feature_path).resolve()

    if not pp.is_dir():
        return {"error": f"Project directory not found: {project_path}"}
    if not fp.is_dir():
        return {"error": f"Feature directory not found: {feature_path}"}

    result = _build_claude_md(pp, fp, import_alias=import_alias, write=write)
    return result.to_dict()


@mcp.tool()
def generate_dep_graph(path: str, format: str = "mermaid") -> dict:
    """Generate a feature-level dependency graph for a project.

    Walks all imports across all features, builds a feature → feature
    dependency graph, and outputs it as Mermaid (default) or JSON.
    Deep imports (paths that bypass the feature barrel) are flagged as
    tech debt.

    Args:
        path: Absolute path to the project root.
        format: "mermaid" or "json".

    Returns:
        Result with the rendered graph content.
    """
    project_path = Path(path).resolve()
    if not project_path.is_dir():
        return {"error": f"Directory not found: {path}"}

    result = _generate_dep_graph(project_path, format=format)
    return result.to_dict()


@mcp.tool()
def lint_claude_md(feature_path: str) -> dict:
    """Validate a feature's CLAUDE.md for staleness and missing sections.

    Checks: required sections present, public API matches actual exports,
    referenced files still exist, manual section markers intact.

    Args:
        feature_path: Absolute path to the feature directory.

    Returns:
        Lint report with all issues found.
    """
    fp = Path(feature_path).resolve()
    if not fp.is_dir():
        return {"error": f"Directory not found: {feature_path}"}

    report = lint_feature_claude_md(fp)
    return report.to_dict()


@mcp.tool()
def extract_invariants(feature_path: str) -> dict:
    """Scan a feature for invariant candidates worth documenting as gotchas.

    Heuristically surfaces:
      - Warning comments (DON'T, NEVER, FIXME, HACK)
      - Intentional callouts ("intentional", "deliberate", "by design")
      - Magic numbers with explanatory comments (e.g. `600ms // ...`)
      - TODO markers

    These are SUGGESTIONS only — Scarlet does not decide what's important.
    Use this output as raw material when filling in the gotchas section
    of a feature's CLAUDE.md.

    Args:
        feature_path: Absolute path to the feature directory.

    Returns:
        Categorized invariant candidates with file paths and line numbers.
    """
    fp = Path(feature_path).resolve()
    if not fp.is_dir():
        return {"error": f"Directory not found: {feature_path}"}

    report = _extract_invariants(fp)
    return report.to_dict()


@mcp.tool()
def list_consumers(project_path: str, feature_name: str) -> dict:
    """List all features that import from the given feature.

    Args:
        project_path: Absolute path to the project root.
        feature_name: Name of the feature to find consumers for.

    Returns:
        Dict with consumer feature names.
    """
    pp = Path(project_path).resolve()
    if not pp.is_dir():
        return {"error": f"Directory not found: {project_path}"}

    graph = build_feature_graph(pp)
    consumers = [from_f for from_f, to_f in graph.edges if to_f == feature_name]
    return {"feature": feature_name, "consumers": consumers}
