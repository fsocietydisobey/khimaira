# Roster Restart/Resume — Identity-Regeneration Gap

> **Status:** OPEN. Reported by **janice-0** (jp master, session 5ddb6421) on 2026-06-04
> via two `session_post_notice`s to khimaira-master. Written up at wind-down for
> next-session pickup. Both notices acked in chat-fdf7c4cbd3bd (~2026-06-04 18:5x).

## Gap class (one line)

Roster **restart/resume REGENERATES identity at BOTH layers** — a new khimaira
**session-id** *and* a new kitty **window-id** per agent — instead of RESUMING the
existing identity. Every restart leaks an orphan and silently breaks id-based
coordination.

Same class as the dispatch-stall / membership-collapse / SSE-deafness gaps closed
this session: **a roster STATE CHANGE silently breaks coordination.** (See
`[[bug-class-enumeration]]`.)

## Two manifestations

### A. Window-ID renumbering → silent nudge failure  (severity: HIGH)

- Restart/resume re-creates kitty windows with **new window IDs**. Evidence:
  `jp-backend-lead-1` 242→375, `jp-agent-1` 243→379, `janice-0` 244→371.
- `kitty @ send-text --match id:<stale>` hits a dead ID and **NO-OPS SILENTLY** —
  no error, nonzero exit not raised on the send path used → the wake LOOKS like it
  succeeded ("WOKE agent-N") while reaching **nothing**.
- This is the undiagnosable **"I nudged, no agent responded"** symptom. A failed
  wake that reports success — and it would defeat the auto-recovery being built to
  fix the *other* roster gaps.
- janice's own mitigation: match on the **stable role title**
  (`--match title:jp-backend-lead-1`) instead of the volatile window id →
  title-match survives renumbering, nudges landed immediately.

### B. Session-ID proliferation → orphan accumulation + resolution ambiguity

- Restart/resume spawns a **fresh khimaira session-id per agent** → multiple
  sessions per role accumulate (one new orphan per restart).
- Evidence (`session_list`, 2026-06-04): `jp-data-lead-1` had **4** session-ids;
  `jp-backend-lead-1` **3**; `jp-architect-1` **2**; same pattern across roles.
- Consequences: resume-picker clutter; name→id resolves to most-recently-active but
  the orphans linger and a **stale one can win a resolution** → feeds the
  membership-collapse churn and transfer/delete-resolving-to-the-wrong-sid.

## Unified root

Restart/resume **creates fresh identities instead of resuming them.** Identity is
regenerated at BOTH layers (session-id + window-id) on every restart — the silent
driver behind the nudge breakage, the session clutter, AND the name-resolution churn.

## What already partially mitigates this (today's work)

- **alive-guard** (`d7b4eb7`): `delete_session` refuses an *active* session →
  protects live sessions from accidental deletion during orphan cleanup.
  **Cleanup-side, not the root.**
- **`/khimaira-delete-rosters`**: prefix-aware orphan cleanup, safe via the
  alive-guard. **Cleanup-side.**
- **Slot model** (roster-identity Family A+B, closed live today): heals **chat**
  identity (`member → slot → current-sid`) across restart — but the underlying
  session-id still proliferates in `session_list` (the slot bridges chat-auth; it
  does **not** dedupe session records or help kitty window resolution).
- **Nudge scripts already re-enumerate `kitty @ ls` FRESH every run** (never cache
  an id-map) → they've been landing despite the renumber. The remaining hazard is
  (a) any tool that *caches*, and (b) the silent-no-op on a dead-id send.

## Asks + proposed direction

### Quick wins (hardening — do first)

1. **Title-match / fresh-enumerate everywhere.** All wake/nudge/recovery tooling
   matches on the stable role title (`--match title:<role>`) OR re-enumerates
   `kitty @ ls` fresh at every wake; **NEVER cache an id-map across a restart.**
   Update the `/khimaira-nudge` skill to prefer title-match + WARN against caching.
2. **Loud-fail on dead targets.** After `send-text`, assert the target exists /
   received (or use a match form that errors on no-match) → a silent no-op becomes a
   visible failure.
3. **Audit `roster_recovery` auto-wake** (`packages/khimaira/src/khimaira/monitor/
   roster_recovery.py`): verify the send-text auto-wake **re-resolves** window IDs at
   wake-time and does not cache a stale id-map. If it caches, auto-wake silently
   fails for the whole roster after every restart — the recovery system defeating
   itself.

### The deeper fix (root)

4. **Resume should re-attach the existing session-id**, not spawn fresh — identity
   must survive restart.
5. If a fresh session is unavoidable, **auto-retire the prior session-id** for that
   role on restart (mark superseded / prune) so `session_list` holds exactly **one**
   live session per role.
6. A **stable session↔role↔window binding** that survives restart (key windows by a
   durable title/env; expose a resolver) so tools never juggle ephemeral ids. Ties
   together the window-id gap + the alive-guard + the slot model.

## Connection to existing machinery

`bin/roster` already stamps `KHIMAIRA_ROSTER_SLOT=<ROSTER_INSTANCE_ID>:<name>` per
window (+ a TRAP-2 token file at `~/.local/state/khimaira/roster-tokens/<wid>`), and
the slot registry resolves `<name> → current-sid`. The gap: that machinery handles
**chat auth** across restart but does **not** (a) dedupe the session-id records in
`session_list`, nor (b) help kitty window-id resolution. The root fix most likely
**extends** the slot/roster-token model to: (a) auto-retire prior session-ids per
slot on a new launch; (b) expose a title/slot-based window resolver for nudge tooling.

## Class-invariant test (per bug-class-enumeration)

A single invariant catches both manifestations regardless of entry path:

> After ANY roster restart, `session_list` holds exactly ONE live session per
> role-name, AND a nudge resolved by role-name/title reaches the live window.

## Next-session starting point

1. Read this doc + janice's two notices (acked; in the chat-fdf7c4cbd3bd record,
   ~2026-06-04 18:5x).
2. **Quick win first** — harden `/khimaira-nudge` + the nudge scripts to title-match
   + loud-fail (cheap, high value; stops the silent-nudge-failure that masquerades as
   "no agents responding").
3. Audit `roster_recovery` auto-wake for id-caching (ask #3).
4. Then design the root fix (resume-preserves-session-id + auto-retire-prior) with
   architect — it's an extension of the slot / roster-token model, not a greenfield.

## Cross-references

- Today's roster-identity close (Family A + B) — the slot model this extends.
- `d7b4eb7` alive-guard + `/khimaira-delete-rosters` — the cleanup side.
- `[[bug-class-enumeration]]`, `[[behavioral-rule-promotion]]`.
