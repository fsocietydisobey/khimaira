# Linear adapter — Phase 1.5 follow-up

**Status**: design + Protocol skeleton landed 2026-05-14 in session
`khimaira-6`. Real implementation pending — needs daemon-side MCP
dispatch infrastructure (~1d).

**Parent spec**: [`tasks/task-sources/IMPLEMENTATION.md`](../task-sources/IMPLEMENTATION.md)
defines the `TaskSource` Protocol + JSONL/GitHub adapters. This spec
extends that with the architectural and code-level details specific
to Linear and the daemon-side dispatch layer it requires.

---

## Why this is non-trivial

The JSONL adapter reads a file. The GitHub adapter shells out to `gh`.
Both are **hook-safe** — they run as stdlib subprocesses from the
SessionStart hook with no MCP / network client required.

Linear has no `gh`-equivalent CLI we can shell out to. The canonical
read path is the official Linear MCP server
(`npx -y mcp-remote https://mcp.linear.app/mcp`), which is already in
the user's `claude mcp list`. But:

- The SessionStart hook runs in a stdlib-only subprocess. No MCP client.
- The slash command path (`/tasks` via `mcp__khimaira__list_tasks`)
  works because the agent has MCP access. But that only surfaces tasks
  *when the user explicitly asks*, not on session boot.

So Linear sits in an awkward middle zone: it should surface at boot
(like JSONL / GitHub), but the boot path can't reach it.

## The architectural fix: daemon-side MCP dispatch

The khimaira-monitor daemon is a long-running process with access to
the MCP client subsystem (it already calls khimaira's own MCP tools
internally for audit logging, etc.). It's the natural host for
"adapter calls that need MCP."

**New endpoint**:

```
GET /api/task-sources/fetch?kind=linear
→ { "tasks": [ { "id": "...", "title": "...", ... } ] }
```

The daemon:
1. Receives the HTTP GET from the SessionStart hook (stdlib `urllib`)
2. Resolves `kind=linear` → daemon-side Linear adapter
3. Daemon-side adapter calls `mcp__linear__list_issues(assignee=me, state≠done)` via the daemon's MCP client
4. Daemon normalizes Linear's response to the `Task` shape
5. Returns JSON to the hook

The hook's `LinearTaskSource.fetch_open_tasks()` becomes a thin HTTP
client: it doesn't care that the actual work happens daemon-side.
`hook_safe()` returns `True` (after the daemon infrastructure lands)
because, from the hook's perspective, it's just an HTTP GET.

### Why this generalizes

Every future non-hook-safe adapter (Jira via mcp-jira, Asana via
mcp-asana, etc.) gets the same treatment:

- Adapter implementation lives daemon-side
- Hook-side `TaskSource` is a thin HTTP client pointing at
  `/api/task-sources/fetch?kind=<name>`
- One protocol, one HTTP boundary, one Task shape coming out

The `hook_safe_only` filter parameter becomes vestigial — after
daemon dispatch lands, every adapter is reachable from the hook
via HTTP.

---

## Implementation steps

### Step 1 — daemon-side MCP client wrapper

`packages/khimaira/src/khimaira/monitor/mcp_client.py` (new). Wraps
calling external MCP servers from inside the daemon. The simplest
shape:

```python
async def call_external_mcp(server: str, tool: str, args: dict) -> dict:
    """Spawn the configured MCP server as a subprocess, send the
    tool-call request, parse the response, kill the subprocess.

    Args:
        server: the MCP server key from claude mcp list (e.g. "linear").
        tool: the tool name (e.g. "list_issues").
        args: keyword arguments for the tool call.

    Returns:
        The decoded JSON response from the tool call.

    Raises:
        TimeoutError, MCPClientError on failure.
    """
```

Open design question: do we spawn the MCP server per-call (expensive,
clean) or keep a long-lived connection (fast, lifecycle-tricky)?
Per-call is the natural first cut; optimize later if Linear is hot
enough to matter.

### Step 2 — daemon-side Linear adapter

`packages/khimaira/src/khimaira/monitor/task_source_adapters/linear.py` (new).
Calls `mcp__linear__list_issues` via the wrapper from Step 1,
normalizes the response.

Linear's MCP response shape (from the public Linear MCP docs):

```json
{
  "issues": [
    {
      "id": "abc-123",
      "identifier": "TEAM-42",
      "title": "Fix the thing",
      "state": { "name": "In Progress" },
      "url": "https://linear.app/team/issue/TEAM-42",
      "project": { "name": "Q2 Backlog" }
    }
  ]
}
```

Normalized to `Task`:

```python
Task(
    id=issue["identifier"],          # "TEAM-42"
    title=issue["title"],
    state=issue["state"]["name"].lower(),
    source="linear",
    project=issue.get("project", {}).get("name", ""),
    url=issue["url"],
)
```

### Step 3 — `/api/task-sources/fetch` HTTP endpoint

`packages/khimaira/src/khimaira/monitor/api/task_sources.py` (new).
Registers under `/api`. Handler:

```python
@router.get("/task-sources/fetch")
async def fetch_tasks(kind: str) -> dict:
    """Daemon-side task fetch — for adapters that can't run from
    the SessionStart hook (need MCP / network)."""
    if kind == "linear":
        return {"tasks": await daemon_adapters.linear.fetch()}
    raise fastapi.HTTPException(404, f"unknown task source kind: {kind!r}")
```

Errors: unknown kind → 404; adapter raised → 502 with the error string.

### Step 4 — hook-side LinearTaskSource

Already drafted (skeleton) at
`packages/khimaira/src/khimaira/task_sources/linear.py`. Switch
`fetch_open_tasks` from returning `[]` to issuing the HTTP GET against
`/api/task-sources/fetch?kind=linear`. Switch `hook_safe()` to return
`True` (once Step 3 lands).

### Step 5 — tests

Per the existing pattern in `test_task_sources.py`:

- Adapter happy path (mock the HTTP response)
- Adapter handles daemon unreachable → returns `[]`, logs warning
- Adapter handles 404 (kind not recognized) → `[]`, log warning
- Adapter handles 502 (daemon errored) → `[]`, log warning
- `hook_safe()` returns True after daemon endpoint exists

Plus an integration test (marked `@pytest.mark.integration`) that
exercises the real daemon endpoint against a mocked Linear MCP.

### Step 6 — config + docs

- Register `kind: linear` in `task_sources/config.py:_build_source()` —
  already done as part of the skeleton commit.
- Add example to `~/.khimaira/task_sources.yaml.example`:
  ```yaml
  sources:
    - kind: linear
      enabled: true
      # Optional: state filter (defaults to non-Done states)
      # states: ["In Progress", "Todo"]
  ```
- Update parent `tasks/task-sources/IMPLEMENTATION.md` "Follow-ups
  > Linear adapter" section to point at this spec + mark when each
  step lands.

---

## What's in the skeleton (landed 2026-05-14)

- `packages/khimaira/src/khimaira/task_sources/linear.py` — Protocol-conforming `LinearTaskSource` stub. Returns `[]` for now. `hook_safe()` returns `False` (will flip to `True` after Step 3 lands).
- `packages/khimaira/src/khimaira/task_sources/config.py` — `kind: linear` registered in `_build_source()`. Users can already write `{kind: linear, enabled: true}` in their config; it resolves but produces no tasks until the real impl ships.
- Unit test asserting the skeleton conforms to Protocol + returns `[]` cleanly.

---

## Open questions (for the implementation session)

1. **MCP-server spawn cost.** First-cut design spawns the Linear MCP per-call. If `npx -y mcp-remote https://mcp.linear.app/mcp` takes >1s to boot, the SessionStart hook adds a perceptible delay. Measure before committing to a connection-pooling design.

2. **Auth.** Linear's MCP server uses OAuth flow. Initial `claude mcp add linear` triggers a browser-based consent. Does the daemon's per-call spawn re-auth? Need to verify the OAuth token persists across MCP-server processes (it should, but verify).

3. **Default state filter.** Linear has 5-10 workflow states per team. "Non-Done" is the obvious default but "actively in progress" might be the more useful surface. Decide after dogfooding.

4. **Per-team filter.** A user in 5 Linear teams sees a flood. Should the adapter support a `teams: [...]` filter in config? Defer to v0.2 of the adapter — start with everything-assigned-to-me.

5. **Refresh cadence.** SessionStart calls every adapter on boot. For hot-iterating Linear users, that might be every few minutes. Daemon-side caching with a short TTL (60s?) would smooth this.

---

## Effort estimate

- Step 1 (MCP client wrapper): 2-3 hours. Most complexity is in the per-call subprocess lifecycle + cleanup.
- Step 2 (daemon-side Linear adapter): 1 hour. Mostly response-shape normalization.
- Step 3 (HTTP endpoint): 30 min. Trivial router addition.
- Step 4 (hook-side switch): 30 min. The skeleton stays the same shape; just swap return-empty for HTTP-fetch.
- Step 5 (tests): 1-2 hours. Unit tests + one integration test.
- Step 6 (docs / config): 30 min.

**Total: 1-1.5 days** of focused work. Roughly matches the original
NORTH_STAR estimate.

---

## Related specs

- [`tasks/task-sources/IMPLEMENTATION.md`](../task-sources/IMPLEMENTATION.md) — parent spec (Protocol + JSONL/GitHub adapters)
- [`tasks/cross-machine-backend/IMPLEMENTATION.md`](../cross-machine-backend/IMPLEMENTATION.md) — superseded earlier; kept as architectural archeology for cross-machine ideas
