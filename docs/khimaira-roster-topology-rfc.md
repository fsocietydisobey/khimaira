# Roster Topology RFC — Domain Leads (T7)

> **Status:** Phase 0 RFC awaiting Joseph sign-off. Phases 1-4 deferred pending review.
> **Author:** architect-1 (per consult msg-1aede6a8dc2a + enumeration msg-2a44845324d2)
> **Date:** 2026-05-25

---

## Executive Summary

**Problem:** generalist agents make weak domain decisions (React patterns, DB schemas, LangGraph idioms); master gets overloaded decomposing tasks across domains it doesn't know deeply.

**Proposed:** replace the generalist agent pool with **3 domain leads per roster** (persistent specialists). Leads own full decomposition AND execution within their domain. Master coordinates contracts between domains; does not decompose internals. Terminal-neutral or terminal-reducing.

**Decision needed from Joseph:**
- Approve T7 (3-lead-per-roster topology, terminal-reducing)?
- Approve phased migration (Phase 1 pilot in khimaira-dev, then collapse, then extend to jeevy)?
- Accept temporary +2 terminals during Phase 1 pilot (1-2 weeks)?

If yes to all three: architect-1 designs Phase 1 implementation briefs.
If no: iterate this RFC.

---

## Why T7 (not T1-T6)

Seven topology options evaluated (full enumeration in consult msg-2a44845324d2). Summary:

| Option | Description | Verdict |
|---|---|---|
| T1 | Status quo (generalist agents + master decomposes) | BROKEN — both pain points |
| T2 | Domain leads persistent (intake's sketch) | Viable; refined as T7 |
| T3 | Domain-tagged generalist agents | Partial — executor side only |
| T4 | Richer CONTEXT UPDATE addenda | Supplementary, not replacement |
| T5 | Pair-of-experts (agent + advisor consult) | High overhead per task |
| T6 | Per-domain sub-masters | Too heavy; recreates centralization at lead level |
| **T7** | **Leads as workers and decomposers** | **CHOSEN** — addresses both pain points within terminal budget |

T7 specifically: leads own full domain (decomposition + execution + spawn-on-fan-out). Replaces generalist agent pool entirely. Master coordinates BOUNDARIES between domains, never internals.

---

## Role Definitions

### Domain Lead (e.g. `backend-lead-1`)

**Budget:** sonnet/medium (between agent and architect)

**Owns:**
- Full decomposition for their domain (master hands intent; lead returns decomposed plan)
- Execution of work within their domain
- Domain-specific patterns + conventions for the codebase (the agent that "knows React" or "knows the DB schema")
- Transient agent spawning for fan-out on large tasks (chat_task_create to ephemeral agent slot)

**Does NOT own:**
- Cross-domain coordination (master's role)
- Roster-wide policies (master's role)
- Design questions spanning domains (architect's role)
- Quality review (critic / verifier's roles)

**Cross-domain handshake:** via contracts master defines, NOT peer-to-peer coordination.

### Master (updated scope)

**Owns:**
- Routes intent to relevant lead(s)
- Cross-domain coordination (frontend + backend + DB simultaneously)
- Boundary contracts between domains ("backend exposes endpoint X, frontend consumes")
- Roster-wide policies + budget allocation

**Does NOT own:**
- Within-domain decomposition (lead's job)
- Domain-specific implementation choices (lead's job)

### Other roles (unchanged)

- **intake** — user-facing; routes to master same as today
- **tracker** — STATE.md + pipeline monitoring; unchanged
- **architect / analyst / critic / verifier** — cross-cutting advisory; stay generalist; consulted on patterns spanning domains
- **observer** — surveillance; unchanged in Phase 0 (potential merge with tracker is separate consult)

---

## Master ↔ Lead Protocol

**Single-domain task:**
1. Master receives intent from intake handoff
2. Master identifies single relevant domain (e.g. "this is purely backend")
3. Master hands intent to backend-lead with one-line scope
4. Lead decomposes + executes + reports back
5. Master integrates lead's report into final answer for user

**Cross-cutting task (multi-domain):**
1. Master receives intent
2. Master identifies domains involved (e.g. backend + data)
3. Master DEFINES CONTRACTS between domains ("backend exposes POST /widgets returning {id, name}; data layer provides widgets table with PK widget_id")
4. Master sends per-domain slice to each lead with explicit contract reference
5. Each lead decomposes + executes WITHIN their slice
6. Master integrates at contract boundaries; verifies leads honored contract

**Master is coordination-thin per domain** (no per-domain decomposition) and **coordination-rich at boundaries** (contract definition + integration verification).

---

## Per-Roster Lead Selection

**Same protocol across rosters, different domains per roster.** Pick 3 leads matching each roster's tech surface:

| Roster | Domain leads | Rationale |
|---|---|---|
| **khimaira-dev** | `backend-lead`, `data-lead`, `devops-lead` | Backend-heavy Python monorepo; minimal frontend |
| **jeevy-product** | `frontend-lead`, `backend-lead`, `data-lead` | React/Next + backend + DB; uses khimaira's devops |

**Why 3 not 4:** terminal budget. Three leads + four advisory + three coordination = 10 terminals per roster. Saves 1 vs current.

**Don't force identical topology:** domains differ between rosters. **Do force identical protocol:** master ↔ lead handshake same shape across rosters; Joseph learns one routing pattern.

---

## Phased Migration

**Phase 0 (THIS):** ship RFC; Joseph approves topology + phasing.

**Phase 1 — Pilot (1-2 weeks):** add 2 domain leads (backend-lead, data-lead) to khimaira-dev as ADDITIONAL persistent roles. Keep agent pool. Validate lead protocol works for real tasks. Terminal count temporarily +2 (12 instead of 10).

**Phase 2 — Collapse:** retire generic agent-N pool. Leads do the work. Transient agent slots only for explicit fan-out from leads. Terminal count back to 10.

**Phase 3 — Extend:** apply pattern to jeevy-product with its domain selection.

**Phase 4 — Observation + iteration:** track lead bottleneck signals, refine.

Each phase produces its own brief; this RFC defines only Phase 0 scope.

---

## Cross-Cut with Discipline Fixes (2026-05-25)

Today's discipline fixes (master-parallel-dispatch Cat 1, intake-drip-feed Cat 1, master-defaults-to-user Cat 1) and this RFC are COMPLEMENTARY, not duplicative:

| Symptom | Discipline fix (shipped today) | Structural fix (this RFC) |
|---|---|---|
| Master serial-dispatch | IN-MASTER-6 + checkpoint | Reduces master's dispatch volume — leads dispatch internally |
| Master defaults to user | IN-MASTER-4 refined | Reduces master's "I don't know" frequency — lead owns domain |
| Agent makes wrong domain call | Not addressed | Leads have domain context — primary fix |
| Master decomposition error | Partial (PARALLEL-CAPABLE convention) | Lead decomposes within domain — primary fix |

Discipline fixes ship NOW; structural fix is multi-week. Discipline fixes are NOT blocked on this RFC.

---

## Open Questions (for Joseph)

1. Accept temporary +2 terminals during Phase 1 pilot (12 instead of 10) for 1-2 weeks?
2. Confirm domain selection per roster (khimaira: backend/data/devops; jeevy: frontend/backend/data) — or adjust?
3. Lead budget at sonnet/medium acceptable, or upgrade to opus/medium for decomposition-heavy roles?
4. Phase 1 success criteria — what signals "leads are working" vs "iterate before Phase 2 commit"?

---

## Risks + Mitigations

- **Lead becomes mini-master-overload:** narrower domain (one tech surface) → context fits in single session.
- **Cross-cutting forces master to be domain-aware:** master sees CONTRACTS not internals; lower domain depth required.
- **Migration cost:** Phase 1 runs parallel with existing pool; cleanup at Phase 2.
- **Two topologies cognitive load:** SAME protocol across rosters, only leads differ.
- **Wrong domain boundaries:** Phase 1 pilot validates; iterate before Phase 2 commit.

Full risk analysis in consult msg-2a44845324d2.

---

## Sign-Off Block

When approved, master signs below + opens Phase 1 brief:

```
Phase 0 sign-off: APPROVED by Joseph on 2026-05-25
Decisions: T7 approved, phased migration approved, +2 terminals during pilot accepted
Phase 1 brief: pending architect-1 dispatch
```
