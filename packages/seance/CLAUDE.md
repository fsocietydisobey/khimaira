# Project: Séance

> **Now part of the khimaira monorepo (NORTH_STAR Phase 0, 2026-05-13).**
> Séance's tools are exposed via khimaira's unified MCP server under
> source-prefixed names: `mcp__khimaira__seance_semantic_search`,
> `mcp__khimaira__seance_index_project`, etc. The standalone
> `seance serve` command below remains for backward compat and
> isolation testing, but the canonical install path is through
> khimaira (`uvx khimaira mcp`).

Semantic codebase search MCP server. Indexes source code into vector embeddings using AST-aware chunking (tree-sitter) and serves natural language queries over those embeddings via the MCP protocol. Gives AI assistants (Claude Code, etc.) the ability to answer vague questions like "how does auth work?" without knowing exact symbol names.

## Commands

```bash
uv run seance index <project-path>          # Index a codebase (full)
uv run seance reindex <project-path>        # Incremental reindex (git-diff aware)
uv run seance search <project> <query>      # Search from CLI
uv run seance list                          # List indexed projects
uv run seance serve                         # Start MCP server (stdio)
```

## Architecture

```
src/seance/
  __init__.py
  cli.py                  # CLI entry point (index, search, serve, list)
  server.py               # MCP server (tools: semantic_search, index_project, etc.)
  config.py               # Config loader (env vars, defaults)
  indexer/
    __init__.py
    chunker.py             # Tree-sitter AST chunking (Python, TypeScript, JS)
    embedder.py            # Embedding interface (Google text-embedding-004)
    pipeline.py            # Orchestrates: parse → chunk → embed → store
  search/
    __init__.py
    engine.py              # Query engine: embed query → ChromaDB nearest neighbor
  storage/
    __init__.py
    vectordb.py            # ChromaDB wrapper (persistent, per-project collections)
```

## Conventions

### Python
- Python 3.12+. Modern syntax: `str | None`, `list[str]`, `dict[str, Any]`.
- Async where needed (MCP server), sync for indexing pipeline.
- Type hints on all function signatures.
- Imports: stdlib → third-party → `seance.*` (absolute imports).
- Format with `black` after every change.
- Use `uv add <package>` to add dependencies.

### Chunking strategy
- AST-based via tree-sitter. One chunk per semantic unit:
  - Function/method → one chunk
  - Class signature (without method bodies) → one chunk
  - Module-level statements → one chunk
- Each chunk carries metadata: file path, symbol name, symbol type, language, line range.
- Chunks are what get embedded and stored. Queries match against chunks.

### Embedding
- Google `text-embedding-004` via `google-genai` SDK.
- Batch embedding calls (up to 100 texts per request) to minimize API round trips.
- Embedding dimension: 768.

### Storage
- ChromaDB (embedded mode, persistent to disk).
- One collection per indexed project.
- Storage location: `~/.seance/` by default, configurable via `SEANCE_STORAGE_DIR`.
- Metadata stored per chunk: file_path, symbol_name, symbol_type, language, start_line, end_line, last_indexed.

### MCP Tools
- `semantic_search(query, project, top_k=10)` — natural language search over indexed codebase
- `index_project(path, name)` — full index of a project
- `reindex_changed(project)` — incremental reindex using git diff
- `find_similar(project, file, symbol, top_k=5)` — find similar code to a given symbol
- `list_projects()` — list all indexed projects with stats

### Indexing
- Git-aware: respects `.gitignore`, skips `.git/`, `node_modules/`, `.venv/`, `__pycache__/`.
- Incremental: `git diff --name-only` to find changed files, re-embed only those.
- First index of a large project (~3000 chunks) should complete in under 60 seconds.

## Things to avoid

- Don't embed binary files, images, or lock files.
- Don't chunk by line count — always use AST boundaries.
- Don't store raw source code in ChromaDB — store only the text chunk and metadata. The source file is the source of truth.
- Don't add dependencies without checking if an existing one covers the need.
- Don't commit `.env` files or API keys.
