"""CLI entry point for Séance.

Commands:
  seance index <path>          Full index of a codebase
  seance reindex <path>        Incremental reindex (git-diff aware)
  seance search <project> <q>  Search from the command line
  seance list                  List indexed projects
  seance serve                 Start MCP server (stdio)
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import click

from seance.config import load_config


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Enable verbose logging.")
def main(verbose: bool) -> None:
    """Séance — semantic codebase search."""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        stream=sys.stderr,
    )


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--name", "-n", default=None, help="Project name (defaults to directory name).")
def index(path: str, name: str | None) -> None:
    """Index a codebase for semantic search."""
    from seance.indexer.pipeline import IndexingPipeline

    config = load_config()
    project_path = Path(path).resolve()
    project_name = name or project_path.name

    pipeline = IndexingPipeline(config)
    stats = pipeline.index_project(project_path, project_name)

    click.echo(f"Indexed '{project_name}': {stats['file_count']} files, "
               f"{stats['chunk_count']} chunks, {stats['embedded_count']} embedded")


@main.command()
@click.argument("path", type=click.Path(exists=True, file_okay=False))
@click.option("--name", "-n", default=None, help="Project name (defaults to directory name).")
def reindex(path: str, name: str | None) -> None:
    """Incrementally reindex changed files (git-diff aware)."""
    from seance.indexer.pipeline import IndexingPipeline

    config = load_config()
    project_path = Path(path).resolve()
    project_name = name or project_path.name

    pipeline = IndexingPipeline(config)
    stats = pipeline.reindex_changed(project_path, project_name)

    click.echo(f"Reindexed '{project_name}': {stats['changed_files']} changed files, "
               f"{stats['chunk_count']} chunks, {stats['embedded_count']} embedded")


@main.command()
@click.argument("project")
@click.argument("query")
@click.option("--top-k", "-k", default=10, help="Number of results.")
@click.option("--language", "-l", default=None, help="Filter by language.")
@click.option("--type", "-t", "chunk_type", default=None, help="Filter by chunk type.")
def search(project: str, query: str, top_k: int, language: str | None, chunk_type: str | None) -> None:
    """Search an indexed codebase with a natural language query."""
    from seance.search.engine import SearchEngine

    config = load_config()
    engine = SearchEngine(config)
    results = engine.search(
        project_name=project,
        query=query,
        top_k=top_k,
        language=language,
        chunk_type=chunk_type,
    )

    if not results:
        click.echo("No results found.")
        return

    for i, r in enumerate(results, 1):
        click.echo(f"\n{'─' * 60}")
        click.echo(f"  [{i}] {r.symbol_name} ({r.chunk_type}) — score: {r.score:.4f}")
        click.echo(f"  {r.file_path}:{r.start_line}-{r.end_line}")
        click.echo(f"{'─' * 60}")
        # Show first 10 lines of the chunk
        lines = r.text.splitlines()[:10]
        for line in lines:
            click.echo(f"  {line}")
        if len(r.text.splitlines()) > 10:
            click.echo(f"  ... ({len(r.text.splitlines()) - 10} more lines)")


@main.command(name="list")
def list_projects() -> None:
    """List all indexed projects."""
    from seance.storage.vectordb import VectorStore

    config = load_config()
    store = VectorStore(config)
    projects = store.list_projects()

    if not projects:
        click.echo("No projects indexed yet.")
        return

    for p in projects:
        click.echo(f"  {p['name']}: {p['chunks']} chunks")


@main.command()
def serve() -> None:
    """Start the MCP server (stdio transport)."""
    from seance.server import mcp
    mcp.run()
