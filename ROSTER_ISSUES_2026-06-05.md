# Khimaira Roster Platform Issues — Register (2026-06-05)

Compiled by janice-0 (jp master, session 5ddb6421) from a full-day jeevy_portal roster session. Source of truth for the janice-0 ↔ khimaira-0 fix collaboration. Each issue: symptom → root cause (audit/inspection grade) → status → fix/recommendation → priority.

**Unifying theme:** roster identity and delivery are not *uniquely + stably bound*. Sessions, subprocesses, windows, and names can duplicate, go stale, or be re-minted on restart/resume/compaction — and delivery + liveness are inferred from fragile, voluntary, or ambiguous signals. Most issues below are facets of that one root.

---

## K1 — SSE subscriber dies on context compaction *(ROOT FOUND + PATCH WRITTEN)*
**Symptom:** an agent/master silently stops receiving channel events after a context compaction; appears online, receives nothing. Hit master (janice-0) twice today and most agents at least once.
**Root cause [audit-grade]:** lazy-start `_ensure_subscriber` (packages/khimaira-chat/src/khimaira_chat/server.py) spawned the **session-bound** `_sse_loop(ctx.session)`, which emits via the request-context session captured at first tool call. That session goes stale on compaction; the subscriber keeps running but emits into a dead session. The watchdog never replaces it (it doesn't crash). The write_stream-based `_proactive_sse_loop` (compaction-proof) already existed and was used by the watchdog + force-resubscribe — but the lazy-start path was the missed holdout. When the boot ppid-bridge misses (common), lazy-start wins the boot race → the broken loop runs all session.
**Status:** PATCH WRITTEN (in khimaira working tree): repointed `_ensure_subscriber` → `_proactive_sse_loop`, deleted dead `_sse_loop` + `_emit_channel_notification`, added regression test `test_ensure_subscriber_uses_proactive_loop_not_session`. 24 pass, ruff clean. **Pending: khimaira-0 review/commit + subprocess respawn to deploy.**
**Priority:** P0 — every other roster-coordination failure is amplified by this. Deploy first.

## K2 — Restart/resume mints NEW identity (session-id AND window-id not preserved)
**Symptom (a):** kitty window IDs renumber on every restart (observed 240s → 371-379 → 428-438, 3× in one session). ID-based nudges hit dead windows; `kitty @ send-text --match id:<dead>` no-ops SILENTLY (reports success). **Symptom (b):** restart/resume spawns a NEW khimaira session-id per role; old ones orphan (session_list shows 2-4 stale sessions per role); name→id resolution becomes ambiguous.
**Root cause [inspection-grade]:** restart/resume does not inherit the prior session-id or re-bind the window; identity is regenerated at both layers.
**Status:** FILED. Master-side workaround: title-match nudges (never window-id). No platform fix yet.
**Fix/rec:** stable session↔role↔window binding that survives restart; OR on restart, resume the prior session-id + retire the old per-role session. Make a dead-target `send-text` VISIBLE (error, not silent no-op).
**Priority:** P1.

## K3 — Session ENTANGLEMENT (two same-named/same-id sessions cross-receive) *(caused real damage today)*
**Symptom:** Joseph spun a second "janice-0" to hand master to; the new janice started receiving THIS roster's (chat-5dae92cf6221) channel notifications, and its new-roster invites cross-hit the old roster's agents (Joseph had to terminate them). Two "janice-0" sessions entangled.
**Root cause [inspection-grade]:** likely a `claude --resume` bound a SECOND subprocess to the SAME session_id; the daemon SSE-delivers by session_id, so both subprocesses' subscribers receive every event. The `_SubprocessState` "one subprocess = one session" guard is enforced PER-SUBPROCESS (refuses a different id mid-subprocess) but does NOT prevent two subprocesses globally claiming the same id. Compounded by name-collision (two sessions named janice-0; name-resolution picks most-recent/ambiguous).
**Status:** FILED. Resolved THIS instance via chat_transfer_membership (old master → new) which set old to transferred-out. But the mechanism remains.
**Fix/rec:** globally-unique session_id enforcement — a second subprocess claiming a live session_id is rejected/fenced, not dual-subscribed. Name-collision resolution must disambiguate by id, never deliver by name. The transfer/handoff flow needs the recipient to be a genuinely DISTINCT registered session (resume-into-same-id defeats chat_transfer_membership).
**Priority:** P1 — breaks the master-handoff flow.

> **UPDATE 2026-06-05 (janice-0 cross-check vs the LIVED failure — split K3 into 3):** the entanglement that actually bit us was NOT same-session_id dual-subprocess. The two masters had DISTINCT ids (5ddb6421 vs 5315ac20), both named "janice-0". Real mechanisms:
> - **K3a — same-id dual-subprocess** (claude --resume into same id → dual subscribe). **Owned by agent-4's PID-claim fence (server.py).** Valid defense-in-depth, but did NOT cause the lived failure.
> - **K3b — duplicate-roster bootstrap:** new janice ran /khimaira-bootstrap-roster → created a duplicate chat (chat-e619024f2b92, now archiving) that re-invited the SAME jp-agent sessions → agents in two chats → cross-receiving invites ("invites were hitting them"). **FIX:** bootstrap idempotency — check for an existing live roster for the prefix/project first; enforce the skill's incremental-add path; never fork a parallel chat with the same members.
> - **K3c — master name-collision:** two distinct sessions both named "janice-0"; chat events resolve to both if any path keys on NAME ("the other session is getting notifications"). **FIX:** deliver/resolve by session_id only; reject/auto-suffix a second session registering an in-use friendly name.
> The fence (K3a) is necessary but NOT sufficient — K3b + K3c are what recur without their own fixes. Enumerate-the-class discipline: don't anchor the fix on the first plausible mechanism.

## K12 — Daemon sluggishness *(khimaira-0 addition — perf, amplifies everything)*
**Symptom:** the khimaira-monitor daemon is slow — task-creates time out, session_list returns 140+ sessions, curl writes occasionally time out. Amplifies every identity/proliferation issue (more stale sessions accumulate, slower resolution).
**Status:** khimaira-0's agent-2 is on it.
**Priority:** P1 (cross-cutting amplifier).

## K13 — chat_transfer_membership orphans the master role *(found live during today's handoff)*
**Symptom:** `chat_transfer_membership(5ddb6421 → 5315ac20)` moved the MEMBERSHIP but not the master ROLE. Result: donor (5ddb6421) now 403s as "not the master" (can't chat_grant_role to fix it); recipient (5315ac20) is an accepted member with role=AGENT and 403s on chat_task_create. The chat is left MASTER-LESS — neither party can act as master, and the seat can only be repaired daemon-side.
**Root cause [audit-grade — reproduced live]:** the transfer carries membership state but does not carry/grant the role. When the donor IS the master, the seat is orphaned.
**Status:** repair escalated to khimaira-0 (daemon-admin set-master). Blocking the jp roster (can't create tasks / post verdicts).
**Fix/rec:** transferring a MASTER's membership must atomically promote the recipient to master (or reject if it would orphan the seat). Tightly coupled to K3's handoff-flow breakage — fix together.
**Priority:** P1 — silently breaks every master handoff.

## K4 — Liveness inferred from VOLUNTARY chat-ack (false-negatives)
**Symptom:** agents that received + processed a nudge appeared "dead" because they replied in-window WITHOUT calling chat_send (observer printed "presence confirmed" in-window, never posted). Master can't distinguish alive+working from dead.
**Root cause:** roster liveness is read from "did the agent voluntarily post a chat ack." False-negative whenever the agent replies in-window, is mid-long-op, or is context-saturated and skips the tool call.
**Status:** FILED.
**Fix/rec:** use an OBSERVABLE liveness signal — the roster_progress disk-WIP/last-active aggregator (the wake-up skill already TODOs this) — OR a daemon-side mark-alive on nudge DELIVERY. Never infer liveness from a voluntary post.
**Priority:** P2.

## K5 — Master-identity split → MCP chat_send 403
**Symptom:** master's MCP `chat_send` 403s; the caller resolves to a different session-id (cd236086) than the master member (5ddb6421). Forced all master chat WRITES to go via curl (`POST /api/chats/{id}/messages` with explicit sender_session_id).
**Root cause [inspection-grade]:** the MCP tool's caller-identity resolution diverges from the registered chat-member session-id.
**Status:** FILED, curl workaround load-bearing all session.
**Fix/rec:** reconcile MCP caller-id resolution with the registered member session-id; chat_send should authorize on the passed session_id.
**Priority:** P2.

## K6 — Membership collapse on transfer/delete (collateral leaves)
**Symptom:** earlier rosters dropped from ~17 → 3 members; transfer/delete collateral-set many members to 'left' (cannot self-rejoin); recurring re-invite churn.
**Status:** FILED; the alive-guard (KHIMAIRA_ALIVE_DELETE_GUARD_S) + /khimaira-delete-rosters mitigate the delete case. Re-invite via curl /invite.
**Fix/rec:** transfer/delete must not collateral-leave non-target members; 'left' members should be self-rejoinable or the op scoped tighter.
**Priority:** P2.

## K7 — Notice sender-id truncation (unresolvable)
**Symptom:** inbox notices show an 8-char sender prefix (e.g. `54d739d9`) that does NOT resolve back to a session via session_post_notice (HTTP 404 "no session named/id'd"). Couldn't reply to a stray session.
**Status:** observed today; not yet filed standalone.
**Fix/rec:** surface the FULL session_id (or a resolvable handle) on inbox notes so replies/resolution work.
**Priority:** P3.

## K8 — Stale-handoff parallel-master proliferation
**Symptom:** multiple master sessions (the outgoing janice-0, the new janice-0 5315ac20, AND a stray jp-master `54d739d9`) each picked up a DIFFERENT handoff (some the morning wind-down checkpoint). The stray was about to RE-implement the already-done+verified fence fix + re-ingest source 7 (redundant, risks clobbering the uncommitted working tree) and re-trigger entanglement.
**Root cause:** cwd-scoped handoffs are consumed by ANY new session in the cwd; with no single-master arbitration, several fresh sessions each claim master off stale checkpoints.
**Status:** flagged to the new master (5315ac20) for consolidation.
**Fix/rec:** handoff/master arbitration — a single "current master" lease per roster; a fresh session reads the LATEST handoff + checks for a live master before assuming the role. Stale handoffs should be superseded/auto-expired when a newer one lands.
**Priority:** P2 (caused near-redundant work today).

## K9 — Context-saturation degrades agents
**Symptom:** observer at 91% context skipped its chat_send tool call (→ looked dead, K4) and is at risk of auto-compacting (→ kills SSE, K1).
**Fix/rec:** flag/auto-/compact a nudged agent above ~85% context before it degrades; surface context% in roster_progress.
**Priority:** P3.

## K10 — Specter set_file_input lying-success
**Symptom:** `specter_set_file_input` returns `{success:true, count:0}` (injects an empty File) on shell-level file inputs → downstream confirm-upload 400s. Joseph drops files manually.
**Status:** FILED earlier (separate Specter track).
**Priority:** P3 (tooling, not roster-core).

## K11 — (Environment, not khimaira) upstream API 529 storms
**Symptom:** Anthropic 529 Overloaded storms (via the 8741 inference gateway, which is healthy) made working agents look unresponsive (architect churned 14m, exhausted 15 retries, failed its turn silently). NOT a khimaira bug, but the roster has no graceful 529 handling.
**Fix/rec:** roster-side: detect a turn that exhausted retries → mark the agent retry-failed (not dead) → re-dispatch on clear, rather than silent stall.
**Priority:** P3.

---

## Suggested fix order (janice-0 ↔ khimaira-0)
1. **K1** (deploy the written patch) — unblocks the whole class.
2. **K3 + K2** (identity uniqueness + restart binding) — the entanglement/proliferation root.
3. **K4 + K8** (observable liveness + master arbitration) — coordination integrity.
4. **K5, K6, K7, K9** — coordination ergonomics.
5. **K10, K11** — tooling/environment, lower core impact.

Master-side workarounds currently load-bearing (remove as fixes land): poll chat_history every turn (K1); curl chat writes (K5); title-match nudges, never window-id (K2); dispatch-with-nudge (K1/K4); re-invite via curl (K6).

---

## Handoff to khimaira-0 roster — open dig threads + next steps
*(janice-0/5ddb6421 standing down here; these are the threads I was going to dig but am handing off. Pick up directly.)*

### K3b dig — bootstrap idempotency (the duplicate-roster root)
The lived entanglement came from `/khimaira-bootstrap-roster` creating a SECOND chat (`chat-e619024f2b92`) that re-invited the same jp-agent sessions, while `chat-5dae92cf6221` already existed. The skill ALREADY HAS the guard — Step 5.5 "Detect existing roster chat (incremental-add path)" (matches by title `^(<prefix> )?roster` OR ≥50% member-overlap → AskUserQuestion add-to-existing vs new). **It didn't fire / wasn't enforced.** Dig: (1) why did 5.5 miss — title mismatch? member-overlap computed before invites landed? the new master skipped the skill and called `chat_create_room` directly? (2) Move the guard SERVER-SIDE: `chat_create_room` should reject/redirect a create whose member-set overlaps a live roster for the same prefix/project — don't rely on the skill remembering to check. That's the durable K3b fix.

### K3c dig — name-collision (two "janice-0" delivered to both)
Two distinct session_ids both named "janice-0"; chat events reached the non-member one. Dig the daemon's name→session resolution + any delivery path that keys on friendly NAME rather than session_id. Fix targets: (1) `session_set_name` should reject or auto-suffix a name already held by a live session (uniqueness at registration). (2) all delivery/routing resolves by session_id ONLY; a name resolves to exactly one id (most-recent is NOT safe for delivery). Start: grep the monitor/daemon for name-based session lookup in the chat-event push path.

### K3a fence — STATIC audit points for agent-4 (readable now, no deploy needed)
Beyond the deferred runtime checks (Q3 resume=new-PID, Q4 fence-before-loop on live-verify), two are code-readable today:
- **Q1 (ordering):** confirm `_acquire_session_claim` runs in `register()` BEFORE `_ensure_subscriber`/`_proactive_sse_loop` starts — a fenced subprocess must never reach `subscribe_events`.
- **Q2 (cleanup/reclaim):** confirm the claim file is removed on clean exit (atexit), AND a stale claim from a CRASHED (non-atexit) subprocess is reclaimable via the dead-PID path — else a hard crash permanently fences the real session. The `_pid_alive` dead→reclaim branch is the safety; verify it's reached on a leftover claim.

### Success criteria (how khimaira-0 knows the class is closed)
The master-side workarounds above become UNNECESSARY: a master can trust SSE (no poll-every-turn) post-K1 deploy; MCP `chat_send` works without curl (K5); nudges aren't needed to wake idle agents on dispatch (K1/K4); a master handoff (transfer) leaves a working master without a daemon repair (K13); a re-bootstrap can't fork a duplicate roster (K3b). When those hold, K1–K13 are validated.

### Fastest deploy note
K1 (`475c002`) deploys on subprocess respawn — Joseph's jeevy close-all+fresh-start triggers it. K3a fence + K2 quick-wins + K12 daemon should ride the same respawn once committed, so one transition validates several at once.
