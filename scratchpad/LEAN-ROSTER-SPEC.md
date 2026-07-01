# Lean-roster — design spec + change inventory (SCOPING ONLY, no build)

> Author: khimaira-void-1 · 2026-06-28 · Joseph-greenlit. IMPLEMENTATION-READY — the lean
> roster REPLACES livyatan for jeevy work. Status: ✅ COMPLETE — PART A (hooks) VERIFIED
> against CC docs; PART B inventory done (two repos); PART C launcher concretes + auto-naming
> + roster-swap. Launcher-side changes are live immediately; daemon-side changes need the
> restart window that opens once livyatan is fully terminated. Builds NOT applied (deploy-gated).

## Target lean roster
- **master** (absorbs intake — user-facing + orchestration)
- **consultant** (architect + analyst merged — design/analysis, NO commit gate)
- **gatekeeper** (critic + verifier merged — the commit gate; escalates to a 2nd
  INDEPENDENT verdict for high-stakes)
- **agent(s)** (default 1, scalable; strict file-ownership separation when N>1)
- tracker: NOT an LLM seat → replaced by the hook layer (PART A)

---

## PART A — HOOK LAYER (tracker replacement) — ✅ VERIFIED (CC hooks docs via claude-code-guide)

Goal: move the deterministic "keep-moving / drain-before-idle" function from a tracker
LLM seat into Claude Code hooks (deterministic, zero-token, can't drift).

**VERIFIED hook API** (https://code.claude.com/docs/en/hooks.md):
- Real + used here: `Stop`, `SubagentStop`, `SessionStart`, `Notification`, `PostToolUse`,
  `UserPromptSubmit`, `PreToolUse`.
- `Stop` / `SubagentStop` support **block-and-continue** via exit 2 + `{"decision":"block",
  "reason":"…","hookSpecificOutput":{"additionalContext":"…"}}` → CC PREVENTS the stop and
  re-engages the model with `reason` (user-visible) + `additionalContext` (injected scaffold).
  **No new prompt needed.** Loop guard: stdin carries `stop_hook_active` (bool); block cap =
  8 (env `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP`).
- `TeammateIdle` / `TaskCompleted` ARE real — but they're **agent-TEAMS** events, not
  general-session hooks. Our roster is independent kitty sessions → `Stop` is correct. (IF the
  roster ever migrates to native agent-teams, `TeammateIdle` becomes the more precise drain
  trigger — note for the future, not now.)
- ⚠️ **Cold-inbound: NO hook fires** when a message arrives at an already-stopped session.
  Confirmed. So hooks CANNOT wake a fully-idle seat — that residual REQUIRES the external
  daemon directed-wake (the audited-SAFE path) or a notice that surfaces on next SessionStart.

**GROUNDING — the hook infra ALREADY EXISTS** (settings.json `hooks` block; modules in
`packages/khimaira/src/khimaira/hooks/`): PreToolUse→themis_pretool, PostToolUse→post_tool_use,
Notification→harvest_approval, SessionStart→session_start, UserPromptSubmit→user_prompt_submit,
SubagentStop→subagent_stop, **Stop→`khimaira.hooks.session_end`**. So the keystone is an
EXTENSION of the existing `session_end` Stop hook, not net-new wiring. ⚠️ Tension to resolve:
`session_end.py` is TODAY deliberately fail-open / NEVER-block ("must NEVER block CC from
exiting cleanly", always exit 0). The keystone REVERSES that conditionally — it must exit 2
(block) WHEN owed-work is detected, while preserving fail-open-on-ERROR (a hook exception must
still exit 0, never wedge a seat). Exit-2/block mechanism VERIFIED — see the keystone below.

**Keystone hook — drain-before-idle (extends `session_end.py`, the existing `Stop` hook):**
On Stop, stdin = `{session_id, stop_hook_active, cwd, …}`. Verified logic:
```python
data = json.load(sys.stdin)
try:
    owed = daemon_owed_check(data["session_id"])   # (a) owed verdict (_get_session_obligations)
                                                   # (b) _session_has_directed_unanswered (role-agnostic)
    if owed and not _own_attempt_cap(data["session_id"]):
        print(json.dumps({
            "decision": "block",
            "reason": f"Drain before idle: {owed.summary}",
            "hookSpecificOutput": {"additionalContext": owed.drain_steps},
        }))
        sys.exit(2)            # CC re-engages the model with reason+context — NO new prompt
    sys.exit(0)                # else: existing distill/detect behavior
except Exception:
    sys.exit(0)                # FAIL-OPEN — a hook error must never wedge a seat
```
- `additionalContext` carries the SPECIFIC drain step, e.g. "file chat_task_verdict(approve)
  on task-X, THEN chat_send a one-line reply" — the explicit MSG-reply step closes the
  MSG-vs-action gap IN-SESSION.
- Loop control: each block re-engages the model; the next Stop re-checks → drains iteratively
  until clear. Backstops: `stop_hook_active` + `CLAUDE_CODE_STOP_HOOK_BLOCK_CAP` (default 8;
  raise if a legit drain needs more) PLUS an own per-session attempt counter that fail-opens
  after N so a stuck agent can't be wedged.

**Dissolves the three failures found today** (DIRECTED-WAKE-AUDIT.md):
1. **critic re-wake loop** — the seat drains owed verdicts BEFORE idling, so the 70-min daemon
   cooldown re-wake loop can't start.
2. **self-termination + MSG-vs-action gap** — the Stop-gate forces the chat-MSG reply in-session
   → `_session_has_directed_unanswered` clears before idle (seat never idles owing).
3. **cold-idle-wake** — `SessionStart` `additionalContext` surfaces owed work on resume.

**RESIDUAL (verified): a cold directed-DM to an ALREADY-stopped seat fires NO hook.** The seat
must be woken externally → the daemon `roster_recovery` directed-wake (audited SAFE: detection
+ actuation + kitty-RC) forces a turn → the Stop-gate then drains on that turn. So **hooks own
in-session drain; the daemon owns cold-inbound wake.** This SUPERSEDES kitty-nudge for the
live-seat case (in-process, no keystroke inject); kitty-wake remains ONLY for cold-inbound.

**Supporting hooks (all VERIFIED real):**
- `SessionStart` (matcher `resume`) — inject owed-work into `additionalContext` on resume
  (replaces the tracker boot-surface).
- `SubagentStop` — agent sub-work completion → advance task state (same block-and-continue if
  a sub-deliverable is owed).
- `Notification` (matcher `idle_prompt`) — fires when a seat goes idle-waiting, but CANNOT
  block (stderr/desktop-notify only) → informational, NOT a drain lever.

---

## PART B — COMMAND/CONFIG CHANGE INVENTORY (per-file checklist)

⚠️ Role-set change = the "audit every hardcoded enumeration" hazard (verify-live-runtime).
Old roles to merge/retire: intake, architect, analyst, critic, verifier, tracker.

| File | What changes |
|---|---|
| **packages/themis/src/themis/rules/*.yaml** | **CANONICAL role registry** — `VALID_ROLES` is glob-derived from these filenames. ADD `consultant.yaml`, `gatekeeper.yaml`. Merge architect+analyst→consultant; critic+verifier→gatekeeper; fold intake rules→master.yaml; drop tracker.yaml. Audit `universal.base.yaml` + `*.base.yaml` for role enumerations. |
| **…/monitor/chats.py — ROLE_ consts (72–81)** | Add `ROLE_CONSULTANT`, `ROLE_GATEKEEPER`; retire merged consts (or alias). |
| **…/chats.py — ROLE_BUDGET (125+)** | Per-role model/effort. Add consultant/gatekeeper tiers; fold intake→master; drop tracker. (keys MUST ⊆ VALID_ROLES — guarded by test_role_budget_keys_subset_of_valid_roles.) |
| **…/chats.py — `_VERDICT_AUTHOR_ROLES` (2083) + `_ROLE_VERDICTS` (1894)** | ⚠️ LOAD-BEARING. Today: critic→approve/changes, verifier→ship/hold. Gatekeeper authors ALL FOUR. Rework the dual-verdict gate (`committable_gate_tasks`/dual-verdict "critic=approve + verifier=ship", ~1811/1834/2080) → single gatekeeper verdict, PLUS the high-stakes 2nd-independent-verdict escalation path. |
| **…/chats.py — infer_role_from_name (158)** | Add consultant/gatekeeper name patterns; handle old names. |
| **…/chats.py — verdict_role semantics (1359/1366)** | "critic"|"verifier" → gatekeeper. |
| **…/monitor/api/chats.py — _get_session_obligations** | Verdict-role checks (critic/verifier) → gatekeeper; assignee_role enums. |
| **…/monitor/roster_recovery.py** | `_NO_FILE_EDIT_ROLES = {"analyst","observer","tracker"}` → lean: consultant should be no-file-edit (design role), tracker gone. `_HUMAN_INTERFACE_ROLES = {"intake","master"}` → `{"master"}` (intake folds in). `_roster_role_map` (487), idle-consult-seat enum ("architect/critic/analyst/verifier", ~1245). |
| **…/monitor/guard5.py / guard6.py** | `if role in ("intake","data-lead")` (guard5:319, guard6:173) — intake refs. guard5:231 critic+verifier dual-verdict check → gatekeeper. |
| **…/monitor/auto_dispatch.py** | `_ROLE_KEYWORDS` dict (39) — per-role dispatch keywords; add consultant/gatekeeper, drop merged. "critic=approve + verifier=ship" string (454) → gatekeeper verdict. |
| **…/khimaira/hooks/ (EXISTING)** | Stop→`session_end.py` (extend for drain-before-idle keystone, see PART A — reverse its never-block default conditionally); session_start.py (surface owed work on resume); subagent_stop.py. These are the PART A implementation sites — already wired in settings.json. |
| **tests** | test_role_convention_lint, test_role_budget_keys_subset_of_valid_roles, test_guard5, test_direct_verdict_obligation, test_gate_wake_regression, test_gate_complete_wake, test_tracker_auto_rollforward (tracker tests → retire), test_backfill_member_roles, test_chats_v2_e2e — update to lean role set. |
| **…/khimaira/roles/*.md** | Add consultant.md, gatekeeper.md; fold intake.md→master.md; drop tracker.md. |
| **`~/dotfiles/bin/roster`** ⚠️ SEPARATE REPO (not khimaira) | The launcher — heavy role enumeration. `role_model()` (master\|architect\|intake→opus[1m], else sonnet), `role_effort()` (architect→max, master→high, observer→low, else medium), `role_tier()` (master=1…tracker=7), `role_col()` (layout), `infer_role()`, the `OPT_ANALYST/VERIFIER/TRACKER/ARCHITECT/CRITIC` defaults + `--[no-]analyst/verifier/tracker/architect/critic` arg parsing, and the 9-seat layout comments. ALL need the lean set: define consultant/gatekeeper model+effort+tier+col, drop intake (→master) + tracker (→hooks) as seats, merge architect+analyst→consultant / critic+verifier→gatekeeper, add agent-scaling flag. **Lean-roster is a TWO-REPO change (khimaira + dotfiles).** |
| **~/.claude/commands/*.md (skills)** | Audit for hardcoded old-roles: khimaira-nudge, khimaira-wind-down, khimaira-architect, khimaira-classify, khimaira-chat-roles, khimaira-delete-rosters, khimaira-chain, khimaira-research, **khimaira-tracker-*** (tracker skills become moot — retire/repoint to hook surface). |
| **settings.json (hook config)** | Where PART A hooks register (Stop/SessionStart/etc). Audit for any role refs. |
| **khimaira-bootstrap-roster skill + map** | The roster onboarding map (master.md §dispatch) — new seat set. |

**Open items:** (1) ~~read bin/roster~~ DONE (`~/dotfiles/bin/roster`, separate repo);
(2) ~~PART A hook API verification~~ DONE (✅ verified vs CC docs); (3) during build, finish
grepping guards/daemon for any residual role enums beyond those mapped above (guard7,
_roster_role_map deep refs) — the mapped set covers the load-bearing ones.

---

## PART C — IMPLEMENTATION (concrete; launcher-side = LIVE on next `roster`, no daemon restart)

### LIVE-vs-RESTART split (master's #2)
- **LAUNCHER-side, live immediately** (no daemon restart — takes effect on the next `roster`
  spin / next session boot / next hook invocation): `~/dotfiles/bin/roster`; role `.md` docs
  (session-loaded by the SessionStart hook — confirmed `session_start.py:1214` reads
  `<roles_dir>/<role>.md`); the Stop-hook SCRIPT
  (`session_end.py` runs fresh per Stop — editing it is live). The directed-message half of
  drain-before-idle works immediately (`_session_has_directed_unanswered` is role-agnostic).
- **DAEMON-side, needs restart** (window opens once livyatan is fully terminated → low-risk
  restart): everything that must RECOGNIZE consultant/gatekeeper — Themis `VALID_ROLES` +
  rule yamls, `chats.py` (ROLE_ consts, ROLE_BUDGET, `_VERDICT_AUTHOR_ROLES`/`_ROLE_VERDICTS`,
  dual→single verdict gate, `infer_role_from_name`), `_get_session_obligations`, guard5/6/7,
  auto_dispatch. ⚠️ The owed-VERDICT half of drain-before-idle needs this (daemon must treat
  gatekeeper as a verdict role) — so the hook ships launcher-side but is only fully effective
  after the daemon restart recognizes gatekeeper.

### C1 — `~/dotfiles/bin/roster` (LAUNCHER, live) — concrete lean config
4 seat types: master · consultant · gatekeeper · agent(×N, default 1). Recommended (sign-off):
```sh
role_model()  { case "$1" in master) echo "opus[1m]";; consultant|gatekeeper) echo opus;; *) echo sonnet;; esac; }
role_effort() { case "$1" in consultant) echo max;; master|gatekeeper) echo high;; *) echo medium;; esac; }
role_tier()   { case "$1" in master) echo 1;; consultant) echo 2;; gatekeeper) echo 3;; agent) echo 4;; *) echo 4;; esac; }
role_col()    { case "$1" in agent) echo 2;; *) echo 1;; esac; }
```
Flags: DROP `--[no-]analyst/verifier/architect/critic/tracker/intake`; ADD `--agents N`
(scale agents, default 1) + keep `--consultant/--gatekeeper` toggles (default on). Update the
layout comments (4-seat). Rationale to confirm: consultant inherits architect's opus/max
(design depth); gatekeeper inherits critic's opus + verifier's gate at high effort; master
absorbs intake (opus[1m]); agent sonnet/medium.

### C1b — AUTO-ROSTER-NAMING (LAUNCHER-side only, live immediately; Joseph priority)
`roster start` with NO name → auto-pick an unused codename as the seat prefix;
`roster start <name>` still overrides.
- **Pool source:** a BAKED wordlist (array in bin/roster — no external file dep), curated
  creature/mythological (livyatan/muther vibe), e.g. `chimera griffin kraken phoenix wyvern
  basilisk cerberus sphinx hydra manticore …`. ⚠️ The pool MUST be self-non-nesting: no member
  is a substring of another (so the curated list is itself the substring-safety guarantee).
- **Collision-check source:** union of (a) live kitty window titles (`kitty @ ls` — what
  wake/reap actually key on) AND (b) active session names (`session_list` / daemon registry),
  → extract active roster prefixes. Use BOTH because the substring hazard bit kitty-title
  matching specifically (memory: kitty-title-match) and the reaper keyed on decorated titles
  (memory: bug3-reaper-cascade).
- **Exclusion rule (LOAD-BEARING, not cosmetic):** a candidate is rejected if it is in-use OR
  is a **substring OR superstring** of any active prefix. This structurally prevents the
  `agent-1`-matches-`muther-agent-1` class that broke daemon wakes + caused the reaper-cascade
  (false-reap → delete_session → chat-leave cascade). Pick the first pool word passing both.
- **Persistence:** minimal v1 = avoid ACTIVE collisions only (no recently-used file). Optional
  later: a `~/.cache` used-names ring to rotate and reduce reuse churn — NOT required for v1.
- **Failure mode:** pool exhausted (all nesting/in-use) → fail loud with a clear message
  ("no collision-safe codename free; pass an explicit name"), never silently pick an unsafe one.
- ⚠️ **Completeness caveat:** the collision check is only as complete as the kitty context it
  runs in. `roster start` runs INTERACTIVELY in Joseph's full kitty → `kitty @ ls` sees every
  window → complete. A sandboxed/headless invocation sees a partial window set → could miss a
  collision. Not a blocker in practice (rosters launch interactively), but noted.
- Launcher-side only: bin/roster, live on next `roster start`, NO daemon restart. IMPLEMENTED
  (bash -n clean; picker tested live → `chimera`). ⚠️ bare-`roster start` now auto-names (was:
  khimaira self-roster) — behavior change pending Joseph's muscle-memory call.

### C2 — role docs (LAUNCHER/session-side, live on next boot) — `…/khimaira/roles/`
- **consultant.md** (NEW = architect.md ∪ analyst.md): design decisions + analysis/research;
  NO commit gate; no file edits (advisory). Carry architect's bug-class-enumeration discipline.
- **gatekeeper.md** (NEW = critic.md ∪ verifier.md): the commit gate — authors BOTH the
  critique verdict (approve/changes) AND the ship verdict (ship/hold); for HIGH-STAKES,
  escalates to a 2nd INDEPENDENT verdict (spec the trigger: >2 files OR core-arch OR security;
  the 2nd verdict from a fresh gatekeeper instance or the consultant as backup reviewer).
- **master.md** (EDIT, absorbs intake): fold intake.md's user-facing/front-door duties in;
  drop the master→intake handoff convention. Retire intake.md, architect.md, analyst.md,
  critic.md, verifier.md, tracker.md (git rm; history is the backup).

### C3 — hook scripts (LAUNCHER/session-side, live per invocation) — `…/khimaira/hooks/`
The drain-before-idle keystone extends `session_end.py` (Stop hook) — exit-2/block mechanism
VERIFIED + specced in PART A (see keystone pseudocode). Net: on Stop, check (owed verdict via
daemon) OR (`_session_has_directed_unanswered`); if owed → exit 2 + `{"decision":"block",…}`
to feed the drain directive; else exit 0. Keep fail-open-on-error. `session_start.py`: inject
owed work into `additionalContext` on resume (cold-idle).

### C4 — ROSTER SWAP flow (DOCUMENTATION-ONLY — no code change needed; verified)
Joseph's live blocker: close livyatan-master + transfer its context to the new lean master,
no manual copy. The swap = TWO one-command steps:
1. **Wind down old** — `/khimaira-wind-down` (or `/khimaira-write-handoff`) on the retiring
   master → writes `scratchpad/MASTER-HANDOFF.md` + `session_post_handoff(scope_cwd=<project_root>)`.
2. **`roster start`** (auto-named, C1b) IN THE SAME project dir → the new master boots there;
   its SessionStart hook auto-surfaces the cwd-scoped handoff → it reads MASTER-HANDOFF.md +
   continues. No manual context copy.

**KEY FINDING — `scope_cwd` is CORRECT and SUFFICIENT; do NOT add `scope_project`.** Both
skills already tag `scope_cwd=project_root`, and old + new roster SHARE the project dir, so
the cwd-scoped handoff surfaces for the new master. Verified empirically: void-1 (this
session) booted by reading the `scope_cwd=~/dev/khimaira` MASTER-HANDOFF.md — the exact
mechanism. ⚠️ `scope_project` would actually be WRONG for an auto-named roster: the handoff's
project tag wouldn't match a CREATURE-named roster (chimera ≠ jeevy_portal), whereas the
shared cwd always matches. So the spec's earlier "add scope_project" precaution is retracted —
`scope_cwd` is the right axis.

EXISTING primitives (all present, no change): `session_post_handoff(scope_cwd=…)`;
SessionStart handoff surfacing (`session_start.py` emits "👀 khimaira handoffs", cwd-scoped,
auto-claims first consumer); `/khimaira-wind-down` + `/khimaira-write-handoff`. **NEW work:
NONE — pure documentation** (the two-step runbook above). Skill/launcher-side, no daemon restart.

---

## Substrate gaps surfaced today (log as separate tickets — block directed-private build)

1. **chat-create dedup-guard 409** — `chat_create_room` 409s ANY new room that overlaps an
   existing chat by even ONE member; `fresh=True` does NOT override. Confirmed by both void-1
   and master (couldn't spin a [void-1+wakeprobe] room because both overlap the master chat).
   ⚠️ Directly bites directed-private if it spins per-pair DM rooms — the build needs either a
   fix to the guard or a different room model (shared room + private send_to). Joseph logged it.
2. **directed-wake self-termination gap** (3 facets) + **obligation-detection gap** — see
   DIRECTED-WAKE-AUDIT.md; the PART A drain-before-idle hook is the fix family.

## Post-reset resume pointer (for the fresh session)
- Durable inputs: this file + `scratchpad/DIRECTED-WAKE-AUDIT.md` + `scratchpad/ISSUE38-*`.
- PARKED (all need Joseph's post-7pm reset / a deploy window):
  • PART A hook-name verification (claude-code-guide) → finish PART A.
  • #38 live-tollgate apply (`scratchpad/ISSUE38-LIVE-TOLLGATE.patch`, verified, unapplied).
  • lean-roster BUILD (this spec) — two-repo, post-design-signoff.
- Phase B was BANKED: the fresh-DM-surfacing facet rides as the acceptance test on the hook
  fix post-build (don't re-grind the throwaway repro).

---

## DEFERRED TICKET — full-delete legacy roles (gated on jp/janice migration)

**Status:** DEFERRED (master decision Option A, 2026-06-28). Do NOT do this until jp/janice
(and any non-lean roster) have migrated off the old roles.

**What "retire-old-roles" actually did (scope A, shipped):** pruned ONLY master.md's
contradictory intake-relay references — the front-door section is now authoritative; the
remaining `intake` mentions (Step-7 arc-complete signal, CONTEXT-UPDATE reuse note,
constraints list, interaction table) are tagged `(legacy roster only)` and read "you ARE
that seat" in the lean roster. The `🏁 INTAKE COMPLETE` marker text STAYS (lint-guarded:
`test_role_convention_lint.py:212`). One file touched: `roles/master.md`.

**What scope A deliberately did NOT do (this deferred ticket):** delete the 6 legacy role
docs + 6 legacy Themis yamls + the `ROLE_CRITIC`/`ROLE_VERIFIER`/`ROLE_ARCHITECT`/etc.
constants in `chats.py`. Those stay IN PLACE as the compat substrate.

**Why full-delete is blocked (all four would break TODAY):**
1. **jp/janice / any non-lean roster** still validate roles + load role docs from the old
   set — deleting breaks running/future legacy rosters.
2. **Gate legacy-compat path** (master-approved): `_maybe_auto_advance_gate_complete` +
   `_VERDICT_AUTHOR_ROLES` keep the critic=approve + verifier=ship dual-verdict path so
   in-flight pre-cutover tasks don't strand → NameError on `ROLE_CRITIC` if consts deleted.
3. **~6 lint tests** assert the legacy docs/yamls/consts EXIST (deliberate regression guards).
4. **bin/roster** legacy seat cases (`--critic`/`--verifier`/`--architect`/`--analyst`,
   opt-in) reference the old roles.

**Unblock condition + steps when ready:** once no roster spawns the old roles AND no
in-flight task rides the legacy gate path → (a) delete 6 docs + 6 yamls + consts; (b) rip
out the gate legacy-compat branch; (c) update the ~6 lint tests; (d) drop bin/roster legacy
cases. Single coherent change, gated on that migration. Cheap to leave dormant until then —
lean is already the DEFAULT; legacy is no-longer-spawned-by-default, not invalid.

---

## DEFERRED — skill retire-vs-repoint (fold into the full-delete cleanup, 2026-06-28)

Surfaced by the live-bootstrap skill audit (chimera-0 caught bootstrap-roster's stale role
enum pre-commit). FIXED 7 skills with real role-set logic (added consultant/gatekeeper
first-class, kept legacy additive): bootstrap-roster, delete-rosters (regex), nudge (regex),
chat-roles (ROLE_BUDGET table), distill (role branch), orchestrate, recall-bugs. All in
~/dotfiles/claude/commands (live on save; not committed — dotfiles is Joseph's repo).

These 6 skills are LEGACY-valid but MOOT for the lean roster — leave in place (additive),
retire only when jp/janice migrate off the old roles (same gate as the role full-delete):
- **khimaira-spawn-intake** — intake retired in lean (master absorbs it).
- **khimaira-spawn-architect** — architect role kept (legacy); no lean spawn-consultant
  sibling yet. Optional future: add `spawn-consultant`.
- **khimaira-tracker / -tracker-digest / -tracker-open / -tracker-stale** — tracker→hooks in
  lean; render "tracker not found" on a lean roster (no tracker seat).
