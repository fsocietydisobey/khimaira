# Séance

Semantic codebase search as an MCP server. Indexes source code into vector embeddings using AST-aware chunking and serves natural language queries over those embeddings. Gives AI assistants the ability to answer vague questions like *"how does auth work?"* without knowing exact symbol names.

Built for [Claude Code](https://docs.anthropic.com/en/docs/claude-code), but works with any MCP-compatible client.

## Why "Séance"

A séance, in the 19th-century spiritualist sense, is a ritual for communing with what cannot be reached directly — a structured sitting to hear voices from behind a veil. The word comes from the French *séance* ("a sitting"), but by the time it reached English it carried a specific meaning: *a deliberate attempt to access the hidden*.

That's the tool's function. `grep` reaches what you can already name. Séance reaches what you can't — you describe what you're looking for conceptually (*"how does auth work?"*, *"where are retries handled?"*, *"code related to payment processing"*) and the vector index answers back from the part of the codebase you couldn't point at by name.

It pairs with its sibling tool in the ritual:

> *To find what is hidden, hold a séance. To bind the names you find, give them to [Scarlet](https://github.com/fsocietydisobey/scarlet).*

Séance summons. Scarlet inscribes. Together they form a loop — reveal the hidden, then fix its name in a permanent record.

## How it works

```mermaid
flowchart LR
    subgraph Indexing
        A[Source Files] --> B[Tree-sitter AST Parser]
        B --> C[Semantic Chunks]
        C --> D[Google Embedding API]
        D --> E[(Qdrant Vector DB)]
    end

    subgraph Search
        F[Natural Language Query] --> G[Google Embedding API]
        G --> H[Nearest Neighbor Search]
        E --> H
        H --> I[Ranked Code Results]
    end
```

### Indexing pipeline

```mermaid
flowchart TD
    A[Project Directory] --> B{Discover Files}
    B -->|.py .ts .tsx .js .jsx| C[Tree-sitter Parse]
    B -->|.gitignore respected| D[Skip ignored files]
    C --> E[Extract Semantic Chunks]
    E --> F[Functions / Methods / Classes]
    E --> G[Interfaces / Types]
    E --> H[React Components]
    E --> I[React Hooks]
    E --> J[Module-level Code]
    H --> K{Large component?<br/>>80 lines}
    K -->|Yes| L[Sub-chunk:<br/>logic / handlers / render]
    K -->|No| M
    F & G & I & J & L --> M[Prepend file path context]
    M --> N[Batch Embed via Google API]
    N --> O[Upsert into Qdrant]
    O --> P[Persistent on-disk storage]
```

### What gets chunked

Unlike naive line-based splitting, Séance uses **tree-sitter** to parse source code into an AST and extracts chunks at semantic boundaries. The chunker recognizes both general code structure and React-specific patterns.

| Chunk type | What it captures | Example |
|---|---|---|
| `function` | Standalone functions, helpers, utilities | `def hash_password(...)` |
| `class` | Full class definition including constructor and docstring | `class VectorStore:` |
| `method` | Individual methods within a class | `VectorStore.upsert_chunks(...)` |
| `interface` | TypeScript interface declarations | `interface SearchResult { ... }` |
| `type_alias` | TypeScript type aliases | `type Config = { ... }` |
| `module` | Module-level imports, constants, and statements | Top-of-file setup code |
| `component` | React components (PascalCase functions/consts) | `const LoginForm = () => { ... }` |
| `hook` | React custom hooks (functions prefixed with `use`) | `function useRecentItems() { ... }` |
| `component_logic` | Sub-chunk: hooks + state within a large component | `useState`, `useEffect` declarations |
| `component_handlers` | Sub-chunk: event handlers within a large component | `const handleSubmit = ...` |
| `component_render` | Sub-chunk: JSX return within a large component | The `return (...)` statement |

Each chunk carries metadata: file path, symbol name, symbol type, language, and line range — so search results are immediately actionable.

### React component sub-chunking

Modern React components are large (200+ lines) and mix state, effects, handlers, and JSX in one function body. Treating them as a single chunk dilutes semantic retrieval. When a component exceeds ~80 lines, Séance also emits **sub-chunks** alongside the full-component chunk:

```mermaid
flowchart LR
    A[DrawingHistory<br/>341-line component] --> B[component<br/>full body]
    A --> C[component_logic<br/>useState, useEffect]
    A --> D[component_handlers<br/>handleClick, fetchDrawings]
    A --> E[component_render<br/>return JSX]
```

This means a query for *"click handlers in the drawing list"* matches `component_handlers` precisely, instead of competing against 300 lines of surrounding noise. You can also filter search by `chunk_type=component_handlers` to restrict results to handler sections only.

### File path context in embeddings

Folder structure is strong semantic signal in feature-organized codebases. Before embedding, Séance prepends a context header to each chunk:

```
// File: frontend/src/features/auth/components/LoginForm.tsx
// Symbol: LoginForm (component)
// Language: typescript
const LoginForm = ({ onSuccess }) => { ... }
```

This lets the embedding model associate the code with its location in the project hierarchy, dramatically improving queries like *"login form in the auth feature"* or *"the dashboard user card"* where the answer is as much about *where* the code lives as *what* it does.

### Vector storage

Séance uses **Qdrant** in embedded mode — no Docker, no server process. The Qdrant engine runs inside the Python process and persists data to `~/.seance/qdrant/`. Each indexed project gets its own collection.

```mermaid
graph LR
    subgraph "~/.seance/qdrant/"
        A[meeting-scribe<br/>59 chunks]
        B[khimaira<br/>338 chunks]
        C[jeevy-portal<br/>...]
    end
```

## Supported languages

| Language | Extensions | Parser |
|---|---|---|
| Python | `.py` | `tree-sitter-python` |
| TypeScript | `.ts`, `.tsx` | `tree-sitter-typescript` |
| JavaScript | `.js`, `.jsx`, `.mjs` | `tree-sitter-javascript` |

## Installation

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- A [Google AI API key](https://aistudio.google.com/app/apikey) (for embeddings)

### Setup

```bash
git clone git@github.com:fsocietydisobey/seance.git
cd seance
uv sync
```

Set your Google AI API key:

```bash
export GOOGLE_AI_API_KEY=your_key_here
```

Or create a `.env` file (see `.env.example`).

## CLI usage

### Index a codebase

```bash
# Full index — parses, chunks, embeds, and stores everything
uv run seance index /path/to/project --name my-project

# Incremental reindex — only re-embeds files changed since last git commit
uv run seance reindex /path/to/project --name my-project
```

### Search

```bash
# Natural language search
uv run seance search my-project "how does authentication work"

# Filter by language
uv run seance search my-project "error handling" --language python

# Filter by chunk type
uv run seance search my-project "data models" --type class

# Limit results
uv run seance search my-project "API endpoints" -k 5
```

### List indexed projects

```bash
uv run seance list
```

### Start MCP server

```bash
uv run seance serve
```

## MCP integration

### Claude Code

Register Séance as an MCP server so Claude can search your codebases during conversations:

```bash
claude mcp add seance -- uv --directory /path/to/seance run seance serve
```

Once registered, Claude gets these tools:

```mermaid
graph TD
    A[Claude Code] -->|MCP protocol| B[Séance MCP Server]
    B --> C[semantic_search]
    B --> D[index_project]
    B --> E[reindex_changed]
    B --> F[find_similar]
    B --> G[list_projects]

    C -->|"'how does auth work?'"| H[Ranked code chunks<br/>with file paths + line numbers]
    D -->|"/path/to/project"| I[Full index of codebase]
    E -->|"git-diff aware"| J[Incremental update]
    F -->|"file + symbol name"| K[Similar code patterns]
    G --> L[All indexed projects + stats]
```

### MCP tools

| Tool | Description | Example |
|---|---|---|
| `semantic_search` | Natural language code search | *"payment processing flow"* |
| `index_project` | Full index of a project directory | Index before first search |
| `reindex_changed` | Incremental reindex via git diff | Fast update after code changes |
| `find_similar` | Find code similar to a given symbol | Duplicate detection, pattern finding |
| `list_projects` | List all indexed projects with chunk counts | Check what's indexed |

### Other MCP clients

Séance uses the standard [MCP protocol](https://modelcontextprotocol.io/) over stdio. Any MCP-compatible client can use it. Add to your client's MCP config:

```json
{
  "mcpServers": {
    "seance": {
      "command": "uv",
      "args": [
        "--directory", "/path/to/seance",
        "run", "seance", "serve"
      ]
    }
  }
}
```

## Architecture

```mermaid
graph TB
    subgraph "src/seance/"
        CLI[cli.py<br/>CLI entry point]
        SRV[server.py<br/>MCP server]
        CFG[config.py<br/>Config loader]

        subgraph "indexer/"
            CHK[chunker.py<br/>Tree-sitter AST chunking]
            EMB[embedder.py<br/>Google embedding API]
            PIP[pipeline.py<br/>Indexing orchestrator]
        end

        subgraph "search/"
            ENG[engine.py<br/>Query engine]
        end

        subgraph "storage/"
            VDB[vectordb.py<br/>Qdrant wrapper]
        end
    end

    CLI --> PIP
    CLI --> ENG
    CLI --> SRV
    SRV --> PIP
    SRV --> ENG
    PIP --> CHK
    PIP --> EMB
    PIP --> VDB
    ENG --> EMB
    ENG --> VDB
    CFG -.->|env vars| EMB
    CFG -.->|storage path| VDB
```

### Data flow

```mermaid
sequenceDiagram
    participant U as User / Claude
    participant S as Séance Server
    participant C as Chunker (tree-sitter)
    participant E as Embedder (Google API)
    participant Q as Qdrant (embedded)

    Note over U,Q: Indexing
    U->>S: index_project("/path/to/repo", "my-project")
    S->>C: Discover + parse source files
    C-->>S: 338 semantic chunks
    S->>E: Batch embed (50 texts/request)
    E-->>S: 338 × 3072-dim vectors
    S->>Q: Upsert chunks + vectors + metadata
    Q-->>S: Stored in "my-project" collection
    S-->>U: {file_count: 92, chunk_count: 338}

    Note over U,Q: Searching
    U->>S: semantic_search("how does auth work?", "my-project")
    S->>E: Embed query string
    E-->>S: 1 × 3072-dim vector
    S->>Q: Nearest neighbor search (top 10)
    Q-->>S: Ranked chunks with metadata
    S-->>U: [{file, symbol, lines, score, text}, ...]
```

## Configuration

All configuration via environment variables. See `.env.example` for the full list.

| Variable | Required | Default | Description |
|---|---|---|---|
| `GOOGLE_AI_API_KEY` | Yes | — | Google AI API key for embeddings |
| `SEANCE_STORAGE_DIR` | No | `~/.seance/` | Where Qdrant stores vector data |
| `SEANCE_EMBEDDING_MODEL` | No | `gemini-embedding-001` | Embedding model to use |
| `SEANCE_CHUNK_OVERLAP` | No | `2` | Line overlap between chunks |

## Rate limits

The Google AI free tier allows ~100 embedding texts per minute. Séance handles this automatically with retry + exponential backoff, but initial indexing of large codebases will be slow:

| Project size | Chunks | Free tier time | Paid tier time |
|---|---|---|---|
| Small (~15 files) | ~60 | ~2 seconds | ~2 seconds |
| Medium (~100 files) | ~350 | ~4 minutes | ~3 seconds |
| Large (~500 files) | ~2000 | ~20 minutes | ~10 seconds |

Enabling billing on your Google AI project eliminates the rate limit bottleneck. Alternatively, incremental reindex (`seance reindex`) only processes changed files and is fast regardless of project size.

## Tech stack

- **[tree-sitter](https://tree-sitter.github.io/)** — AST parsing for Python, TypeScript, JavaScript
- **[Qdrant](https://qdrant.tech/)** — Vector database (embedded mode, no Docker needed)
- **[Google Gemini Embedding API](https://ai.google.dev/)** — `gemini-embedding-001` (3072-dim vectors)
- **[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)** — Model Context Protocol server
- **[Click](https://click.palletsprojects.com/)** — CLI framework
- **[uv](https://docs.astral.sh/uv/)** — Package management and virtual environments

## License

MIT
