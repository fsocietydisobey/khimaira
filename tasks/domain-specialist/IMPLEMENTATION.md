# Domain-specialist consolidation — analyst repurpose + assign-time injection + observer fold-in

**Status:** role-doc layer LANDED (2026-06-06); daemon layer is FLEET WORK after #14.
**Decided by:** Joseph, 2026-06-06 ("the injection and elastic domain scoped agents sounds
like the right approach… what if we just repurposed analyst to be that specialist?" +
"tracker and observer are too closely related… consolidate them into one").

## Design principle

A lead is a **context profile, not a session**. Leads (backend/data/frontend-lead) are
retired — standing specialist sessions cost API slots, boot context, and coordination
surface for value that's only realized at task time. The replacement has three pieces:

| Piece | Carrier | Status |
|---|---|---|
| 1. Domain-tagged consults | analyst (existing seat, opus/max) | ✅ role doc landed |
| 2. Assign-time domain injection | task briefs via `chat_task_create` | ⬜ THIS SPEC (fleet) |
| 3. Elastic domain agents | plain agents spawned with a domain brief | ✅ no code needed |

Observer is also retired as a separate seat (fold into tracker): the daemon's guards
(Guard-4/5/6, liveness, auto-wake, HITL notifier) now do the structural monitoring that
observer's polling loop did behaviorally. Tracker interprets daemon alerts + posts
judgment-needing anomaly notices, riding its existing synthesis cadence.

## Landed (role-doc layer, this commit)

- `roles/analyst.md` — "Domain-specialist consults" section: `📐 ANALYST CONSULT
  [domain=X]` → load mnemosyne `<project>:<domain>` + `docs/domain/<X>-knowledge.md`
  before answering. Activation model UNCHANGED (idle-by-default, consult-only — the
  standing-duty accretion is how leads went wrong).
- `roles/tracker.md` — "Roster health watch" section (absorbed observer duties).
- `bin/roster` (dotfiles) — observer default OFF; `--observer` still force-includes.
- Mnemosyne boot memory routes to master (68db243); observer/lead code branches dormant.

## Fleet work — piece 2: assign-time domain injection (chats.py — DO AFTER #14 LANDS)

**Goal:** when master creates a task with a domain, the task body is enriched with that
domain's mnemosyne knowledge — implementer agents get specialist context with zero
standing sessions.

**Changes (all in `packages/khimaira/src/khimaira/monitor/`):**

1. `chats.py create_task(...)`: accept optional `domain: str | None = None`. Valid
   values: backend / frontend / data / devops / orchestration. Store on the task event
   (`"domain": domain`).
2. When `domain` is set, query mnemosyne (`khimaira.hooks.mnemosyne_client.query`,
   fail-open) for `<project>:<domain>` — project from the chat's cwd via
   `detect_project`. Append to the task body:
   ```
   🧠 domain context (<project>:<domain>, auto-injected, PROVISIONAL):
   <answer — HARD CAP 3000 chars + truncation marker>
   ```
   Mnemosyne down → skip silently (task creation must never block on it).
3. `api/chats.py` CreateTaskReq: add `domain` field, thread through (same pattern as
   the #14 field threading at api/chats.py:1741-1745).
4. MCP tool `chat_task_create` (khimaira-chat server): expose `domain` param.

**Tests (per CLAUDE.md conventions):**
- create_task with domain + mnemosyne mocked → body contains the injected block, capped.
- mnemosyne unreachable → task created WITHOUT the block (fail-open).
- invalid domain string → ValueError → API 400 (not 500).
- no domain (default) → byte-identical behavior to today.

**Doc updates:** `roles/master.md` dispatch section — set `domain=` on implementation
tasks when the work is clearly domain-scoped; consult template gains the
`[domain=X]` tag (analyst side already documented).

## Explicitly out of scope

- Removing observer/lead role files, budget entries, or `--observer`/`--*-lead` flags —
  dormant branches are cheap insurance and removing roles has bitten us before
  (hardcoded role-enumeration class).
- #60 (mnemosyne lead-as-editor harvest) — moot with leads retired; strike from backlog.
- Any change to analyst's activation model.
