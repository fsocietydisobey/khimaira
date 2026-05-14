# External task source integration — Phase 1.5

**Status**: shipped 2026-05-13 — Protocol + JSONL adapter + **GitHub `gh` adapter** + SessionStart integration + 22 tests. Linear adapter is follow-up work pending daemon-side MCP dispatch.
**Phase**: NORTH_STAR Phase 1.5

## Goal

khimaira surfaces "what's assigned to you" at session boot. It does
NOT prescribe where those tasks live. Users plug in their own task
tracker via the `TaskSource` Protocol. khimaira ships reference
adapters; users / community add more.

This is the **third Phase 1.5 reframe**:
- First scope: full state replication across machines (9-10d)
- Second scope: cross-machine task-dispatch primitive (3-5d)
- Third scope (yesterday's iteration): "Linear-surfacing hook" (~1d)
- **Final scope (this doc)**: generic external task source integration (~1d MVP, adapters extensible)

The final reframe replaces the Linear-named version with a vendor-
neutral Protocol. Reason: khimaira is a generic OSS tool. Naming
Linear in the public roadmap bakes a single-vendor choice into
khimaira's identity. Users who use Jira / GitHub Issues / Asana /
plain TODO.md / nothing all deserve the same surface.

## Design

### `Task` dataclass

Unified shape returned by every adapter (`khimaira.task_sources.Task`):

```python
@dataclass(frozen=True)
class Task:
    id: str               # source-stable id, e.g. "KHI-12" or "todo:42"
    title: str            # short summary, displayed inline
    state: str = ""       # source-defined state
    source: str = ""      # adapter name
    project: str = ""     # optional project label
    url: str = ""         # optional clickable link
    tags: list[str] = field(default_factory=list)
```

### `TaskSource` Protocol

What every adapter implements:

```python
class TaskSource(Protocol):
    name: str
    async def fetch_open_tasks(self) -> list[Task]: ...
    def hook_safe(self) -> bool: ...
```

`hook_safe()` matters: SessionStart hook runs as a stdlib-only
subprocess with no MCP client. Adapters that talk to MCP servers
(Linear, etc.) return `False` and are skipped by the hook — they get
surfaced via slash command or, later, via a daemon-side dispatch
layer (see "Follow-ups" below).

### Shipped adapter — `JsonlTaskSource`

Reads tasks from `~/.khimaira/todo.jsonl` (or configured path). One
task per line. No dependencies, no network, always works. Closed
states (`done`/`completed`/`closed`/`cancelled`/`archived`,
case-insensitive) are excluded from open-task fetches.

```jsonl
{"id": "TODO-1", "title": "fix the auth bug", "state": "open"}
{"id": "TODO-2", "title": "write the README", "state": "in-progress"}
{"id": "TODO-3", "title": "ship v0.5", "state": "done"}
```

Why JSONL: it's the no-dependency baseline. khimaira's task surface
works on day one for any user who can `echo '...' >> ~/.khimaira/todo.jsonl`.

### Config

`~/.khimaira/task_sources.yaml` (or `$XDG_CONFIG_HOME/khimaira/task_sources.yaml`):

```yaml
sources:
  - kind: jsonl
    path: ~/work/todo.jsonl    # optional; default is ~/.khimaira/todo.jsonl
    enabled: true
```

When no config file exists, khimaira defaults to a single JSONL source
at the default path. Returns empty cleanly if that file doesn't exist
either.

### SessionStart integration

The hook calls `fetch_all_open_tasks(hook_safe_only=True)` after the
existing inbox + handoffs steps. Renders the result as:

```
📋 khimaira tasks — 2 open assignment(s):

  • TODO-2 (in-progress) — write the README [jsonl]
  • TODO-1 (todo) — fix the auth bug [jsonl]
```

Sort order: by source, then by state (in-progress / in-review above
todo), then by id. Lines truncated at 110 chars.

If no tasks are open, the block is omitted entirely.

## Implementation map

| File | Status | Purpose |
|---|---|---|
| `packages/khimaira/src/khimaira/task_sources/__init__.py` | ✅ | `Task` + `TaskSource` Protocol |
| `packages/khimaira/src/khimaira/task_sources/jsonl.py` | ✅ | JSONL adapter |
| `packages/khimaira/src/khimaira/task_sources/config.py` | ✅ | Config loader + fan-out |
| `packages/khimaira/src/khimaira/hooks/session_start.py` | ✅ | Hook integration |
| `packages/khimaira/tests/test_task_sources.py` | ✅ | 13 unit tests |

## Follow-ups (community / future work, not Phase 1.5 core)

### Linear adapter

**Status (2026-05-14)**: design + skeleton landed. Full implementation
spec at [`tasks/linear-adapter/IMPLEMENTATION.md`](../linear-adapter/IMPLEMENTATION.md).
Estimate: ~1-1.5 days for the daemon-side MCP dispatch +
LinearTaskSource HTTP-client switch.

The skeleton at `packages/khimaira/src/khimaira/task_sources/linear.py`
returns `[]` cleanly + `hook_safe()=False`. Users can add
`{kind: linear, enabled: true}` to their `task_sources.yaml` today —
it resolves but produces no tasks until the daemon endpoint ships.

Architecture (full detail in the dedicated spec):

1. Add `GET /api/task-sources/fetch?kind=<source>` endpoint to the
   khimaira daemon.
2. Daemon-side: invoke the requested adapter via its MCP client
   subsystem, return JSON list.
3. Adapters that need MCP (Linear, future Jira/Asana/etc.) live as
   daemon-side implementations; hook-side adapters become thin
   HTTP clients pointing at the daemon endpoint.
4. Hook's `hook_safe_only=True` becomes irrelevant — daemon
   abstraction makes every source hook-safe at the protocol level
   once the endpoint lands.

### ~~GitHub Issues adapter~~ ✅ shipped

`GithubTaskSource` lives at `packages/khimaira/src/khimaira/task_sources/github.py`.
Shells out to `gh issue list --assignee @me --state open --json ...`.
Hook-safe (no MCP / network dependency from the daemon perspective —
gh handles auth). Setup: `gh auth login` once, then add
`{kind: github, enabled: true}` to `~/.khimaira/task_sources.yaml`.

Failure modes all handled silently — gh not installed, not authed,
times out, returns garbage → return [], log warning, never break the
SessionStart hook.

### `/tasks` slash command

For users with Linear / non-hook-safe adapters, a slash command that
runs `fetch_all_open_tasks(hook_safe_only=False)` from agent context
(which DOES have MCP access). Surfaces what the hook can't.

### Aggregated cross-machine savings

Mentioned in the original cross-machine-backend spike but deferred
(see `tasks/cross-machine-backend/IMPLEMENTATION.md`). Independent
of task sources — separate Phase 1.6-ish work if it ever becomes a
priority.

## Done when (1.5 MVP)

- ✅ Protocol + Task dataclass shipped
- ✅ JsonlTaskSource adapter shipped
- ✅ SessionStart hook surfaces tasks alongside handoffs
- ✅ Config loader supports default + custom YAML
- ✅ 13 unit tests passing
- ✅ Linear / Jira / GitHub explicitly NOT named in the public roadmap

## Why this reframe matters

khimaira's NORTH_STAR principle #1: "Editor-agnostic via MCP." The
same logic applies to task trackers: tracker-agnostic via Protocol.
Naming any one vendor in the public roadmap quietly limits khimaira's
audience. Generic Protocol + community-extensible adapters is the
posture that lets khimaira land in any user's workflow.

## References

- Predecessor specs (all superseded):
  - `tasks/cross-machine-backend/IMPLEMENTATION.md` — original
    state-replication scope
- Module: `packages/khimaira/src/khimaira/task_sources/`
- Hook integration: `packages/khimaira/src/khimaira/hooks/session_start.py`
- Tests: `packages/khimaira/tests/test_task_sources.py`
