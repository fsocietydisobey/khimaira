# chimera

> Multi-model AI orchestration for the terminal AI era.

chimera is a dev framework that makes your terminal AI tool — Claude Code, Codex CLI, Gemini CLI, or local Ollama — 5–10× more efficient. It pre-resolves task-relevant context, manages your dev stack with a debugger-attached browser, and routes every prompt to the cheapest competent model. No API keys required to start; bring your own when you want premium models.

## Architecture

Three pillars over a pure-CLI substrate:

- **Context resolver** — Séance (semantic search) + Scarlet (codebase cartography) + Serena (LSP) deliver only the files that matter for each task.
- **Runtime manager** — `chimera dev` spins up your dev server, a Chrome with `--remote-debugging-port` attached, and your project Postgres in one command.
- **AI dispatcher** — auto-router classifies each task and dispatches to your terminal AI tool of choice (with Ollama as the free local fallback).

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full structural map.

## Packages

| Package | Purpose |
|---|---|
| [`chimera`](packages/chimera) | The orchestrator — patterns, runtime, dispatcher |
| [`scarlet`](packages/scarlet) | Codebase cartography (CLAUDE.md, dep graphs, invariants) |
| [`seance`](packages/seance) | Semantic search via vector embeddings |
| [`specter`](packages/specter) | Browser debugging via Chrome DevTools Protocol |

Each package is independently installable. Each has both a library API (in-process import) and an MCP server (for direct shell use).

## Status

Pre-alpha. Active scaffolding. The legacy version lives at [`fsocietydisobey/chimera-legacy`](https://github.com/fsocietydisobey/chimera-legacy) for archeology purposes.
