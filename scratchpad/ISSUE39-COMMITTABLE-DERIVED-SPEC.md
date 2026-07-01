# ISSUE #39 — Committable-Derived Verdict-Obligation: Enhancement Spec

> Author: khimaira-void-1 · 2026-06-28 · status: SPEC ONLY (no commit/deploy; §5 gate untouched)
> For: khimaira-master → Joseph. Companion to void-0's uncommitted #39 working-tree change.

## Goal (bottom line)

Decide whether to add a **behavior-independent cold-start verdict-wake** on top of
void-0's #39 (which gates cold-start wake on `gate_required`). **The headline finding:
the "committable-derived, behavior-independent" fix the memory called for is ALREADY
SHIPPED for the warm path, and Guard-7 already provides a behavior-independent
*surfacing* backstop for the cold path. The only genuinely-new thing a committable-
derived enhancement would add is a targeted *wake* for the `gate_required`-UNSET
cold-start cell — and that cell cannot be made behavior-independent without choosing a
"review-wanted" signal, because 0-verdicts-unset is intrinsically ambiguous.**

So the real fork is narrower and different from the "as-is vs behavior-independent
bundle" framing. See §5.

## In plain terms

A reviewer (critic/verifier) "owes a verdict" on a task the master marked done. If the
substrate never registers that obligation, the idle reviewer is never woken and the
pipeline stalls silently (verdict-starvation). There are already several mechanisms that
engage reviewers; this spec maps all of them, finds the ONE remaining gap, and asks
whether closing it is worth the cost.

---

## 1. The existing engagement mechanisms (audit-grade map)

Four independent mechanisms already touch reviewer-engagement. Read these before
deciding anything — most of the "fix" already exists.

| # | Mechanism | File | Trigger | Action | Depends on `gate_required`? |
|---|---|---|---|---|---|
| M1 | Direct-verdict obligation (warm) | `api/chats.py:874-924` | done task, **≥1 verdict present**, reviewer's slot empty, ≤1h | **targeted WAKE** of owing reviewer via roster_recovery | **NO** (behavior-independent) |
| M2 | #39 cold-start obligation | `api/chats.py:891-924` (uncommitted) | done task, **0 verdicts**, ≤1h, **gate_required=True** | **targeted WAKE** of both reviewers | **YES** (behavior-dependent) |
| M3 | Guard-7 signal-3 | `guard7.py:316-344` | done task, not committable, 15min–6h | **SURFACE** (notice+chat) to resolved master/peer | **NO** (behavior-independent) |
| M4 | Guard-5 Part A auto-create | `api/chats.py:601-640` | `gate_required` task → done | auto-create per-role review-tasks | **YES** |

Key consequences:
- **M1 is the memory's "mirror `_committable_task_ids`" fix — and it is ALREADY COMMITTED
  in HEAD.** Verified: `git show HEAD:...api/chats.py` lines 874-924 contain the
  direct-verdict block scanning `crit_present`/`ver_present` per task, scoped by
  per-chat `member_roles`. It fires regardless of `gate_required`. The memory
  (`project_obligation_gate_task_premise_dormant`) predates this landing.
- **M3 means cold-start-unset does NOT silently stall.** Even with #39 gating the *wake*
  on `gate_required`, an un-gated 0-verdict done task is still SURFACED to the master by
  Guard-7 within 15min–6h. The difference is surface (master must act) vs. wake (owing
  reviewer woken directly).
- `_committable_task_ids` (`chats.py:1846`) itself is the **commit-ready** detector
  (BOTH verdicts present). M1 is its structural *inverse* (a slot still empty). "Mirror"
  = same scan shape, inverted predicate.

Recency constants (env-tunable, fail-open):
- M1/M2 window: `_OWED_VERDICT_WINDOW_S` = 3600s (`KHIMAIRA_OWED_VERDICT_WINDOW_S`)
- M3 window: `_VERDICT_STALL_S` 900s … `_VERDICT_MAX_AGE_S` 6h

---

## 2. Bug-class enumeration

**Bug class (abstract):** A `done` work-task whose reviewer-verdict is owed but for which
no obligation is registered that *wakes* the owing reviewer → verdict-starvation.

**Enumeration grid** — (`gate_required` SET vs UNSET) × (cold-start: 0 verdicts vs
warm: ≥1 verdict present), classified at HEAD and with void-0's #39 applied. Evidence
quality tagged per the bug-class-enumeration rule.

| Cell | State | HEAD | With #39 | WAKE path | SURFACE path | Evidence |
|---|---|---|---|---|---|---|
| **C1** | gate_required SET × warm | SAFE | SAFE | M1 (gate_required irrelevant) | M3 | **audit-grade** — 75 tests green incl. warm/recency |
| **C2** | gate_required SET × cold-start | **BROKEN** | **SAFE** | M2 engages both reviewers | M3 | **audit-grade** — `test_gate_required_cold_start_fires_both_reviewers` + `_respects_recency` ran green |
| **C3** | gate_required UNSET × warm | SAFE | SAFE | M1 (behavior-independent) | M3 | **audit-grade** — warm path exercised by recency tests |
| **C4** | gate_required UNSET × cold-start | SKIP | **SKIP (by design)** | **none** (no targeted wake) | **M3 surfaces to master** | **inspection-grade** — Guard-7 code-read, not executed this session |

**Reading the grid:**
- C1/C3 (warm, either gate state): already behavior-independent and SAFE in HEAD via M1.
  This is the memory's durable fix — **already done.**
- C2 (gated cold-start): the one cell void-0's #39 *fixes* (BROKEN→SAFE). Audit-grade.
- **C4 (un-gated cold-start) is the ONLY residual wake-gap.** It is *not silent* (M3
  surfaces to master), but the owing reviewer is not directly woken. It is **deliberately
  skipped** because 0-verdicts-unset is ambiguous: it could be "review wanted, not
  started" OR "review legitimately skipped" (an audit/research done-task). Without a
  "review-wanted" signal, auto-waking C4 storms reviewers on every review-exempt task.

**UNKNOWN flagged for audit follow-up:** M3's blast radius on review-exempt tasks. Guard-7
signal-3 has no `gate_required` / review-exempt guard, so it *surfaces* on every
non-committable done task in the 15min–6h window — including audit/research tasks that
legitimately skip review. This is a pre-existing Guard-7 property, not a #39 regression,
but it is the same ambiguity C4 faces and should be audited if M3 is leaned on as the
backstop. (Mark **inspection-grade**; needs a live Guard-7 dry-run to classify.)

---

## 3. The class-invariant test

One assertion that fires regardless of path **or** whether master gated — the
regression guard for the whole class. Parametrize over the 2×2 grid so a future edit to
any cell trips it.

```python
# packages/khimaira/tests/test_direct_verdict_obligation.py

import pytest

# (gate_required, n_verdicts_present, expect_owed) — the class contract.
# C1/C3 warm: owed regardless of gate (M1, behavior-independent).
# C2 gated cold-start: owed (M2, #39).
# C4 un-gated cold-start: NOT owed via the WAKE path (deliberate; M3 surfaces instead),
#   UNLESS the chosen enhancement option flips this (see §5 — update the param then).
@pytest.mark.parametrize(
    "gate_required, seed_verdict, expect_owed_wake",
    [
        (True,  "critic",   True),   # C1 warm gated   → verifier owed
        (True,  None,       True),   # C2 cold gated   → both owed  (#39)
        (False, "critic",   True),   # C3 warm ungated → verifier owed (M1, already HEAD)
        (False, None,       False),  # C4 cold ungated → NOT woken (Guard-7 surfaces)
    ],
)
def test_verdict_owed_class_invariant(isolated_state, gate_required, seed_verdict, expect_owed_wake):
    """CLASS INVARIANT: a recently-done task owes a reviewer wake iff review is
    'wanted' (≥1 verdict present OR gate_required) and that reviewer's slot is empty.
    Behavior-independent for the warm cells; gate-dependent only for cold-start.
    The recency window + verdict-presence flip bound and clear every cell."""
    task = _done_task()
    if gate_required:
        task["gate_required"] = True
    lines = [_meta(), task]
    if seed_verdict == "critic":
        lines.append(_verdict(task_id=WORK_TASK, by=CRITIC_SID, verdict="approve"))
    _write_chat(lines)

    # verifier is the still-empty slot in every warm case here
    owed = _owed(
        apichats._get_session_obligations(VERIFIER_SID), chats.ROLE_VERIFIER, WORK_TASK
    )
    assert owed is expect_owed_wake
```

Plus the existing storm-guard twin (`test_gate_required_cold_start_respects_recency`)
keeps the recency bound on every cell. The invariant's value: it documents C4 as a
*deliberate* non-wake, so a future "just remove the gate_required check" edit fails
loudly and forces the §5 decision to be re-made on purpose.

---

## 4. How a committable-derived backstop COMPOSES with #39 (layered, not replacement)

If Joseph wants C4 closed as a *wake* (not just M3 surfacing), the enhancement **stacks
under** void-0's #39 — it does not replace it:

```
M1  warm direct-verdict wake .......... behavior-independent ... ALREADY HEAD
 └─ M2  #39 cold-start wake ............ gate_required signal .... void-0 (uncommitted)
     └─ NEW: committable-derived C4 .... review-wanted-by-default  (this spec, optional)
M3  Guard-7 surfacing ................. behavior-independent ... ALREADY HEAD (backstop)
```

The layers are ordered by explicitness of the "review-wanted" signal: explicit verdict
present (M1) > explicit gate flag (M2) > inferred default (NEW). #39 stays the
*explicit* path; the new layer is only the *inferred* fallback for teams that never set
`gate_required`. They never conflict because each only ADDS obligations the layer above
didn't already produce (obligation list is a union; idempotent per task_id).

---

## 5. The decision fork (paste-ready for Joseph)

> **Verdict-starvation (#39) — three real options. The warm path and a surfacing
> backstop already ship; the only open question is the un-gated cold-start *wake* (cell
> C4).**
>
> **Option A — ship #39 as-is (RECOMMENDED).**
> Cold-start wake gated on `gate_required` (M2). Warm path already behavior-independent
> (M1). Un-gated cold-start (C4) is *surfaced to master* by Guard-7 (M3) within
> 15min–6h, so it does not silently stall — master acts. Net: strict improvement, safe
> (fail-open, recency-bounded), zero new blast radius. Cost: closing C4 as a *master-
> involved surface* rather than a *direct reviewer wake* — acceptable, because master
> is in the loop anyway.
>
> **Option B — A + default-flip (`gate_required` defaults True for agent work-tasks).**
> Makes C4 a behavior-independent *wake* by making review the opt-OUT default. Closes the
> whole class without master discipline. Cost: M2 + M4 now fire for *every* work-task;
> requires an explicit `review_exempt` marker for audit/research tasks; materially larger
> blast radius; needs a live storm dry-run before deploy. This is the "behavior-
> independent bundle," but it is a behavior *change*, not a pure backstop.
>
> **Option C — A + role-heuristic C4 wake.**
> Wake on un-gated 0-verdict done tasks only when `assignee_role` is an implementer/agent
> role AND a critic/verifier is a chat member. Behavior-independent, no master action,
> narrower blast than B. Cost: maintains a role allowlist (audit every hardcoded role
> enumeration — see memory `feedback_verify_live_runtime_path`); heuristic can misfire on
> edge roles.
>
> **My recommendation: Option A.** The memory's "durable behavior-independent fix" is
> already shipped (M1 warm + M3 surfacing); the marginal value of B/C is only the
> *direct wake* of C4, and both pay a real storm-guard cost to buy it. Ship #39 as-is now
> (when a daemon-restart window opens); treat full un-gated-cold-start auto-wake as a
> deliberate non-goal unless we observe (≥2×, per behavioral-rule-promotion) that
> Guard-7's master-surface is too slow in practice — then revisit C.

**Note for master:** this differs from your stated lean ("recommend the bundle"). The
bundle's behavior-independent half is mostly already in HEAD; the part that's genuinely
new (C4 wake) is not a clean win. Flagging so the Joseph recommendation is accurate. If
you still want the bundle, Option C is the lower-risk bundle than B.

---

## 6. What is NOT touched

- No commit, no daemon restart, no new branch (§5 standing gate; livyatan live).
- void-0's #39 working-tree change is untouched and remains the C2 fix.
- This spec is the input to a fork decision, not an implementation. If Joseph picks B/C,
  a follow-up task implements + adds the §3 invariant with the C4 param flipped to True.
```
