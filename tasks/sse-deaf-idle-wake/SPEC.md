# SSE-deaf idle wake — muther ISSUE 1/2 root-cause + fix

> Status: SPEC · audit-grade root-cause done 2026-06-18 · scoped by khimaira-0
> Source: muther (jeevy roster) 2026-06-18 platform bug report, ISSUEs 1+2
> Corpus: `shared-docs/ESCAPED-BUGS-LOG.md` → `sse-deaf-wake-misdiagnosed-as-delivery`

## The reframe (what the audit falsified)

muther reported ISSUE 1 as **"SSE-deafness / incomplete server.py migration"** — a
message-**delivery** bug, with a fix "reportedly written/pending-deploy." **The audit
falsifies the mechanism.** There is no incomplete migration, and chat-server delivery is
architecturally complete. The "fix that wasn't deployed" describes code that doesn't exist.

This is the same failure shape as the Specter selector-scope reversal (bug-class-enumeration
case study 2): an **inspection-grade hearsay mechanism** that audit-first overturns. Had we
trusted it, we'd have chased a server.py migration while the real bug sat untouched.

## What the delivery stack actually is (audit-grade, observed in code)

Four layers, all present and working:

1. **Persist-before-fanout** — every message → JSONL via `_append()` BEFORE any SSE push
   (`monitor/chats.py:371-374`, msg written at `:1215`). Broadcast failure never loses data.
2. **Directed-msg durable fallback** — a `to=[...]` message to a member with a dead SSE
   subscriber posts a durable **inbox notice** (`chats.py:4215-4227`). Task begin-signals +
   assignments are directed (`send_message(..., to=[agent_id])` at `:3642/:3924/:3963`).
3. **Cursor backfill on SSE reconnect** — per-`(session,chat)` cursors in `cursors.jsonl`
   replay missed events on reconnect (`chats.py:4299-4371`).
4. **Turn-time catch-up poll** — `_poll_missed_chat_events()` EXISTS (in the UPS hook at
   `hooks/user_prompt_submit.py:955`, NOT chats.py), polls each chat since a watermark.

## The real root cause — bug-class enumeration

**Class:** *a durably-stored dispatch is never consumed because every mechanism that would
consume it only runs on a turn the idle agent never takes — and the one auto-mechanism that
does run is age-capped narrower than the wake can be delayed.*

| Path | Status / evidence | Mechanism |
|---|---|---|
| **A — turn-gating / watchdog coverage** | **UNKNOWN [inspection-grade]** — needs live jeevy daemon logs | SSE re-subscribe (#3) + catch-up poll (#4) run ONLY on a turn. Idle CC sessions take no turns until stdin injected; the only injector is `roster_recovery` kitty send-text. BUT the watchdog already wakes on `obligations OR pending_task OR pending_invite OR unread_inbox OR unconsumed_chat`, and `_session_has_unconsumed_chat` (roster_recovery.py) catches ANY inbound msg (directed/undirected, any role) by `ts > last_active`. So critic/verifier/tracker SHOULD be woken. muther's "never woke critic/tracker" is REAL but its mechanism is unconfirmed — most likely a Path-B masquerade (woke, but catch-up surfaced nothing → looked like no wake). **Do NOT blind-fix the watchdog; confirm against live logs first.** |
| **B — staleness cap vs wake latency** | **BROKEN [audit-grade]** | Catch-up poll skips any msg older than **10 min** (`user_prompt_submit.py:976,1018`: `cutoff = now-10min`, filter `ts >= cutoff`). But wake latency stretches past that: `_IDLE_MIN_S=300` floor + `_COMPACT_COOLDOWN_S=300` between attempts + `_WIP_THRESHOLD_S=900`. A dispatch waiting >10 min is INVISIBLE to the auto-poll even when a wake lands — agent wakes, polls, sees nothing fresh, returns to idle. **The auto-catch-up window is narrower than the wake can be delayed.** This is why only a MANUAL kitty nudge worked: its explicit text ("call chat_my_chats + check inbox + roster chat") makes the agent actively `chat_history` with NO age filter, bypassing the cap. |
| **C — undirected broadcast to idle-consult roles** | **BROKEN [audit-grade]** | `IDLE_CONSULT_ROLES = {architect, analyst, critic, verifier}` (`chats.py:89`). Undirected broadcasts to these are wake-suppressed (`chats.py:4194-4196`) AND get NO durable notice (notice fires only for directed msgs, Path #2). So an undirected gate-consult to critic-1/verifier-1 is recoverable ONLY via the turn-gated, 10-min-capped poll — = muther's "reached NEITHER critic-1 nor verifier-1." The `:4181-4182` comment "suppression is lossless (backfill via _poll_missed_chat_events)" is true ONLY inside the 10-min turn-gated window — misleading as written. |

## Coverage decision

Fix the two **audit-grade** paths now; **gate Path A on live evidence** (don't blind-edit a
watchdog that already covers the roles on inspection).

### Fix B — decouple catch-up from a fixed age cap (UPS hook)
`hooks/user_prompt_submit.py` `_poll_missed_chat_events`: the **watermark** already prevents
re-surfacing already-seen msgs (per-chat last-seen event_id). The 10-min age cap is a crude
SECONDARY filter that actively harms: it drops unseen-but-old dispatches. **Raise the cap well
above the max wake latency** (e.g. `KHIMAIRA_CHAT_POLL_STALENESS_MIN`, default 60 min) — OR
drop the age filter for messages NEWER than the watermark (watermark already bounds it). Keep a
generous cap only as a cold-start bound (first poll, no watermark) so a brand-new session
doesn't replay a day of history. Env-gated, fail-open.

### Fix C — durable notice for suppressed idle-consult dispatches (chats.py)
`monitor/chats.py` wake-filter (`:4194-4196`): when an undirected MSG is suppressed for an
idle-consult-role member with no live subscriber, post the SAME durable inbox notice that
directed msgs get (Path #2, `:4215-4227`). The message is already in JSONL; this just adds the
turn-surfacing safety net so a suppressed consult survives to the target's next turn regardless
of the staleness cap. Cheap, isolated, closes the critic/verifier hole at the source.

### Path A — audit-first, deferred
Pull the live jeevy `roster_recovery` logs (`journalctl --user -u khimaira-monitor | grep
roster-recovery`) for the incident window. Confirm whether the wake FIRED for critic/verifier/
tracker. If it fired → Path A is a Path-B masquerade (B fixes it). If it did NOT fire → real
coverage gap, enumerate why (`_env_enabled` off for jeevy? `_discover_roster_windows`
cross-roster scoping per completed #19? idle floor?) before any watchdog edit.

## Test contract
- **B:** `test_poll_surfaces_old_unseen_msg` — watermark unset/behind, msg 20 min old, NOT
  self → surfaced (currently dropped by the 10-min cap). + `test_poll_cold_start_bounded` —
  no watermark, 100 historical msgs → bounded replay, not a full-history dump.
- **C:** `test_suppressed_consult_posts_notice` — undirected MSG to a critic member with no
  live subscriber → `post_notice` called for that sid. + `test_directed_still_single_notice`
  — directed msg path unchanged (no double-notice).
- **Live-verify (Path A gate):** after B+C deploy, reproduce on a throwaway 2-seat roster —
  dispatch an undirected consult to an idle critic, wait past 10 min, confirm the wake lands
  AND the agent surfaces the consult on wake. A quiet idle seat stays silent.

## Deploy
Daemon bounce (muther notified first). B is in the UPS hook (per-session, picked up on next
turn — no bounce needed for B itself, but bundle). C is daemon-side `chats.py` → bounce. Run
full `roster_recovery` + `chats` + `user_prompt_submit` test suites green first.
roster_recovery.py / guard5.py / auto_dispatch.py are MANUAL-FORMATTED — never whole-file black.
