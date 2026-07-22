# memory-kg — knowledge-graph layer for the Claude native-memory system

**Dispatched by:** khimaira-master (`dda2a643-dfa3-4b98-9de9-2825d6e2853a`), 2026-07-22
**Assignee:** khimaira-intern-0

## Goal

Give the Claude native-memory system (the `MEMORY.md` / `MEMORY_ARCHIVE.md` entries that
`claude_memory_retrieval.py` already indexes into Qdrant) a **knowledge-graph layer**:
typed, non-destructive relationships between memory entries (`SUPERSEDES`, `RELATES_TO`,
`CAUSED_BY`), served through khimaira's **existing** generic KG adapter contract so the
existing graph viewer + `kg_*` MCP tools work against it with zero UI/tool changes.

## In plain terms

We rejected AI-consolidation (merging/rewriting memory entries) as lossy and risky. A
graph gets the same "these entries are connected / this one supersedes that one" value by
adding an *edge* instead of rewriting *content*. Originals stay untouched forever.

## Read these first (the existing machinery you're plugging into)

1. `packages/khimaira/src/khimaira/monitor/api/graph.py` — the daemon's generic KG proxy.
   Contract: nodes `{id, type, label, badge?}`, edges `{from, to, type, id?, weight?}`.
   The daemon proxies `/api/graph/<project>` → the project's registered `kg_adapter.url`,
   and derives sibling endpoints (`node/<id>`, `schema`, `health`) via `_sub_url()`.
2. `packages/khimaira/src/khimaira/attach/registry.py` — `set_kg_adapter()` /
   `get_kg_adapter()`: how a project registers its adapter URL.
3. `packages/khimaira/src/khimaira/claude_memory_retrieval.py` — UNCOMMITTED, in the
   working tree. Note `uuid5(NAMESPACE_URL, f"{project}:{link}")` stable point IDs and the
   configured source files (jeevy + khimaira MEMORY.md / MEMORY_ARCHIVE.md).
4. `packages/khimaira/src/khimaira/claude_memory_index.py` — UNCOMMITTED. The entry parser
   (title/link/body) — reuse it; don't write a second MEMORY.md parser.
5. `docs/KG-SYSTEM-TRACKER.md` — how jeevy's adapter is wired, for reference.

## Design (agreed with Joseph; deviate only with a logged reason)

- **Nodes** = memory entries. `id` = the SAME `uuid5(project:link)` the Qdrant index uses,
  so vector layer and graph layer address identical entities. `type` = the entry's
  metadata type (`user`/`feedback`/`project`/`reference`) or `memory` fallback. `label` =
  entry title. `badge` = `archived` when the entry lives in MEMORY_ARCHIVE.md.
- **Edges** = a small SQLite table at `~/.local/state/khimaira/memory_kg.sqlite3`
  (`from_id, to_id, type, created_at, note`). Edge types: `SUPERSEDES`, `RELATES_TO`,
  `CAUSED_BY` (validate against this enum; reject unknown types loudly).
- **Adapter endpoints served by the monitor daemon itself** (new router, e.g.
  `/internal/memory-kg/{graph,node/<id>,schema,health}`) — no new service/process. Nodes
  are derived live from the memory files on each request (they're tiny — ~100 entries);
  edges come from SQLite. Register via `set_kg_adapter("khimaira-memory",
  url="http://127.0.0.1:8740/internal/memory-kg/graph")` (no `token_env` — localhost,
  same process; confirm graph.py treats missing token_env as "no auth header" and doesn't
  500).
- **Edge writes**: one new MCP tool `memory_link(from_link, to_link, type, project, note="")`
  in `server/mcp.py` near `memory_search`, resolving links → uuid5 ids. Explicit calls
  only — NO automatic edge inference anywhere. (AI may *suggest* an edge in conversation;
  only an explicit tool call commits it. Propose, don't dispose.)
- A dangling edge (points at an entry link that no longer exists in either file) is
  reported in the graph payload as-is — the viewer tolerates it — but `health` should
  count them.

## Hard constraints (non-negotiable)

- **Read-only w.r.t. the memory markdown files.** This feature NEVER writes MEMORY.md or
  MEMORY_ARCHIVE.md — not in code, not in tests. The only writable state is the new
  SQLite file.
- **No hook-file changes.** Nothing in `hooks/` — hook edits are live-in-production the
  instant they're saved (no staging boundary; we got burned on 2026-07-21).
- **Tests use tmp_path fixtures only.** `tests/conftest.py` already redirects configured
  memory paths into tmp_path — keep everything inside that guard. SQLite path must be
  injectable for tests.
- **Do NOT commit.** Leave everything in the working tree like the rest of this batch.
  The repo already has substantial uncommitted work (memory system, chats fixes) — do not
  revert, reformat, or `ruff format` whole files; targeted edits only.
- **Daemon code = needs restart to go live.** Don't restart the daemon yourself; note in
  your completion report that a restart is required for the new router.

## Definition of done

1. New module (suggest `packages/khimaira/src/khimaira/monitor/memory_kg.py` + router in
   `monitor/api/`), `memory_link` MCP tool, registry entry (registration can be a small
   idempotent call at daemon startup or a documented one-shot CLI — your call, document it).
2. Tests: nodes derived from fixture memory files; edge add/list round-trip; unknown edge
   type rejected; dangling-edge health count; contract-shape conformance (node/edge field
   rules from graph.py's `_NODE_REQUIRED`/`_EDGE_REQUIRED`).
3. Full existing test suite still green (`uv run pytest packages/khimaira/tests/ -q`).
4. **When done: `mcp__khimaira__session_post_notice(target_session_id="dda2a643-dfa3-4b98-9de9-2825d6e2853a", text="memory-kg: <status summary>")`**
   — khimaira-master won't see your completion otherwise. Report evidence (test counts,
   file list), not intent. If you observe ANY effect on real files outside tmp_path,
   disclose it explicitly in the notice.
