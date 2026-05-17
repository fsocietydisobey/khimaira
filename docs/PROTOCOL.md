# Khimaira Protocol

> The integration contract for adapter authors. Three surfaces:
> **HTTP**, **MCP**, **CLI**. Pick the one that matches how your tool
> talks to the world — they're all backed by the same in-process state.

## TL;DR

| Surface | Where | Who calls it |
|---|---|---|
| **HTTP REST** | `http://127.0.0.1:8740/api/*` | Web dashboards, scripts, anything that speaks HTTP |
| **MCP** | `khimaira mcp` (stdio FastMCP) | Claude Code, Cursor, Cline, Continue, any MCP host |
| **CLI** | `khimaira <subcommand>` | Humans at a terminal, shell pipelines, hooks |

All three share the same backing state on disk
(`~/.local/state/khimaira/`, `~/.khimaira/`). Mutations made via one
surface are immediately visible to the others.

Machine-readable surfaces:

- HTTP — OpenAPI at `http://127.0.0.1:8740/api/openapi.json`, Swagger UI at `http://127.0.0.1:8740/api/docs`
- MCP — `khimaira tools --category mcp --json`
- CLI — `khimaira <subcommand> --help`

**Adapter authors**: read this doc once to understand the conceptual
map and stability tiers, then live in OpenAPI / `khimaira tools` for
the canonical surface. This document does not duplicate per-endpoint
schemas — those live in code and are auto-published.

---

## Stability tiers

Each tool / endpoint / subcommand carries one of three tiers. Tiers
are advisory until 1.0; the policy below is what adapter authors
should plan against.

| Tier | Meaning | Change policy |
|---|---|---|
| **stable** | Adapter authors may pin to this surface | Breaking changes require deprecation window + changelog entry. Bug-fix-shaped changes always permitted. |
| **beta** | Shipped, in use, may evolve | Breaking changes announced in commit message + dashboard banner. No formal window. |
| **experimental** | Recently added or under active design | May change or be removed without notice. Adapter authors should expect breakage. |

Until khimaira ships a tagged 1.0, **assume beta** for anything not
explicitly marked. The `stable` tier is reserved for the session /
handoff / usage primitives — those have shipped users (other
sessions, hooks) and breaking them breaks the cross-session
coordination layer.

Per-surface stability is called out in each section below.

---

## HTTP REST surface

### Base URL, bind, auth

```
http://127.0.0.1:8740
```

- Loopback-only bind by design. **The loopback bind IS the auth
  layer** — there is no token, no API key, no header. If the request
  reaches the daemon, it is by definition coming from a process on
  the same host that owns the loopback interface.
- Port override: `KHIMAIRA_MONITOR_PORT` env var. Bind host is
  hardcoded to `127.0.0.1`; do not change without understanding the
  security model.
- All API endpoints live under `/api/`. Anything outside `/api/` is
  static SPA assets served from the bundled web dashboard.

### Versioning

The API is **currently unversioned**. Endpoints live at `/api/<resource>`,
not `/api/v1/<resource>`. This is a known gap vs the project's stated
API-design rule and will be addressed before any 1.0 cut. Until then:

- Adapter authors should treat the surface as `beta` overall and pin
  to a specific khimaira version (`khimaira --version`) in their
  integration.
- A versioning sweep will move endpoints to `/api/v1/` and leave the
  unversioned paths as deprecated aliases for one release.

### Response shape

Endpoints return JSON. Most follow one of two patterns:

```json
// Collection
{ "sessions": [ ... ] }
{ "decisions": [ ... ] }

// Single resource — returned directly, no envelope
{ "session_id": "...", "status": "...", "decisions": [...] }
```

There is **no global `{data, meta}` envelope**. Pagination, where
present, uses offset query params (`?limit=20&offset=0`) and returns
the page inline.

### Errors

Errors follow FastAPI's default shape:

```json
{ "detail": "Human-readable message" }
```

Status codes are explicit, not catchall 500s:

- `404` — unknown session name / unknown question ID / unknown project
- `408` — long-poll timeout (`/sessions/{id}/questions/{qid}/wait`)
- `410` — resource withdrawn (e.g. question was deleted before answer)
- `422` — validation error (invalid workspace name, workspace mismatch on cross-workspace question)
- `500` — unexpected; treat as a bug to report

If you hit a 500 with a stack trace, file it — every session-resolving
endpoint should map unknown inputs to 404 with a helpful message. See
[project root `CLAUDE.md`](../CLAUDE.md) § "Every endpoint that resolves
a session name needs unknown-name coverage" for the contract.

### Route inventory (by router)

Conceptual map, not a schema reference. Use OpenAPI for parameters
and response models.

#### Sessions + handoffs · `monitor/api/sessions.py` · tier: **stable**

The cross-session coordination layer. These endpoints back the
`session_*` MCP tools and the `/inbox`, `/handoffs`, `/tell`, `/ask`
slash commands.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/sessions` | List all tracked sessions |
| GET | `/api/sessions/recent_decisions` | Cross-session decision log |
| GET | `/api/sessions/resolve/{query}` | Resolve a session name → ID |
| GET | `/api/sessions/{session_id}` | Full state digest |
| GET | `/api/sessions/{session_id}/summary` | Lightweight digest |
| GET | `/api/sessions/{session_id}/pending` | Unread inbox notes (drains by default) |
| GET | `/api/sessions/{session_id}/incoming` | Open questions targeted at this session |
| GET | `/api/sessions/{session_id}/inbox/surface` | Peek inbox without draining (used by hooks) |
| POST | `/api/sessions/{session_id}/inbox/ack` | Mark notes read |
| POST | `/api/sessions/{session_id}/decision` | Log a decision |
| POST | `/api/sessions/{session_id}/touch` | Record a file touch |
| POST | `/api/sessions/{session_id}/question` | Open a question |
| GET | `/api/sessions/{session_id}/questions/{qid}/wait` | Long-poll for answer (408 on timeout) |
| POST | `/api/sessions/{session_id}/answer` | Answer a question on another session |
| POST | `/api/sessions/{session_id}/notice` | One-way FYI to another session |
| POST | `/api/sessions/{session_id}/status` | Set status (`researching`, `implementing`, …) |
| POST | `/api/sessions/{session_id}/name` | Set friendly name |
| POST | `/api/sessions/{session_id}/workspace` | Set workspace (privacy boundary) |
| GET | `/api/sessions/{session_id}/workspace` | Read current workspace |
| GET | `/api/sessions/{session_id}/transcript/query` | Grep the on-disk transcript |
| GET | `/api/sessions/{session_id}/transcript/summary` | Heuristic summary (no LLM) |
| GET | `/api/sessions/{session_id}/inbox/archive` | Search already-read notes |
| POST | `/api/handoffs` | Post a handoff for a future session |
| GET | `/api/handoffs/consume` | Claim + read cwd-scoped handoffs |
| GET | `/api/handoffs/in-scope` | Read without claiming |
| POST | `/api/handoffs/{handoff_id}/subscribe` | Watch an owner's progress |
| POST | `/api/handoffs/{handoff_id}/unsubscribe` | Stop watching |
| POST | `/api/handoffs/{handoff_id}/release` | Owner steps aside |
| POST | `/api/handoffs/{handoff_id}/invite` | Delegate a slice to a named session |
| POST | `/api/route` | Smart-route a message to a target — tries session-name first, falls back to project-label. Backs `/tell`. |

Note: this is **message routing** (cross-session messaging), distinct
from **task routing** (classify + dispatch to a runner). Task routing
is the CLI's `khimaira route` and the MCP `route` / `auto` /
`delegate` tools; it is in-process, not an HTTP endpoint.

See [`docs/INBOX-AND-HANDOFFS.md`](INBOX-AND-HANDOFFS.md) for the mental
model and which slash command / MCP tool maps to which endpoint.

#### Heartbeats + observability · `monitor/api/heartbeats.py`, `processes.py`, `mcp_calls.py`, `usage.py`, `anomalies.py` · tier: **beta**

Used by attached LangGraph projects and the bundled web dashboard.
Adapter authors generally don't call these directly — they're read
via the dashboard or the `monitor_*` MCP tools.

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/heartbeat` | Emit a heartbeat (called by `khimaira_observer`) |
| GET | `/api/heartbeats/stats` | Aggregate buffer stats |
| GET | `/api/heartbeats/{project}` | Recent runs |
| GET | `/api/heartbeats/{project}/cost` | Cost breakdown |
| GET | `/api/heartbeats/{project}/cost/timeseries` | Cost over time |
| GET | `/api/heartbeats/{project}/slow` | Slow calls |
| GET | `/api/heartbeats/{project}/by-correlation/{cid}` | Trace by correlation ID |
| GET | `/api/heartbeats/{project}/{run_id}` | Single run detail |
| GET | `/api/heartbeats/{project}/{run_id}/stream` | SSE stream of run events |
| GET | `/api/processes` | List tracked subprocesses |
| POST | `/api/processes/spawn` | Spawn a tracked subprocess |
| POST | `/api/processes/{label}/wait` | Block until process completes |
| POST | `/api/processes/{label}/kill` | SIGTERM (then SIGKILL after 5s) |
| GET | `/api/processes/{label}` | Process snapshot |
| GET | `/api/processes/{label}/stream` | SSE stream of process output |
| GET | `/api/mcp-calls` | Recent MCP tool invocations |
| GET | `/api/mcp-calls/summary` | Aggregate by tool |
| GET | `/api/usage` | Usage records (dispatch ledger) |
| GET | `/api/anomalies` | Self-watch findings |
| GET | `/api/heartbeat` | Anomalies-router liveness check (distinct from `/heartbeat` POST) |

#### LangGraph project introspection · `monitor/api/threads.py`, `topology.py`, `projects.py`, `api_routes.py`, `frontend_components.py`, `schema_drift.py` · tier: **beta**

Surfaces metadata about attached LangGraph projects. Backs the web
dashboard and the `monitor_*` MCP tools. Adapter authors integrating
non-LangGraph workloads can ignore this section.

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/projects` | List attached / scanned projects |
| GET | `/api/projects/{name}` | Single project detail |
| GET | `/api/threads/{project}` | Recent threads |
| GET | `/api/threads/{project}/{thread_id}` | Thread detail + checkpoint history |
| GET | `/api/threads/{project}/{thread_id}/wait` | Block until thread reaches a state |
| GET | `/api/threads/{project}/{thread_id}/stream` | SSE stream of thread events |
| GET | `/api/topology/{name}` | Compiled-graph topology |
| GET | `/api/api_routes/{name}` | FastAPI routes in the attached project |
| GET | `/api/frontend_components/{name}` | React/Next components |
| GET | `/api/schema_drift/{name}` | Pydantic/SQLAlchemy vs DB drift |

### Streaming endpoints

Endpoints whose path ends in `/stream` use Server-Sent Events
(`text/event-stream`). Adapter authors:

- Use a streaming HTTP client. Do NOT buffer the full response.
- Each `data:` line is a JSON message; consume one per emit.
- Heartbeat events are sent every ~15 seconds to keep the connection
  open through reverse proxies.
- Disconnect terminates the subscription cleanly on the server side
  (no need to call a cleanup endpoint).

### Long-poll endpoints

`/sessions/{id}/questions/{qid}/wait` is the canonical long-poll.
**Your HTTP client's read timeout MUST be greater than the `timeout`
query parameter** or you'll get a client-side timeout before the
server has a chance to return.

Default `timeout` is 300 seconds. Override with `?timeout=<seconds>`.

---

## MCP surface

### Connection

```jsonc
{
  "mcpServers": {
    "khimaira": {
      "command": "uvx",
      "args": ["khimaira", "mcp"]
    }
  }
}
```

The server is a [FastMCP](https://github.com/jlowin/fastmcp)
stdio-protocol process. Add it to any MCP host (Claude Code, Cursor,
Cline, Continue, custom). Restart the host after editing.

`khimaira mcp` reads MCP protocol messages on stdin and writes
responses on stdout; logs go to stderr. The MCP server runs in the
same process namespace as the user — adapter authors don't manage
sandboxing.

### Tool naming

All tools are exposed under the `mcp__khimaira__` prefix when the
host concatenates server-name to tool-name (Claude Code's convention).
The raw tool names (as registered with FastMCP) are without the
prefix:

```
session_log_decision         ↔ mcp__khimaira__session_log_decision
auto                         ↔ mcp__khimaira__auto
chain_pipeline               ↔ mcp__khimaira__chain_pipeline
```

This doc uses the prefixed form to match what an adapter author will
see in their host's tool listing.

### Two MCP servers

khimaira registers **two** MCP servers with the host:

| Server name | Command | Purpose |
|---|---|---|
| `khimaira` | `uv ... run khimaira mcp` | Orchestration, session state, perception tools, pipelines |
| `khimaira-chat` | `uv ... run python -m khimaira_chat.server` | Real-time cross-session chat (per-session stdio subprocess, SSE-push delivery) |

Both are registered by `khimaira bootstrap`. Tools from each appear under `mcp__khimaira__*` and `mcp__khimaira-chat__*` respectively. The chat server is NOT re-registered on the main khimaira server — it needs its own stdio process per session so each session gets its own SSE subscription.

### Tool categories

~119 tools total across both servers (khimaira native + re-registered sibling packages). Counts in this
table are approximate; the live install is authoritative —
`khimaira tools --category mcp`.

| Category | Tools | Purpose | Tier |
|---|---|---|---|
| **Cross-session coordination** | `session_*` (~25) | Sessions, inboxes, handoffs, questions, decisions, transcripts | **stable** |
| **Routing + dispatch** | `auto`, `delegate`, `classify`, `khimaira_configure` | The orchestration core — classify a task, route to cheapest competent runner | **stable** |
| **Pipeline orchestration** | `chain*`, `architect`, `research`, `brainstorm`, `swarm`, `approve`, `status`, `history`, `rewind` | LangGraph-backed pipelines (SPR-4, ACL, DCE, HVD, CLR, POB) | **beta** |
| **Process supervision** | `spawn_process`, `wait_for_process`, `kill_process`, `list_processes`, `follow_process` | Track long-running subprocesses (test runners, dev servers, builds) | **beta** |
| **LangGraph observability** | `monitor_*`, `wait_for_run` | Inspect attached LangGraph projects (topology, runs, anomalies, schema drift) | **beta** |
| **Health + introspection** | `health`, `list_mcp_calls`, `usage_report` | Daemon liveness, recent invocation history, aggregate usage | **stable** |
| **Semantic code search** | `seance_*` (5) | Re-registered from `seance` — natural-language search over indexed codebases (`semantic_search`, `index_project`, `reindex_changed`, `find_similar`, `list_projects`) | **beta** |
| **Browser debugging** | `specter_*` (34) | Re-registered from `specter` — CDP-based console/network/screenshot/interaction tools for any Chromium tab on `:9222` | **beta** |
| **Codebase cartography** | `scarlet_*` (9) | Re-registered from `scarlet` — tree-sitter-driven feature scanning, CLAUDE.md generation, dep graphs, barrels | **beta** |

#### Sibling tool naming

Sibling packages keep their own FastMCP instances (so the legacy
standalone `seance serve` / `specter serve` / `scarlet serve` paths
continue to work for backward compatibility), but khimaira's MCP
re-registers each tool at boot under a **source-prefixed** name:

```
seance.semantic_search       → mcp__khimaira__seance_semantic_search
specter.take_screenshot      → mcp__khimaira__specter_take_screenshot
scarlet.analyze_project      → mcp__khimaira__scarlet_analyze_project
```

The prefix is `<source>_<tool_name>`. Adapter authors targeting the
unified server should expect every sibling tool to carry this prefix
— there is no collision with khimaira's native tools because
khimaira tools have no `seance_`, `specter_`, or `scarlet_` prefix.

Per-tool signatures and docstrings are the source of truth. Get the
full list:

```bash
khimaira tools --category mcp               # human-readable
khimaira tools --category mcp --json        # machine-readable
```

### Calling convention

All MCP tools return `str` (JSON-encoded or human-readable). Adapter
authors should `json.loads()` defensively — most tools return JSON,
but error paths may return a plain string with a leading emoji
(`"📭 inbox empty."`). The convention is:

- Successful structured return → JSON object as string
- Successful unstructured return → human-readable string starting with an emoji glyph
- Error → MCP error envelope (host translates to host-native error)

This is **not strictly enforced** yet. Adapter authors should treat
the return as opaque text unless documented otherwise.

### Session-id discovery

Almost every cross-session tool takes a `session_id` as the first
argument. To find your own session ID inside an MCP-host context:

1. The SessionStart hook writes the ID into a `🆔 khimaira session_id`
   line in the boot context.
2. Or call `session_list()` and pick the most-recently-active entry
   matching your file-touch fingerprint.

Adapter authors building outside the Claude Code hook ecosystem
should call `session_list()` once at boot and cache the ID for the
session's lifetime.

---

## CLI surface

### Subcommands

Registered in `packages/khimaira/src/khimaira/cli/__init__.py`. Live
list: `khimaira --help` is authoritative; `khimaira tools --category cli`
introspects the same set but currently under-reports (known gap).

| Subcommand | Purpose | Tier |
|---|---|---|
| `khimaira task` | Classify + dispatch a task to the cheapest competent runner | **stable** |
| `khimaira route` | Classify-only; print the JSON decision without dispatching | **stable** |
| `khimaira mcp` | Run the FastMCP server on stdio | **stable** |
| `khimaira monitor` | Start, stop, or supervise the observability daemon | **stable** |
| `khimaira attach` / `attached` / `detach` | Inject the observer into a project's venv | **beta** |
| `khimaira observer` | Internal subprocess called by `attach` | **experimental** |
| `khimaira bootstrap` | First-time setup; reads a profile YAML and configures everything | **beta** |
| `khimaira doctor` | Diagnostic — what's misconfigured, what's drifted | **stable** |
| `khimaira heal` | Best-effort auto-fix for what `doctor` reports | **beta** |
| `khimaira install-hooks` | Wire SessionStart / PostToolUse / UserPromptSubmit into `~/.claude/settings.json` | **stable** |
| `khimaira tools` | List CLI subcommands, MCP tools, slash commands, web routes, REST endpoints | **stable** |
| `khimaira dev` | Local dev orchestration: dev server + browser-debug Chrome + monitor | **beta** |
| `khimaira models` | View / sync the model registry at `~/.khimaira/models.yaml` | **beta** |
| `khimaira usage` | Show usage records + savings vs Opus-direct baseline | **stable** |

Per-subcommand help: `khimaira <subcommand> --help`.

### Invocation patterns

Three equivalent ways to invoke:

```bash
khimaira <subcommand> ...                          # console script (PATH must include the venv)
uvx khimaira <subcommand> ...                       # ephemeral; uvx resolves the package
python -m khimaira.cli <subcommand> ...              # direct module; works in any venv with khimaira installed
```

`uvx khimaira` is the recommended path for adapter authors and CI —
no global install, no venv activation, no PATH gymnastics.

### Slash commands

24 Claude-Code-flavored slash commands at `~/.claude/commands/*.md`
(symlinked from `dotfiles`). These are not part of the protocol —
they're a Claude Code UX convenience that wraps the MCP tools.

```bash
khimaira tools --category slash       # list them
```

Adapter authors targeting non-Claude-Code hosts should call the
underlying MCP tool directly, not try to ship slash commands.

---

## Discoverability — `khimaira tools`

One command lists every surface:

```bash
khimaira tools                          # everything
khimaira tools --category cli           # CLI subcommands
khimaira tools --category mcp           # MCP tools
khimaira tools --category slash         # Claude Code slash commands
khimaira tools --category web           # monitor web routes
khimaira tools --category rest          # HTTP API endpoints
khimaira tools <substring>              # filter by name
khimaira tools --json                   # machine-readable for adapters
```

Adapter authors should treat `khimaira tools --json` as the canonical
machine-readable surface listing. It introspects the running install,
so it's always current.

---

## Examples

### Adapter sketch — read inbox + log a decision via HTTP

```bash
# Find a session by name
SESSION=$(curl -s http://127.0.0.1:8740/api/sessions/resolve/my-session-name \
  | jq -r .session_id)

# Read pending inbox (drains by default)
curl -s "http://127.0.0.1:8740/api/sessions/$SESSION/pending" | jq

# Log a decision
curl -s -X POST "http://127.0.0.1:8740/api/sessions/$SESSION/decision" \
  -H 'content-type: application/json' \
  -d '{"text": "Use Postgres read-replica for safety", "why": "Migration window is tight"}'
```

### Adapter sketch — dispatch a task via MCP

From any MCP host (pseudo-code; exact syntax depends on the host's
tool-call convention):

```python
result = mcp.call(
    "khimaira",
    "auto",
    {
        "prompt": "Rename foo to bar in src/lib/foo.py",
        "project": "my-app",
        "budget_usd": 0.05,
    },
)
# result is a JSON string — parse and inspect
```

`auto` runs classify → pool-router → dispatch, returns the runner
response + a `mode="auto"` usage record. Cheapest competent runner
wins.

### Adapter sketch — post a handoff for the next session

```bash
curl -s -X POST http://127.0.0.1:8740/api/handoffs \
  -H 'content-type: application/json' \
  -d '{
    "from_session_id": "'"$SESSION"'",
    "text": "Next session: finish the migration sweep. Spec at tasks/migration/IMPLEMENTATION.md.",
    "scope_cwd": "'"$(pwd)"'",
    "expires_in_hours": 168
  }'
```

The next session that boots in that cwd auto-consumes the handoff
via SessionStart and gets the directive surfaced in its boot context.

---

## Change policy

Until 1.0, the policy is conservative-by-default:

- **Stable surfaces** (`session_*`, `auto`/`delegate`/`classify`,
  `tools`, `task`, `route`, `monitor`, `install-hooks`, `doctor`,
  `usage`): breaking changes require a deprecation window of at
  least one minor release. Bug-fix-shaped changes (clearer error
  messages, additional optional fields) always permitted.
- **Beta surfaces**: breaking changes called out in commit message
  and dashboard banner. No formal deprecation window.
- **Experimental surfaces**: may change or be removed without
  notice.

Adapter authors pinning to a specific khimaira version (`uvx
khimaira==<version>`) get full reproducibility. Adapter authors
tracking `main` get fast iteration but should expect breakage in
beta and experimental surfaces.

### When something breaks

- Open an issue on the khimaira repo with the adapter's invocation
  pattern and the error.
- The first response will identify which tier the broken surface
  belongs to and what the change policy promised.

---

## Known gaps

- **No URL versioning.** `/api/<resource>` not `/api/v1/<resource>`.
  Will be fixed before 1.0; old paths kept as deprecated aliases for
  one release.
- **No global response envelope.** Some endpoints return `{<resource>: [...]}`,
  others return the resource directly. Adapter authors should treat
  per-endpoint shapes as the contract.
- **No formal pagination.** List endpoints today are unbounded or
  capped at a hardcoded limit (`limit=50` etc). Cursor-based
  pagination is on the roadmap for collections that grow without
  bound.
- **No authentication.** Loopback-only bind is the auth layer. If
  khimaira ever grows a multi-host mode, auth lands then.
- **No OpenAPI for MCP.** FastMCP exposes a JSON tool manifest, but
  it's not a strict schema. Use `khimaira tools --category mcp --json`
  for the introspection-based listing.

---

## See also

- [`docs/INBOX-AND-HANDOFFS.md`](INBOX-AND-HANDOFFS.md) — mental model
  for the cross-session coordination layer
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — internal architecture
- [`docs/INSTALL.md`](INSTALL.md) — install paths for end users
- [`NORTH_STAR.md`](../NORTH_STAR.md) — strategic roadmap + principles
- [`CLAUDE.md`](../CLAUDE.md) — engineering rules captured from real
  bugs in this codebase
