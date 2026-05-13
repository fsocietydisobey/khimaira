# Workspaces — multi-session scoping

> **Status:** Shipped (MVP). Opt-in workspace filter on read paths +
> cross_workspace guard on targeted questions. CLI commands (move /
> list-by-workspace) deferred to v2 per spec.

## Problem

Today's khimaira multi-session model is "everything visible, only targets
get pinged." Every session writes to `~/.local/state/khimaira/sessions/`,
and every session can read every other session's state via
`list_sessions()`, `session_state(any_id)`, and `session_recent_decisions()`.

Pings (UserPromptSubmit hook auto-injects) are correctly scoped — only
the explicit `target_session_id` of a question receives the
`📨 khimaira incoming` block. Unrelated sessions don't get pinged.

But READ visibility is global. Concrete cases where that's a problem:

1. **Multi-client work:** sessions for client-A and client-B both
   discoverable in `list_sessions()`. Pre-engagement-isolation
   policies might require hard separation.
2. **Personal vs. work projects:** a personal experiment session
   showing up in `list_sessions()` alongside production work
   sessions creates noise.
3. **Long-lived session bloat:** sessions you ran weeks ago still
   appear in `list_sessions()` and `session_recent_decisions()`,
   diluting the "what's active right now" signal even with TTL
   filters.

The right primitive is **workspaces** — named groups of sessions that
share visibility. Sessions inside a workspace see each other; sessions
outside don't (unless an explicit cross-workspace flag is passed).

## Design

### Data model

Add `workspace` field to `status.json`:

```json
{
  "status": "implementing",
  "detail": "...",
  "name": "khimaira-builder",
  "workspace": "khimaira-dev",     // NEW; defaults to "default"
  "updated_at": "..."
}
```

Backward compatibility: sessions without a `workspace` field are
treated as belonging to workspace `"default"`. No migration script
needed — read paths fall through.

### Set-time API

Extend `session_set_name` (or add `session_set_workspace`):

```python
# Option A — overload set_name
session_set_name(session_id, "khimaira-builder", workspace="khimaira-dev")

# Option B — separate tool
session_set_workspace(session_id, "khimaira-dev")
```

**Recommendation: Option B.** Separation of concerns. Name and
workspace are orthogonal — a session's name describes what it does;
its workspace describes which group it belongs to. Bundling them
forces every name change to re-think workspace.

### Read-side filtering

All read paths gain an optional `workspace` filter. Default behavior:

| Tool | Default scope | Override |
|---|---|---|
| `list_sessions()` | Caller's own workspace | `workspace="*"` for all |
| `session_state(id)` | Allowed if same workspace as caller | Explicit `cross_workspace=True` |
| `session_recent_decisions()` | Caller's workspace | `workspace="*"` |
| `incoming_questions()` | Caller's workspace | (no override — pings stay scoped) |

The hook's auto-inject paths (incoming + pending) use the caller's own
workspace as the scope. Cross-workspace targeted questions are blocked
by default — they'd have to be explicitly flagged when logging.

### Write-side targeting

`session_log_question(target_session_id="X")` should resolve `X` within
the caller's workspace first. If `X` is in a different workspace,
either:

- **Reject** (safer default — forces explicit cross-workspace intent)
- **Allow with `cross_workspace=True` flag**

**Recommendation: reject by default + add `cross_workspace=True` flag**
to `log_question` for the rare case you want to explicitly cross.

### Caller identity

To filter "by caller's workspace," the daemon needs to know which
workspace the calling session belongs to. The current API tools take
`session_id` as the first arg, so the daemon can look up the workspace
from the caller's `status.json`. No new auth surface.

### Migration

Zero migration. Existing sessions auto-default to workspace `"default"`.
Old broadcast questions remain visible only via session_state polling.
Old targeted questions still work (as long as target_session_id is in
the same default workspace, which it will be by definition).

### Edge cases

- **Renaming workspaces:** sessions own their workspace string. Bulk
  rename = a script that walks `~/.local/state/khimaira/sessions/` and
  rewrites status.json. Out of scope for v1; add as utility command
  later if needed.

- **Session moves workspace mid-flight:** allowed. The `set_workspace`
  call updates status.json atomically. Sessions that had visibility
  prior lose it on next read.

- **Empty/unset workspace:** treat as `"default"` everywhere. Don't
  let `""`, `None`, missing key produce three different behaviors.

- **Workspace name validation:** require kebab-case `^[a-z0-9-]+$`,
  max 40 chars. Prevents path injection and shell-quoting issues.

## Implementation steps

1. **`sessions.set_workspace(session_id, workspace)`** — atomic
   read-merge-write of status.json. Mirror set_status's
   "preserve other fields" pattern. Validate workspace name.

2. **`sessions.get_workspace(session_id)`** — lookup helper
   returning the resolved workspace (defaults to `"default"` if
   unset). Used internally by every read path.

3. **Read-path filters** — modify `list_sessions`, `state`,
   `recent_decisions`, `incoming_questions`, `pending_notes` to
   accept an optional `workspace` arg. Default: caller's workspace
   (resolved via the session_id arg they pass).

4. **`log_question` cross-workspace check** — add validation when
   `target_session_id` is set; reject if mismatched workspaces
   unless `cross_workspace=True`.

5. **HTTP API** — add `?workspace=X` query param to relevant GETs;
   add `cross_workspace` field to question POST body.

6. **MCP tools** — add `session_set_workspace`, update docstrings on
   read tools to mention workspace filtering, add `cross_workspace`
   kwarg where relevant.

7. **Hook** — UserPromptSubmit hook needs to know caller's workspace
   to scope its fetches. Trivial: it already has the session_id; the
   daemon-side endpoints will filter by it automatically once
   read-paths gate on workspace.

8. **CLI commands** — `khimaira sessions list --workspace X`,
   `khimaira sessions move <id> <workspace>`. Optional v2.

## Test plan

- Unit: workspace filter logic in `incoming_questions`,
  `list_sessions`, `state` — assert sessions in workspace A don't
  appear when caller is in workspace B.

- Integration: spin two real sessions, set distinct workspaces, verify:
  - cross-workspace `session_state` returns 404 (or empty) without
    `cross_workspace=True`
  - cross-workspace targeted question rejected without
    `cross_workspace=True`
  - same-workspace ops unaffected (no regression on the existing
    khimaira ↔ jeevy collaboration path)

- Backward compat: existing sessions (no workspace field) all behave
  as workspace `"default"`. New session that doesn't call
  `set_workspace` is in `"default"` and sees other default sessions.

## Open decisions (for whoever picks this up)

1. **Should `session_recent_decisions()` honor workspace?** Today it's
   a debugging convenience. If we scope it, we lose the "what's
   happening across all my work" view. Counter-argument: that view is
   the noise problem we're trying to solve.
   - Lean: **scope by default, add `workspace="*"` for the all-view.**

2. **Should the daemon's web UI (khimaira-monitor frontend) show all
   workspaces or just one?** Today it shows everything. With
   workspaces, the UI needs a workspace switcher.
   - Lean: **default to "all workspaces" in UI, since the user is
     the trusted observer. The privacy boundary is for inter-session
     visibility, not for the user themselves.**

3. **Workspace inheritance from working directory?** Auto-detect
   workspace from `cwd` (e.g., khimaira repo → `khimaira-dev`)?
   - Lean: **no.** Explicit > implicit. cwd is fragile (sessions
     can `cd` mid-flight).

## Effort estimate

~80 LOC core (set/get + filter helpers) + ~40 LOC API/MCP wiring +
~30 LOC tests = **~150 LOC, ~half-day of focused work.**

## Notes

- Don't ship until there's a real privacy/noise pain point. Today's
  user (Joseph) has khimaira + jeevy both under "default" — adding
  workspaces now would force a no-op `set_workspace("default")` step
  with zero benefit until a third unrelated project shows up.

- If/when shipped, the `khimaira attached` CLI output should display
  workspace per project. The observer auto-attach machinery doesn't
  need workspace logic — observers just write heartbeats; they're not
  sessions.
