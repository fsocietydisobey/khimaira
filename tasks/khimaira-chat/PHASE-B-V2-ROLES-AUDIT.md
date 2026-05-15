# PHASE-B v2 ROLES AUDIT

**Status**: research synthesis (no code changes)
**Date**: 2026-05-15
**Authors**: khimaira-0 (orchestrator), test-agent (Lane R1), test-master (Lane R2)
**Trigger**: Joseph flagged that the v2 role-lift design tally from Round 6's design conversation was reasoned in a vacuum and never cross-checked against existing role-shaped patterns in the codebase. This doc grounds the v2 design in actual prior art so the new vocabulary composes with khimaira's rest of the role-system instead of forking it.

## Top-line recommendations

1. **Name the v2 dict `member_roles` on `room.meta`, NOT bare `roles`.** `OrchestratorConfig.roles` already exists at `packages/khimaira/src/khimaira/config/router.py` as a `{provider, model}` binding map (which LLM-provider/model serves which logical task). Reusing the bare name creates two unrelated namespaces with the same key. `member_roles` both prefixes for disambiguation and self-documents the dict-keys-are-session-ids semantic.

2. **Preserve the single-master-with-delegation invariant.** Every existing khimaira authority pattern (Supervisor over workers, HVD over chain-patterns, Decomposer over swarm workers, chat-master over chat-agents) uses one-level-up control with a single decision-maker. Reject quorum / multi-master designs in v2; the established "any single master can approve" framing aligns.

3. **Adopt the NodeRole discipline: small finite enum, explicit declaration, descriptive names.** The proposed v2 enum: `{master, agent, observer, critic?}` — first three settled; critic deferred to a Joseph-decision (see Open Questions §1).

4. **Don't bake the metaphor-named chat patterns (Tree-of-Life / Ouroborus / Leviathan) into the enum.** All three are pure aspirational vocabulary with zero code-enforced contracts. Two have isomorphic shapes implemented elsewhere; one has none. Naming them at the API level would prematurely commit to one shape that real use-cases haven't validated.

5. **Preserve the two-layer separation.** Chat participant roles live on `room.meta.member_roles`. Graph-internal node roles (SPR-4 critic, supervisor, executor) live in LangGraph state. A single session can play different roles at the two layers without contradiction. v2 must not flatten them.

---

## Existing role-shaped patterns — inventory

### Group A — Chat-orchestration prior art

#### A1. Handoff `_claim_role` (owner / observer)

| Field | Content |
|---|---|
| **Where** | `packages/khimaira/src/khimaira/monitor/sessions.py:1505-1517` (auto-claim logic in `consume_handoffs`); `packages/khimaira/src/khimaira/hooks/session_start.py:_format_handoffs` (surface-time framing) |
| **Role concepts** | `owner` (first session to consume; gets directive framing — "you are the PRIMARY OWNER") vs `observer` (subsequent sessions; default = stand down with subscribe / observe / stand-down options) |
| **Authority / lifecycle** | Auto-claim: first-come-first-served. Owner-only ops: `session_release_handoff`, `session_invite_handoff`. Observers can `session_subscribe_handoff` to receive owner progress updates. |
| **Composes with chat-role v2?** | **Direct precedent.** The owner/observer pair is already established khimaira vocabulary for "one decision-maker plus interested-but-non-blocking peers." v2's observer role isn't new vocabulary — it's the chat-room expression of the same concept. Strong recommendation to keep the wording identical (`observer` in both surfaces) so cross-surface readability stays. |

#### A2. Chat v1 implicit master-from-creator

| Field | Content |
|---|---|
| **Where** | `packages/khimaira/src/khimaira/monitor/chats.py:541, 629, 960` (master gating reads `room.meta.created_by`); `chat_task_update`'s `done → approved` requires master |
| **Role concepts** | One implicit role (`master = room.meta.created_by`); everyone else is implicitly `agent` |
| **Authority / lifecycle** | Master can: approve/request_changes on tasks, delete chat, transfer master via `chat_transfer_membership` (Phase B v1.3 fix). Member lifecycle: pending → accepted → (rejected/left/removed/transferred-out). |
| **Composes with chat-role v2?** | **This is what v2 lifts.** v1 has the role *concept* but no explicit field — v2 adds `member_roles` so non-creator sessions can hold non-`agent` roles. Backward-compat shim: chats without `member_roles` default to `{room.meta.created_by: "master"}`. |

### Group B — Graph-internal pipeline patterns (from Lane R1)

Seven role-shaped patterns in `packages/khimaira/src/khimaira/{nodes,graphs,subgraphs,server}/`. All are intra-graph LangGraph-state-scoped roles — **distinct runtime layer from chat participant roles**.

#### B1. Hub-and-spoke supervisor (SPR-4 orchestrator)

| Field | Content |
|---|---|
| **Where** | `nodes/supervisor.py:128-268` (build_supervisor_node), `graphs/supervisor.py:1-92` |
| **Role concepts** | Supervisor (decision-maker hub) + worker nodes (research / architect / implement / validator / gemini_assist / human_review). Roles encoded as `next_node` strings, not first-class entities. |
| **Authority / lifecycle** | Dictatorial routing: every node returns to supervisor; it picks next via Pydantic-structured Haiku output. `MAX_RETRIES_PER_NODE = 3`. Human review is an explicit interrupt. Termination on `next_node="finish"` OR retry-cap. |
| **Composes with chat-role v2?** | **Orthogonal.** Supervisor authority is graph-internal; chat-master authority is durable cross-session. No collision, no reuse. A future "supervisor session" inside a chat could naturally hold chat-master role + drive a supervisor graph — same mental model, different runtime layer. |

#### B2. SPR-4 phased pipeline (`chain_pipeline`)

| Field | Content |
|---|---|
| **Where** | `server/mcp.py:834-916`, `nodes/pipeline/{research,architect,implement,critic}.py`, `subgraphs/research.py:17-22` |
| **Role concepts** | Phase stages (research → planning → implementation → review), each with a **critic** that loops until quality threshold or max steps. Producer + critic role pair per phase. |
| **Authority / lifecycle** | Critic = "quality gate"; can return `handoff_type=needs_more_research` to loop back. No master per se — pipeline is its own authority; critic is judge-not-king. Bounded by `max_phase_steps=5`. Auto-pauses for human approval at review. |
| **Composes with chat-role v2?** | **Aligned analogically, distinct runtime.** "Critic" maps cleanly to chat-master's `done → approved` decision. SPR-4 is the in-graph version of what chat-master/agent achieves cross-session. v2's optional `critic` enum value would be the cross-surface alignment. |

#### B3. HVD (Hypervisor Daemon) meta-orchestrator

| Field | Content |
|---|---|
| **Where** | `nodes/hypervisor_dispatcher.py:1-124`, `graphs/hypervisor.py:1-166`, `server/mcp.py:chain_hypervisor` |
| **Role concepts** | Hypervisor (meta-orchestrator) + four child patterns (CLR, PDE, SPR-4, idle), health scanner, directive checker, absorb step |
| **Authority / lifecycle** | Strictly more authority than any single chain pattern: decides which pattern runs, kills on directive violations, budget-gates everything. Five explicit dispatch rules. Termination: idle decision OR budget exhausted. |
| **Composes with chat-role v2?** | **Aligned conceptually, distinct runtime layer.** HVD is to chain-patterns as chat-master is to chat-agents. Both have fixed decision-vocabularies (HVD: `{clr,pde,spr4,idle}`; chat-master: `{approved, changes_requested}`). v2's "master role" maps to HVD's authority shape one level up. |

#### B4. CLR (Closed-Loop Refiner) — `chain_refiner`

| Field | Content |
|---|---|
| **Where** | `server/mcp.py:919-978`; `graphs/refiner.py`; `nodes/refiner/` |
| **Role concepts** | Health scanner (observer), classifier (triage), executor (worker), validator (gate). Cycle counter caps iteration. |
| **Authority / lifecycle** | No external master — CLR is its own authority. Self-terminates on convergence (5 cycles no improvement), budget exhaustion, or spec completion. No human review interrupt. |
| **Composes with chat-role v2?** | **Orthogonal.** Fully autonomous; no role hand-off semantics chat-role v2 could express. A chat can trigger CLR, but the chat-roles don't survive into the CLR run. Clean separation. |

#### B5. PDE (Parallel Dispatch Engine) — `swarm`

| Field | Content |
|---|---|
| **Where** | `nodes/swarm/{task_decomposer,worker,aggregator}.py`, `graphs/swarm.py`, `server/mcp.py:swarm` |
| **Role concepts** | Decomposer (planner), workers (parallel agents, file-disjoint), aggregator (merger). 15-task cap. |
| **Authority / lifecycle** | Decomposer authority defines worker scope. Workers are anonymous + ephemeral. Aggregator has atomic-batch authority (failure rejects ALL worker results). No persistent master; graph is the authority. |
| **Composes with chat-role v2?** | **Aligned.** decomposer-worker-aggregator triplet is structurally identical to chat's master-agent-master (assign → execute → review) pattern. v2 master IS the decomposer + aggregator authority. Strong reuse case. |

#### B6. POB (Proactive Observation Builder) — `chain_toolbuilder`

| Field | Content |
|---|---|
| **Where** | `nodes/toolbuilder/{watcher,friction,proposer,forge,pr_creator}.py`, `server/mcp.py:chain_toolbuilder` |
| **Role concepts** | Watcher (observer), friction-analyzer (judge), proposer (designer), forge (implementer), pr_creator (gatekeeper-to-human). |
| **Authority / lifecycle** | Strict stage succession, no looping. PR creator is the **terminal gate** — never merges, only opens. Human is ultimate authority. |
| **Composes with chat-role v2?** | **Orthogonal.** Authority terminates at "open PR"; human is the gate. No conceptual overlap. |

#### B7. Config router `roles` namespace — ⚠️ NAMESPACE COLLISION

| Field | Content |
|---|---|
| **Where** | `config/router.py:1-76` — Router class, `get_role_provider(role)`, loads from `OrchestratorConfig.roles` |
| **Role concepts** | Provider-binding keys: `"classify"`, `"architect"`, `"research"`, etc. → `{provider, model}` dict (which LLM serves which logical pipeline task) |
| **Authority / lifecycle** | None — pure dispatch metadata. No state machine, no permission semantics, no transitions. |
| **Composes with chat-role v2?** | **CONFLICTING NAME, ORTHOGONAL SEMANTICS.** Bare `room.meta.roles` would cross wires. **Use `member_roles` on `room.meta` instead** (top recommendation #1). |

### Group C — Research stack + chat-pattern metaphors (from Lane R2)

#### C1. SPR-4 critic-loop subgraph (research/critic role pair) — REAL CODE

See B2 + B1 above for the full SPR-4 picture. Lane R2 highlighted the **producer/critic contract discipline**: producer can only write its assigned state field; critic can only emit `(score, feedback, handoff_type)`; neither can terminate the chain. v2 should inherit this "narrow role + bounded authority + structured handoff" discipline.

#### C2. Visual NodeRole taxonomy + regex-based role inference — REAL CODE

| Field | Content |
|---|---|
| **Where** | `apps/monitor-ui/src/components/project/FlowCanvas.tsx:116-135` |
| **Role concepts** | Explicit enum: `NodeRole = "entry" | "exit" | "router" | "gate" | "critic" | "synthesis" | "executor" | "default"`. Roles inferred from node *names* via regex. |
| **Authority / lifecycle** | Descriptive (visual styling), not enforcement. Persists for node lifetime; renderer is read-only. |
| **Composes with chat-role v2?** | **The strongest semantic alignment** in the audit. Both systems name **roles**; both name **critic** specifically. v2 should ADOPT the discipline (small finite set, explicit declaration) but NOT import the regex-inference (chat roles should be declared, not inferred — that's the v1 implicit-master-from-creator problem we're escaping). |

#### C3. Tree-of-Life — PURE METAPHOR

`grep -rn` returns zero hits. First appears in chat-84afd6396a3d Round 5 design as an aspirational pattern (root → branches → leaves hierarchical task decomposition). No code analog at all. v2 should NOT design for this; if it emerges, the natural primitive is `room.meta.parent_chat_id` (a separate hierarchy primitive), not a role enum value.

#### C4. Ouroborus — PURE METAPHOR (but the SHAPE exists as SPR-4 critic loops + CLR)

Name appears nowhere in code. Introduced in Round 5 design. **However**, the cyclic-feedback-until-convergence shape is implemented twice: SPR-4 critic loops (B2) and CLR (B4). v2 doesn't need an "ouroborus" role — drafter/critic are just two sessions playing existing roles (executor + critic) with a convention about message ordering. **If v2 adds `critic` to the role enum, that's the chat-level building block for Ouroborus — no new primitive needed.**

#### C5. Leviathan — PURE METAPHOR (but the SHAPE exists as process registry + monitor watch + persistent scheduler)

Name appears nowhere in code. Introduced in Round 5 design. **However**, the long-running-supervisor-spawning-workers shape is implemented in three places: `monitor watch` supervisor, process registry (`packages/khimaira/src/khimaira/monitor/processes.py`), and persistent scheduler (`packages/khimaira/src/khimaira/monitor/scheduler.py`). v2 should NOT add a "leviathan" role — the existing master + auto-accept + transfer_membership primitives compose to express it.

### Group D — MCP namespaces (from Lane R3)

#### D1. FastMCP-instance-per-tool with source-prefixed re-registration

| Field | Content |
|---|---|
| **Where** | `packages/khimaira/src/khimaira/server/sibling_tools.py` (registry); `packages/{seance,specter,scarlet,sibyl}/src/*/server.py` (per-tool FastMCP instances with `instructions=...` role declarations) |
| **Role concepts** | Each tool is a distinct **capability persona** declared via FastMCP `instructions` block: Séance (semantic searcher), Specter (browser observer), Scarlet (codebase cartographer — explicitly: "Scarlet generates structure; you generate meaning"), Sibyl (audio capturer / transcriber) |
| **Authority / lifecycle** | No authority semantic at the MCP-namespace level — each tool is callable by anyone with khimaira-MCP access. Source-prefix collision protection is the only "role boundary" enforced. |
| **Composes with chat-role v2?** | **Orthogonal** — capability declaration ≠ chat-internal authority. But one composition point: the FastMCP `instructions` block is already a role-declaration pattern in khimaira (Scarlet's "I do structure, you do meaning" is a role-pair contract). v2 *could* support an optional `description` field on each role for the same self-documenting effect. Cheap, no enforcement. |

**Operational note**: Séance returned references to a non-existent `packages/scribe/` directory during the audit (actual package is `packages/sibyl/`). Stale index — run `seance_reindex_changed(path="/home/_3ntropy/dev/khimaira")`. Not blocking.

---

## Cross-cutting findings

### F1. The bare word "roles" is already in use

**Finding (highest priority)**: `OrchestratorConfig.roles` at `packages/khimaira/src/khimaira/config/router.py` is a `{role_name → {provider, model}}` dict. v2's role lift on `room.meta` MUST use a different key.

**Recommendation**: `member_roles` on `room.meta`. Both prefixes for disambiguation AND self-documents the keys-are-session-ids semantic.

### F2. Single-master-with-delegation is a load-bearing invariant

Across every existing authority pattern — Supervisor (B1), HVD (B3), Decomposer (B5), chat-master (A2) — authority converges on a **single decision-maker one level up from the workers**. Quorum, 2-of-3-must-agree, multi-master — none of these patterns exist anywhere in khimaira.

**Recommendation**: codify single-master-with-delegation as a v2 invariant. The proposed v2 semantic ("any single master can approve") aligns, but the doc should explicitly reject quorum / multi-master designs that would diverge.

### F3. Critic ≠ Master (the SPR-4 precedent)

SPR-4's critic is a judge-not-king: provides scored feedback but can't terminate the chain — supervisor still routes after critic feedback. NodeRole has `critic` as a peer of `gate`, `synthesis`, `executor`, etc. — not a master.

**Implication for v2**: if a `critic` role lands in the chat enum, it's an **opinion-haver, not an approver**. Approval gating stays master-only. Critic feedback is non-binding but visible. **This is the resolution of the test-agent ("critic is v3") vs test-master ("critic in v2") tension** — the answer is "critic is fine in v2 *if* its semantic is opinion-only, not approver."

### F4. Owner / observer is already khimaira vocabulary

Handoffs' `_claim_role` (A1) uses `owner` and `observer` semantics. v2's `observer` role isn't new vocabulary — it's the chat-room expression of the same role concept. Strong cross-surface coherence.

### F5. Two-layer separation: chat-participant roles vs graph-internal node roles

Both layers can name **critic** and mean closely-related-but-distinct things:
- **Graph-internal critic** (B2 / C2): a LangGraph node that scores producer output, returns structured handoff. Bounded by graph state, ephemeral, no cross-session visibility.
- **Chat-participant critic** (proposed v2): a session that scores/comments on tasks in a chat room. Cross-session, durable, visible to other members.

A single session can be running an SPR-4 graph (playing graph-internal supervisor) AND a member of a chat (playing chat-participant agent or critic). v2 must keep both axes distinct; the role enum is *participant-shaped*, not *node-shaped*.

### F6. Metaphor patterns compose from primitives, don't deserve enum values

Tree-of-Life (C3), Ouroborus (C4), Leviathan (C5) have either zero code analog or isomorphic shapes implemented elsewhere via process primitives + chat primitives composing. Naming them at the role-enum level would prematurely commit to one shape.

**Recommendation**: keep the role enum function-shaped (`master`, `agent`, `observer`, `critic?`). If/when one of the metaphor patterns shows up organically, it'll emerge from composition — the role enum doesn't need to know about it.

---

## v2 design recommendations

### Schema

```python
room.meta = {
  ...,
  "member_roles": {
    # Default: {created_by: "master"} when absent (v1 backward compat).
    # Explicit overrides land here.
    "<session_id_a>": "master",
    "<session_id_b>": "agent",      # default for non-master accepted members
    "<session_id_c>": "observer",   # sees messages, can read tasks, can't send
    "<session_id_d>": "critic",     # IF v2 includes critic (see Open Q §1)
  }
}
```

### Primitives

```python
# Master-only: edit member_roles for a target session.
chat_grant_role(chat_id, target_sid, role)

# Master-only: bulk read (optional helper; member_roles is also visible via
# chat_history if surfaced in load_room response).
chat_get_roles(chat_id)
```

### Authority gates (move from implicit to explicit)

- `chat_task_update(done → approved | changes_requested)` → check `member_roles.get(sid) == "master"`
- `chat_delete` → check `member_roles.get(sid) == "master"`
- `chat_send` for observer → reject with descriptive error ("observer cannot send; transition role first")
- `chat_transfer_membership` continues to propagate master role on creator-transfer (Phase B v1.3 fix landed; preserve as non-regression invariant under v2)

### Backwards compatibility

- Existing chats without `member_roles` default to `{room.meta.created_by: "master"}` at load_room time
- All existing master-role checks (currently `created_by == sid`) switch to `member_roles.get(sid) == "master"` with the default fallback above
- `chat_transfer_membership` also writes a fresh `member_roles` entry for the recipient when the source is master (mirrors the META-update pattern Lane E shipped for `created_by`)

---

## Open questions (Joseph-decision)

### Q1. Should `critic` ship in v2 or wait for v3?

**Yes (test-master, R2)**: cheap (one enum value), composes with the Ouroborus shape without new primitives, semantically aligns with NodeRole + SPR-4 + the `_claim_role` precedent.

**No (test-agent, R1)**: defer until a forcing function organically appears, matching the conservative "wait for use case" pattern that worked for the `master` role lift.

**My read**: ship it IF critic semantic is opinion-only (not approver). Per F3, that resolves the tension — critic is a peer that comments, not a peer that approves. Backwards-compatible: existing chats default to no critics; new code that consumes critic feedback is additive.

### Q2. Should observer pre-bump to v2 alongside the role-dict lift?

Both agents vote yes (audit-bot integration win, one enum value, zero state-machine work). My earlier read was conservative-no but the audit changes the math: observer is already khimaira vocabulary (handoff `_claim_role`, F4) — adding it to chats keeps cross-surface coherence. **Revised recommendation: yes, ship observer in v2.**

### Q3. Master-leaves-chat footgun mitigation

Current behavior: master calls `chat_leave` → `done → approved` becomes unreachable until they re-join. test-master's recommendation: refuse `chat_leave` for the master; force `chat_transfer_membership` first.

**My read**: also yes for v2. Cheap to enforce, matches the "explicit intent at call sites" pattern, and prevents an obvious footgun.

### Q4. Speculative Phase B v1.4 — `chat_set_creator` admin primitive

Test-master proposed an admin-only `chat_set_creator(chat_id, new_creator_session_id)` for retroactive role propagation on chats transferred pre-v1.3 (like `chat-84afd6396a3d` itself, where `meta.created_by` is still khimaira-21 because the original transfer happened before the Lane E fix shipped).

**Status**: not part of v2; surface for Joseph's roadmap call.

---

## Forcing functions roster (consolidated)

| # | Function | Origin | v2 mitigation |
|---|---|---|---|
| 1 | Co-master delegation (creator wants to grant master rights without leaving) | Round 5 design | Solved by `chat_grant_role(target, "master")` |
| 2 | Cross-team handoff during long-running chat | Round 5 design | Solved by `chat_grant_role` + `chat_transfer_membership` |
| 3 | Observer role (audit / log readers / compliance) | Round 5 design + handoff `_claim_role` precedent (F4) | Solved by `role="observer"` |
| 4 | Master-leaves-chat (chat_leave by creator orphans master role) | test-master Round 6 design | Solved by refusing chat_leave for master (Q3) |
| 5 | `chat_transfer_membership` MUST propagate creator/master role | v1.2 dogfood failure (Lane E) | Already patched in v1.3; v2 must preserve as non-regression invariant |
| 6 | Critic semantic for Ouroborus shape | test-master Round 6 design + SPR-4 critic precedent | Optional `role="critic"` (opinion-only, see Q1) |

---

## Stale-state operational findings

- Séance index references `packages/scribe/` (non-existent; actual package is `packages/sibyl/`). Reindex: `mcp__khimaira__seance_reindex_changed(path="/home/_3ntropy/dev/khimaira")`. Not blocking; surfaces as a one-time finding.

---

## Authorship

- **Lane R1** (chain pipeline + HVD): test-agent — 7 patterns, namespace collision discovery
- **Lane R2** (research layers + chat-pattern metaphors): test-master — 5 patterns, NodeRole alignment + critic-in-v2 case
- **Lane R3** (MCP namespaces + handoff `_claim_role` + NodeRole + assembly): khimaira-0 — 2 patterns + synthesis

Research only; no production code changed by this audit.
