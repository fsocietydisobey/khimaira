# Task-Claim Atomicity Gap — concurrent claimants clobber

> **Status:** OPEN. Surfaced 2026-06-05 by analyst-1 + architect-1 after **two
> uncoordinated multi-claims within minutes** (API-key task double-claim → Themis
> intake task triple-claim). Per `[[behavioral-rule-promotion]]` (≥2 occurrences →
> promote to structural).

## The gap

A task dispatched as **"to any available agent — claim it"** + N idle agents → they
**all race-claim it simultaneously** ("executing now") and are about to read-modify-write
the **same files concurrently** → clobber (last-writer-wins / malformed merge / one
overwrites the other's edit). The claim is **not atomic** — there's no first-claim lock,
so every idle agent thinks it won.

Observed 2026-06-05:
- API-key task → agent-3 + agent-5 both claimed within seconds (both targeting
  `~/.claude/settings.json` — a concurrent read-modify-write clobber).
- Themis intake task → agent-2 + agent-3 + agent-4 all claimed within seconds (all
  targeting `intake.yaml`).
- Both caught by analyst before any write; master serialized manually each time. **Manual
  serialization every time is the behavioral band-aid; it drifts.**

## The structural fix (architect's CAS — the right answer)

**Atomic compare-and-swap at `chat_task_update` → `in_progress`:** only a
`pending → in_progress` transition succeeds. A 2nd claimant's update finds the task
already `in_progress` → the call **fails/no-ops with "already claimed by <agent>, stand
down."** First claim wins atomically; the losers are told, immediately, before any write.

- This is the **structural** answer. The behavioral alternative — "master explicitly
  assigns to ONE named agent, never 'any agent claims'" — works but **drifts** (depends on
  master/intake remembering every time; intake's "dispatch to any available agent" pattern
  re-introduces the race). Adopt the behavioral rule NOW (assign-by-name) *and* ship the
  CAS so it can't recur.

## Where to implement

- The claim transition lives in the chat-task lifecycle (`chat_task_update` /
  `chat_task_signal_start` path, `packages/khimaira/src/khimaira/monitor/chats.py` +
  its API route). The CAS guard: reject a `→ in_progress` if `status != pending`
  (or `!= pending` AND `assignee != caller`), returning a structured
  "already claimed" so the losing claimant stands down cleanly.
- Pairs with architect's structural-prevention spec batch (Rule-4 / Guard-N / Rule-1).

## Interim (in effect now)

Master assigns TASK 1 (Themis intake) **explicitly to agent-4 by name** (not "any agent
claims") — the behavioral mitigation, applied immediately. The CAS makes it permanent.

## Class-invariant test

> Two agents issuing `chat_task_update(task, in_progress)` concurrently → exactly ONE
> succeeds; the other receives an "already claimed" rejection and adds no second writer.

## Cross-references

- `[[behavioral-rule-promotion]]` — 2× in minutes = promote to structural.
- `tasks/roster-restart-identity-gap/` — sibling roster-coordination structural gap.
