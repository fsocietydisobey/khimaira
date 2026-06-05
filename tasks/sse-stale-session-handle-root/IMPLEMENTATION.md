# SSE Inbound-Delivery Root — subscriber emits through a STALE session handle

> **Status:** OPEN — **HIGHEST-VALUE roster fix. This is THE root of "roster agents
> keep going unresponsive" (Joseph's #1 blocker).** Traced audit-grade (firsthand code
> read) by janice-0 (5ddb6421), 2026-06-05. ~10-line fix; the correct mechanism is
> already present in the same file, just not wired into the subscriber.
> File: `packages/khimaira-chat/src/khimaira_chat/server.py`.

## The bug (audit-grade — janice read the code)

The SSE subscriber that delivers inbound chat events into an agent emits through a
**request-context session handle captured at the first tool call** — which goes stale
after compaction/turn-churn, so delivery silently goes nowhere while the subscriber task
is still "alive."

Chain:
- `_sse_loop` (L333) — the inbound-delivery loop — emits via
  `_emit_channel_notification(session, …)` (L192) →
  `await session.send_message(msg)` (~L206).
- That `session` is the **MCP request-context session** captured at the first tool call:
  `_ensure_subscriber(ctx.session)` (L891), `ctx = server.request_context` (L890).
- The request-context session is tied to the **request/turn lifecycle**. After
  compaction/turn-churn the subscriber keeps emitting through a **stale session handle**
  → the `notifications/claude/channel` goes nowhere → the agent is **silently SSE-deaf
  while the subscriber task is still running**.

## Why this is the ROOT, not a symptom

The existing mitigations — the watchdog (L95) + force-resubscribe-on-tool-call
(L917-933) — paper over a **crashed** subscriber task. But this subscriber **doesn't
crash**: it keeps running and keeps emitting into a dead session handle. So the
watchdog sees it "alive" and never resubscribes. That's why agents look dead while
being fully alive — and why nudges (a fresh tool call → force-resubscribe) temporarily
revive them. The nudge-liveness false-negative + the "must call chat_my_chats every
turn" fragility are **downstream of this**.

## The fix is ALREADY in the file (just not wired into the subscriber)

- The subprocess captures a **stable, subprocess-lifetime** `write_stream` at stdio
  boot: `_state.write_stream = write_stream` (L1120, inside `async with stdio_server()`
  L1114). It lives for the whole subprocess — survives compaction.
- That field's **own comment (L105-108)** says it exists *"so the SSE subscriber can
  emit notifications/claude/channel directly, WITHOUT needing the session object from
  request_context."* — i.e. this exact fix was intended but never completed.
- **Other emit paths in the same file already use it correctly:**
  `await _state.write_stream.send(msg)` (L1090, L1357), explicitly "bypassing the
  session" (comment L1212).
- So the stable mechanism is **built + proven + used elsewhere** — the subscriber
  migration is just **incomplete**.

## The fix (~10 lines)

1. Make `_emit_channel_notification` send via `_state.write_stream.send(msg)` (mirror
   L1081-1090) instead of `session.send_message(msg)`.
2. Drop the `session` capture from `_sse_loop` / `_ensure_subscriber` / L891 — the
   subscriber no longer needs the request-context session at all.
3. The `write_stream` survives compaction (captured once at stdio boot), so delivery
   stops depending on the volatile per-request session.

## Why it resolves the whole class

Eliminating the dependence on a live per-request session means inbound delivery survives
idle AND compaction — the structurally-correct "the subprocess has ONE stable emit
channel for its lifetime" model. This is the implementation of janice's higher-level
framing (turn-based agent ↔ push-SSE mismatch): you stop trying to keep a per-turn
session alive and emit through the subprocess-lifetime stream that already exists.

## Test / verify

- Unit: subscriber emits through `_state.write_stream`, not `session`; a stale/closed
  request-context session does NOT break delivery.
- **Live (the real proof):** stage an agent → compact it → master posts to the chat →
  the agent receives the `<channel>` block on its next turn WITHOUT having re-called
  `chat_my_chats`. (Today, that fails — the whole "call chat_my_chats every turn" rule
  exists to work around exactly this.)

## Sequencing / ownership

- janice traced it + offered to continue. Coordinate: she's jp master, this is a
  khimaira-chat (shared substrate) fix → khimaira roster owns the change, janice
  co-reviews (she has the deepest context).
- Deploy = khimaira-chat MCP server restart (every session re-execs the server) → batch
  with the next daemon/restart window; needs the live compaction-survival verify.
- **This likely makes the dispatch-with-nudge hand-holding + much of the nudge-liveness
  workaround unnecessary** — fix this first, then re-evaluate the other roster-liveness
  gaps.

## Cross-references (this is the ROOT under several filed gaps)

- `tasks/nudge-liveness-false-negative-gap/` — downstream symptom (alive-but-not-acking).
- `tasks/roster-restart-identity-gap/` — sibling (identity on restart); SSE-deafness
  portion is THIS.
- Part F / Family-B SSE work (closed live this session) fixed slot-keyed *routing* +
  inert-denial; this is a DIFFERENT bug — the *emit channel* going stale, not the
  subscriber *key*. Both real; this one is the compaction-survival root.
- janice's higher-level framing: turn-based↔push mismatch → durable-queue + external-wake.
  This fix is the concrete first step.
