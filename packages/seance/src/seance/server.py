"""MCP server exposing Séance's semantic search tools.

Tools:
  - semantic_search: Natural language search over an indexed codebase
  - index_project: Full index of a project directory
  - reindex_changed: Incremental reindex using git diff
  - find_similar: Find code similar to a given symbol
  - list_projects: List all indexed projects with stats
"""

from __future__ import annotations

import logging
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from seance.config import load_config
from seance.indexer.pipeline import IndexingPipeline
from seance.search.engine import SearchEngine
from seance.storage.vectordb import VectorStore

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "seance",
    instructions=(
        "Séance provides semantic code search over indexed codebases. It understands "
        "meaning, not just keywords — use it for vague, conceptual queries.\n\n"
        "## When to use Séance vs other tools\n\n"
        "- **Séance**: conceptual queries — \"how does auth work?\", \"what handles "
        "retries?\", \"find code related to payment processing\".\n"
        "- **Grep**: exact symbol names, specific strings, known function/class names. "
        "Faster and more precise when you know the literal text.\n"
        "- **Serena (LSP-based, project-specific)**: \"find all references to X\", "
        "\"who calls Y\" — use for call-graph navigation, not conceptual search.\n\n"
        "## Workflow\n\n"
        "1. If the user has been actively coding, call `reindex_changed(path)` before "
        "searching to keep the index fresh.\n"
        "2. Use `semantic_search` with natural language — don't keyword-optimize the "
        "query. Describe what you're looking for conceptually.\n"
        "3. Read the top 2–3 results to build context, then read the actual source "
        "files with the standard Read tool for full understanding.\n"
        "4. Use filters (`language`, `chunk_type`) when looking for something specific "
        "(e.g., Python classes related to state management).\n\n"
        "## Tips\n\n"
        "- If `semantic_search` returns nothing useful, try re-phrasing the query more "
        "abstractly. Semantic embeddings are sensitive to framing.\n"
        "- `find_similar` is useful for locating duplicate logic or patterns that "
        "follow the same structure.\n"
        "- Call `list_projects` first if you don't know which project names are indexed."
    ),
)


def _get_config():
    """Lazy config loading so env vars are read at call time, not import time."""
    return load_config()


@mcp.tool()
def semantic_search(
    query: str,
    project: str,
    top_k: int = 10,
    language: str | None = None,
    chunk_type: str | None = None,
) -> list[dict]:
    """Search for code semantically related to a natural language query.

    Args:
        query: Natural language description of what you're looking for.
               Examples: "authentication middleware", "how are payments processed",
               "error handling in the API layer".
        project: Name of the indexed project to search.
        top_k: Maximum number of results to return (default 10).
        language: Optional filter — "python", "typescript", or "javascript".
        chunk_type: Optional filter — "function", "class", "method", "interface", etc.

    Returns:
        List of matching code chunks with file paths, line numbers, and relevance scores.
    """
    config = _get_config()
    engine = SearchEngine(config)
    results = engine.search(
        project_name=project,
        query=query,
        top_k=top_k,
        language=language,
        chunk_type=chunk_type,
    )
    return [r.to_dict() for r in results]


@mcp.tool()
def index_project(path: str, name: str | None = None) -> dict:
    """Index a codebase for semantic search. Run this before searching a project.

    Parses all Python, TypeScript, and JavaScript files using tree-sitter,
    splits them into semantic chunks (functions, classes, etc.), generates
    embeddings, and stores them in the vector database.

    Args:
        path: Absolute path to the project root directory.
        name: Optional project name. Defaults to the directory name.

    Returns:
        Indexing stats: file count, chunk count, embedding count.
    """
    config = _get_config()
    project_path = Path(path).resolve()

    if not project_path.is_dir():
        return {"error": f"Directory not found: {path}"}

    project_name = name or project_path.name

    pipeline = IndexingPipeline(config)
    stats = pipeline.index_project(project_path, project_name)
    stats["project_name"] = project_name
    stats["project_path"] = str(project_path)
    return stats


@mcp.tool()
def reindex_changed(path: str, name: str | None = None) -> dict:
    """Incrementally reindex only files that changed since the last git commit.

    Much faster than a full index — only re-embeds files detected by git diff.
    The project must be a git repository.

    Args:
        path: Absolute path to the project root directory.
        name: Optional project name. Defaults to the directory name.

    Returns:
        Reindexing stats: changed file count, chunk count, embedding count.
    """
    config = _get_config()
    project_path = Path(path).resolve()

    if not project_path.is_dir():
        return {"error": f"Directory not found: {path}"}

    project_name = name or project_path.name

    pipeline = IndexingPipeline(config)
    stats = pipeline.reindex_changed(project_path, project_name)
    stats["project_name"] = project_name
    return stats


@mcp.tool()
def find_similar(
    project: str,
    file_path: str,
    symbol_name: str,
    top_k: int = 5,
) -> list[dict]:
    """Find code similar to a specific symbol in the indexed codebase.

    Useful for finding duplicate logic, related implementations, or
    patterns that follow the same structure.

    Args:
        project: Name of the indexed project.
        file_path: Path to the file containing the symbol.
        symbol_name: Name of the symbol to find similar code for.
        top_k: Maximum number of results (default 5).

    Returns:
        List of similar code chunks with relevance scores.
    """
    config = _get_config()
    engine = SearchEngine(config)
    results = engine.find_similar(
        project_name=project,
        file_path=file_path,
        symbol_name=symbol_name,
        top_k=top_k,
    )
    return [r.to_dict() for r in results]


@mcp.tool()
def list_projects() -> list[dict]:
    """List all indexed projects with their chunk counts.

    Returns:
        List of projects with name and chunk count.
    """
    config = _get_config()
    store = VectorStore(config)
    return store.list_projects()
