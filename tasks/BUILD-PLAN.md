# khimaira — Build Plan

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
    [ khimaira ]                  ← orchestrator — never makes API calls itself
         ↓ subprocess
[ terminal AI CLIs (any) ]      ← brain — also subprocess-only
```

**Three pillars:**
1. **Context resolver** (Séance + Scarlet + Serena) — minimize prompt tokens
2. **Runtime manager** (`khimaira dev`) — dev server + Chrome DevTools + Postgres
3. **AI dispatcher** (AMR auto-router) — pick cheapest competent runner per task

**Audience:** the 80% of devs who paste files into Claude Code, hit subscription
limits, and don't manually compose Séance/Scarlet/Specter. Pitch:
*"khimaira makes your terminal AI tool 5–10× more efficient. Zero config to start.
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
| `TaskClassification` | ✅ | `shared/types/src/khimaira_types/classification.py` | AMR classifier output |
| `FileContext` + `ContextBundle` | ✅ | `shared/types/src/khimaira_types/context.py` | resolver output |
| `UsageRecord` | ✅ | `shared/types/src/khimaira_types/usage.py` | now with `task_id` for per-task budgeting |
| `RoutingDecision` | ✅ | `shared/types/src/khimaira_types/routing.py` | router output |
| `RuntimeStatus` | ✅ | `shared/types/src/khimaira_types/runtime.py` | dev server / browser / DB status |

---

## Phase 2 — CLI Runners (the pure-CLI substrate) ✅ DONE

The only place khimaira talks to LLMs. No API SDK calls anywhere else.

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
| `mcp__khimaira__route` MCP tool | ⬜ | `server/tools/route.py` | classify-only, returns recommendation |
| `mcp__khimaira__chain_auto` MCP tool | ⬜ | `server/tools/chain_auto.py` | end-to-end auto-routed dispatch |

---

## Phase 4 — Context Resolver (Pillar 1) ✅ DONE (with fallbacks)

| Item | Status | Where | Notes |
|---|---|---|---|
| `resolve_context(task)` primitive | ✅ | `context/resolver.py` | Always returns a ContextBundle, even with no perception tools installed |
| Séance source (semantic search) | 🟡 | inline | Tries `from seance.api.search import semantic_search`; falls back when unavailable |
| Scarlet source (cartography) | 🟡 | inline | Tries `from scarlet.api.feature_metadata import scan_features`; reads existing CLAUDE.md files when unavailable |
| Grep source (keyword fallback) | ✅ | inline | Always available; ripgrep when on PATH |
| Filesystem source (recently modified) | ✅ | inline | Files modified in last 7 days, decaying score |
| Score merge + budget truncation | ✅ | inline | Multi-source bonus, sort by relevance, cap at max_files + budget_chars |
| Verified end-to-end | ✅ | | 383ms / 8 files / multi-source merge working |
| **Séance library API** | ⬜ | `packages/seance/src/seance/api/` | Hooks in transparently when added (resolver tries import; falls back gracefully) |
| **Scarlet library API** | ⬜ | `packages/scarlet/src/scarlet/api/` | Same pattern — drop in, no resolver change needed |

---

## Phase 5 — Runtime Manager (Pillar 2) ✅ DONE (core)

| Item | Status | Where | Notes |
|---|---|---|---|
| `khimaira dev <project>` | ✅ | `cli/dev.py` | full lifecycle: detect → spawn → wait-ready → launch browser → wait-for-SIGINT → tear-down |
| `dev_server.detect()` | ✅ | `runtime/dev_server.py` | npm/pnpm/yarn/bun + uvicorn + django + manage.py heuristics |
| `browser.find_chrome()` + `build_launch_cmd()` | ✅ | `runtime/browser.py` | platform-aware Chrome detection, dedicated user-data-dir, free-port picker |
| Process registry integration | ✅ | uses Phase 12 | every spawn tracked; SIGINT walks reverse spawn order to kill |
| Auto-start khimaira-monitor | ✅ | `_ensure_monitor()` | probes /api/projects, starts daemon if down |
| Wait-for-ready via wait_for_process | ✅ | uses Phase 12 | uses framework-specific URL pattern (Vite "Local: ...", Django "Starting...") |
| `runtime/postgres.py` | ⬜ | deferred | DB connect/teardown — projects can connect manually for now |
| `runtime/healthcheck.py` | ⬜ | deferred | explicit readiness probes — wait_for_process covers most cases |
| Specter integration (browser auto-attach) | 🟡 | partial | Chrome launches with `--remote-debugging-port`; Specter package can `connect_to_tab` to it |

---

## Phase 6 — khimaira CLI commands

| Command | Status | File | Notes |
|---|---|---|---|
| `khimaira task <description>` | ✅ | `cli/task.py` | end-to-end: classify → route → dispatch → record. Verified with --dry-run. |
| `khimaira route <description>` | ✅ | `cli/route.py` | classify-only, prints routing decision JSON |
| `khimaira doctor` | ✅ | `cli/doctor.py` | env diagnostic, lists available runners + modes |
| `khimaira monitor {start,stop,restart,status,rescan}` | ✅ | `cli/monitor.py` | thin wrapper over migrated monitor.cli |
| Entry point (`khimaira.cli:main`) | ✅ | `cli/__init__.py` | argparse dispatch |
| `khimaira dev <project>` | ⬜ | `cli/dev.py` | (Phase 5) |
| `khimaira init` | ⬜ | `cli/init.py` | first-run UX, detects + suggests Ollama |
| `khimaira install --target` | ⬜ | `cli/install.py` | configures Claude Code / Gemini / Codex MCP |

---

## Phase 7 — Monitor migration (from khimaira-legacy) ✅ DONE (core)

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
| `khimaira.usage` module | ✅ | + `make_langchain_callback` for legacy node usage |
| `monitor/auto_fix.py` | ✅ | |
| `monitor/discovery/*` | ✅ | project + connection + topology discovery |
| `monitor/metadata/*` | ✅ | observation collector + scan |
| **NEW** `monitor/api/savings.py` | ⬜ | burn-down chart data |
| **NEW** `monitor/api/runtime.py` | ⬜ | dev/browser/db status |
| **NEW** `monitor/api/routing.py` | ⬜ | AMR decision log |

---

## Phase 8 — Patterns migration (from khimaira-legacy) ✅ DONE

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
| `khimaira.server.mcp` MCP entry point | ⬜ | exposes graph factories as MCP tools |

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

## Phase 11 — Multi-session shared state ✅ DONE (backend; hooks deferred)

| Item | Status | Where | Notes |
|---|---|---|---|
| JSONL session store at `~/.local/state/khimaira/sessions/<sid>/` | ✅ | `monitor/sessions.py` | decisions, files_touched, questions (in-place updates), status, **inbox** |
| `mcp__khimaira__session_log_decision(text, why)` | ✅ | A's write |
| `mcp__khimaira__session_log_question(text)` → returns question_id | ✅ | A's write |
| `mcp__khimaira__session_log_touch(file, summary, line_range)` | ✅ | A's write |
| `mcp__khimaira__session_set_status(state, detail)` | ✅ | A's status update |
| **`mcp__khimaira__session_post_answer(target_session_id, question_id, answer)`** | ✅ | **B → A write-back: updates question status + drops inbox note** |
| `mcp__khimaira__session_state(session_id)` | ✅ | B's read — full digest |
| `mcp__khimaira__session_recent_decisions()` | ✅ | B's read across all sessions |
| `mcp__khimaira__session_list()` | ✅ | B's read — sessions index |
| **`mcp__khimaira__session_pending_notes(session_id, mark_read)`** | ✅ | **A's inbox read — answers B has posted, auto-marks read** |
| `/api/sessions/*` REST endpoints | ✅ | `monitor/api/sessions.py` | dashboard data |
| Smoke test verified: bidirectional flow A ↔ B ↔ A | ✅ | | |
| **PostToolUse hook → auto session_log_touch** | ✅ | `scripts/hooks/post_tool_use.py` | stdlib Python, direct JSONL write, dedupes within MultiEdit, skips non-edit tools |
| **Periodic `<khimaira:reminder>` injection** | ✅ | `scripts/hooks/user_prompt_submit.py` | per-session counter at `~/.local/state/khimaira/hook-counters/`, fires every 8th turn |
| **SessionStart hook → call session_pending_notes** | ✅ | `scripts/hooks/session_start.py` | reads inbox.jsonl, atomic-rewrites with read=true, emits hookSpecificOutput JSON |
| **`khimaira install-hooks` installer** | ✅ | `cli/install_hooks.py` | idempotent merge into ~/.claude/settings.json, marker-based uninstall, --dry-run + auto-backup |
| Dashboard panel: live sessions view | ⬜ | deferred | `apps/monitor-ui/src/components/sessions/` |

**Critical design notes (incorporated from review):**

1. **File-touch is automated; decisions/questions are nudged.** PostToolUse hook on Edit/Write/MultiEdit captures every file mutation with line ranges — zero agent burden, can't be forgotten. Decisions and questions require the agent to recognize "this is a decision" — extraction from prose is unreliable, so we inject a periodic reminder rather than auto-extract.

2. **The write-back path is symmetric.** B → A communication is NOT optional. Without `session_post_answer` + `session_pending_notes` + the SessionStart auto-read, the design collapses to "B reads A, then human relays" — which only solves half the problem. Both directions plumbed from day 1.

3. **Inbox-read should be automatic.** SessionStart hook calls `session_pending_notes` and surfaces unread answers in the system prompt. The agent sees "session B answered Q3" without the user having to know to ask.

4. **Estimated scope:** ~300-400 LOC + 2-3 Claude Code hook scripts. Reuses existing khimaira-monitor daemon, JSONL pattern, FastMCP server scaffolding.

---

## Phase 13 — MCP call telemetry ✅ DONE

| Item | Status | Where | Notes |
|---|---|---|---|
| `logged_tool` decorator | ✅ | `monitor/mcp_calls.py` | wraps every MCP tool; appends ts/tool/args/elapsed/success/output_size/error to JSONL |
| Applied to all 42 khimaira MCP tools | ✅ | `server/mcp.py` | one-shot script inserted decorator after each `@mcp.tool()` |
| `~/.local/state/khimaira/mcp-calls.jsonl` | ✅ | append-only, line-atomic via asyncio lock |
| `GET /api/mcp-calls` | ✅ | `monitor/api/mcp_calls.py` | filterable: window_minutes, tool, only_failures |
| `GET /api/mcp-calls/summary` | ✅ | `monitor/api/mcp_calls.py` | aggregate by-tool stats + polling-replacement metric |
| `mcp__khimaira__usage_report(window_minutes)` | ✅ | `server/mcp.py` | "is khimaira being used effectively?" answer in one call |
| `mcp__khimaira__list_mcp_calls(...)` | ✅ | `server/mcp.py` | recent invocations, drill-down |
| Polling-replacement estimator | ✅ | `summarize()` | counts wait_for_process calls + their blocked time, divides by 5s/poll baseline |
| Smoke-tested | ✅ | | 10 synthetic records → summary correct, by-tool breakdown correct, error samples captured |

**Side fix in same commit:** `run_claude` and `run_gemini` module-level
convenience functions had been migrated to return `RunnerResult` (breaking
all legacy callers that expected str). Reverted to returning `.text` for
backwards compat. Added `run_claude_full` / `run_gemini_full` for new
callers that want the full result object. New `khimaira task` flow already
uses `runner.run()` directly so it's unaffected.

---

## Phase 12 — Process observability ✅ DONE (backend; UI deferred)

| Item | Status | Where | Notes |
|---|---|---|---|
| Process registry in monitor daemon | ✅ | `monitor/processes.py` | spawn/wait/follow/kill — 4MB ring buffer per process |
| `mcp__khimaira__spawn_process` | ✅ | `server/mcp.py` | |
| `mcp__khimaira__wait_for_process` | ✅ | `server/mcp.py` | **Polling replacement primitive — verified end-to-end (signal_match in 0.6s)** |
| `mcp__khimaira__follow_process` | ✅ | `server/mcp.py` | snapshot of current output |
| `mcp__khimaira__list_processes` | ✅ | `server/mcp.py` | |
| `mcp__khimaira__kill_process` | ✅ | `server/mcp.py` | SIGTERM + 5s grace + SIGKILL |
| `/api/processes/{label}/stream` SSE | ✅ | `monitor/api/processes.py` | dashboard endpoint |
| `POST /api/processes/spawn`, `POST /api/processes/{label}/wait` | ✅ | `monitor/api/processes.py` | long-poll wait endpoint |
| Dashboard panel: live process output | ⬜ | `apps/monitor-ui/src/components/processes/` | UI deferred — backend wired |

**Use cases this unblocks:**

1. **Wait for tests:** `wait_for_process("npm-test", completion_signal=r"\d+ passed|\d+ failed", timeout_s=300)` — single tool call replaces polling loop.
2. **Wait for dev server ready:** `wait_for_process("dev-server", completion_signal=r"Local: http", timeout_s=30)` — agent kicks off `khimaira dev`, waits one call for the server to be up before it interacts with the browser.
3. **Wait for build:** `wait_for_process("vite-build", completion_signal=r"built in", timeout_s=120)`.
4. **Long migrations:** `wait_for_process("alembic-upgrade", completion_signal=r"Done|Error", timeout_s=600)`.

**Estimated scope:** ~250-350 LOC (process registry + 5 MCP tools + 1 API
route + dashboard panel). Plumbs through the existing monitor daemon — no
new long-running services.

**Sequencing:** lands cleanly AFTER Phase 5 (Runtime manager), since
`khimaira dev` will be a heavy user of the process registry.

---

## Phase 10 — API removal (the deprecation path)

The dev-tool pitch requires "no API keys, no surprise bills." Migration sequence:

| Step | Status | Notes |
|---|---|---|
| Flip every node default to CLI | ⬜ | API stays as opt-in (`KHIMAIRA_USE_API=true`) |
| Build `run_structured` helper | ⏳ | (Phase 2) |
| Migrate API-using nodes one at a time | ⬜ | watching parse-failure rate via usage tracker |
| Delete API provider code | ⬜ | once parse-failures stabilize <1% |
| Remove `langchain_anthropic` dep | ⬜ | |

API-using nodes inventory (from legacy):
- `validator`, `supervisor`, `critic`, `stress_tester`, `scope_analyzer`, `arbitrator`, `retry_controller`, `compliance`, `refiner/classifier`, `swarm/task_decomposer`, `hypervisor_dispatcher`, `toolbuilder/friction`, `toolbuilder/proposer`, `nodes/balanced/integration_gate`

---

## Known footguns (address before public release)

### Monitor auto-scan kicks off Claude Opus on daemon startup

When `khimaira monitor start` runs, the metadata scanner queues every
discovered project for an LLM-driven scan. Defaults to **Claude Opus 4.7
on a ~94k-char prompt = ~$1.40 per project, per fresh start.**

For a dev-tool that's pitching "no surprise bills," this is a problem.
First-run experience burns ~$3 on khimaira + 1 other project before the
user has even queried anything.

**Fix candidates (do one before public release):**
1. Default `KHIMAIRA_MONITOR_SCAN_MODEL=gemini` — uses Gemini CLI (subscription) instead of Anthropic API (per-call billing).
2. Make auto-scan opt-in: env var `KHIMAIRA_AUTO_SCAN=1` to enable; default off until user runs `khimaira monitor scan` manually.
3. Skip scan entirely under `KHIMAIRA_LOCAL_ONLY=1`.
4. Use AMR routing (Phase 3) for the scan call instead of hardcoded Opus — let the auto-router pick a cheaper model.

**Recommendation:** option 4 once AMR's escalation path is solid; option 2
as the conservative interim default (better UX surprise: "no scan ran, run
this command" vs "$3 vanished without consent").

---

## Decisions & rationale (sticky notes)

- **Repo:** `khimaira` reclaimed; old code lives in `fsocietydisobey/khimaira-legacy` (archived).
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
