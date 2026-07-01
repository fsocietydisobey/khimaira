# Dual→single verdict-gate rework — DESIGN NOTE (HOLD: surface before implementing)

> Author: khimaira-void-1 · 2026-06-28 · for khimaira-master review. This is the
> load-bearing gate machinery #39 touched — DESIGN ONLY, no code until master signs off.
> Bug-class-enumeration style.

## Goal / bottom line
Merge critic + verifier into one **gatekeeper** role WITHOUT silently dropping the
property the dual gate actually buys: **two INDEPENDENT judgments before a commit.** The
naive merge (one gatekeeper posts both verdicts) preserves the *mechanical* dual-positive
but DESTROYS independence (one mind, two rubber-stamps). The fix: single gatekeeper verdict
for normal tasks (the lean tradeoff Joseph chose); a **2nd INDEPENDENT gatekeeper verdict**
required for high-stakes — restoring two-eyes exactly where risk justifies the cost.

## What the dual gate provides today (must be consciously preserved or dropped)
1. **Independence** (THE load-bearing property): `approve` (critique) and `ship`
   (verification) are authored by TWO DIFFERENT sessions (critic ≠ verifier). `committable`
   = done + critic=approve + verifier=ship → two independent eyes. *A single gatekeeper
   loses this.* — must re-introduce for high-stakes.
2. **Two-axis judgment**: critique (logic/correctness) vs verification (does-it-ship/tests).
   One gatekeeper can hold both axes, but the note should say whether we keep both verdict
   *types* or collapse to one.
3. **Author-binding integrity**: only the reviewer role can post the gate verdict (blocks
   master self-stamping; IN-MASTER-9 is the audited exception). Must carry over to gatekeeper.

## Bug-class enumeration — every site assuming `critic ≠ verifier` + dual-positive
Class: *"code that hard-codes two distinct verdict-authoring roles and/or a two-role
dual-positive commit gate."*
1. `chats.py:_VERDICT_AUTHOR_ROLES` (2083) — verdict→role map. **BROKEN** (no gatekeeper).
2. `chats.py:_ROLE_VERDICTS` (1894) — role→verdicts (inverse). **BROKEN.**
3. `chats.py:_committable_task_ids` (1846) — requires critic=approve AND verifier=ship.
   **BROKEN** (one role; and the count-of-independent-verdicts changes for high-stakes).
4. `chats.py:_maybe_auto_advance_gate_complete` + `_maybe_wake_master_on_gate_complete`
   (~2124/2139) — "both verdicts present" trigger. **BROKEN.**
5. `chats.py:_maybe_nudge_missing_verdict` (1900) — role→verdict nudge. **BROKEN.**
6. `chats.py:record_gate_verdict` author-binding (2080) + valid verdict set. **BROKEN.**
7. `api/chats.py:_get_session_obligations` — `crit_present`/`ver_present` + per-role
   `owed_verdict` (the path #39 + the C3 drain hook depend on). **BROKEN for gatekeeper.**
8. `master_override_verdict` (2152) IN-MASTER-9 — author-binding for the override. **BROKEN.**
9. Log/string literals "critic=approve + verifier=ship" (1811/1818, guard5:231,
   auto_dispatch:454). **COSMETIC** but update for accuracy.
SAFE: anything keyed on the generic `verdict_role` field (already role-parametric) — but
verify it's fed the gatekeeper role.

## Design options (verdict model) — pick one
- **Option A — keep 4 verdicts, rebind author to gatekeeper** (lowest churn): gatekeeper may
  author all of approve/changes/ship/hold; `committable` stays "approve + ship present."
  ✓ minimal diff. ✗ independence is fake (one session posts both); the two-verdict ritual is
  now busywork. ✗ doesn't model the high-stakes 2nd-verdict cleanly.
- **Option B — single pass/block verdict + count-based gate** (RECOMMENDED, more churn):
  gatekeeper posts ONE verdict per review — reuse `ship`/`hold` as pass/block (drop
  approve/changes from the GATE; optionally keep as free-text critique in the task note).
  `committable` = **N independent `ship` verdicts from N DISTINCT gatekeeper sessions**, N=1
  normal / N=2 high-stakes. ✓ models independence honestly via the count; ✓ the escalation
  is just "N=2". ✗ touches committable + obligation + auto-advance + UI verdict rendering.
- **Option C — new `pass`/`block` vocab**: cleanest names, MOST churn (record_gate_verdict,
  get_gate_verdicts, UI, every literal). Not worth it vs B.

**Recommendation: Option B.** It's the only one that makes independence a real, countable
property and makes the high-stakes escalation fall out as N=2. Worth the churn because this
is the gate; a fake-independence gate (A) is a latent correctness hole.

## Escalation trigger (high-stakes → 2nd independent verdict)
- **Trigger** (reuse master.md's existing high-stakes test): task touches >2 files OR core
  architecture OR security/auth OR role-doc edits. Master sets a `high_stakes=True` flag on
  `chat_task_create` (or the gate auto-detects from the task's file set — prefer explicit).
- **2nd verdict source**: a SECOND gatekeeper INSTANCE (gatekeeper-2) — distinct session_id,
  so the independence is real. If only one gatekeeper seat exists, the **consultant** serves
  as the backup independent reviewer (it has the design context but is a different mind).
  Spell out the fallback so a 1-gatekeeper roster can still satisfy high-stakes.
- `committable(high_stakes)` = 2 `ship` verdicts from 2 distinct sessions; `committable(normal)`
  = 1. (Generalizes the current "2 fixed roles" into "N distinct sessions".)

## Obligation / wake / drain (C3) interplay
- `_get_session_obligations` must compute `owed_verdict` for the gatekeeper role: a done task
  with fewer than N distinct gatekeeper `ship`/`hold` verdicts → each gatekeeper that hasn't
  voted owes one. (Mirror the current crit/ver presence logic but count distinct sessions.)
- The **C3 drain hook already consumes `owed_verdict`** generically → it works the moment
  obligations recognize gatekeeper. No C3 change needed (confirmed).
- `committable_gate_tasks` (drives master-commit wake + Guard-7 signal-3) → recompute on the
  N-distinct-`ship` rule.
- IN-MASTER-9 override + auto-advance + master-wake-on-complete → key on "N reached," not
  "both present."

## Class-invariant test (catches the whole class regardless of role count)
One parametrized test asserting: *for a done task, `committable` is true IFF it has ≥ N
distinct `ship` verdicts from sessions holding the gatekeeper role (N=2 high-stakes else 1),
AND a gatekeeper member with no recorded verdict on an owed task registers an `owed_verdict`
obligation.* Run it for {normal, high-stakes} × {0,1,2 verdicts} × {same-session-twice (must
NOT count as 2), two-distinct-sessions}. The "same session voting twice ≠ independence" case
is the load-bearing assertion that guards the independence property.

## What I will NOT do until you sign off
No edits to any of the enumerated sites. On your pick (A/B/C) + confirmation of the
escalation trigger + the 1-gatekeeper fallback (consultant-as-2nd), I implement Option B
(or your choice) as a single coherent change + the class-invariant test, surface for review,
then it rides the daemon-restart window (it's daemon-side — recognizes gatekeeper).
