# Architecture

> The end-state structural map of khimaira. This is the build target, not
> the current state — most directories below are placeholders awaiting
> migration from `khimaira-legacy` and original implementation.

## Vision

```
[ user's terminal AI CLI ]      ← shell (Claude Code, Codex, Gemini CLI)
         ↓ MCP
    [ khimaira ]                  ← orchestrator — never makes API calls itself
         ↓ subprocess
[ terminal AI CLIs (any) ]      ← brain — also subprocess-only
```

khimaira composes three pillars over a pure-CLI substrate:

1. **Context resolver** — Séance + Scarlet + Serena answer *"what files actually matter for this task?"* before anything hits the LLM. This is where 5–10× token reduction lives.
2. **Runtime manager** — `khimaira dev` spins up the dev server, Chrome with `--remote-debugging-port`, project Postgres, and hooks Specter to the browser. One command tears it all down.
3. **AI dispatcher** — auto-router (AMR pattern) classifies each task with a cheap model and dispatches to the appropriate CLI runner: Claude Code, Codex CLI, Gemini CLI, Ollama (local), or `llm` (Simon Willison's, covers OpenRouter and 100+ providers).

## Repository Layout

```
khimaira/                              # monorepo root, uv workspace
│
├── packages/                         # 4 publishable packages
│   ├── khimaira/                      # the orchestrator
│   ├── scarlet/                      # codebase cartography
│   ├── seance/                       # semantic search
│   └── specter/                      # browser debug
│
├── shared/
│   ├── types/                        # cross-package schemas (khimaira_types)
│   └── transport/                    # MCP + SSE helpers (khimaira_transport)
│
├── apps/
│   └── monitor-ui/                   # the observability dashboard (React)
│
├── docs/
├── scripts/
└── tasks/
```

Each perception package (`scarlet`, `seance`, `specter`) exposes BOTH a library API (`<pkg>.api.*` for in-process import by khimaira) AND an MCP server (`<pkg>.server.mcp` for direct shell use). Same logic, two transports — the model is "SDK or SQL, same engine."

## khimaira package internals

```
packages/khimaira/src/khimaira/
├── cli/                              # the 4 user-facing commands
│   ├── init.py                       #   khimaira init  → first-time setup
│   ├── dev.py                        #   khimaira dev   → spin up the stack
│   ├── task.py                       #   khimaira task  → context-resolved dispatch
│   └── doctor.py                     #   khimaira doctor → diagnose env
│
├── context/                          # ★ PILLAR 1
│   ├── resolver.py                   #   resolve_context(task) → ContextBundle
│   ├── relevance.py                  #   merge Séance + Scarlet + Serena scores
│   ├── budget.py                     #   per-task token budget
│   └── cache.py                      #   memoize per (project, task-hash)
│
├── runtime/                          # ★ PILLAR 2
│   ├── lifecycle.py
│   ├── dev_server.py
│   ├── browser.py
│   ├── postgres.py
│   ├── logs.py
│   └── healthcheck.py
│
├── dispatch/                         # ★ PILLAR 3
│   ├── classifier.py                 #   cheap model → TaskClassification
│   ├── router.py                     #   classification → runner+model
│   ├── escalation.py                 #   validator-gated retry
│   ├── structured.py                 #   prompt-engineered structured output
│   └── runners/                      #   ALL CLI subprocess
│       ├── claude.py
│       ├── codex.py
│       ├── gemini.py
│       ├── ollama.py
│       └── llm.py
│
├── patterns/                         # ARCHITECTURE — task-processing graphs
│   ├── spr4/                         #   SPR-4: phased pipeline
│   ├── clr/                          #   CLR: closed-loop refiner
│   ├── pde/                          #   PDE: parallel dispatch (swarm)
│   ├── hvd/                          #   HVD: hypervisor / pattern selector
│   ├── amr/                          #   AMR: auto model router
│   ├── acl/                          #   ACL: atomic component library
│   ├── dce/                          #   DCE: dead code eliminator
│   └── pob/                          #   POB: proactive observation builder
│
├── nodes/                            # node factories per pattern
├── tools/                            # library imports of perception packages
├── monitor/                          # observability daemon (FastAPI on 127.0.0.1:8740)
├── server/                           # MCP server — what khimaira exposes
├── config/                           # YAML config + routing matrix + budgets
├── core/                             # state, guards, memory, fitness
└── prompts/
```

## Runners — the only LLM call sites

`dispatch/runners/` is the only place khimaira talks to LLMs. No `langchain_anthropic`, no API SDKs anywhere else in the tree. Pure subprocess. This is what makes the "no API keys required" pitch true.

| Runner | Subprocess | Tier | Cost model |
|---|---|---|---|
| `claude` | `claude -p "..."` | CLI | subscription |
| `codex` | `codex exec "..."` | CLI | subscription |
| `gemini` | `gemini -p "..."` | CLI | subscription |
| `ollama` | `ollama run <model> "..."` | Local | $0 marginal |
| `llm` | `llm -m <model> "..."` | CLI (Simon Willison's; covers OpenRouter, etc) | provider-specific |

## Patterns

| Pattern | Designation | What it does |
|---|---|---|
| SPR-4 | Sequential Phase Runner | 4-phase pipeline with balanced forces |
| CLR | Closed-Loop Refiner | Continuous evolution loop |
| PDE | Parallel Dispatch Engine | Parallel swarm dispatch |
| HVD | Hypervisor Daemon | Meta-orchestrator, picks patterns |
| **AMR** | **Automatic Model Router** | **Picks model+runner per task (Cursor's Auto, transparent)** |
| ACL | Atomic Component Library | Immutable atomic primitives |
| DCE | Dead Code Eliminator | Dead code purging |
| POB | Proactive Observation Builder | Proactive tool-builder |

## Audience

The 80% of devs who don't optimize their AI workflow — who paste files into Claude Code, hit subscription limits, and don't know how to compose Séance/Scarlet/Specter manually. The pitch: *"khimaira makes your terminal AI dev tool 5–10× more efficient. Zero config to start. Local model fills the gaps for free."*
