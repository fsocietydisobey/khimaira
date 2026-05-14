# Project: Scarlet

Codebase cartographer MCP server. Walks any project, extracts structural metadata via tree-sitter, and generates the documentation scaffolding AI assistants need: per-feature `CLAUDE.md` files, barrel exports (`index.js`/`index.ts`), dependency graphs (Mermaid), and symbol manifests.

The dark cousin of Séance: where Séance summons knowledge from the dead code via vector search, Scarlet inscribes the names of every entity into a permanent record. *"To find what is hidden, hold a séance. To bind the names you find, give them to Scarlet."*

## Commands

```bash
uv run scarlet scan <project-path>            # Survey project, return structure
uv run scarlet describe <project> <feature>   # Generate/refresh one feature CLAUDE.md
uv run scarlet sync <project>                 # Refresh all feature CLAUDE.md files
uv run scarlet barrel <project> <feature>     # Generate index.js barrel export
uv run scarlet graph <project>                # Generate Mermaid dependency graph
uv run scarlet lint <project>                 # Validate CLAUDE.md files for staleness
uv run scarlet serve                          # Start MCP server (stdio)
```

## Architecture

```
src/scarlet/
  __init__.py
  cli.py                        # CLI entry point (Click)
  server.py                     # MCP server (FastMCP) — exposes tools to Claude
  config.py                     # Config loader (.scarlet.yml + env vars)
  analyzer/
    __init__.py
    project.py                  # analyze_project — detect framework, structure
    features.py                 # scan_features — list feature folders, count entities
    metadata.py                 # extract_feature_metadata — parse, extract structured data
    imports.py                  # walk_imports — for dep graph and consumer analysis
  generator/
    __init__.py
    barrel.py                   # generate_barrel — create index.js/.ts barrel exports
    claude_md.py                # generate/update CLAUDE.md skeletons
    dep_graph.py                # generate Mermaid/JSON dependency graph
    template.py                 # template engine for CLAUDE.md skeleton
  validator/
    __init__.py
    linter.py                   # lint_claude_md — staleness checks, schema validation
```

## Conventions

### Python
- Python 3.12+. Modern syntax: `str | None`, `list[str]`, `dict[str, Any]`.
- Type hints on all signatures.
- Imports: stdlib → third-party → `scarlet.*` (absolute imports).
- Format with `black` after every change.
- Use `uv add <package>` for dependencies.

### Separation of concerns (CRITICAL)
Scarlet's core principle: **the deterministic parts go in Python, the judgment parts stay in the LLM.**

- **Deterministic (Python tools):** parsing, metadata extraction, barrel generation, dep graphs, lint checks, diff output. Anything an AST can answer.
- **Judgment (LLM via tools):** synthesizing vocabulary from code, identifying which gotchas matter, writing prose explanations. Anything requiring interpretation.

The tools generate **structure**. Claude generates **meaning**. On every CLAUDE.md update, the auto-derivable sections refresh and the human/Claude-written sections (Vocabulary, Conventions, Common tasks, Gotchas) are preserved.

### MCP Tools exposed
- `analyze_project(path)` — detect project type, framework, state mgmt, folder strategy
- `scan_features(path)` — list feature folders with their state (has CLAUDE.md? has barrel? component count?)
- `extract_feature_metadata(path, feature)` — parse a feature, return structured data (exports, hooks, slices, consumers)
- `generate_barrel(path, feature)` — create the `index.js` barrel export from analysis
- `build_claude_md_skeleton(path, feature)` — generate skeleton CLAUDE.md with auto-sections filled
- `update_feature_claude_md(path, feature)` — refresh auto-sections, preserve human sections
- `generate_dep_graph(path, format)` — output Mermaid/JSON dependency graph
- `lint_claude_md(path, feature)` — check for staleness, missing sections, broken references

### Project config (.scarlet.yml)
Each project that uses Scarlet drops a `.scarlet.yml` at its root to declare conventions:

```yaml
project_type: nextjs
state_management: redux-toolkit
test_framework: jest
features_root: frontend/src/features
study_guides_path: frontend/study-guides
barrel_export_strategy: re_export_default
claude_md_template: |
  # Feature: {feature_name}
  ...
```

This makes Scarlet generic across projects rather than baked to one codebase.

## Things to avoid

- **Don't generate prose.** The Vocabulary, Conventions, and Gotchas sections of feature CLAUDE.md files come from human/AI judgment, not code parsing. Auto-write only what's derivable from the AST.
- **Don't clobber human-written content.** On update, preserve everything between `<!-- BEGIN MANUAL -->` / `<!-- END MANUAL -->` markers.
- **Don't add dependencies without checking** if an existing one covers the need.
- **Don't index or store source code** — Scarlet is stateless. It walks, extracts, and writes. No vector DB, no persistent storage.
