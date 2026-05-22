# Themis — Role-Invariant Enforcement Service

**Status**: spec landed 2026-05-21. Phase 1 implementation pending.

**Origin**: Two role-boundary violations within a single sprint, each costing real
time and producing identical postmortems:

1. **2026-05-21** — `jp-intake-1` bumped `udoc-viewer` to 0.6.41 by editing
   `package.json`, deleting `DocMentisAnnotationHover.css`, and removing an import
   directly — instead of scoping the work and handing off to a worker agent.
   Self-reported the violation. Worker agents sat idle. Intake's status surface
   drifted because it was mid-edit.

2. **2026-05-21** — `janice-0` (jp master) went silently offline for ~15h because
   the `chat_my_chats` SSE registration call wasn't made on a post-compaction turn.
   Missed `jp-architect-1`'s optimization analysis. Joseph waited ~15 min with no
   master dispatching. Second occurrence of the same failure pattern (first:
   2026-05-19 v1.6 deputize retest).

Both share a root cause: **stronger language in role briefs is not enough.** Even
self-aware agents that know the rule violate it under load. Both incidents have
been addressed at the brief-template layer (see `roles/intake.md` and
`roles/master.md` post-2026-05-21 edits) but those are post-hoc patches — the next
load-bearing incident likely has a different surface.

The structural fix is to make role boundaries impossible to violate, not just
strongly advised against. Themis is that fix.

---

## Current state (pre-task)

Role boundaries live in two places today:

1. **`packages/khimaira/src/khimaira/roles/*.md`** — the role file, auto-injected
   into each session's context by the SessionStart hook. Rules are stated as
   prose constraints under "Constraints" / "Never" / "Failure mode in practice"
   sections.

2. **`~/dotfiles/claude/commands/khimaira-bootstrap-roster.md`** — the brief
   template embedded inline in the bootstrap skill. Mirrors the role.md rules
   but in a denser format suited for chat dispatch.

Enforcement is purely advisory:

- The agent reads the role.md and the brief on session start.
- The agent self-checks before acting.
- Observer scans transcripts post-hoc for anomalies but doesn't catch
  rule-specific violations.

When an agent violates a boundary, the incident is logged informally
("Failure mode in practice (2026-05-21)") as a postmortem in the relevant
role.md. There is no programmatic enforcement, no violation log, no pattern
analysis, no PreToolUse gate.

PreToolUse hook capabilities are confirmed (probe v2, 2026-05-21):

- Stdin envelope contains `session_id`, `cwd`, `tool_name`, `tool_input`,
  `transcript_path`, `tool_use_id`, `permission_mode`, `effort.level`.
- `session_id` is the khimaira session_id directly (no mapping needed).
- Block mechanism: emit `{"decision": "block", "reason": "..."}` on stdout +
  exit 0 → tool blocked, reason surfaced to the model. (Alternative: exit 2 +
  stderr also blocks but produces a noisier "hook error:" prefix.)

---

## What this adds

A new package `packages/themis/` providing:

1. **Per-role Invariant rule store** — YAML files defining each role's
   inviolable rules with matchers, severity, and human-readable block messages.
2. **MCP tool surface** — `themis_my_rules`, `themis_check`, `themis_list_rules`,
   `themis_record_violation`, `themis_violations_for`, registered on khimaira's
   MCP server under source-prefixed names (per existing Séance/Specter pattern).
3. **PreToolUse hook script** — installed via `khimaira attach`, called on every
   matched tool call to check the proposed action against the session's role
   invariants. Blocks via JSON `{"decision": "block", ...}` on violation.
4. **Daemon endpoints** — `/api/sessions/<id>/role` (resolve role from chat
   membership), `/api/themis/check` (combined role-resolve + rule-check for
   minimum hook latency), `/api/themis/violations` (read violation log).
5. **Violations log** — append-only JSONL at
   `~/.local/state/khimaira/themis_violations.jsonl`. Replay-on-boot rebuilds
   query state. Each entry references `session_id`, `tool_use_id`, `rule_id`,
   `tool_name`, `tool_input_summary`.

---

## Decisions (all locked)

| # | Question | Decision | Why |
|---|---|---|---|
| D1 | Rule storage format | YAML, one file per role at `packages/themis/src/themis/rules/<role>.yaml` | Human-editable, no recompile loop, matches the project's existing taste (chats.py role budgets are dicts but rule schemas need richer structure than dicts read cleanly). |
| D2 | Block mechanism | Stdout JSON `{"decision": "block", "reason": "..."}` + exit 0 | Verified working via probe v2. Cleaner UX than exit 2 + stderr (no "hook error:" prefix). Extensible — `{"decision": "approve"}` is reserved for future explicit-allow signals. |
| D3 | Daemon endpoint shape | One combined endpoint `POST /api/themis/check` that takes `{session_id, tool_name, tool_input, cwd}` and returns `{ok: bool, violation?: {rule_id, message}}` | One HTTP call per hook fire, not two. Latency budget: <20ms p99. |
| D4 | Role resolution source | Query chat membership for the most recently-active chat that has a role assigned to this session. If session is in multiple chats with different roles, use the most recent. If session has no role assignment, return null → hook passthrough. **Live-queried at every check call**, not cached at session-start. During `/khimaira-deputize`, the vice acquires master-role membership and Themis serves it master rules; after `/khimaira-resume`, the original session reacquires master-role and Themis serves it master rules. | Roles are already tracked in `chat_create_room`'s `member_roles`. No new role-binding primitive needed. Live-querying eliminates the deputize/resume role-staleness gap (per architect-1 ctx-734a8fc7 must-fix #3, was open Q2). |
| D5 | Severity levels | `block` (PreToolUse returns block), `warn` (logged + `session_post_notice` fired to observer-1 + tool runs), `audit` (logged only, no surfaces) | Block is the load-bearing one. Warn covers rules too vague for mechanical blocking (e.g., "broadcast CONTEXT UPDATE before first delegation" — hard to detect mechanically). Audit is for new rules under trial — collect data before flipping to block. Warn-mode surface channel locked to `session_post_notice` (async, won't interrupt observer's scan loop) per architect-1 ctx-734a8fc7. |
| D6 | Conditional rules | Invariants may declare `conditions:` — named checks evaluated against runtime state before the rule fires. Phase 1 ships with `idle_agents_exist` and `chat_my_chats_not_called_this_turn` only. | Allows rules like "master can't spawn a standalone agent IF roster agents are idle." Without conditions the rule is wrong (fine when roster is at capacity). |
| D7 | Failure mode when daemon down | Fail-open: hook exits 0, allows tool, logs warning to `~/.claude/hooks/themis_fail_open.log`. | Themis is a guardrail, not a security gate. Daemon downtime shouldn't lock the user out of editing. |
| D8 | Phase boundaries | Phase 1: data + MCP tools (no PreToolUse hook yet). Phase 2: hook integration. Phase 3: analytics + dashboard. | Phase 1 is independently useful — observer can call `themis_check` in its scan loop for post-hoc violation surfacing. Phase 2 is the structural fix. Phase 3 is nice-to-have. |
| D9 | Hook install path | Installed via `khimaira attach <project>` into the project's `.claude/settings.local.json`. Removed via `khimaira detach`. | Matches existing observer-template pattern. Project-scoped — Themis doesn't fire on non-khimaira projects. |
| D10 | Initial rule set | Start with 2-4 rules per role, all targeting concretely-observed failures. Expand only when a new failure mode is observed twice. | Avoid the YAGNI trap of writing 22 letters before any have been validated. The 2026-05-21 incidents give us the first 4 must-have rules; add more on the next incident. |
| D11 | Phase 2 calendar commitment | Phase 2 hook integration ships within **1 week** of Phase 1 daemon ship. | Phase 1 alone is post-hoc detection only (observer scan-loop calls `themis_check`); only Phase 2 prevents violations. Separating phases gives risk isolation (Phase 2 bugs can lock out editing) but the gap must be short — the structural fix IS Phase 2. Per architect-1 ctx-734a8fc7. |
| D12 | `themis_violations_for` read auth | Caller may read its own `session_id`'s violations only, unless `role ∈ {master, observer, critic}` — those roles may read any session_id. Daemon enforces in the endpoint handler. | Privacy gap: agent-1 reading agent-2's violation history is a real concern in shared rosters. Master/observer/critic legitimately need cross-session visibility. Per architect-1 ctx-734a8fc7. |
| D13 | Fast-rollback CLI | `khimaira themis disable <rule_id>` flips a rule's severity to `audit` (logs only, no block) without editing YAML. State override stored in `~/.local/state/khimaira/themis_overrides.jsonl`, consulted on every check. `khimaira themis enable <rule_id>` reverts. | When a rule produces a false-positive lockout, YAML edit + `khimaira themis sync` is slow. Override layer is faster than `khimaira detach`. Per architect-1 ctx-734a8fc7. |

---

## Schema

### Per-role rule file — `packages/themis/src/themis/rules/<role>.yaml`

```yaml
role: intake
invariants:
  - id: IN-INTAKE-1
    name: NO_FILE_EDIT
    severity: block
    matchers:
      - tool: Edit
      - tool: Write
      - tool: MultiEdit
      - tool: NotebookEdit
    message: |
      🛑 Themis IN-INTAKE-1 (NO_FILE_EDIT): intake cannot call {tool_name}.
      Hand off to an agent via /khimaira-assign <agent> "<task spec>" instead.
      The boundary exists so intake's status answers stay current — the
      moment intake implements, its "what's the status?" answer drifts.

  - id: IN-INTAKE-2
    name: NO_API_DISPATCH
    severity: block
    matchers:
      - tool: mcp__khimaira__auto
      - tool: mcp__khimaira__delegate
      - tool: mcp__khimaira__research
    message: |
      🛑 Themis IN-INTAKE-2 (NO_API_DISPATCH): these tools hit the Anthropic API
      and duplicate the roster's dispatch layer. Use /khimaira-assign instead.

  - id: IN-INTAKE-3
    name: NO_STANDALONE_AGENTS
    severity: block
    matchers:
      - tool: Task
    message: |
      🛑 Themis IN-INTAKE-3 (NO_STANDALONE_AGENTS): cannot spawn worktree or
      background Claude Code agents. Check session_list() — if agents are idle,
      use /khimaira-assign. Standalone agents bypass the enforcement gate,
      context broadcast, observer, and task lifecycle.
```

### Matcher shapes

```yaml
# Exact tool name match (most common)
matchers:
  - tool: Edit

# Pattern match on a tool_input field
matchers:
  - tool: Edit
    tool_input_field:
      field: new_string
      pattern: 'os\.getenv\(["\'](?:ANTHROPIC|OPENAI|AZURE|GOOGLE|GEMINI)_'

# Bash command convenience matcher
matchers:
  - tool: Bash
    tool_input_field:
      field: command
      pattern: '--no-verify\b'
```

Each matcher is OR-combined within an invariant. An invariant fires if ANY
matcher matches. Multiple invariants per role evaluate in **two passes**:

1. **Collect** every matched invariant whose `conditions:` all evaluate true.
2. **Select** by severity rank: `block` > `warn` > `audit`. Ties within the
   same severity are broken by id-order (lexicographic on `id`).

Exactly one invariant is returned per `themis_check` call. The hook acts on
`severity` (block → exit-block, warn/audit → exit-allow). Lower-severity
matches NEVER suppress higher-severity matches on the same call — this
guarantees the structural enforcement contract isn't silently degraded by
id-order accidents (e.g., an audit rule with a low id silently shadowing
a block rule with a higher id).

### Condition shapes

```yaml
conditions:
  - check: idle_agents_exist     # Phase 1 — looks up session_list() for
                                  # sessions with role=agent and status=idle
  - check: chat_my_chats_not_called_this_turn  # Phase 1 — queries chat server
                                                # for last subscriber heartbeat
```

Conditions are **AND-combined only** (no OR/NOT in Phase 1). ALL conditions
must evaluate true for the rule to fire. If `conditions:` is absent, the rule
always fires when matched. Document this constraint visibly in each YAML
file so rule authors don't reach for OR/NOT and find no support.

Conditions live in `packages/themis/src/themis/conditions.py` as named functions
that take `(payload, daemon_client)` and return bool. New conditions added as
needed; not extensible via YAML (intentional — conditions are code, not data).
A DSL is deferred — refactor cost when a real DSL need emerges (5+ named
conditions OR a first OR/NOT case) is ~1 day (yaml `check: <name>` becomes
`check: <expr>`; existing functions become the eval registry). Per architect-1
ctx-734a8fc7 worth-noting.

### Violations log — `~/.local/state/khimaira/themis_violations.jsonl`

```json
{
  "ts": "2026-05-21T17:42:18-05:00",
  "session_id": "d13300a7-da03-4ff3-9e47-a7ef463b09dc",
  "session_name": "khimaira-0",
  "role": "master",
  "rule_id": "IN-MASTER-2",
  "tool_name": "mcp__khimaira__auto",
  "tool_use_id": "toolu_014Gp...",
  "tool_input_summary": "{...truncated to 500 chars...}",
  "decision": "blocked",
  "cwd": "/home/_3ntropy/dev/khimaira"
}
```

Append-only. **Compaction policy** (per architect-1 ctx-734a8fc7): on >1MB
OR explicit `khimaira monitor compact-themis`, atomic rename: copy current
`themis_violations.jsonl` to `themis_violations.YYYYMMDD-HHMMSS.jsonl.gz`
(gzipped archive), then truncate the live file to entries newer than
`now() - 30d`. Archives kept indefinitely (small relative to git history).
Replay-on-boot reads live file only; archives are for postmortem queries via
a separate `khimaira themis query-archive` CLI (Phase 3+).

---

## Module layout

```
packages/themis/
├── pyproject.toml
├── README.md
├── src/themis/
│   ├── __init__.py
│   ├── data.py              # Invariant model + YAML loader
│   ├── engine.py            # rule evaluation
│   ├── conditions.py        # named condition functions
│   ├── violations.py        # violations log read/write
│   ├── server.py            # FastMCP tool registration (mirrors seance/specter pattern)
│   └── rules/
│       ├── intake.yaml
│       ├── master.yaml
│       ├── agent.yaml
│       ├── observer.yaml
│       ├── architect.yaml
│       ├── analyst.yaml
│       ├── verifier.yaml
│       └── critic.yaml
└── tests/
    ├── conftest.py
    ├── test_data.py         # YAML schema, loader edge cases
    ├── test_engine.py       # matcher logic, condition AND-combining
    ├── test_conditions.py   # each named condition
    ├── test_violations.py   # JSONL round-trip, compaction
    └── test_mcp.py          # MCP tool surface

scripts/hooks/
└── themis_pretool.py        # PreToolUse hook script (referenced from settings.json)

packages/khimaira/src/khimaira/attach/observer_template/
└── (existing observer template — Phase 2 adds the themis_pretool entry to settings.local.json)

packages/khimaira/src/khimaira/monitor/api/
└── themis.py                # /api/themis/check + /api/themis/violations + /api/sessions/<id>/role
```

---

## MCP tool surface (Phase 1)

All tools registered on khimaira's MCP server under `themis_` prefix
(re-registered from `themis.server` at boot, per existing Séance/Specter/Scarlet
pattern documented in CLAUDE.md).

| Tool | Args | Returns | Use case |
|---|---|---|---|
| `themis_my_rules` | `session_id` | `[{id, name, severity, message_template, matcher_summary}]` | Agent self-introspection: "what rules am I bound by?" Read-only. |
| `themis_list_rules` | `role` (optional) | `[{role, invariants: [...]}]` | Observer/master surveying the full rule set. |
| `themis_check` | `session_id`, `tool_name`, `tool_input`, `cwd?` | `{ok: bool, violation?: {rule_id, name, message, severity}}` | Hook + observer post-hoc scan. The load-bearing primitive. |
| `themis_record_violation` | `session_id`, `rule_id`, `tool_name`, `tool_input`, `tool_use_id`, `cwd` | `{logged: true}` | Hook records after blocking. Observer records on warn-severity hits. |
| `themis_violations_for` | `session_id?`, `role?`, `since?`, `limit=50` | `[{ts, session_id, role, rule_id, tool_name, decision, ...}]` | Postmortem queries: "what did intake-1 violate this week?" **Read auth (D12)**: caller may read its own `session_id`'s violations only, unless `role ∈ {master, observer, critic}`. Daemon enforces in the endpoint handler. |

Daemon HTTP endpoints (same logic, served via FastAPI for hook consumption):

| Endpoint | Method | Body / Query | Returns |
|---|---|---|---|
| `/api/sessions/<id>/role` | GET | — | `{role: "master" \| null}` |
| `/api/themis/check` | POST | `{session_id, tool_name, tool_input, cwd?}` | `{ok, violation?, role}` |
| `/api/themis/violations` | POST | `{record}` | `{logged: true, id}` |
| `/api/themis/violations` | GET | `?session_id=&role=&since=&limit=50` | `[record, ...]` |

---

## PreToolUse hook (Phase 2)

### Script — `scripts/hooks/themis_pretool.py`

```python
#!/usr/bin/env python3
"""Themis PreToolUse hook — enforces role-invariants by blocking violating
tool calls before they execute.

Failure mode: fail-open. If daemon is unreachable or response is malformed,
log a warning and allow the tool. Themis is a guardrail, not a security gate.
"""
import json
import os
import sys
import time
from pathlib import Path
from urllib.request import urlopen, Request

DAEMON = "http://127.0.0.1:8740"
TIMEOUT_S = 0.5
FAIL_OPEN_LOG = Path.home() / ".claude" / "hooks" / "themis_fail_open.log"


def fail_open(reason: str) -> None:
    FAIL_OPEN_LOG.parent.mkdir(parents=True, exist_ok=True)
    with FAIL_OPEN_LOG.open("a") as f:
        f.write(f"[{time.strftime('%Y-%m-%dT%H:%M:%S%z')}] {reason}\n")
    sys.exit(0)


def block(rule_id: str, message: str) -> None:
    print(json.dumps({"decision": "block", "reason": message}))
    sys.exit(0)


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as e:
        fail_open(f"stdin parse failed: {e}")

    session_id = payload.get("session_id")
    tool_name = payload.get("tool_name")
    tool_input = payload.get("tool_input", {})
    cwd = payload.get("cwd", "")

    if not session_id or not tool_name:
        fail_open("missing session_id or tool_name in payload")

    try:
        body = json.dumps({
            "session_id": session_id,
            "tool_name": tool_name,
            "tool_input": tool_input,
            "cwd": cwd,
        }).encode()
        req = Request(
            f"{DAEMON}/api/themis/check",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urlopen(req, timeout=TIMEOUT_S) as r:
            verdict = json.load(r)
    except Exception as e:
        fail_open(f"daemon /api/themis/check failed: {e}")

    if verdict.get("ok"):
        sys.exit(0)

    v = verdict.get("violation") or {}
    if v.get("severity") == "block":
        # Daemon also records the violation server-side; no extra HTTP needed.
        block(v.get("rule_id", "IN-?"), v.get("message", "rule violated"))
    else:
        # warn / audit — allow but the daemon has already logged
        sys.exit(0)


if __name__ == "__main__":
    main()
```

### settings.local.json entry (installed by `khimaira attach`)

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Edit|Write|MultiEdit|NotebookEdit|Bash|Task|mcp__khimaira__auto|mcp__khimaira__delegate|mcp__khimaira__research|mcp__khimaira-chat__chat_send|mcp__khimaira-chat__chat_send_to|mcp__khimaira-chat__chat_history|mcp__khimaira-chat__chat_task_create",
        "hooks": [
          {
            "type": "command",
            "command": "/home/_3ntropy/dev/khimaira/.venv/bin/python3 /home/_3ntropy/dev/khimaira/scripts/hooks/themis_pretool.py"
          }
        ]
      }
    ]
  }
}
```

Matcher pattern matches every tool that any current invariant matches. Keep
the matcher tight — don't fire the hook on Read/Grep/Glob (no rules target
them, just wasted hook overhead).

### Hook lifecycle in `khimaira attach`

`packages/khimaira/src/khimaira/attach/__init__.py` already injects observer
config + the SessionStart/UserPromptSubmit/PostToolUse hook entries. Phase 2
adds:

1. Compute the full matcher pattern from the current rule set (union of every
   invariant's matchers' `tool:` fields).
2. Add the PreToolUse entry to the project's `settings.local.json` with the
   themis_pretool.py path resolved to the user's khimaira install.
3. `khimaira detach` removes the entry.

Rebuilds when rules change: `khimaira themis sync` (new CLI command) re-derives
the matcher pattern and updates `settings.local.json` for every attached
project. Run after editing rule YAML files.

---

## Initial Invariant set (Phase 1 ship)

Starter rules — 2-4 per role, all targeting concretely-observed failures.
Expand on next incident.

### intake.yaml — 3 invariants

| ID | Name | Severity | Matches |
|---|---|---|---|
| IN-INTAKE-1 | NO_FILE_EDIT | block | Edit, Write, MultiEdit, NotebookEdit |
| IN-INTAKE-2 | NO_API_DISPATCH | block | mcp__khimaira__{auto,delegate,research} |
| IN-INTAKE-3 | NO_STANDALONE_AGENTS | block | Task |

### master.yaml — 3 invariants

| ID | Name | Severity | Matches | Conditions |
|---|---|---|---|---|
| IN-MASTER-1 | CHAT_MY_CHATS_FRESH | block | chat_send, chat_send_to, chat_history, chat_invite | chat_my_chats_not_called_this_turn |
| IN-MASTER-2 | NO_API_DISPATCH | block | mcp__khimaira__{auto,delegate,research} | — |
| IN-MASTER-3 | NO_STANDALONE_AGENTS_WHEN_IDLE | block | Task | idle_agents_exist |

### agent.yaml — 3 invariants

| ID | Name | Severity | Matches |
|---|---|---|---|
| IN-AGENT-1 | LOAD_DOTENV_OVERRIDE | block | Edit/Write with `os.getenv(API_KEY_PATTERN)` in new content |
| IN-AGENT-2 | NO_NO_VERIFY | block | Bash with `--no-verify` |
| IN-AGENT-3 | NO_API_DISPATCH | block | mcp__khimaira__{auto,delegate,research} |

### observer.yaml — 2 invariants

| ID | Name | Severity | Matches |
|---|---|---|---|
| IN-OBSERVER-1 | READ_ONLY | block | Edit, Write, MultiEdit, NotebookEdit |
| IN-OBSERVER-2 | NO_TASK_ASSIGNMENT | block | mcp__khimaira-chat__chat_task_create |

### architect.yaml / analyst.yaml / critic.yaml — 1 invariant each

| ID | Role | Name | Severity | Matches |
|---|---|---|---|---|
| IN-ARCHITECT-1 | architect | NO_FILE_EDIT | block | Edit, Write, MultiEdit, NotebookEdit |
| IN-ANALYST-1 | analyst | NO_FILE_EDIT | block | Edit, Write, MultiEdit, NotebookEdit |
| IN-CRITIC-1 | critic | NO_FILE_EDIT | block | Edit, Write, MultiEdit, NotebookEdit |

### verifier.yaml — special case

Verifier MAY edit test files (Mode B requires it). Phase 1 omits a NO_FILE_EDIT
rule for verifier entirely. Phase 3 can revisit with a path-allowlist matcher
(e.g., block edits to non-test files).

---

## Conditions (Phase 1 implementations)

### `idle_agents_exist`

Queries `GET /api/sessions` (existing endpoint). Returns `True` if any session
with `name` matching `(^|.*-)(agent-\d+)$` has `status == "idle"` and was active
within the last 30 minutes.

### `chat_my_chats_not_called_this_turn`

Queries `GET /api/chats/subscribers/<session_id>` (NEW endpoint to add in the
chat MCP server — returns last heartbeat timestamp for the SSE subscriber).
Returns `True` if the timestamp is older than the current turn's start. The
turn-start timestamp is recorded by the existing UserPromptSubmit hook to
`~/.local/state/khimaira/sessions/<id>/turn_start.txt`.

---

## Phase boundaries

### Phase 1 — Data + MCP (no enforcement)

**Effort**: ~4-6h
**Ships when**: all MCP tools work, observer can use `themis_check` in its scan
loop, violations log records hits.

**Deliverables:**
- `packages/themis/` module with `data.py`, `engine.py`, `conditions.py`,
  `violations.py`, `server.py`
- All 8 rule YAML files with the starter Invariants
- Daemon endpoints `/api/themis/check`, `/api/themis/violations`,
  `/api/sessions/<id>/role`
- MCP tools registered: `themis_my_rules`, `themis_list_rules`, `themis_check`,
  `themis_record_violation`, `themis_violations_for`
- Tests: rule-loader, matcher, condition AND-combining, violations JSONL
  round-trip, daemon endpoint happy + unhappy paths

**Useful immediately**: observer's brief can be updated to call `themis_check`
once per scan-loop iteration on each agent's last tool call, surfacing
violations to master without any hook integration.

### Phase 2 — PreToolUse hook integration

**Effort**: ~3-4h
**Ships when**: hook installed via `khimaira attach`, blocks confirmed-violation
tool calls, never causes false-positive blocks, hook latency p99 <300ms (revised
from <25ms per architect-1 ctx-734a8fc7 — Python cold-start dominates).

**Deliverables:**
- `scripts/hooks/themis_pretool.py` (the hook script)
- `khimaira attach` updated to inject the PreToolUse entry into target project's
  `settings.local.json` (idempotent — attach+detach diff must equal zero per
  Phase 2 acceptance gate, must-fix #4)
- `khimaira themis disable <rule_id>` / `enable <rule_id>` CLI (D13) — fast
  rollback that flips severity to audit-mode via override file without
  YAML edit + sync cycle
- `khimaira detach` updated to remove it
- New CLI: `khimaira themis sync` (re-derives matcher pattern, updates all
  attached projects)
- Tests: hook script fail-open paths (daemon down, malformed payload), hook
  script block paths (each severity), `khimaira attach` round-trip with hook
  presence

**Acceptance test**: spawn an intake-role session, ask it to edit a file, verify
the hook blocks the Edit and surfaces the IN-INTAKE-1 message.

### Phase 3 — Analytics + dashboard (optional, defer)

**Effort**: ~2-3h
**Ships when**: enough Phase 1+2 production data to justify queries.

**Deliverables:**
- `themis_violations_summary(session_id?, role?, since?)` MCP tool — aggregate
  counts by rule_id, by role
- Optional: dashboard widget in the existing khimaira monitor frontend
- `khimaira themis report` CLI for postmortem queries

---

## Testing requirements (cross-phase)

Per `packages/khimaira/tests/conftest.py` conventions:

### Unit tests
- **Rule loader**: every YAML in `rules/` parses; required fields enforced;
  unknown roles flagged.
- **Matcher**: exact-tool match, tool_input_field regex, multiple matchers
  OR-combined, no false positives.
- **Conditions**: each named condition evaluates to bool deterministically given
  fixture daemon state.
- **Engine**: per-invariant evaluation, two-pass (collect-all-matches →
  select-by-severity-rank-with-id-order-tiebreak), conditions AND-combine,
  severity=warn doesn't block.
- **Engine — severity precedence** (`test_engine_severity_precedence`,
  ADDED per analyst-1 consult ctx-734a8fc7): fixture with one `audit` +
  one `block` invariant both matching the same `(tool, tool_input)` payload
  MUST return the `block` invariant regardless of id-order. Without this
  test the engine can silently degrade to advisory because of id ordering.
- **Violations log**: append round-trip, expired-entry GC, compaction trigger
  threshold.

### Integration tests
- **Daemon endpoints**: `/api/themis/check` returns ok=true for clean payload,
  ok=false with full violation record for matched, role-resolution edge cases
  (no role assigned → null, multiple chat memberships → most-recent wins).
- **Hook script**: end-to-end against a live test daemon, both block and
  fail-open paths.

### Unhappy path
- Malformed YAML — service refuses to start, surfaces which file + line.
- Daemon down at hook time — hook fails open, logs reason.
- Session has no role — daemon returns null, hook allows.
- Two rules match same tool with same severity — id-order tiebreak (verify
  ordering). Two rules match same tool with different severities — higher
  severity wins regardless of id-order (verify with explicit
  `test_engine_severity_precedence` fixture).

### Performance

- **`themis_check` (daemon endpoint, role lookup local)**: p99 latency
  target <20ms in a 100-call benchmark.
- **`themis_check` (incl. cross-daemon role-resolution hop to khimaira-chat)**:
  role lookup queries `member_roles` from the chat-server daemon. Add an
  explicit cross-daemon benchmark in Phase 1. If p99 > 15ms over the local
  baseline, add a **daemon-side** per-session role cache (chat server
  invalidates on membership change; hook-side cache is NOT acceptable — fresh
  process every call has nothing to invalidate). Per architect-1 ctx-734a8fc7
  must-fix #2.
- **Hook total overhead** (stdin parse + Python cold-start + HTTP + decision):
  **p99 target <300ms** (revised from earlier <25ms). Empirically measured by
  architect-1 ctx-734a8fc7: Python cold-start alone for the spec's stdlib
  imports (`json`, `sys`, `time`, `os`, `pathlib`, `urllib.request`) is
  276–313ms median ~295ms on this machine. The <25ms target was unachievable
  in Python. Accept ~300ms for Phase 1/2 ship. If user-visible lag emerges,
  escalation path = persistent hook daemon (tiny socket-client script + unix-
  socket listener inside `khimaira-monitor`, realistic 25–50ms p99). Rust/Go
  binary rewrite deferred as scope creep.

Per CLAUDE.md test conventions: each endpoint that resolves a session needs
unknown-name coverage (404 not 500). Round-trip coverage required for the
violations JSONL.

---

## Migration plan

Phase 1 doesn't break anything — no enforcement, just new tooling. Existing
role.md files stay as-is; agents continue to read them at session start.

Phase 2 introduces the hook. Concern: if a Phase 2 rule is too strict and
blocks legitimate work, agents grind to a halt.

Mitigation: ship Phase 2 with **observer-side warn-mode for 1 week**. Every
Phase 2 rule starts as `severity: warn` (not `block`). Observer logs all
violations. After 1 week of clean observation, flip each rule to `severity:
block`. This catches the "I didn't realize legitimate work hit this rule" case
before it locks anyone out.

Rule format makes this trivial: edit one field per rule + `khimaira themis sync`.

After Phase 2 lands, role.md "Constraints" / "Never" / "Failure mode in
practice" sections can be **refactored to reference Themis** ("see Themis
IN-INTAKE-1 for the enforcement contract") instead of duplicating the rule
text. Briefs shrink, single source of truth emerges. Defer this refactor until
Phase 2 has been stable for 2 weeks.

---

## Open questions / risks

| # | Question | Mitigation |
|---|---|---|
| Q1 | Hook latency at scale — how does p99 hold up with 8 sessions all firing PreToolUse on every Bash? | Daemon's `/api/themis/check` should be in-memory dict lookup (no DB query). Profile in Phase 1 tests. If too slow, add a per-session cached role + per-(role, tool) rule index. |
| Q2 | What if a role gets unassigned mid-session (e.g., master deputizes — does the vice inherit master's rules)? | **LOCKED — see D4 (extended).** Role lookup is live-queried at every check call (not cached at session-start). During `/khimaira-deputize`, the vice acquires master-role membership and Themis serves it master rules. After `/khimaira-resume`, the original session reacquires master-role. Integration test in Phase 2 acceptance. Per architect-1 ctx-734a8fc7 must-fix #3. |
| Q3 | What about tools that aren't in the matcher pattern but should be? E.g., a new khimaira MCP tool added later — how does Themis pick it up? | `khimaira themis sync` re-derives the matcher pattern from the union of all `tool:` fields across rule YAMLs. Run after rule edits. Document in role.md updates. |
| Q4 | Should rules ever apply across all roles (e.g., the credentials rule)? | Phase 1 ships per-role. If a rule duplicates across many roles (NO_API_DISPATCH does), introduce a `roles: [intake, master, agent, observer]` field in invariant schema to flatten. Defer until a clear pattern emerges. |
| Q5 | What's the failure mode if two rules match the same tool with different severities (warn vs block)? | See §"Matcher shapes" — engine is two-pass: collect-all-matches then select-by-severity-rank (`block > warn > audit`), with id-order as tiebreak only within the same severity. `test_engine_severity_precedence` enforces this; the test MUST exist before Phase 1 ships. Per analyst-1 consult ctx-734a8fc7. |
| Q6 | Hook script Python path — what if user has multiple Python installs? | Hardcode to `/home/_3ntropy/dev/khimaira/.venv/bin/python3` (the khimaira install). `khimaira attach` resolves this from `Path(__file__).parents[N] / ".venv" / "bin" / "python3"`. Document in CLAUDE.md. |
| Q7 | Auto-detection of new tool names? E.g., if Claude Code ships `mcp__newserver__some_tool`, Themis can't block it until a rule is added. | Acceptable. Themis is rule-based: rules are added on-demand (not pre-emptively), and tools without a matching rule are allowed. Same shape as a typical security allowlist (rules opt-in) — phrasing clarified per architect-1 ctx-734a8fc7. |

---

## Acceptance criteria

Phase 1 ships when:
- [ ] All 8 rule YAML files exist with the starter Invariants.
- [ ] `themis_check` returns correct verdict for every (role, tool, tool_input)
      combination in the test matrix.
- [ ] **`test_engine_severity_precedence` passes** — two-invariant fixture
      (audit + block on same matcher) returns block regardless of id-order.
      Non-negotiable; see §"Matcher shapes" engine semantics.
- [ ] Daemon `/api/themis/check` p99 latency <20ms in a 100-call benchmark
      (local role lookup baseline).
- [ ] **Cross-daemon `/api/themis/check` benchmark**: 100 calls including
      role-lookup hop to khimaira-chat. If p99 > 15ms over the local baseline,
      add daemon-side per-session role cache (NOT hook-side). Per architect-1
      ctx-734a8fc7 must-fix #2.
- [ ] Violations log round-trip + GC tests pass.
- [ ] Violations log compaction: trigger at >1MB, gzip-archive + truncate to
      30d window, replay-on-boot ignores archives.
- [ ] `themis_violations_for` read auth (D12): own-session reads allowed for
      all roles; cross-session reads allowed only for master/observer/critic;
      unauthorized cross-session reads return empty list + log warning.
- [ ] `themis_list_rules` and `themis_my_rules` return per-role rule data.
- [ ] Observer's brief updated to call `themis_check` in the scan loop (one
      line addition).

Phase 2 ships when:
- [ ] Hook script blocks Edit by intake-role session with IN-INTAKE-1 message.
- [ ] Hook script allows Edit by agent-role session.
- [ ] `khimaira attach` installs the PreToolUse entry; `khimaira detach`
      removes it.
- [ ] **`khimaira attach`/`detach` idempotency** (must-fix #4 per architect-1
      ctx-734a8fc7): attach Themis + detach Themis leaves `settings.local.json`
      byte-identical to pre-attach state. Verifies observer + hand-edited
      entries are not clobbered. Test diff must equal zero.
- [ ] Hook latency p99 **<300ms** end-to-end (revised from <25ms — Python
      cold-start dominates; see §Performance). Per architect-1 ctx-734a8fc7
      must-fix #1.
- [ ] **Deputize→resume integration test** (must-fix #3): vice receives
      master rules during `/khimaira-deputize`; resumed master receives master
      rules after `/khimaira-resume`. Role lookup is live-queried at check
      time, not cached.
- [ ] All Phase 2 rules ship in `severity: warn` for the 1-week observation
      window. Observer surfaces all warn-hits to master via `session_post_notice`
      (per D5). Flip to `block` after review.
- [ ] Failure modes tested: daemon down (fail-open), malformed payload
      (fail-open), session has no role (passthrough).
- [ ] `khimaira themis disable <rule_id>` flips a rule to audit-mode; `enable`
      reverts. Override state lives at `~/.local/state/khimaira/themis_overrides.jsonl`
      (D13).
- [ ] **Calendar gate (D11)**: Phase 2 ships within 1 week of Phase 1.

---

## Pointers

| What | Where |
|---|---|
| Role definitions today | `packages/khimaira/src/khimaira/roles/*.md` |
| Brief template | `~/dotfiles/claude/commands/khimaira-bootstrap-roster.md` |
| Existing hooks | `packages/khimaira/src/khimaira/hooks/` |
| Existing daemon FastAPI routes | `packages/khimaira/src/khimaira/monitor/api/` |
| Existing MCP server registration pattern | `packages/seance/src/seance/server.py` (mirror this for themis) |
| Probe v2 script (block-mechanism verification) | `~/.claude/hooks/themis_probe.py` |
| Probe v2 log with confirmed block contracts | `~/.claude/hooks/themis_probe.log` |
| First incident (intake violates NO_FILE_EDIT) | `roles/intake.md` § "Failure mode in practice (2026-05-21)" |
| Second incident (master chat_my_chats gap) | `roles/master.md` § "⚡ Real-time chat setup" |
