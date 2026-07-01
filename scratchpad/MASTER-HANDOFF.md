# khimaira master handoff → fresh lean session (2026-06-28)

You are taking over as **khimaira master** from `khimaira-0` (being retired: 230k ctx,
14,302 turns, peaked ~1M — pre-fix heavy launch + long history). Read this top-to-bottom,
then `chat_my_chats(session_id=<yours>)` to bind SSE, then stand by for Joseph.

## 1. THE FIX shipped last night (the reason you boot lean)

**Root cause of the roster usage spike:** setting `ANTHROPIC_BASE_URL` (routing through the
khimaira concurrency-proxy :8741 → backup account) **auto-DISABLES Claude Code's MCP
tool-search**, so ~250 tool schemas (khimaira 119 + linear + khimaira-chat + ai-orchestrator)
load FULL into the system prompt (~150k tok/turn). The counter-flag is `ENABLE_TOOL_SEARCH=1`.

**What was changed (durable):**
- `ENABLE_TOOL_SEARCH=1` added to the `env` block of BOTH `~/.claude/settings.json` and
  `~/.claude-jeevy/settings.json` → applies on every launch path.
- Removed `ai-orchestrator` MCP server from both `.claude.json`.
- Backups: `~/.claude-jeevy/backups/ctx-fix-20260628-000215/`.
- `bin/roster` already pairs `--env ENABLE_TOOL_SEARCH=1` with the proxy (line 449).

**LIVE-VERIFIED on the livyatan roster relaunch:** idle sonnet seats cluster at ~85.6k
(down from ~194k, 56% cut); tool-search deferral confirmed. Full detail in memory
`project_proxy_disables_tool_search`.

**Caveat:** fix is RELAUNCH-only — can't slim a live session's already-loaded system prompt.
That's why khimaira-0 + void-0 stay heavy and you (fresh) boot lean. For a MANUAL launch you
MUST pass `ANTHROPIC_BASE_URL=http://127.0.0.1:8741 ENABLE_TOOL_SEARCH=1` explicitly (settings
env is a backstop; the process env is authoritative). `roster` does this automatically.

## 2. Current roster state

- **livyatan roster** (jeevy work, `roster livyatan start` from ~/work/jeevy_portal): 9 seats
  up + bootstrapped + HEALTHY. master 198k / agents ~102k / consult seats ~88k. On backup token,
  lean. Nothing owed to khimaira master here.
- **khimaira-void-0** (40f5c915): idle ~8h, holding 387k ctx. Stood down per a quota-conservation
  hold. Its delivered work ↓ (§3). May be replaced by a fresh **khimaira-void-1** to pick up the
  parked items lean.
- **khimaira-0** (this/retiring): the heavy one. Once you're master, it can be let go.

## 3. Where khimaira-void-0 left off (for a possible void-1)

- **#39 (critic/verifier verdict-starvation):** PAIR complete, 383 tests green, **draft-PR staged
  at `scratchpad/open_issue39_cold_start_pr_khimaira.sh`** — NOT committed (void is agent-role).
  The engagement substrate was already live; the real hole was one branch (a `done` 0-verdict
  gate_required task falling through the cold-start path). Ready to commit + deploy when quota allows.
- **#40 actuation:** void's audit REVERSED the relayed diagnosis — likely already fixed by `401f26e`
  (it joins wrapped lines). Parked on jeevy-master's concrete capture.
- **#40 auto-wake verdict:** healthy. **#38 Tier-1:** confirmed live.
- **OPEN / held (sit with master):** #38 Tier-2 contract-gating (673-gated conformance shell) +
  #40 evidence — both awaiting Joseph's quota/sequencing decision.

A **void-1** would: relaunch lean on backup, read this doc, and pick up #38 Tier-2 / #39 deploy /
#40 evidence.

## 4. Context-% (`_compute_context_pct`) — do NOT "fix" it

It feeds ONLY the auto-compact trigger (not any display). It deliberately assumes a 1M window:
under-report → daemon defers → CC auto-compact backstops (recoverable); over-report → premature
/compact → data loss. Wrong-in-the-safe-direction BY DESIGN. Only optional enhancement: stamp the
real window per-seat at launch via the existing `KHIMAIRA_CONTEXT_WINDOW` override in `bin/roster`
(launcher change, no daemon restart) IF daemon-side 200k-seat management is ever wanted.

## 5. Constraints / standing rules

- Don't restart the monitor daemon mid-live-roster (livyatan is up). Deploy-gated changes bundle.
- Don't push jeevy code (Joseph handles); khimaira commits/merges authorized; no new git branches
  without Joseph's OK; jeevy work on joseph/langgraph-dev.
- Memory index: `~/.claude/projects/-home--3ntropy-dev-khimaira/memory/MEMORY.md` (auto-loaded).
  Key: `project_proxy_disables_tool_search`, `project_account_failover_proxy`,
  `project_dead_master_recovery`, `project_daemon_systemd_kitty_rc`.
