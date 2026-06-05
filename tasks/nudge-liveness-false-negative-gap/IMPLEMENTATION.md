# Nudge-Based Liveness is Unreliable — false "everyone idle"

> **Status:** OPEN. Reported by janice-0 (5ddb6421) 2026-06-05, audit-grade
> (`kitty get-text` on the actual windows). **This is the likely explanation for the
> recurring "everyone idle / no one responding" symptom Joseph hit repeatedly this
> session.** Same family as the wake-up skill's step-4 INTERIM note
> ("false-dark-prone; a deaf-but-working session may be misclassified DARK").

## The gap

Master infers roster liveness from **"did the agent post a chat ack"** — but the ack
depends on the agent **voluntarily calling `chat_send`/`chat_my_chats`**. An ALIVE agent
that received and *processed* the nudge can still produce **no ack**, so it looks dead.
An alive agent is indistinguishable from a dead one.

### Observed (audit-grade)

- **jp-observer-1** (win 386): the nudge text was in its window and it processed it —
  replied *in-window* "Observer presence confirmed. Idle standby…" — but did NOT call
  `chat_send` → no ack reached the roster chat. **Demonstrably alive; answered the nudge
  as a conversational reply instead of a tool call.** Also at **91% context**.
- **jp-tracker-1** (win 387): received the nudge, mid-long-op ("Awaiting chat list…",
  10m on a prior op). Alive, slow, mid-op.
- From master's view both looked "dead / didn't receive anything." **They were neither —
  they received AND processed; they just didn't POST.**

## Root cause

Liveness inferred from a *voluntary* chat ack has a false-negative whenever the agent
(a) replies in-window without the `chat_send` tool call, (b) is mid-long-op, or
(c) is **context-saturated** and abbreviates/skips the tool call (and may auto-compact,
dropping the nudge entirely). Contributing factor: **context saturation** — a near-limit
agent (observer at 91%) degrades and is more likely to skip the tool call.

## Why it matters / connects

This is the **false-"everyone idle"** root. This session, master repeatedly saw "everyone
idle," nudged, and re-nudged — but some agents were *alive and working*, just not acking
(and separately, some genuinely were rate-limited — see the concurrency-proxy). The two
causes (throttle vs no-ack) look identical from master's seat. Combined with the sibling
gaps (SSE-deafness [fixed], dead-window-ids on restart, session proliferation, membership
collapse, this), **roster liveness is currently inferred from unreliable voluntary
signals.** The whole class needs an *observable, involuntary* liveness signal.

## Asks (janice) + direction

1. **Don't infer liveness from a voluntary chat ack.** Use an OBSERVABLE signal — the
   `roster_progress` hook-independent **disk-WIP / last-active aggregator** (the wake-up
   skill already TODOs this), OR a **deterministic daemon-side "mark-alive" that the nudge
   triggers server-side** (delivery == proof-of-alive, independent of the agent choosing
   to call a tool).
2. **Make the nudge register liveness at the daemon on DELIVERY** — when the nudge is
   delivered to the window, the daemon marks that session alive, not waiting for the
   agent's response. (Or make the nudge prompt force the tool call unambiguously.)
3. **Surface context-saturation:** a nudged agent above ~85% context should be FLAGGED
   (and ideally auto-`/compact`ed) — a saturated agent silently degrades + skips tool
   calls + may auto-compact away the nudge. (Ties to: agent-6/verifier/critic all hit
   high context this session.)

## Class-invariant

> An ALIVE-but-not-acking agent (replied in-window / mid-op / context-saturated) MUST be
> distinguishable from a genuinely-dead one — via an involuntary observable signal
> (disk-WIP/last-active or daemon-side delivery-mark), not the agent's voluntary post.

## Cross-references

- `tasks/roster-restart-identity-gap/`, `tasks/task-claim-atomicity-gap/` — sibling
  roster-coordination structural gaps. Consider one unified "roster liveness/identity"
  structural-prevention initiative.
- The wake-up skill's step-4 disk-WIP TODO is the existing seed for ask #1.
