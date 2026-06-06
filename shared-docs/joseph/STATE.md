# Khimaira Roster State — 2026-06-04 post-redeploy

## ▶ In Flight (3 workstreams, active)

### Part F (SSE-deafness janice fix) — ▶ LIVE-VERIFY IN FLIGHT
- **Status**: DEPLOYED LIVE (fc33b1b + 5256dcc, daemon redeployed post-deploy-window 40s SSE-drop). Code PASS (explicit revoked_sids check), test (a) PASS (real superseded s1), test (b) fast-follow (un-slotted non-regression). Live-verify staging NOW (agent-6, msg-7a3e235f318d). Refined evidence bar (msg-06660b70e7ec architect, msg-afbdcb5a4ab8 analyst): capture mechanism + outcome (which-path-staged + migration-bridge-re-key-if-staged + revoked-no-op-log + slot-keyset + s2-queue-via-prior-member's-slot + would-miss-under-old-code). Analyst-1 + architect-1 co-review pending evidence post-stage.
- **Gates**: critic-APPROVE (code correct), analyst-APPROVE (Family-B Family-A gates clear), data-lead-APPROVE, architect-APPROVE (design + mechanism), frontend-lead-APPROVE (design). **REMAINING**: live-verify (B)-rigor observed evidence → analyst/architect clear → master APPROVE → Part F (Family B / SSE-deafness) CLOSED LIVE.
- **Payoff**: janice's SSE-deafness actually fixed (not a hypothesis, observed live).

### API Throttle Proxy (kills 32-session server-overload) — ▶ IMPLEMENTATION IN FLIGHT
- **Status**: Research COMPLETE (axis locked empirically), impl-spec APPROVED (master greenlit), task created (task-c44c45c4835b), agent-1 IN PROGRESS (claim accepted).
- **Axis**: CONCURRENCY (simultaneous in-flight), NOT RPM/TPM; ceiling DYNAMIC. Empirical evidence (analyst-1 msg-203dd35a41e3): backend-lead + frontend-lead trip PAIRED-within-1-2s repeatedly across hours (13-times, 13:16-13:57) → concurrent-overlap pattern, not rate.
- **Spec** (architect msg-126de0f4cd53 + addenda):
  - CROSS-SESSION CONCURRENCY-CAP: shared asyncio.Semaphore(N) across 32 sessions; queue excess; FAIRNESS via X-Claude-Code-Session-Id
  - ADAPTIVE RETRY: honor Retry-After + jitter (CLI lacks this); 429s absorbed transparently
  - ⭐ FLAG-1 NEVER-CRASH → DEGRADE-TO-PASS-THROUGH (#61 fail-open discipline): all 32 point ANTHROPIC_BASE_URL here; CLI won't re-point mid-session → crashed proxy = hard-outage-of-32 worse than storm. Fail-open MUST be in-process; wrap throttle logic best-effort, direct-forward fallback.
  - FLAG-2 CONSERVATIVE-N-BOOTSTRAP + ADAPTIVE-N (AIMD): start LOW/safe, relax UP toward headroom with margin; multiplicative-DECREASE on 429-rate-rise, additive-INCREASE when clean.
  - **MOCK-UPSTREAM TESTING** (analyst msg-da20bda9e5f6 + architect concur): throttle-logic + FLAG-1-degrade + FLAG-2-AIMD + streaming tested vs MOCK injectable-upstream (fake-Anthropic: 200/429-with-Retry-After/5xx/slow-stream) — DETERMINISTIC, no real-throttle-trip/call-burn. Real API ONLY for small passthrough/streaming smoke-test. (Testing the 429-handler on real API reproduces the bug + non-deterministic.)
- **Gaters**: analyst-1 (axis + FLAG-1/FLAG-2 + mock-upstream), architect-1 (design), master (design + staged-rollout never-dark-32).
- **Design questions standby** (architect-1): FLAG-1 never-crash wrapping, AIMD controller shape (429-rate-window vs single-429, increment cadence), streaming-slot-hold (semaphore release after stream drains).

### Interim Margin (CLI tuning + model-leak fix) — ⏳ DISPATCH READY (Joseph-authorized)
- **Status**: Joseph-authorized (intake relay, msg-1d384360cbd1). Two items, dispatch-ready:
  - (b) CLI env-tuning: ↑CLAUDE_CODE_MAX_RETRIES + ↓CLAUDE_CODE_MAX_TOOL_USE_CONCURRENCY (→~3). Low effort, immediate margin while proxy builds.
  - (c) Verifier-tier model-leak fix: dispatch to available agent. Flagged earlier; master has context.
- **Purpose**: buy headroom against 429-storms while the proxy (the durable fix) is built. Both env-only, zero outage risk.

### Roster Auto-Recovery Watcher (compaction cascade) — ✅ DEPLOYED + LIVE (PID 470873)
- **Status**: DEPLOYED (fc33b1b + 5256dcc re-deploy, daemon restart 2026-06-04). Running: threshold 85%, interval 60s. Recovered 6 stalled sessions post-deploy. Manual recovery NO LONGER NEEDED.
- **B3 gate**: critic-APPROVE + verifier-SHIP (retroactive, B3 EMERGENCY-BYPASS applied + disclosed). Gate closed.
- **Effect**: Sessions ≥85% context trigger auto-distill + /compact; mechanical recovery live.

### Roster auto-recovery watcher (keystroke injection, silent-mutation class) — ✅ DEPLOYED + LIVE
- **Status**: DEPLOYED (PID 470873, 04:27 UTC). Running: threshold=85%, interval=60s.
- **B3 gate**: CRITIC APPROVE (retroactive, 4626a007cc31) + VERIFIER SHIP (retroactive, 4626a007cc31). Gate closed.
- **Artifact**: commit 4626a007cc31 (agent-3 implementation), 28 tests green, both safety fixes verified (ambiguity abort ✅ + TOCTOU exact-match ✅).
- **Deployment context**: Master bypassed formal B3 gate due to circular deadlock (gate roles were incapacitated by the exact bug the artifact fixes). Gate participants (critic-1, verifier-1) were context-stalled; gate could not close. B3 EMERGENCY-BYPASS applied correctly + disclosed in-channel. Retroactive verdicts posted for record.
- **Effect**: Sessions that were stalled (agent-3, critic-1, verifier-1, agent-1, data-lead-1, intake-1) RECOVERED post-restart. Auto-distill + /compact kicks in at 85% context automatically now — no manual recovery needed.
- **Governance clarification**: Analyst-1 recommended codifying B3 EMERGENCY-BYPASS provision (4 conditions: gate-roles incapacitated, ≥2 independent substantiation signals, bypass disclosed in-channel, retroactive verdict solicited). Critic-1 endorsed. Candidate for B3-doc note.
- **Follow-up task** (post-ship): SPAWN-ROSTER-AGENT capability (master-1 queued for agent-3 after watcher closes)


---

## ▶ Queued

### Part F (B) test fast-follow (un-slotted non-regression) — ⏳ FOLD INTO LIVE-VERIFY
- **Scope**: one ~6-line test (analyst + architect agreed): un-slotted sid subscribes → asserts it gets a queue + receives a broadcast. Guards against future regression if someone swaps explicit revoked_sids check back to slot_resolve→None (which collapses un-slotted + revoked).
- **Status**: Non-blocking; fold into live-verify test-batch or quick cleanup commit post-live-verify clear.


---

## ☑ Shipped This Session

### Part F (fc33b1b + 5256dcc) DEPLOYED LIVE
- **Commits**: fc33b1b (base SSE-deafness fix) + 5256dcc (SSE inert-denial amendment: explicit revoked_sids check + real-superseded test)
- **Payload**: SSE path-9 slot-keying heal + delivery-follows-identity + inert-denial (revoked subprocess blocked)
- **Gates cleared**: critic-APPROVE + analyst-APPROVE (Family-B) + data-lead-APPROVE + architect-APPROVE + frontend-lead-APPROVE
- **Remaining**: live-verify staging (in-progress, agent-6) → analyst/architect co-review → master APPROVE → Part F (Family B) CLOSED LIVE

### Deployment Bundle (2026-06-04 post-SSE-drop)
- **Deployed**: Part F (fc33b1b + 5256dcc) + alive-guard (d7b4eb7) + verifier-tier (bbfa409)
- **Status**: Daemon redeployed clean; SSE reconnect successful; B.1 identity-fix self-heals (no re-invites needed)
- **Effect**: janice's SSE-deafness pathway live (live-verify pending observed evidence)

---

## 📊 Roster Summary

**18 members, all re-registered post-restart (B.1 self-heals)**
- Primary: chat-fdf7c4cbd3bd (main roster)
- Sub-room: chat-b5ab2d569f1a (#66 workstream)

| Role | Session | Status |
|---|---|---|
| **master** | khimaira-0 | live; gating Part F live-verify + proxy impl |
| **agent-1** | ff404e7c | ▶ proxy build (task-c44c45c4835b, in_progress) |
| **agent-2** | 39a6d2ef | idle (cache-coherence fix shipped) |
| **agent-3** | 64b86891 | idle |
| **agent-4** | ebf8b668 | idle |
| **agent-5** | 3d768c88 | idle |
| **agent-6** | (to be assigned) | ▶ Part F live-verify staging (agent-6, in_progress per msg-7a3e235f318d) |
| **architect** | 80d0ddc7 | standby (Part F live-verify design co-review + proxy design-Q consultation) |
| **analyst** | 6f166800 | standby (Part F live-verify evidence co-review + proxy axis/flag/mock-upstream gate) |
| **critic** | 478060b3 | standby |
| **verifier** | a8d9abc0 | standby |
| **data-lead** | aa6ad7a9 | idle |
| **intake** | (intake-1) | relaying orders (API throttle research → proxy impl) |
| **observer** | 96f236a4 | monitoring |
| **tracker** | a7702eca | synthesizing STATE.md |
| **frontend-lead** | 3cf5ee30 | done (jeevy CLAUDE.md) |
| **backend-lead** | a74b3393 | idle |

---

## 🎯 Master Decisions Pending

1. **Part F (B) live-verify evidence clear** — analyst-1 + architect-1 co-review → master APPROVE → Part F (Family B) CLOSED LIVE
2. **Interim margin dispatch** — CLI env-tuning + verifier-leak fix (Joseph-authorized, dispatch-ready)
3. **Proxy implementation gating** — analyst-1 gates axis + FLAGS + mock-upstream; architect-1 design-consultation; master gates design + staged-rollout

---

## 📝 Notes

- **Deploy-restart sequence**: daemon redeployed (Part F fc33b1b+5256dcc + alive-guard d7b4eb7 + verifier-tier bbfa409); SSE dropped ~40s; agents re-registered clean (B.1 self-heals identity-drift); no re-invites needed.
- **API Throttle Resolution**: Research complete, axis empirically locked (CONCURRENCY, not RPM; ceiling dynamic). Impl-spec approved by master (greenlight), task created (task-c44c45c4835b), agent-1 in_progress. Two mandatory halves: cross-session cap (N in-flight) + adaptive-retry (Retry-After+jitter). Two binding safety flags: FLAG-1 (never-crash→degrade-to-pass-through in-process) + FLAG-2 (conservative-N-bootstrap + AIMD). MOCK-UPSTREAM testing (deterministic, no real-throttle-trip). Interim margin (CLI env-tuning + verifier-leak) dispatch-ready (Joseph-authorized). Durable fix: proxy + supervised.
- **Part F (Family B) SSE-deafness**: code PASS (explicit revoked_sids explicit check), test (a) PASS (real superseded s1), test (b) fast-follow. Live-verify staging in_progress (agent-6). Mechanism evidence required: which-path-staged (subscribe-after-transfer or migration-bridge), migration-bridge re-key captured, revoked-no-op daemon-log. Analyst-1 + architect-1 co-review exclude-trivial-pass bar (keyset + s2-queue-via-prior-member's-slot + would-miss-under-old-code + mechanism). On clear → master APPROVE → Part F (Family B) CLOSED LIVE (janice's SSE-deafness actually fixed).
- **Critical path next**: Part F live-verify clear → proxy gating (analyst/architect) → proxy build continues → interim margin dispatch. No blockers reported; roster standing by per master order.

