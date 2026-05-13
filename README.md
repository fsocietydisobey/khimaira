# khimaira

> Multi-model AI orchestration + LangGraph observability + multi-session collaboration for the terminal AI era.

khimaira is a dev framework that makes your terminal AI tool — Claude Code, Codex CLI, Gemini CLI, or local Ollama — 5–10× more efficient. Three things in one:

1. **Orchestrator** — pre-resolves task-relevant context, manages your dev stack with a debugger-attached browser, and routes every prompt to the cheapest competent model.
2. **LangGraph observer** — zero-touch venv-injected tracing for any LangGraph app. Auto-correlates runs, captures external HTTP (Roboflow, OpenAI, Anthropic), surfaces cost + slow calls + waterfall traces in a local dashboard.
3. **Multi-session shared state** — externalize session decisions, file touches, questions; coordinate parallel Claude/Codex/Gemini windows via targeted questions, real-time blocking calls, FYI notices, and forward-looking handoffs that survive across sessions.

**No API keys required to start; bring your own when you want premium models.**

---

## How it fits into your workflow

```mermaid
flowchart LR
    User([You]) -->|terminal| Shell["Claude Code · Codex CLI · Gemini CLI<br/>(any AI shell)"]
    Shell -->|MCP| Khimaira["⬢ khimaira<br/>orchestrator"]
    Khimaira -->|subprocess| Claude["claude"]
    Khimaira -->|subprocess| Codex["codex"]
    Khimaira -->|subprocess| Gemini["gemini"]
    Khimaira -->|subprocess| Ollama["ollama (local)"]
    Khimaira -->|subprocess| LLM["llm (Simon Willison's<br/>+ OpenRouter)"]
    Khimaira -.->|library| Scarlet["Scarlet<br/>cartography"]
    Khimaira -.->|library| Seance["Séance<br/>semantic search"]
    Khimaira -.->|MCP| Specter["Specter<br/>browser debug"]

    style Khimaira fill:#1f6feb,stroke:#0d419d,color:#fff
    style Ollama fill:#2da44e,stroke:#1a7f37,color:#fff
```

You drive your AI shell as usual. Khimaira is the layer that picks the right tool for each task and shrinks the prompt before it goes out.

---

## Three pillars

```mermaid
flowchart TB
    subgraph Khimaira["khimaira orchestrator"]
        direction LR
        P1["⓵ Context resolver<br/><sub>What files matter for THIS task?</sub>"]
        P2["⓶ Runtime manager<br/><sub>khimaira dev — full stack + Chrome + DB</sub>"]
        P3["⓷ AI dispatcher<br/><sub>Auto-route to cheapest competent CLI</sub>"]
    end

    P1 --> Out["minimal prompt<br/>(5-10× fewer tokens)"]
    P2 --> Browser["Chrome --remote-debugging-port"]
    P2 --> DB[("Postgres")]
    P2 --> Server["dev server (vite/next/uvicorn)"]
    P3 --> Routing["route → CLI runner<br/>+ usage tracker + budget"]

    style Khimaira fill:#0d1117,stroke:#1f6feb,color:#fff
    style P1 fill:#1f6feb,color:#fff
    style P2 fill:#1f6feb,color:#fff
    style P3 fill:#1f6feb,color:#fff
```

1. **Context resolver** — Séance (semantic search) + Scarlet (codebase cartography) + grep + filesystem heuristics. Answers *"what files actually matter?"* before anything hits the LLM. Where the 5-10× token reduction lives.
2. **Runtime manager** — `khimaira dev` starts your dev server, launches Chrome with `--remote-debugging-port` for Specter, ensures khimaira-monitor is up. One Ctrl-C tears it all down.
3. **AI dispatcher** — auto-router (AMR pattern) classifies each task and dispatches to the cheapest competent CLI runner: Claude Code, Codex, Gemini, Ollama, or `llm` (Simon Willison's, covers OpenRouter + 100+ providers).

---

## How a single task flows through khimaira

```mermaid
sequenceDiagram
    autonumber
    participant U as You
    participant S as AI CLI shell
    participant C as khimaira
    participant CR as Context resolver
    participant R as AMR router
    participant Run as CLI runner<br/>(claude/ollama/...)
    participant T as Usage tracker

    U->>S: "fix the auth bug where ..."
    S->>C: mcp__khimaira__task(description)
    C->>CR: resolve_context(task)
    CR-->>C: ContextBundle (3 files, 2.1k tok)
    C->>R: classify + route(task, context)
    R-->>C: claude / haiku-4-5 (trivial: $0.005 ceiling)
    C->>Run: run_claude(prompt, model=haiku-4-5)
    Run-->>C: RunnerResult
    C->>T: record(runner, model, tokens, cost)
    C-->>S: result + cost summary
    S-->>U: "fix applied. cost: $0.003"
```

Every dispatch is **classify → route → run → record**. The classifier is a small cheap call (~$0.0004); the savings from routing trivial tasks down-tier dwarf its cost.

---

## Why pure CLI substrate

```mermaid
flowchart LR
    Old["khimaira v1<br/>Anthropic API SDK"] -.->|deprecated| New["khimaira v2<br/>CLI subprocess only"]

    New --> Sub1["claude (Claude Code subscription)"]
    New --> Sub2["codex (OpenAI subscription)"]
    New --> Sub3["gemini (Google subscription)"]
    New --> Sub4["ollama (local — $0 marginal)"]
    New --> Sub5["llm + OpenRouter (mixed)"]

    Old -.->|"❌ surprise bills<br/>(fire_swarm $$$)"| Pain[("budget pain")]
    New -->|"✅ no API keys<br/>required"| Win[("dev-friendly")]

    style Old fill:#cf222e,color:#fff
    style New fill:#2da44e,color:#fff
    style Sub4 fill:#2da44e,color:#fff
```

**Pitch in one sentence:** *"khimaira orchestrates your terminal AI tools without ever making an API call of its own. No keys, no surprise bills, no external SDK dependencies."*

---

## Repository layout

```mermaid
flowchart TB
    Root[khimaira/<br/>workspace]
    Root --> P[packages/]
    Root --> S[shared/]
    Root --> A[apps/]
    Root --> D[docs/]

    P --> P1[khimaira<br/>orchestrator]
    P --> P2[scarlet<br/>cartography]
    P --> P3[seance<br/>semantic search]
    P --> P4[specter<br/>browser debug]

    S --> S1[khimaira-types<br/>schemas]
    S --> S2[khimaira-transport<br/>MCP/SSE helpers]

    A --> A1[monitor-ui<br/>React dashboard]

    style Root fill:#1f6feb,color:#fff
    style P1 fill:#1f6feb,color:#fff
```

Each `packages/<name>/` has both:
- a **library API** (`<name>.api.*`) for in-process use by khimaira
- an **MCP server** (`<name>.server.mcp`) for direct shell use

Same logic, two transports — like an SDK and a SQL interface to the same database engine.

---

## Quick start

### Three install paths, pick what matches you

**1. You have a khimaira "profile" YAML (you or another maintainer wrote one in dotfiles).** Fastest fresh-machine setup — one command brings the whole agent stack online (khimaira + sibling MCP servers + Claude rules/commands symlinks + supervisor + dashboard SPA):

```bash
git clone git@github.com:<you>/dotfiles.git ~/dotfiles
~/dotfiles/bootstrap.sh
```

See [Profile-driven setup](#profile-driven-setup) below for what the YAML declares. New devs can clone the example profile from this repo and adapt.

**2. You want khimaira and that's it.** No personal config, no sibling tools — just the khimaira CLI + MCP server on this box:

```bash
git clone https://github.com/fsocietydisobey/khimaira.git ~/dev/khimaira
cd ~/dev/khimaira
uv sync
uv run khimaira bootstrap   # uses khimaira-shipped default profile
```

`khimaira bootstrap` with no `--profile` arg runs the built-in baseline: registers khimaira as an MCP server with Claude Code, writes the khimaira SessionStart / UserPromptSubmit / PostToolUse hooks into `~/.claude/settings.json`, installs the host-native supervisor (systemd on Linux, launchd on macOS), builds the dashboard SPA.

**3. You're trying khimaira before committing.** Skip bootstrap, just register the MCP server manually:

```bash
# After uv sync above:
claude mcp add khimaira -s user -- bash -lc \
  'uv --directory ~/dev/khimaira run python -m khimaira.cli mcp'
```

Then `claude` and khimaira's MCP tools (`mcp__khimaira__*`) are available.

### Day-to-day commands

```bash
# Diagnose your environment (daemon up? supervisor active? hooks current?)
khimaira doctor

# Auto-routed dispatch (dry-run first to see what it'd do)
khimaira task --dry-run "rename this variable"

# Start the observability daemon (or use the installed supervisor)
khimaira monitor start
# → http://127.0.0.1:8740 (loopback only — that IS the auth layer)

# Spin up a project's full dev stack with one command
khimaira dev /path/to/project

# List every khimaira surface (CLI commands, MCP tools, slash commands, web routes)
khimaira tools
```

### Profile-driven setup

Profiles let you declare your portable agent setup in one YAML file checked into your dotfiles repo. Same profile applied on N machines yields N matching environments. Bootstrap reads the profile and:

- clones your dotfiles repo
- creates symlinks (`~/.claude/CLAUDE.md` → your dotfiles, etc.)
- clones declared sibling repos (e.g. seance, specter, scarlet) under `~/dev/`
- runs each repo's install command (`uv sync`)
- registers MCP servers with Claude Code
- writes khimaira hooks into `~/.claude/settings.json`
- installs the supervisor
- builds the dashboard SPA

```yaml
# khimaira-profile.yaml (in your dotfiles repo)
name: my-setup
dotfiles:
  repo: git@github.com:me/dotfiles.git
  path: ~/dotfiles
  symlinks:
    - { src: claude/CLAUDE.md, dest: ~/.claude/CLAUDE.md }
    - { src: claude/rules, dest: ~/.claude/rules }
    - { src: claude/commands, dest: ~/.claude/commands }
repos:
  - { name: khimaira, url: git@github.com:fsocietydisobey/khimaira.git, install: uv sync --all-packages }
mcp_servers:
  - name: khimaira
    command: uv --directory ~/dev/khimaira run python -m khimaira.cli mcp
supervisor:
  auto_install: true
install_claude_hooks: true
spa_build: true
```

Then on any machine: `khimaira bootstrap --profile <local-path-or-url>`. Idempotent — safe to re-run.

For ongoing cross-machine sync (after profile changes): `khimaira sync` from a terminal, or `/khimaira-configure` from inside any Claude Code session.

A complete example profile that does all of the above lives at [`tasks/bootstrap-profile/EXAMPLE-PROFILE.yaml`](tasks/bootstrap-profile/EXAMPLE-PROFILE.yaml).

### MCP tools

42+ MCP tools available across orchestration, monitor, process observability, and multi-session shared state. Discoverable via `khimaira tools --category mcp` — ranked by 7-day call count so the most-used tools surface first.

---

## Pillars in detail

### Pillar 1 — Context resolver

Pre-LLM "what's relevant?" — minimizes prompt before anything bills.

```mermaid
flowchart LR
    Task[user task] --> R[resolver]
    R --> S1["Séance<br/>(semantic vector)"]
    R --> S2["Scarlet<br/>(CLAUDE.md + dep graphs)"]
    R --> S3["grep<br/>(keyword fallback)"]
    R --> S4["fs heuristics<br/>(recently modified)"]
    S1 --> Merge[merge + score + budget]
    S2 --> Merge
    S3 --> Merge
    S4 --> Merge
    Merge --> Bundle[ContextBundle<br/>~3 files, ~2k tokens]

    style R fill:#1f6feb,color:#fff
    style Bundle fill:#2da44e,color:#fff
```

When Séance/Scarlet aren't installed, the resolver falls back to grep + fs heuristics. **Quality scales with what's available; the interface doesn't change.**

### Pillar 2 — Runtime manager

`khimaira dev` is the demoable wow-moment.

```mermaid
sequenceDiagram
    participant U as You
    participant CD as khimaira dev
    participant DS as dev server
    participant CR as Chrome+CDP
    participant M as monitor daemon
    participant SP as Specter

    U->>CD: khimaira dev /path/to/project
    CD->>CD: detect framework (vite/next/uvicorn)
    CD->>M: ensure running (start if not)
    CD->>DS: spawn (tracked process)
    DS-->>CD: "Local: http://localhost:5173"
    CD->>CR: launch with --remote-debugging-port
    CR-->>SP: ready for browser debug
    Note over U,SP: working state
    U->>CD: Ctrl-C
    CD->>CR: kill (registry order)
    CD->>DS: kill (registry order)
    CD-->>U: clean shutdown
```

Without `khimaira dev`, the same setup is 4-5 manual commands and orphaned processes when something crashes.

### Pillar 3 — AI dispatcher (AMR — automatic model router)

```mermaid
flowchart LR
    Task[task] --> Cl["Classifier<br/>cheap CLI<br/>(~$0.0004)"]
    Cl --> Class["TaskClassification<br/>type, complexity, model rec"]
    Class --> Rt[Router]
    Rt --> Avail[availability gate]
    Rt --> Priv[privacy gate<br/>KHIMAIRA_LOCAL_ONLY]
    Rt --> Bud[budget gate]
    Avail --> Pick{pick}
    Priv --> Pick
    Bud --> Pick
    Pick --> Disp[dispatch to chosen runner]

    style Cl fill:#1f6feb,color:#fff
    style Pick fill:#2da44e,color:#fff
```

The router picks among installed runners using a YAML routing table that ships with sensible defaults (overridable per-user / per-project).

---

## LangGraph observability

`khimaira attach <app-path>` injects a zero-touch observer into any Python project's venv. No source changes, no env vars, no installed deps in the app's manifest. Restart the app and every LangGraph node, every LLM call, every external HTTP request streams to khimaira-monitor in real time.

```mermaid
flowchart LR
    App["your LangGraph app<br/>(jeevy, etc.)"] -->|venv-injected<br/>khimaira_observer.pth| Obs["observer v0.4.1<br/>BaseCallbackHandler<br/>+ httpx/requests<br/>monkey-patches"]
    Obs -->|POST /api/heartbeat| Daemon["khimaira-monitor<br/>daemon"]
    Daemon -->|in-memory<br/>buffer + SSE| UI["monitor-ui<br/>(localhost:8740)"]
    Daemon -->|REST endpoints| CLI["khimaira observer<br/>trace · compare · slow"]

    style Obs fill:#1f6feb,color:#fff
    style Daemon fill:#1f6feb,color:#fff
```

### What you get out-of-box

| Surface | What it shows |
|---|---|
| `/{project}/topology` | Live LangGraph node-by-node execution + replay |
| `/{project}/cost` | Estimated USD spend by model, token counts, telemetry-overhead callout (LangSmith calls — opt out via `KHIMAIRA_DISABLE_LANGSMITH=true`) |
| `/{project}/trace/{cid}` | Waterfall view of one app run — chain/llm/tool/external bars on a time axis. The *exact* visualization that proves your `asyncio.gather` is actually concurrent (3 starts within ~10ms = textbook parallel) |
| `khimaira observer trace <p> <cid>` | Full event timeline as text |
| `khimaira observer compare <p> <cid-a> <cid-b>` | A/B per-node wall-time deltas with regression markers |
| `khimaira observer slow <p> --llm 5 --external 30` | Recent calls past per-kind threshold + in-flight stuck detection |

### Auto-correlation (zero app code changes)

Every event the observer emits gets auto-tagged with the LangGraph run's top-level `correlation_id`:

```mermaid
sequenceDiagram
    autonumber
    participant App as Your App
    participant LC as LangChain
    participant Obs as KhimairaTracer
    participant CV as ContextVar
    participant HX as httpx (patched)
    participant D as khimaira-monitor

    App->>LC: graph.invoke(state)
    LC->>Obs: on_chain_start(run_id=A, parent=None)
    Note over Obs,CV: parent=None → top-level<br/>set _correlation_id = A
    Obs->>D: chain_start, cid=A
    LC->>Obs: on_llm_start(run_id=B, parent=A)
    Obs->>CV: read cid (= A)
    Obs->>D: llm_start, cid=A
    Note over App,HX: app makes httpx.get() inside chain
    HX->>CV: read cid (= A — propagates via ContextVar)
    HX->>D: external_start, cid=A
    HX->>D: external_end, cid=A
    LC->>Obs: on_chain_end(run_id=A)
    Note over Obs,CV: top-level done → clear cid

    Note over D: GET /by-correlation/A returns ALL above events
```

```python
# Your app — UNCHANGED:
result = graph.invoke(state)
# Now queryable: GET /api/heartbeats/<project>/by-correlation/<run_id>
# returns every chain/llm/tool/external event for this run
```

The observer reads LangChain's `parent_run_id=None` signal on `on_chain_start`, sets a `ContextVar`, and `_enqueue` propagates it through async + thread boundaries to every downstream event including the HTTP monkey-patch interceptors. Override with `khimaira_observer.tag_run(my_id)` only when you want a domain-specific identifier (deliverable_id, business txn id) instead of the auto UUID.

### attach / detach

```bash
khimaira attach /path/to/your/langgraph/app
# drops khimaira_observer.pth + khimaira_observer/ into the venv's site-packages
# (gitignored — production builds don't include them)

khimaira attached
# list all attached projects + observer version per venv

khimaira detach /path/to/your/langgraph/app
```

The observer fails silent on every error path — apps must not break because of telemetry setup.

---

## Multi-session shared state

When one Claude Code session is grinding on a task, you can't ask related questions in another window without losing context. Khimaira externalizes session state so parallel sessions can collaborate — and so future sessions can pick up where stopped ones left off.

### Cross-session messaging — five primitives

```mermaid
flowchart TB
    subgraph Now["live coordination (sessions running NOW)"]
        Q["session_log_question<br/>(broadcast or targeted)"]
        W["session_wait_for_answer<br/>(blocking — same turn)"]
        N["session_post_notice<br/>(FYI, no reply expected)"]
        I["session_post_answer<br/>(B → A inbox)"]
    end

    subgraph Future["across-time coordination"]
        H["session_post_handoff<br/>(scoped by cwd —<br/>any future session sees it)"]
        T["session_query_transcript<br/>session_summarize_transcript<br/>(read what stopped sessions said)"]
    end

    style Now fill:#1f6feb,color:#fff
    style Future fill:#2da44e,color:#fff
```

| When you want to... | Use |
|---|---|
| Ask another *active* session a question, get answer in the same turn | `session_log_question(target_session_id=B)` + `session_wait_for_answer(qid)` |
| Tell another session "FYI, no reply needed" | `session_post_notice(target_session_id=B, text=...)` |
| Leave a note for **whoever opens the next chat in this project** | `session_post_handoff(text=..., scope_cwd=...)` — auto-surfaces on any future session's SessionStart hook in matching cwd |
| Read what a stopped session discussed about topic X | `session_query_transcript(session_id, query="X")` |
| Get the lay of the land of a stopped session before drilling in | `session_summarize_transcript(session_id, focus="X")` — heuristic, no LLM cost |
| Search past inbox notes (drained / acked / auto-expired) | `session_search_archive(session_id, query)` |

### Hooks (auto-surfacing — no manual polling required)

Two hooks ship with khimaira and install via `khimaira install-hooks`:

- **SessionStart** — auto-reads inbox + matched handoffs + lists other active sessions
- **UserPromptSubmit** — auto-fetches inbox notes (with surface-count + 3-turn auto-expire) AND incoming questions targeting this session, injects both into context every turn

You never manually call `session_pending_notes` mid-conversation; the loop is closed structurally.

### Example — real-time cross-session ask

```python
# Session A, mid-turn:
qid = session_log_question(
    session_id=ME,
    text="Roboflow per-page parallelization look right?",
    target_session_id="llm-piping-extension",  # routes to B's hook
)
answer = session_wait_for_answer(ME, qid, timeout=300)
# → A blocks. B's UserPromptSubmit hook surfaces the question on B's
#   next turn. B answers via session_post_answer. A unblocks instantly
#   and continues processing in the SAME turn.
```

### Example — real-time wait_for_answer flow

```mermaid
sequenceDiagram
    autonumber
    participant U as You
    participant A as Session A<br/>(running)
    participant Ch as khimaira daemon
    participant B as Session B<br/>(running)
    participant Bh as B's hook

    A->>Ch: log_question(target=B, "approach 1 or 2?")
    A->>Ch: wait_for_answer(qid, timeout=300)
    Note right of A: A blocks (long-poll)
    U->>B: types anything in B's window
    Bh->>Ch: GET /sessions/B/incoming
    Ch-->>Bh: 1 question targeting B
    Note over B: agent sees `📨 khimaira incoming`
    B->>Ch: post_answer(target=A, qid, "approach 2 because...")
    Ch-->>A: wait_for_answer returns
    Note left of A: A continues in SAME turn
```

User effort: 1 prompt to A (kicks off ask + wait), 1 prompt to B (B answers), A continues automatically. **No copy-paste relay, no cross-window context juggling.**

### Example — handoff to a future session

```python
session_post_handoff(
    from_session_id=ME,
    text="HANDOFF: shipped tasks #58-#65 + observer v0.4.1. Pickup
          tasks/workspaces/IMPLEMENTATION.md if you want workspace
          isolation. Restart jeevy backend to get auto-correlation.",
    scope_cwd="/home/_3ntropy/dev/khimaira",
)
```

```mermaid
flowchart LR
    A["session A<br/>(today, 5pm)"] -->|post_handoff| Disk[("~/.local/state/<br/>khimaira/handoffs.jsonl<br/>scope_cwd, expires_in=7d")]
    Disk -.->|3 days later| Boot[SessionStart hook<br/>in new session]
    Boot -->|cwd matches scope?| Match{match?}
    Match -->|yes + not in read_by| Inject["📦 khimaira handoffs<br/>injected into new agent's<br/>first context block"]
    Match -->|no| Skip[skip]

    style Disk fill:#1f6feb,color:#fff
    style Inject fill:#2da44e,color:#fff
```

### Reading what stopped sessions said

Claude can't be programmatically resumed — but the conversation transcripts are on disk at `~/.claude/projects/<project>/<uuid>.jsonl`. Two tools turn that into queryable knowledge:

```mermaid
flowchart LR
    Past["session A<br/>(now stopped)"] -.->|transcript<br/>on disk| File[("uuid.jsonl<br/>~42MB, 11K turns")]
    Now["session B<br/>(today)"] -->|session_summarize_transcript| Heur["heuristic digest<br/>(no LLM cost)<br/>tools used, files,<br/>recent prompts"]
    Now -->|session_query_transcript<br/>q='roboflow'| Grep["matched turns<br/>+ context lines"]
    File --> Heur
    File --> Grep

    style Heur fill:#2da44e,color:#fff
    style Grep fill:#2da44e,color:#fff
```

No new LLM API calls from khimaira daemon — the agent calling the tool can summarize via its own context if it wants to.

---

## Process observability — replace polling with one blocking call

```mermaid
flowchart LR
    Old["agent: cat log.txt<br/>cat log.txt<br/>cat log.txt<br/>... 30× per run"] -.->|wasteful| OldCost[("30 MCP roundtrips<br/>burns context window")]
    New["agent: wait_for_process(<br/>'tests',<br/>completion_signal=r'\\d+ passed'<br/>)"] -->|one call| NewCost[("1 blocking call<br/>returns when matched")]

    style Old fill:#cf222e,color:#fff
    style New fill:#2da44e,color:#fff
```

The khimaira daemon tails the process internally; the agent makes one blocking MCP call. Single roundtrip replaces dozens of polls.

---

## Status & roadmap

See [`tasks/BUILD-PLAN.md`](tasks/BUILD-PLAN.md) for full status. Cliff-notes:

| Phase | Status |
|---|---|
| 0 — Monorepo scaffold | ✅ |
| 1 — Shared types | ✅ |
| 2 — CLI runners (pure-CLI substrate) | ✅ |
| 3 — AMR (auto model router) | ✅ |
| 4 — Context resolver (with grep/fs fallbacks) | ✅ |
| 5 — `khimaira dev` runtime manager | ✅ |
| 6 — `khimaira task/route/doctor/monitor/mcp/dev` CLI | ✅ |
| 7 — Monitor daemon migration | ✅ |
| 8 — All 8 LangGraph patterns migrated | ✅ |
| 9 — Frontend (`apps/monitor-ui`) | ✅ |
| 10 — API removal (langchain_anthropic deprecated) | ✅ |
| 11 — Multi-session shared state | ✅ |
| 12 — Process observability | ✅ |
| 13 — MCP call telemetry | ✅ |
| 14 — Venv-injected observer (zero-touch LangGraph tracing) | ✅ v0.4.1 |
| 14b — Observer dashboards (cost / trace waterfall / slow alerts) | ✅ |
| 14c — Hooks for cross-session inbox + incoming + handoffs auto-surface | ✅ |
| 14d — Cross-session primitives (targeted Q, wait_for_answer, post_notice, post_handoff, archive, transcript query/summary) | ✅ |
| 4½ — Séance/Scarlet library APIs | ⬜ |
| Workspaces (multi-project session isolation) | ⬜ spec'd at `tasks/workspaces/` |
| Desktop notifications (libnotify push for cross-session events) | ⬜ spec'd at `tasks/desktop-notifications/` |
| React DevTools replacement (Vue-DevTools-quality UI for React) | ⬜ spec'd at `tasks/react-devtools/` |
| Burn-down savings dashboard widget | ⬜ |

---

## Status

Pre-alpha. Active development. Legacy version archived at [`fsocietydisobey/khimaira-legacy`](https://github.com/fsocietydisobey/khimaira-legacy) for historical reference.
