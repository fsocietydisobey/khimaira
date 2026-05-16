# khimaira — engineering rules for AI agents

> Patterns captured from real bugs that hit prod (or the dev loop)
> in this repo. Each rule is here because we shipped a bug it would
> have prevented.

## Testing

### Every endpoint that resolves a session name needs unknown-name coverage

**Why:** `khimaira.monitor.sessions.resolve_session_id` raises
`ValueError` on unknown names/UUIDs. FastAPI lets unhandled exceptions
become HTTP 500 with a stack trace. The right shape is HTTP 404 with
the helpful "use session_list()" message.

**Bug we shipped:** `POST /api/sessions/{name}/notice` 500'd on stale
names; `GET /api/sessions/{name}` 500'd on 12-char-hex inputs that
were question IDs the caller mistook for session IDs.

**The pattern in route handlers:**

```python
@router.post("/sessions/{session_id}/something")
async def something(session_id: str, req: ...) -> dict:
    try:
        return sessions.do_thing(session_id, ...)
    except ValueError as e:
        raise fastapi.HTTPException(404, str(e))
```

**The required tests** (one of each per endpoint):
- happy path → 200 + assert response shape
- unknown session name → 404 + assert error message contains "no session"

See `packages/khimaira/tests/test_sessions_api.py` for examples.

### Every primitive that mutates JSONL needs round-trip coverage

The `read → modify → atomic-rename` pattern is everywhere in
`khimaira.monitor.sessions`. A subtle off-by-one in the rewrite (e.g.
"only rewrite when modified" failing to drop expired entries when no
modify happens) creates state-bloat bugs that don't surface until
the file is large.

**Test pattern:**
```python
def test_x_round_trip(isolated_state):
    # Write
    isolated_state.do_thing(...)
    # Read back via the read-side function
    state = isolated_state.read_thing(...)
    assert state["thing"] == expected
    # Then a path that should mutate or GC
    isolated_state.gc_or_mutate(...)
    # Verify the file state matches expectation
    contents = isolated_state._FILE_PATH.read_text()
    assert "stale_marker" not in contents
```

**Bug we shipped:** `consume_handoffs` only rewrote `handoffs.jsonl`
when an unread+matching entry got its `read_by` updated. If all
entries in the file were expired, no rewrite fired, and the file
accumulated forever. Caught by `test_consume_handoffs_expired_dropped`.

### Test the unhappy path before shipping

Demo / happy-path tests prove "feature works"; they don't prove "feature
doesn't crash on garbage input." Before merging, list at least 3
inputs that should fail gracefully and verify each.

For session-resolving endpoints: bad name, missing argument, malformed JSON.
For storage primitives: empty file, corrupt file, all-expired file.
For long-running supervisors: child exits 0 (clean), exits non-zero (restart),
SIGTERM mid-flight (graceful shutdown).

## API surface

### Every long-lived daemon needs a supervisor

`khimaira monitor` runs as a forked daemon. If it dies (OOM, manual
SIGKILL, parent shell HUP), nothing restarts it. The whole stack
(observer, dashboard, MCP tools that depend on the daemon) silently
breaks.

**Two paths shipped:**
- `khimaira monitor watch` — cross-platform foreground supervisor with
  exponential backoff
- `khimaira monitor install-service` — systemd user unit on Linux
  (the right answer; macOS users use `watch`)

**Rule:** any future long-lived khimaira daemon should ship
foreground + supervisor patterns from day one, not as a follow-up.

### Error paths must include the correct primitive name in messages

When `/inbox 087234eb17d2` returns "📭 inbox empty", that's correct
output but unhelpful — the user passed a question ID, not a session
ID. The right message is "12-char hex looks like a question ID; try
`/notes <session>` or `mcp__khimaira__session_state(<session>)`."

**Rule:** when a tool gets a clearly-wrong-shape input (12-char hex
where a UUID was expected, etc.), the error/empty path should suggest
the closest correct primitive by name. Saves the next layer of
"why doesn't this work?" loop.

## Cross-session coordination

### Default to delegating when other sessions are available

**Before starting any non-trivial work, check the active session pool first.** The SessionStart
hook surfaces the list of other active sessions automatically (`📋 khimaira — N other session(s)
active`). If any are idle/listening and the work is parallelizable, default to delegating —
don't burn master's context on tasks an agent can do.

**Standard pattern at the start of a non-trivial task:**

1. **Read the active-sessions block** from SessionStart context (already in your context window).
   Look for sessions with `status: idle` or `status: listening` — those are immediately available.
2. **Decide what's parallelizable**: research, implementation of independent functions, writing
   tests for new code, verifying assumptions in parallel files. NOT parallelizable: linear edit
   sequences where step N depends on step N-1's exact output.
3. **Pick the assignment primitive**:
   - **`/khimaira-assign <agent> "<task>" --model X --effort Y`** — when you need to coordinate
     budget across multiple agents (master controls cost, fires begin signal once all confirmed)
   - **`chat_task_create` + `chat_send_to`** — when agents are already at the right budget and
     you just need to fan out work (lighter-weight than full assign gate)
   - **`session_log_question(target_session_id=...)`** — when you need a one-shot answer from a
     specific peer without launching a structured task
   - **`session_post_handoff(scope_project=...)`** — when the work is for the NEXT session in
     this project, not a currently-running peer
4. **Coordinate via chat** for any work spanning multiple agents touching the same file. Tell
   each agent who else is working on what so they can sequence edits.
5. **Master's job during delegation**: integrate, review, decide. Don't duplicate agent work.
   If you find yourself doing what an idle agent could do, stop and re-delegate.

**Anti-patterns:**

- Burning Opus context on grep / file-read / boilerplate-function-writing when an idle agent at
  sonnet/medium could do it in parallel
- Forgetting the active-sessions list exists and working solo when 3 agents are sitting idle
- Delegating a task to an agent without telling them who else is working in the same file
  (write conflicts)
- Re-delegating the same thing that's already in_progress on an agent (check chat_history before
  firing new tasks)

**When to do it yourself instead of delegating:**

- Tightly-coupled multi-step edits in one file (overhead of coordination > overhead of doing it)
- Architectural decisions that need master's full context
- Work that requires reading what JUST happened in this conversation (agent doesn't have it)
- Single-line bug fixes where the round trip to an agent costs more than the fix

### Use the right primitive

| Goal | Tool | Notes |
|---|---|---|
| Ask a sister session, need answer | `session_log_question(target_session_id=B)` + `session_wait_for_answer` | Targeted; auto-surfaces in B's hook |
| FYI / ack, no reply expected | `session_post_notice` | Re-surfaces up to 3 turns then auto-expires |
| **Task for the next session in this project** | `session_post_handoff(scope_project=...)` | **Directive, not informational.** Auto-surfaces on any future session's SessionStart hook. The receiving agent is expected to START on it, not wait for user confirmation. |
| **Delegate a slice of your handoff to a specific session** | `session_invite_handoff(parent_id, me, invitee, text)` | Owner-only. Child handoff targets the named session; cwd-peers skip. Invitee gets immediate inbox notice + SessionStart-hook surface on next boot. |
| Read what a stopped session said | `session_query_transcript` / `session_summarize_transcript` | Heuristic — no LLM call from khimaira |
| State a commitment, no audience | `session_log_decision` | Pull-only via `session_state` |

### Handoffs are directives, not FYIs

When you boot and see a `📦 khimaira handoffs` block:

1. **Treat it as your task list.** The prior session left it specifically for whoever picks up here. The user posted it; they don't need to re-authorize each step.
2. **Read the linked files / specs first.** Don't summarize until you've read.
3. **Propose a concrete first action** — pick the highest-priority item, state in one sentence which file/line you're starting at.
4. **Then start.** Don't wait for "yes do that" — the handoff IS the authorization. The user redirects if you're heading wrong.
5. **If genuinely ambiguous**, ask ONE clarifying question. Don't enumerate options.

This is the difference between "agent reads handoff, summarizes, waits for instructions" (wrong — duplicates effort the user already did) and "agent reads handoff, picks an item, starts working, reports progress" (right — what handoffs are for).

**Anti-pattern:** logging questions when no answer is needed. The
ping-pong pattern (ask, wait, ask follow-up) is usually worse than
just deciding and proceeding. See `session_log_question` docstring.

### Naming sessions is load-bearing for handoffs

A session that's about to end its work but hasn't named itself can't
be referred to by future sessions except by UUID. Name yourself
**before** logging a handoff or any decision a future session might
need to find:

```python
session_set_name(session_id, "feature-x-rewrite")
session_post_handoff(from_session_id, "HANDOFF: ...", scope_cwd=...)
```

Otherwise the handoff text refers back to the asker by 8-char prefix,
which is fine but less discoverable than a slug.

## Operational

### Observer changes need both venvs redeployed

`khimaira_observer` is venv-injected via `khimaira attach`. If you change
the observer code in `packages/khimaira/src/khimaira/attach/observer_template/`,
running apps don't pick up the change automatically. They need:

1. `khimaira detach <project>`
2. `khimaira attach <project>`
3. App restart (the running process has the OLD code in memory)

**Rule:** when you bump `khimaira_observer.__version__`, redeploy to
all attached projects (`khimaira attached` lists them) and remind the
user the apps need restart for the new code to take effect.

### Daemon restart wipes the in-memory heartbeat buffer

`khimaira-monitor` keeps heartbeats in-memory (`_runs: dict[(project,
run_id), RunEntry]`). Restarting the daemon flushes the buffer.
LangGraph runs that completed pre-restart are gone from observer
queries (they're still in the LangGraph checkpointer DB; just not in
the live channel).

**Rule:** if you're investigating "I had data and now it's gone,"
check whether the daemon was restarted between observation and query.
The cost dashboard, slow-call alerts, and trace waterfall all read
from the same in-memory store; all are affected the same way.

## Documentation

### Test files are docs

The fastest way to learn what `consume_handoffs` does is read
`test_consume_handoffs_*` in `packages/khimaira/tests/test_sessions_unit.py`.
Tests exercise corner cases the docstrings don't mention.

**Rule:** when adding a new function to the sessions module, add a
test that demonstrates the contract — even if the function "obviously
works." The test is documentation that doesn't go stale.

### Don't `--no-verify` past pre-commit hooks

If a hook fails, fix the failure, don't bypass. Bypassed hooks are
how today's "it worked yesterday" bugs got into main.

## Pointers

| What | Where |
|---|---|
| Open task specs | `tasks/<name>/IMPLEMENTATION.md` |
| Phase status | `tasks/BUILD-PLAN.md` |
| Test conventions | `packages/khimaira/tests/conftest.py` |
| Hook scripts | `scripts/hooks/` |
| Observer template | `packages/khimaira/src/khimaira/attach/observer_template/` |
| Slash commands | `~/.claude/commands/*.md` (symlinked from dotfiles) |
| Discoverability | `khimaira tools` or `/tools` |
