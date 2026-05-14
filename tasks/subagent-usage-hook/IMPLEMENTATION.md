# Subagent Usage Hook — record `~/.claude/agents/` dispatches in `usage.jsonl`

> **Status:** Spec'd, not started. Phase 1.2b of NORTH_STAR Phase 1.2.
> **Depends on:** 1.2a (`tasks/subagent-library/`) shipped.
> Build when: 1.2a has been in use for a few days and `khimaira usage`
> shows zero subagent dispatches despite real-world usage — that's the
> observability gap this task closes.

## Problem

1.2a shipped four `khimaira-*` subagents at `~/.claude/agents/`. When
Claude Code's auto-delegation routes a prompt to (say) `khimaira-factual`,
the subagent runs on Haiku instead of the parent's Opus model. The
**token savings are real**, but they don't appear in `usage.jsonl` —
Claude Code dispatches the subagent directly, bypassing the khimaira
MCP layer.

Net effect: `khimaira usage savings` undercounts. The user is saving
money but the dashboard can't see it.

## Design

Listen for subagent completion via a Claude Code hook event, parse the
relevant metadata, write a `UsageRecord` with `mode="subagent"`.

### Step 1 — verify the hook landscape

Before writing anything: confirm what hook event fires when a subagent
completes, and what metadata it exposes. Candidates:

- `SubagentStop` (most likely)
- `PostToolUse` on the `Task` tool (if subagents go through Task)
- `Stop` with a discriminator field

Verification path:
1. Read `~/.claude/docs` if present, or the Claude Code settings.json
   hook reference.
2. Drop a no-op hook script that just logs `os.environ` + the JSON
   payload to `/tmp/khimaira-hook-probe.log`.
3. Invoke a `khimaira-*` subagent from a fresh Claude Code session.
4. Inspect the log to see what fires and what's in the payload.

**Decision point:** if the payload includes `model` and `input_tokens`
+ `output_tokens`, we write a `UsageRecord` directly. If it only
includes a transcript path, we parse the transcript JSONL to extract
token counts (~50 LOC parser, khimaira-extension's estimate).

### Step 2 — extend the `Mode` Literal

```python
# shared/types/src/khimaira_types/usage.py
Mode = Literal[
    "auto",
    "explicit-tier",
    "manual",
    "subagent",  # NEW — Claude Code routed a prompt to a ~/.claude/agents/*.md
    "unknown",
]
```

Mind: this is a Pydantic `Literal` — adding a value is a breaking
change for old records. Existing records have `mode="auto" | "manual" |
"explicit-tier" | "unknown"` which all stay valid. The new value only
ever appears on records the new hook writes.

### Step 3 — write the hook

Location: `packages/khimaira/src/khimaira/hooks/subagent_stop.py` (new file).

Pattern matches existing hooks at `hooks/session_start.py` and
`hooks/user_prompt_submit.py`.

Shape:
```python
def main() -> None:
    payload = json.loads(sys.stdin.read())
    if not payload.get("subagent_type", "").startswith("khimaira-"):
        return  # only record khimaira-* dispatches
    # Extract model, tokens, latency from payload OR parse transcript
    rec = UsageRecord(
        ts=...,
        runner="claude",      # Claude Code is the runner
        provider="anthropic",
        model=...,
        role=payload["subagent_type"],
        input_tokens=...,
        output_tokens=...,
        latency_s=...,
        source="cli",
        mode="subagent",
    )
    # Append to ~/.local/state/khimaira/usage.jsonl
    ...
```

### Step 4 — register the hook

Two ways the hook can be registered:

(a) `install_claude_hooks: true` in the bootstrap profile already
re-writes `~/.claude/settings.json` on every `khimaira sync`. Add the
new hook event there. Lives at
`packages/khimaira/src/khimaira/cli/bootstrap.py` (search for the
settings.json writer).

(b) Manually add to `~/.claude/settings.json` for testing.

Go with (a) so future machines pick it up.

### Step 5 — savings command awareness

`khimaira usage savings` already groups by `mode`. With `"subagent"`
as a new value, the command should:

- Count subagent dispatches in the savings tally (a subagent on Haiku
  IS a savings event vs the counterfactual Opus turn).
- Optionally surface a separate line in the audit output:
  *"subagent dispatches: 23 (saved ~$X.XX)"*.

Find the function via:
```
grep -n "def \(savings\|_compute_savings\)" packages/khimaira/src/khimaira/cli/usage.py
```

## Tests

Per CLAUDE.md, three unhappy-path scenarios at minimum:

- **No subagent metadata in payload.** Hook should log a warning and
  exit cleanly. Never crash Claude Code's subagent dispatch.
- **Transcript file missing.** If we're parsing transcripts and the
  file isn't there yet (race), retry once with a short sleep, then
  give up cleanly.
- **`usage.jsonl` write failure** (disk full, permission denied).
  Hook must not block the subagent's completion — log and move on.

Plus a round-trip test per CLAUDE.md "every primitive that mutates
JSONL needs round-trip coverage":

- Write a `mode="subagent"` record → read it back via the usage loader
  → confirm it loads as a valid `UsageRecord`, not `unknown`.

## Done when

1. `~/.claude/settings.json` has the `SubagentStop` (or equivalent)
   hook entry pointing at khimaira's handler.
2. Invoking `@"khimaira-factual (agent) ..."` in a fresh session
   produces a line in `~/.local/state/khimaira/usage.jsonl` with
   `mode="subagent"`.
3. `khimaira usage savings` includes subagent dispatches in its
   totals.
4. Tests for the three unhappy paths pass.
5. NORTH_STAR.md Phase 1.2 done-gate is fully satisfied.

## References

- 1.2a spec: `tasks/subagent-library/IMPLEMENTATION.md`
- UsageRecord schema: `shared/types/src/khimaira_types/usage.py`
- Existing hook pattern: `packages/khimaira/src/khimaira/hooks/`
- Bootstrap hook installer: `packages/khimaira/src/khimaira/cli/bootstrap.py`
- Savings command: `packages/khimaira/src/khimaira/cli/usage.py`
