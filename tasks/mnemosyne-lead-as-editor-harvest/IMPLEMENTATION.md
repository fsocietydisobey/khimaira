# Mnemosyne lead-as-editor harvest (v2 — the "all agents distilled" capture model)

**Status**: deferred, named 2026-05-28. The capture-side counterpart to the v2 retrieval task (`tasks/mnemosyne-retrieval-v2/`). Build when capture-v1's per-task hook proves insufficient to populate the brain to oracle-useful density, or when the missing-from-capture roles (master, architect, critic, verifier, observer, tracker, intake) start producing knowledge Joseph wants queryable.

**Origin**: Architect's capture-layer consult (chat msg-5e476cbdbe50 and the C1-C6 expansion in msg-df07d5256991, 2026-05-27). Joseph's framing — "they all have important context" — drove the widening from domain-leads-only to all-roles. Today's `/khimaira-distill` and capture-v1 hook together cover ~one execution role per approved task; this spec is for the "every role's knowledge harvested, curated by leads or master, distilled into the right project:domain key" model.

---

## The gap this closes

Today's three live capture mechanisms (after 2026-05-28):

| Mechanism | What it captures | Gap |
|---|---|---|
| `/khimaira-distill` (manual) | The calling session's curated text, single domain | Manual + single-session + single-domain; stops with "master is not a domain lead" |
| `capture-v1` hook (bc5006a) | Per-task: assignee's logged decisions + done-report on `chat_task_update(approved)` | Per-task scope, one assignee per approval, automatic-only |
| `session_end` Stop hook | Session-end distill for domain-lead sessions only | Only fires on session end; only for lead sessions; never fires for long-lived sessions or non-leads |

**What's NOT captured today, even with all three running:**

- Master's orchestration decisions (long-lived, never hits session-end, no per-task surface)
- Architect's design decisions + bug-class enumerations (per-consult, not per-task)
- Critic's objections raised + how they were resolved
- Verifier's ship-or-no-ship reasoning (the "what verification caught" knowledge)
- Observer's anomaly catches + stuck-agent patterns
- Tracker's state-drift observations
- Intake's routing decisions

These are the **highest-leverage** roles for cross-session memory because they accumulate *judgment* and *meta-knowledge* that doesn't show up in any single agent's task log.

---

## The model

**Invert push → pull. Leads become the editorial layer of the brain.**

Instead of each session pushing its own distillation, **domain leads (and master/architect for cross-cutting knowledge) actively HARVEST + CURATE knowledge from the whole roster** and push curated pairs to mnemosyne.

### Two capture axes (the C6 taxonomy from architect's design)

| Axis | Knowledge type | Harvested by | Stored at |
|---|---|---|---|
| **Code-domain** | Backend/frontend/data/devops implementation knowledge | Domain leads (backend-lead, data-lead, frontend-lead, devops-lead) | `project:backend`, `project:frontend`, etc. |
| **Function/knowledge-type** | Orchestration, architecture, verification, analysis, observation, routing | Master (or designated curator) | `project:orchestration`, `project:architecture`, `project:verification`, etc. |

Every role's knowledge has a home. Code-domain roles harvest into code-domain keys; cross-cutting roles harvest into knowledge-type keys.

---

## Deliverables (C1-C6, architect's enumeration)

### C1 — Harvest architecture

How a curator (lead or master) pulls knowledge from roster sessions:

- **Sources**: `session_recent_decisions(target_session_id=...)`, `session_query_transcript`, `session_summarize_transcript`, chat history for done-reports + verdicts.
- **Curator → mnemosyne**: Curator calls `mnemosyne_client.distill(qualified_domain, curated_text, session_slug=curator_name)` per project:domain key.
- **Data flow** (Mermaid in the architect's design, msg-df07d5256991): roster sessions PRODUCE decisions/done-reports/transcripts; curators consume + dedupe + distill.

### C2 — Attribution

Which curator harvests which session/task:

- **By task domain**: when a task is assigned to an agent and its `ctx-id` or scope clearly maps to one code-domain, the corresponding domain lead is the natural curator.
- **By touched paths**: if the agent's work spans `backend/api/**`, that's backend-lead's territory.
- **Cross-domain work**: master decides at task-assignment time which lead curates, or designates self for orchestration-level knowledge.
- **Lead-less domains**: today khimaira has backend/data/frontend leads. If a domain has no lead, fallback to session-end self-distill (existing mechanism) or master-harvest.

### C3 — Triggering (the load-bearing part)

**Must be event/cadence-based, NOT manual.** Manual = never happens; that's why pre-capture-v1 mnemosyne was at ~33 pairs after hours of work.

Candidate triggers:

1. **On task approval** (already wired for the *assignee* via capture-v1). Extension: also wake the domain lead to harvest the task's surrounding context (critic verdict, verifier reasoning, architect consult if any).
2. **On consult close** (architect/critic/verifier deliverable posted): designated curator harvests the consult chain.
3. **Cadence for long-lived sessions** (master, leads themselves): every N decisions or every M minutes of activity, distill the accumulated delta.
4. **On session-end** (existing): keep as backstop, widen to cover non-lead roles into knowledge-type domains.

### C4 — Master + architect coverage (cross-cutting knowledge)

The highest-value knowledge that has no natural domain lead:

- **Master → `project:orchestration`** (dispatch decisions, gate adjudications, sequencing calls)
- **Architect → `project:architecture`** (design decisions, bug-class enumerations, two-phase audit reversals)
- **Critic → `project:critic-review`** (objections raised, how resolved; signal for future review discipline)
- **Verifier → `project:verification`** (what verification caught, mock-vs-reality failures)
- **Observer/tracker → `project:observation`** (anomaly patterns, drift signals)
- **Intake → `project:routing`** (routing decisions, fan-out patterns)

Either master self-harvests these (auto-fan-out via task #59's `/khimaira-distill` extension, or a dedicated hook on master's decision-logging) OR a designated "curator" role does it.

### C5 — Dedup with existing self-distill

Primary: lead-harvest / master-harvest (this spec).
Fallback: session-end self-distill (existing, kept for lead-less domains).

Need a dedup mechanism so the same knowledge doesn't get distilled twice when both paths fire. Options:
- Tag each distill call with its provenance (`source_session`); dedup at read time via source-session uniqueness.
- Suppress session-end self-distill for domains that are actively harvested (config flag per domain).

### C6 — Curation discipline (the load-bearing constraint as coverage widens)

**The biggest risk as capture widens is NOT cost — it's noise.**

A polluted brain is worse than no brain. If every role distills every decision, the oracle retrieves a junk drawer and accuracy *drops* despite the corpus growing.

The discipline:
- **5-15 high-signal pairs per role-session, NOT every logged decision.** Curators are the editorial layer; their job is to drop the obvious, the trivial, and the local-to-one-task.
- **Haiku distiller already filters at distill time** (5-20 pairs, "only what generalizes") — that's the first signal-quality gate. C6 adds a second gate at the *input* layer: curators pre-filter before feeding the distiller.
- **A knowledge-type taxonomy is durable; ad-hoc free-form curation drifts.** The C4 mapping (role → knowledge-type domain) is the taxonomy.

---

## Phased build plan

### Phase 1 (post-capture-v1 stabilization)

Wait until capture-v1 (bc5006a) has been live ~2-4 weeks. Audit:
- How many pairs landed per domain?
- Which roles' knowledge is missing that the oracle would benefit from?
- Is curation quality holding up (sample-check the distilled pairs)?

If capture-v1 alone gets mnemosyne to a useful density and oracle accuracy is rising, Phase 1 might be all that's needed for a while. Don't build C1-C6 prematurely.

### Phase 2 (lead-harvest extension to existing capture-v1 hook)

Smallest viable extension of the existing hook:
- On `chat_task_update(approved)`, in addition to harvesting the assignee, also notify the domain lead for that task's project:domain.
- Domain lead reads the surrounding consult chain (critic verdict, verifier reasoning) + their own audit notes, distills to their domain.
- ~half-day work on top of capture-v1.

### Phase 3 (master + cross-cutting curation)

- Wire `/khimaira-distill` from master to auto-fan-out across knowledge-type domains (`project:orchestration`, `project:architecture`, ...) — this is task #59 with the knowledge-type taxonomy added.
- Cadence trigger: master self-distills every N decisions (e.g., 25) into `project:orchestration`.

### Phase 4 (cross-role coverage)

- Architect distills to `project:architecture` on consult close.
- Critic distills to `project:critic-review` on verdict post.
- Verifier distills to `project:verification` on SHIP/no-SHIP.
- Observer/tracker distill to `project:observation` on anomaly surface.
- Intake distills to `project:routing` on routing decision.

Each role gets a small role-doc section + a Themis-hint reminder + possibly a hook trigger.

### Phase 5 (dedup + taxonomy hardening)

- Implement C5 dedup mechanism.
- Lock in knowledge-type taxonomy (C4) as the canonical set of cross-cutting domains.
- Add tests: synthetic roster activity → assert each role's knowledge lands in expected domain at expected density.

---

## DoD (per phase)

Each phase is independently dispatchable + ships behind critic+verifier gates + lives-tested via observed mnemosyne `/domains` count growth in the right keys.

The overall DoD for the full v2 capture model:

- All roles named in C4 distill into the right project:domain at the expected trigger.
- Mnemosyne `/domains` shows non-trivial pair counts across at least 4 knowledge-type domains within a representative working session.
- Oracle queries on those domains return curator-tagged pairs (provenance preserved).
- Sample-check: at least 80% of pairs in a domain pass a "would a senior engineer agree this is worth keeping?" sniff test.

---

## Cross-references

- Capture-v1 hook (the producer this spec extends): commit `bc5006a` (`feat(hooks): harvest approval — distill agent decisions into mnemosyne on task approval`)
- Oracle qualified-key fix (the consumer that needs these domains populated): commit `c2f52aa`
- v2 retrieval task (the orthogonal accuracy lever): `tasks/mnemosyne-retrieval-v2/IMPLEMENTATION.md`
- Architect's original capture design: chat msg-df07d5256991 (C1-C5) + msg-5e476cbdbe50 (C6 expansion)
- Joseph's framing: "they all have important context" (chat conversation 2026-05-27)
- Related task #59 (master `/khimaira-distill` auto-fan-out): Phase 3 of this spec absorbs that work
