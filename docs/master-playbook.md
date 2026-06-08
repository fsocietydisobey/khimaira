# Master Playbook — Reference Material

> Extracted from `roles/master.md` to keep the boot-time doc lean.
> Master reads this on demand. Nothing here changes the rules — it
> provides context, worked examples, and incident post-mortems.

---

## Domain Lead Delegation — Full Protocol {#domain-lead-delegation}

When the CONTEXT UPDATE intent maps to a single domain with a lead in the roster,
DO NOT decompose yourself — delegate decomposition to the lead.

**How to identify the lead:**
- `monitor/`, `hooks/`, `mcp_calls/`, `attach/`, non-data packages → `backend-lead`
- DB / JSONL persistence / schema → `data-lead`
- Both → cross-cutting (see step 5)

**Handshake steps:**

1. **Send intent to lead** via `chat_send_to(lead_session_id)`:
   ```
   🎯 BACKEND INTENT [ctx-id: ctx-<8hex>]
   Goal: <one-line distilled goal>
   Scope: see CONTEXT UPDATE v1 — ctx-<same>
   Constraints: <budget, timing, anti-touch list>
   ```

2. **Wait for lead's decomposed plan.** Lead returns:
   ```
   🎯 BACKEND PLAN [ctx-id: ctx-<8hex>]
   Decomposition:
   - <work unit 1> — <self / fan-out>
   - <work unit 2> — <self / fan-out>
   Cross-domain dependencies: <none / lists>
   Estimated complexity: <S/M/L>
   ```

3. **Approve or refine.** Small plans (1-2 units): approve and let lead execute.
   Large plans (≥3 units): refine via chat before execution.
   NEVER decompose internally — refining means challenging the plan shape.

4. **Cross-cutting work (multi-domain):** you define the CONTRACT between domains
   (e.g. "backend exposes `POST /widgets`; data provides `widgets` table with PK
   `widget_id`"). Send per-domain intent to EACH lead with contract reference.
   Leads do NOT peer-coordinate — handshake via contract only.

5. **Lead reports done** via standard task_update protocol (honors IN-AGENT-4
   branch/worktree/merge_intent). Proceed to Step 6 + Step 7.

**PROPOSE-ONLY BRANCH (leads are write-blocked):**
If leads are `propose_only = true`, execution diverges at step 3:
- Lead produces an IMPLEMENTATION-READY plan (file paths, exact changes, acceptance
  criteria) but does NOT execute. Themis blocks all writes (NO_FILE_EDIT_PROPOSE_ONLY).
- Master dispatches an implementing agent via `chat_task_create` + BEGIN with the
  lead's plan as spec.
- Lead guides the implementing agent (domain authority) — answers domain questions
  and reviews output against the plan. This direct lead↔agent guidance is allowed.
- Agent reports to master; critic + verifier review as usual.

**BREAK-GLASS — master as fallback implementer (agent pool exhausted):**
When ALL implementing agents are rate-limited/dead/context-exhausted AND work is
blocked: master is the relief valve. Master has legitimate write authority and MAY
implement directly with the SAME discipline as an agent (analyst/architect review +
suite-green + commit gate). Order: (1) wait/re-prompt a recovering agent; (2) spawn
another agent; (3) master as last resort. Log it (`session_log_decision`).
Never weaken the lead write-gate — a lead self-break-glass re-opens the bypass.
Capacity signal: frequent break-glass → agent pool under-provisioned; flag Joseph.

**When lead is unavailable** (session ended, `target_reachable=false`): fall back to
master-decomposes pattern + log a project decision noting the lead needs restart.

Cross-references: `docs/khimaira-roster-topology-rfc.md`, `docs/domain/README.md`,
`backend-lead.md`, `data-lead.md`.

---

## Post-Approval Domain Knowledge Distillation {#post-approval-distillation}

After approving a domain lead's task, automatically push their domain knowledge to
mnemosyne so long sessions contribute even if the lead never manually runs
`/khimaira-distill`.

**Trigger:** approved task's assignee session name contains a domain-lead pattern.
Skip for: agent, critic, verifier, architect, analyst, intake, tracker, observer.

**What to distill:** the lead's done-report text (task note + key decisions from
`session_state(assignee)`) — NOT the raw transcript. 5–15 bullets is enough.

```bash
/home/_3ntropy/dev/khimaira/.venv/bin/python3 - <<'PYEOF'
from khimaira.hooks.mnemosyne_client import distill
from khimaira.hooks.session_end_utils import detect_domain, detect_project
import json, os
assignee_name = "ASSIGNEE_SESSION_NAME"
done_report   = """LEAD_DONE_REPORT_TEXT"""
domain  = detect_domain(assignee_name)
project = detect_project(os.getcwd())
qualified = f"{project}:{domain}" if project and project != "unknown" else domain
result = distill(domain=qualified, transcript=done_report, session_slug=assignee_name)
print(f"distilled → {qualified}: {result}")
PYEOF
```

Fail-open: if mnemosyne is unreachable (`result=None`), log a warning and continue.

Two knowledge sinks (document in approval note when distilling):
- **mnemosyne PROVISIONAL** — surfaces at SessionStart for next lead session on this project:domain
- **`docs/domain/<domain>-knowledge.md`** (AUTHORITATIVE) — human-written permanent reference

---

## Critic / Verifier Discipline — Observed Failures {#critic-verifier-failures}

**2026-05-22 (tracker wiring approval):** master fired critic consult ~8 seconds AFTER
critic had self-volunteered the review, then approved 23 seconds later. Audit trail
looked like a rubber-stamp even though critic genuinely reviewed. Fix: fire the consult
FIRST (before reading the work), wait for a reply timestamp-gated AFTER the consult,
and document any self-volunteer short-circuit in the approval note.

---

## Dispatch Failure Observed Incidents {#dispatch-failures}

**2026-05-22 (jp roster):** master dispatched investigation to jp-analyst-1;
`chat_send_to` returned msg-id successfully but analyst hit Anthropic API 5xx on
receive — silently failed. Master waited indefinitely until Joseph noticed and prompted
a manual retry. Fix: no-reply within 3-5 min → check `session_state` + retry once.

**2026-05-22 (khimaira roster stress test):** master dispatched 2 parallel read-only
tasks. BOTH agents hit ambient throttling (visible 429 in Joseph's terminals). Master
initially waited on the 3-5 min silence timer when Joseph had ALREADY reported the
visible failure 2 min in. Should have triggered immediate triage. Visible failure =
immediate trigger, not silence-timer trigger. Saves 1-3 min of unnecessary waiting.

---

## Source-of-Truth for Agent State — Failure Example {#agent-state-failure}

**2026-05-22 (JEEVY-534):** Joseph said "critic is done reviewing". Master asked Joseph
"what was the verdict?" instead of running `session_state("jp-critic-1")` first.
Right reflex: query the agent first; user is the signal that critic is done, not the
source of the verdict.

---

## When to Delegate — Anti-Patterns {#delegate-antipatterns}

Anti-patterns to recognize in yourself:
- "I'll just do it, agent round-trip is overhead" — that's the violation.
  Round-trip is cheaper than opus tokens on the work.
- "This is trivially small, the <5 min escape clause applies" — re-read condition 3.
  "Trivially small" requires user-is-blocked precondition. Without that, condition 1 wins.
- "Drafting the task spec is more work than just doing it" — usually not true once
  you've spent >2 minutes on the task itself.
- "But I already started" — partial work is sunk cost. Hand off state to the agent.

**Observed failure (2026-05-22):** master attempted the tracker role wiring (4 mechanical
file edits across chats.py / session_start.py / roster script / bootstrap-roster.md)
directly, justifying it as "trivially small." Three of four Edit calls failed (file-not-Read
precondition); user caught the violation; work was re-delegated to agent-1 at sonnet/medium.
The "trivially small" loophole defeated the delegate-first default exactly as bound to.

---

## Pre-AskUserQuestion Routing — Worked Example {#askuser-routing-context}

**Today's violation (2026-05-22):** master asked Joseph "(a) bundle as sibling task, or
(b) separate?" — that's a SCOPE / DECOMPOSITION decision (analyst) or a DESIGN trade-off
(architect). Should have routed to architect first; user only if architect can't resolve.

**Why this is in the role doc:** memory rules (`feedback_consult_top_tier_before_user`)
load conditionally; role-doc text loads every session. IN-MASTER-4 Themis warn enforces at
tool-call time. Both layers together — role doc raises awareness; Themis catches drift.

---

## Lead-Domain Gate — Detailed Analysis {#lead-domain-gate-analysis}

**S1 — Override must be audited + rate-visible:** the lead-domain override (absent-lead →
master approved-override, same quorum-timeout mechanism as critic/verifier override) is
AUDITED (logged, attributable) and its RATE is visible/alerting. An unaudited override is
theater — diffusion hides in the override count. Evidence from this session: the
quorum-override was used repeatedly when gate-roles were absent; if leads are often absent,
the override becomes the default path and reproduces diffusion.

**S3 — Lead-verdict author ≠ task implementer:** if lead and implementer are the same
session (small roster, lead executes in-domain), that's self-approval = no gate. A lead
who implemented a task cannot be the gate-verdict author. Escalate to master-audited or
a peer-lead instead.

**S4 — Precise claim:** this gate CONVERTS diffusion-of-responsibility into **attributed
ownership** (lead's name on the domain-ok → traceable). Residual rubber-stamp risk is
disincentivized by attribution. The claim is "diffusion located + attributed," NOT
"diffusion eliminated."

---

## Cross-Session UUID Bug #63 — Context {#cross-session-uuid-bug}

**Bug (confirmed 2026-05-28):** The daemon name-registry resolver has a routing defect (#63):
passing a friendly name (e.g. `"agent-3"`, `"intake-1"`) as `target_session_id` to
`session_post_notice`, `session_log_question`, `session_post_answer`, or as a member of the
`to` list in `chat_send_to` silently misroutes the message into a friendly-named on-disk
directory instead of the target's live inbox. Sender receives a `📨` success ack; recipient
receives nothing. 19 confirmed misrouted messages observed 2026-05-28.

**How to get the UUID:** Call `session_list()` — each entry shows `id: <uuid>` alongside
the friendly name. Alternatively, read `sender_id` from any prior chat message.

**When fixed:** Once khimaira task #63 ships the resolver fix, the rule softens to
"either name or UUID is OK." Remove or date-retire the master.md UUID section at that point.

---

## Gap Forwarding — cwd Handoff Rationale {#gap-forwarding-cwd}

**Why cwd-handoff, NOT session_post_notice to khimaira-0 by name:** name-routing is
unreliable (#63 bug: mis-routes to dead sessions). A cwd-scoped handoff surfaces
deterministically. Always forward gaps by handoff first; notice is supplementary.

**Roster invariant:** a `scope_cwd=/path/to/khimaira` handoff surfaces on EVERY session
working in that project, not only the khimaira-dev seat. In a dedicated khimaira-dev
roster this is fine. In a product roster sitting in the same checkout, it creates
cross-surface noise. This applies everywhere cwd-based targeting is used: disk-WIP
attribution (#7), reap-safety, and gap-forwarding all share the same class — cwd ≠ per-seat.

---

## Knowledge-Gap Reflex {#knowledge-gap-reflex}

Before guessing about unfamiliar code, past decisions, or why something was built a
certain way — call `oracle_query` first. It fuses live code search (Séance) + distilled
lessons (mnemosyne). Master also serves as oracle FOR agents: when an agent hits a
knowledge gap, query `oracle_query` and feed the answer back rather than asking the agent
to guess or escalate separately.
