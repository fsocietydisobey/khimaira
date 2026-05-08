# chimera — Build Plan

> Cross-session tracker. Updated at the end of every session. Use this
> as the source of truth for "what's done vs. what's next."

**Status legend:**
- ✅ Done — implemented and tested
- 🟡 Scaffolded — structure in place, real implementation pending
- ⏳ In progress — being built now
- ⬜ Pending — not started

**Last updated:** 2026-05-08 (session 1 — phases 0-3, 6-8 done; session shared-state idea added as Phase 11)

---

## Vision (single source of truth)

```
[ user's terminal AI CLI ]      ← shell (Claude Code, Codex, Gemini CLI)
         ↓ MCP
    [ chimera ]                  ← orchestrator — never makes API calls itself
         ↓ subprocess
[ terminal AI CLIs (any) ]      ← brain — also subprocess-only
```

**Three pillars:**
1. **Context resolver** (Séance + Scarlet + Serena) — minimize prompt tokens
2. **Runtime manager** (`chimera dev`) — dev server + Chrome DevTools + Postgres
3. **AI dispatcher** (AMR auto-router) — pick cheapest competent runner per task

**Audience:** the 80% of devs who paste files into Claude Code, hit subscription
limits, and don't manually compose Séance/Scarlet/Specter. Pitch:
*"chimera makes your terminal AI tool 5–10× more efficient. Zero config to start.
Local model fills the gaps for free."*

---

## Phase 0 — Foundations

| Item | Status | Notes |
|---|---|---|
| Monorepo scaffold | ✅ | commit `0b1d901` — uv workspace, 4 packages + 2 shared |
| `docs/ARCHITECTURE.md` | ✅ | structural map captured |
| `tasks/BUILD-PLAN.md` | ✅ | this file |
| README + `.gitignore` + workspace `pyproject.toml` | ✅ | |

---

## Phase 1 — Shared types ✅ DONE

| Item | Status | Where | Notes |
|---|---|---|---|
| `TaskClassification` | ✅ | `shared/types/src/chimera_types/classification.py` | AMR classifier output |
| `FileContext` + `ContextBundle` | ✅ | `shared/types/src/chimera_types/context.py` | resolver output |
| `UsageRecord` | ✅ | `shared/types/src/chimera_types/usage.py` | now with `task_id` for per-task budgeting |
| `RoutingDecision` | ✅ | `shared/types/src/chimera_types/routing.py` | router output |
| `RuntimeStatus` | ✅ | `shared/types/src/chimera_types/runtime.py` | dev server / browser / DB status |

---

## Phase 2 — CLI Runners (the pure-CLI substrate) ✅ DONE

The only place chimera talks to LLMs. No API SDK calls anywhere else.

| Runner | Status | File | Notes |
|---|---|---|---|
| `CLIRunner` protocol | ✅ | `dispatch/runners/base.py` | |
| `claude` runner | ✅ | `dispatch/runners/claude.py` | + ClaudeAuthError hard-stop on credit-low/auth/rate-limit |
| `codex` runner | ✅ | `dispatch/runners/codex.py` | NEW |
| `gemini` runner | ✅ | `dispatch/runners/gemini.py` | |
| `ollama` runner | ✅ | `dispatch/runners/ollama.py` | NEW (local, free-tier) |
| `llm` runner | ✅ | `dispatch/runners/llm.py` | NEW (Simon Willison's, covers OpenRouter+rest) |
| `run_structured()` | ✅ | `dispatch/structured.py` | prompt-engineered + JSON-extract + retry-on-fail |
| `cli_available()`, subprocess helpers | ✅ | `dispatch/runners/base.py` | now permissive on non-zero exit (Claude needs JSON regardless) |

---

## Phase 3 — AMR (Automatic Model Router) ✅ DONE (core)

| Item | Status | Where | Notes |
|---|---|---|---|
| `classifier.py` (cheap-model task classifier) | ✅ | `dispatch/classifier.py` | Ollama-preferred → Claude Haiku fallback |
| `router.py` (classification → runner+model) | ✅ | `dispatch/router.py` | budget gate + privacy gate + availability fallback chain |
| `routing_table.yaml` (default routing matrix) | ✅ | `config/routing_table.yaml` | 10 task_types × 5 complexity_tiers |
| Config layered loader | ✅ | `config/__init__.py` | shipped → user → project deep-merge |
| Validator-gated escalation | ⬜ | `dispatch/escalation.py` | retry on bigger model when validator fails |
| `mcp__chimera__route` MCP tool | ⬜ | `server/tools/route.py` | classify-only, returns recommendation |
| `mcp__chimera__chain_auto` MCP tool | ⬜ | `server/tools/chain_auto.py` | end-to-end auto-routed dispatch |

---

## Phase 4 — Context Resolver (Pillar 1)

| Item | Status | Where | Notes |
|---|---|---|---|
| `resolver.py` | ⬜ | `context/resolver.py` | primitive: `resolve_context(task) → ContextBundle` |
| `relevance.py` | ⬜ | `context/relevance.py` | merge Séance + Scarlet + Serena scores |
| `budget.py` | ⬜ | `context/budget.py` | per-task token budget enforcement |
| `cache.py` | ⬜ | `context/cache.py` | memoize per (project, task-hash) |
| Séance library API | ⬜ | `packages/seance/src/seance/api/` | will use grep-fallback initially |
| Scarlet library API | ⬜ | `packages/scarlet/src/scarlet/api/` | will read existing CLAUDE.md initially |
| `seance_client.py` | ⬜ | `tools/seance_client.py` | `from seance.api import semantic_search` |
| `scarlet_client.py` | ⬜ | `tools/scarlet_client.py` | |

---

## Phase 5 — Runtime Manager (Pillar 2)

| Item | Status | Where | Notes |
|---|---|---|---|
| `chimera dev` command | ⬜ | `cli/dev.py` | one-command stack startup |
| `lifecycle.py` | ⬜ | `runtime/lifecycle.py` | start/stop everything |
| `dev_server.py` | ⬜ | `runtime/dev_server.py` | npm/pnpm/uv detection |
| `browser.py` | ⬜ | `runtime/browser.py` | Chrome `--remote-debugging-port` |
| `postgres.py` | ⬜ | `runtime/postgres.py` | discover + connect project DB |
| `logs.py` | ⬜ | `runtime/logs.py` | aggregate stdout/stderr |
| `healthcheck.py` | ⬜ | `runtime/healthcheck.py` | readiness probes |
| Specter integration | ⬜ | hooked from `runtime/browser.py` | |

---

## Phase 6 — chimera CLI commands

| Command | Status | File | Notes |
|---|---|---|---|
| `chimera task <description>` | ✅ | `cli/task.py` | end-to-end: classify → route → dispatch → record. Verified with --dry-run. |
| `chimera route <description>` | ✅ | `cli/route.py` | classify-only, prints routing decision JSON |
| `chimera doctor` | ✅ | `cli/doctor.py` | env diagnostic, lists available runners + modes |
| `chimera monitor {start,stop,restart,status,rescan}` | ✅ | `cli/monitor.py` | thin wrapper over migrated monitor.cli |
| Entry point (`chimera.cli:main`) | ✅ | `cli/__init__.py` | argparse dispatch |
| `chimera dev <project>` | ⬜ | `cli/dev.py` | (Phase 5) |
| `chimera init` | ⬜ | `cli/init.py` | first-run UX, detects + suggests Ollama |
| `chimera install --target` | ⬜ | `cli/install.py` | configures Claude Code / Gemini / Codex MCP |

---

## Phase 7 — Monitor migration (from chimera-legacy) ✅ DONE (core)

The observability daemon migrated cleanly — only 3 import patches needed
(ROOTS, runners). Daemon starts in new repo, /api/projects responds, all
endpoints accessible.

| Item | Status | Notes |
|---|---|---|
| `monitor/server.py` | ✅ | FastAPI on 127.0.0.1:8740 |
| `monitor/api/projects.py` | ✅ | |
| `monitor/api/topology.py` | ✅ | |
| `monitor/api/threads.py` (incl SSE) | ✅ | |
| `monitor/api/usage.py` | ✅ | |
| `monitor/api/anomalies.py` | ✅ | |
| `monitor/api/api_routes.py` | ✅ | FastAPI route extractor |
| `monitor/api/frontend_components.py` | ✅ | React component extractor |
| `monitor/api/schema_drift.py` | ✅ | |
| `monitor/anomalies.py` (self-watch) | ✅ | usage_rate + zombie checks active |
| `monitor/watchdog.py` (zombie detector) | ✅ | |
| `chimera.usage` module | ✅ | + `make_langchain_callback` for legacy node usage |
| `monitor/auto_fix.py` | ✅ | |
| `monitor/discovery/*` | ✅ | project + connection + topology discovery |
| `monitor/metadata/*` | ✅ | observation collector + scan |
| **NEW** `monitor/api/savings.py` | ⬜ | burn-down chart data |
| **NEW** `monitor/api/runtime.py` | ⬜ | dev/browser/db status |
| **NEW** `monitor/api/routing.py` | ⬜ | AMR decision log |

---

## Phase 8 — Patterns migration (from chimera-legacy) ✅ DONE

All 8 graph factories import cleanly. Server tools (MCP exposure) still
need wiring in Phase 6+9.

| Pattern | Designation | Status | Notes |
|---|---|---|---|
| SPR-4 | Sequential Phase Runner | ✅ | `chain_pipeline` graph factory |
| TFB | Tri-Force Balancer (inside SPR-4) | ✅ | 6 balanced force nodes |
| CLR | Closed-Loop Refiner | ✅ | `chain_refiner` graph |
| PDE | Parallel Dispatch Engine | ✅ | `swarm` graph |
| HVD | Hypervisor Daemon | ✅ | `chain_hypervisor` graph |
| **AMR** | **Automatic Model Router** | ✅ | **NEW — Phase 3 done** |
| ACL | Atomic Component Library | ✅ | `chain_components` graph |
| DCE | Dead Code Eliminator | ✅ | `chain_deadcode` graph |
| POB | Proactive Observation Builder | ✅ | `chain_toolbuilder` graph |
| `chimera.server.mcp` MCP entry point | ⬜ | exposes graph factories as MCP tools |

---

## Phase 9 — Frontend migration (apps/monitor-ui)

| Item | Status | Notes |
|---|---|---|
| Migrate `monitor_ui/` → `apps/monitor-ui/` | ⬜ | mostly file move |
| Trail rendering (already in legacy commit `7155061`) | ⬜ | brings into new repo |
| **NEW** Burn-down savings widget | ⬜ | shows "you saved X this week" |
| **NEW** Runtime status panel | ⬜ | dev/browser/db |
| **NEW** AMR routing decisions log | ⬜ | "this task routed to Y because Z" |

---

## Phase 11 — Multi-session shared state (Claude Code observability)

The gap: when one Claude Code session is grinding on a task, you can't ask
related questions in another window without losing the working session's
context. Forks (Agent tool) solve "background work" but not "side conversation
that sees what the working agent is doing." Chimera's already-existing
state-watching daemon makes the externalized-state version of this tractable.

| Item | Status | Where | Notes |
|---|---|---|---|
| `~/.local/state/chimera/sessions/<sid>/` JSONL store | ⬜ | new | decisions, files_touched, open_questions, status, **inbox** |
| `mcp__chimera__session_log_decision(text, why)` | ⬜ | `server/tools/session_*.py` | A's write |
| `mcp__chimera__session_log_question(text)` → returns question_id | ⬜ | | A's write — ID is the handle B uses to answer |
| `mcp__chimera__session_status(state)` | ⬜ | | A's status update: researching/implementing/blocked |
| **`mcp__chimera__session_post_answer(session_id, question_id, answer)`** | ⬜ | | **B → A write-back. Without this, B can only read A.** |
| `mcp__chimera__session_state(session_id)` | ⬜ | | B's read — full digest |
| `mcp__chimera__session_recent_decisions()` | ⬜ | | B's read across all sessions |
| **`mcp__chimera__session_pending_notes(session_id)`** | ⬜ | | **A's inbox read — answers B has posted that A hasn't seen** |
| **PostToolUse hook → auto session_log_touch** | ⬜ | `~/.claude/hooks/` | **Free file-touch logging. Highest-leverage automation: agent burden = 0, log is always-correct.** Diff-parses Edit/Write/MultiEdit tool params. |
| **Periodic `<chimera:reminder>` injection** | ⬜ | UserPromptSubmit hook every N turns | Nudges agent to log decisions/questions. NOT auto-extracted from prose — extraction unreliable. |
| **SessionStart hook → call session_pending_notes** | ⬜ | `~/.claude/hooks/` | When A wakes up, surfaces "B answered Q3 while you were running" without user having to ask. Closes the inbox-read loop. |
| `/api/sessions` endpoint | ⬜ | `monitor/api/sessions.py` | dashboard view: all active sessions, their state, pending answers |

**Critical design notes (incorporated from review):**

1. **File-touch is automated; decisions/questions are nudged.** PostToolUse hook on Edit/Write/MultiEdit captures every file mutation with line ranges — zero agent burden, can't be forgotten. Decisions and questions require the agent to recognize "this is a decision" — extraction from prose is unreliable, so we inject a periodic reminder rather than auto-extract.

2. **The write-back path is symmetric.** B → A communication is NOT optional. Without `session_post_answer` + `session_pending_notes` + the SessionStart auto-read, the design collapses to "B reads A, then human relays" — which only solves half the problem. Both directions plumbed from day 1.

3. **Inbox-read should be automatic.** SessionStart hook calls `session_pending_notes` and surfaces unread answers in the system prompt. The agent sees "session B answered Q3" without the user having to know to ask.

4. **Estimated scope:** ~300-400 LOC + 2-3 Claude Code hook scripts. Reuses existing chimera-monitor daemon, JSONL pattern, FastMCP server scaffolding.

---

## Phase 12 — Process observability (replace agent polling with blocking SSE)

The gap: Claude Code (and similar agents) polls long-running processes via
repeated `cat <log>` or `tail` calls. A 5-minute test run → 30+ MCP roundtrips
where 1 would suffice. Each poll burns context window space and time.

The fix: chimera daemon tails the process internally; agents make ONE
blocking MCP call that resolves when the process completes or a pattern
matches. SSE under the hood, MCP tool blocks above it.

| Item | Status | Where | Notes |
|---|---|---|---|
| Process registry in monitor daemon | ⬜ | `monitor/processes.py` | dict {label: ProcessHandle}; spawn + capture stdout/stderr |
| `mcp__chimera__spawn_process(cmd, label, cwd, env)` | ⬜ | server tool | starts a tracked process; returns {label, pid} |
| `mcp__chimera__wait_for_process(label, completion_signal, timeout_s)` | ⬜ | server tool | **BLOCKS until pattern in output OR exit OR timeout. Returns full output + exit code.** This is the polling-replacement primitive. |
| `mcp__chimera__follow_process(label, lines_per_chunk, max_chunks)` | ⬜ | server tool | yields chunks via MCP streaming if supported; otherwise returns when buffer fills |
| `mcp__chimera__list_processes()` | ⬜ | server tool | what's currently being tracked |
| `mcp__chimera__kill_process(label)` | ⬜ | server tool | clean shutdown |
| `/api/processes/<label>/stream` SSE | ⬜ | `monitor/api/processes.py` | for the dashboard view |
| Dashboard panel: live process output | ⬜ | `apps/monitor-ui/src/components/processes/` | tabbed view of all tracked processes |

**Use cases this unblocks:**

1. **Wait for tests:** `wait_for_process("npm-test", completion_signal=r"\d+ passed|\d+ failed", timeout_s=300)` — single tool call replaces polling loop.
2. **Wait for dev server ready:** `wait_for_process("dev-server", completion_signal=r"Local: http", timeout_s=30)` — agent kicks off `chimera dev`, waits one call for the server to be up before it interacts with the browser.
3. **Wait for build:** `wait_for_process("vite-build", completion_signal=r"built in", timeout_s=120)`.
4. **Long migrations:** `wait_for_process("alembic-upgrade", completion_signal=r"Done|Error", timeout_s=600)`.

**Estimated scope:** ~250-350 LOC (process registry + 5 MCP tools + 1 API
route + dashboard panel). Plumbs through the existing monitor daemon — no
new long-running services.

**Sequencing:** lands cleanly AFTER Phase 5 (Runtime manager), since
`chimera dev` will be a heavy user of the process registry.

---

## Phase 10 — API removal (the deprecation path)

The dev-tool pitch requires "no API keys, no surprise bills." Migration sequence:

| Step | Status | Notes |
|---|---|---|
| Flip every node default to CLI | ⬜ | API stays as opt-in (`CHIMERA_USE_API=true`) |
| Build `run_structured` helper | ⏳ | (Phase 2) |
| Migrate API-using nodes one at a time | ⬜ | watching parse-failure rate via usage tracker |
| Delete API provider code | ⬜ | once parse-failures stabilize <1% |
| Remove `langchain_anthropic` dep | ⬜ | |

API-using nodes inventory (from legacy):
- `validator`, `supervisor`, `critic`, `stress_tester`, `scope_analyzer`, `arbitrator`, `retry_controller`, `compliance`, `refiner/classifier`, `swarm/task_decomposer`, `hypervisor_dispatcher`, `toolbuilder/friction`, `toolbuilder/proposer`, `nodes/balanced/integration_gate`

---

## Known footguns (address before public release)

### Monitor auto-scan kicks off Claude Opus on daemon startup

When `chimera monitor start` runs, the metadata scanner queues every
discovered project for an LLM-driven scan. Defaults to **Claude Opus 4.7
on a ~94k-char prompt = ~$1.40 per project, per fresh start.**

For a dev-tool that's pitching "no surprise bills," this is a problem.
First-run experience burns ~$3 on chimera + 1 other project before the
user has even queried anything.

**Fix candidates (do one before public release):**
1. Default `CHIMERA_MONITOR_SCAN_MODEL=gemini` — uses Gemini CLI (subscription) instead of Anthropic API (per-call billing).
2. Make auto-scan opt-in: env var `CHIMERA_AUTO_SCAN=1` to enable; default off until user runs `chimera monitor scan` manually.
3. Skip scan entirely under `CHIMERA_LOCAL_ONLY=1`.
4. Use AMR routing (Phase 3) for the scan call instead of hardcoded Opus — let the auto-router pick a cheaper model.

**Recommendation:** option 4 once AMR's escalation path is solid; option 2
as the conservative interim default (better UX surprise: "no scan ran, run
this command" vs "$3 vanished without consent").

---

## Decisions & rationale (sticky notes)

- **Repo:** `chimera` reclaimed; old code lives in `fsocietydisobey/chimera-legacy` (archived).
- **Naming:** Sigil/Séance/Scarlet/Specter retained for now. Marketing-readiness later.
- **Substrate:** pure CLI subprocess. No API SDK calls in the tree (eventual goal).
- **Library mode:** each perception package exposes `<pkg>.api.*` for in-process import + `<pkg>.server.mcp` for direct shell use. Same logic, two transports.
- **Workspace:** uv workspaces. Each package independently versionable + publishable.
- **Default audience:** single-CLI-subscription dev (most devs). Local-Ollama as the cost-relief story. Multi-provider routing is a power-user feature.
- **Auto-router:** AMR pattern. Classifier on cheap runner; router picks runner+model; validator-gated escalation when output fails quality bar.

---

## Next session pickup

Sorted by priority for resuming:

1. Finish whatever's marked ⏳ in this file
2. Write tests for the runners + AMR (currently untested)
3. Migrate monitor + patterns (mechanical moves from legacy)
4. Begin context resolver (Phase 4)
5. Begin runtime manager (Phase 5)
