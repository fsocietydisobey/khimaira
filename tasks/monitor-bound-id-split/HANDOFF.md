# Monitor-vs-Bound Session ID Split — spec seed + handoff

> Handoff from `khimaira-1` (f3e9660f, outgoing acting-master) to
> `khimaira-master` (dda2a643, incoming). This is the #1 open
> roster-stability bug. Read this whole doc before touching code.
> It is a **spec seed**, not a finished spec — Phase A (audit) comes first.

## Goal (bottom line)

Stop treating the session id as an **identity**. It is a **connection
handle**. Bind chat membership to a **stable seat identity** that survives
id churn, and make the volatile session_id a rebindable pointer to it.
Every fix attempted so far catches "the moment the id changes and repoints
it" — that is an *instance* fix and it structurally cannot cover all paths.

## In plain terms

A session's chat membership is filed under its `session_id`. But that id is
NOT stable — Claude Code mints a new one on `/clear`, relaunch, and resume.
The khimaira-chat MCP subprocess freezes the *old* id at boot and does not
restart on `/clear`, so membership ends up filed under an id nobody uses,
and the live session (new id) looks like a stranger with zero memberships.
That is the lockout. Joseph `/clear`s agents routinely for context hygiene,
so it recurs on **every clear** — high frequency, not rare.

## The bug class

**Any event that gives Claude Code a new `session_id` without the membership
store following along.**

| Path | Status | Evidence grade | Why it breaks |
|---|---|---|---|
| agent `/clear` | BROKEN | audit-grade (griffin repro'd agent-1 `531473a5` + agent-2 `e5856dbb` on 2026-07-03) | same process, subprocess keeps old frozen id, new CC id |
| relaunch (`claude`) | BROKEN | audit-grade (griffin) | new process/ppid/subprocess/id; old membership orphaned |
| resume (`claude -r` / `claude-chat -r`) | BROKEN | audit-grade (earlier this session) | same as relaunch |
| monitor reap on monitor-view liveness | BROKEN | audit-grade (I evicted griffin's gatekeeper this way) | monitor tracks the *new* id, cannot see the bound-old-id is alive → reaps a live seat. A **consequence** of the split, not a separate root. |
| master `/clear` | UNKNOWN / claimed-covered | inspection-grade only — NOT verified | the merged id-fix *may* cover it; do not trust |

## Why the merged id-fix is an instance patch (cannot be the class fix)

Branch `fix/mcp-rebind-on-clear`, commit `3a765e4`, ppid-guarded re-bind at
`packages/khimaira-chat/src/khimaira_chat/server.py:357`. It re-syncs by
matching `incoming_id == ppid-confirmed identity`. **The ppid is stable ONLY
across `/clear`** (same process). Across relaunch/resume the ppid is brand
new — nothing to match — so a ppid-rebind *cannot* close those paths even if
it fired perfectly. It is a `/clear`-only patch by construction.

**It is also apparently not firing even for agent `/clear`** (griffin's live
repros lock out) — a separate live bug. Hypotheses to audit: SessionStart not
posting the ppid→new-id map for agent sessions; a timing race between the
subprocess re-register and the daemon recording ppid→id; or a deploy gap.
`61 green unit tests ≠ the re-bind firing in production` — this is the
verify-the-live-runtime-path lesson. Treat the id-fix as **merged but
live-unverified and NOT working**.

## The class fix — two layers

### Layer 1 — self-healing reclaim (pragmatic; kills the pain on all paths)

Change membership-drop semantics: when a member's id goes stale, DON'T
hard-delete — move it to a **reclaimable** state tagged with the stable
seat key. On any session registration (`chat_my_chats` / SessionStart),
reconcile: "is there a reclaimable membership for my seat? → auto-re-accept
it." A `/clear`'d, relaunched, or resumed session then self-heals its
membership on its first chat call, zero manual re-invite. This is griffin's
"leave a pending invite on eviction" generalized to every path and automated.

### Layer 2 — membership keyed on the seat, id as pointer (structural; closes the class)

Store a chat member as a durable **seat**, with `session_id` as a mutable
"current connection" field on it. Registration updates the pointer;
membership never moves. The split becomes impossible by construction — there
is no id-keyed membership left to orphan.

### The crux: the seat key

What is stable across all three id changes?
- `session_id` — NO (changes on all three)
- ppid — stable across `/clear` only
- kitty window id — stable across `/clear` + resume-in-same-window; NOT a fresh relaunch in a new window
- **roster+role slug** (e.g. `griffin/agent-1`) — assigned by `bin/roster` via env/window-title; stable across ALL three paths as long as the seat is relaunched with the same role. **This is the durable identity.**

Use the roster+role slug as the seat key, **corroborated by ppid/window as
reclaim-authorization** so a wrong session cannot hijack a seat. The hard
part is the authorization gate (collision when two sessions claim one seat,
duplicate role slugs). **This wants an architect pass before implementation.**

## The class-invariant test (replaces path-by-path patching)

One parametrized test:
```
for event in [clear, relaunch, resume]:
    seat joins a chat
    simulate event  # new session_id, SAME seat
    assert chat_my_chats(new_id) still includes the chat
```
Catches any id-change path, including future ones. Without a class-level
invariant, path-by-path fixes pass individually while leaving the class open
(this is exactly the whack-a-mole this bug has already suffered — 4 fixes,
still broken).

## Files to audit (ALL inspection-grade — verify each live before trusting)

- `packages/khimaira-chat/src/khimaira_chat/server.py` — ~L335 frozen id at boot, ~L357 the re-bind branch + ppid guard
- the chat membership store (`chats.py`) — the `leave` / eviction / membership-drop paths (where Layer 1 changes the drop semantics)
- `packages/khimaira/src/khimaira/monitor/registry_gc.py` — the reaper (STAYS DISABLED; it is a split *consequence*)
- the SessionStart hook (`scripts/hooks/`) — does it post ppid→new-id to the daemon on an *agent* `/clear`? This is the prime suspect for "re-bind not firing for agents"
- `bin/roster` — how the seat/role slug + window title + env are assigned (source of the stable seat key)

## Discipline (Joseph's rules — mandatory here)

1. **Two-phase audit-first is MANDATORY** (per `bug-class-enumeration.md`):
   UNKNOWN dominates, the fix crosses subsystem boundaries (CC ↔ MCP
   subprocess ↔ daemon ↔ hooks), and the "why" above is inspection-grade.
   - **Phase A**: audit only — live-repro the split in ONE throwaway session,
     read the actual code paths, post an audit table to chat. Do NOT design
     the fix yet.
   - **Phase B**: Joseph/architect gates on the audit before fix dispatch.
   - **Phase C**: class-invariant test against the audit-confirmed mechanism.
2. **Single-session repro before theorizing** — isolate to ONE throwaway
   session; NEVER restart/nudge the live roster to "diagnose" (it scrambles
   identities and makes the split worse).
3. **Architect pass** on the seat-key authorization gate before building.

## Standing constraints (do not violate)

- **Reaps stay DISABLED** (`KHIMAIRA_REGISTRY_*_REAP_IDLE_S=999999999`,
  `KHIMAIRA_GUARD7=0` in the monitor systemd unit) until the split is closed.
  No manual roster reaps — reaping on monitor-view signals is unsafe while
  the split exists (a live bound id is invisible to window/ppid/turn/idle).
- **No git operations for jeevy** (Joseph pushes jeevy himself; file edits OK).
- **Do NOT commit Joseph's dotfiles WIP** (`approach.md`, `ai-engineering.md`,
  `DIGEST.md` under `~/dotfiles/claude/rules/`).
- **Never `--no-verify` past pre-commit hooks.**
- Master may commit khimaira directly (standing authorization). Agents are
  blocked from git mutations by Themis IN-UNIVERSAL-1 → they escalate to you.

## Interim safety net (holds meanwhile — no fire)

griffin's recovery loop: detect lockout notice → `chat_invite` the bound id →
nudge to accept. Routine on griffin's end. So there is time to do this right.

## First action

Spin up ONE throwaway session, `/clear` it, and observe the split live
(frozen id vs new id; `chat_my_chats(new_id)` == `[]`). Read `server.py`
~L335/357 and the SessionStart hook to confirm/refute the "ppid→id map not
posted for agents" hypothesis. Post the Phase-A audit table to chat. Then gate.
