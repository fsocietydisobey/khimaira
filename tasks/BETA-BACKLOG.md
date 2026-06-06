# Khimaira Roster Platform — Beta Backlog

**Created 2026-06-06**, at the roster-platform **alpha close**. These are the items
deliberately deferred *past* alpha because they are P3 polish, hardening of things
that already work, separate tracks, or features. They are tracked here so they're not
lost — but none of them gate the alpha milestone.

> **Why alpha is closeable without these:** alpha = "the core works reliably," not
> "backlog is zero." The roster launch + coordination path is proven reliable
> (3-for-3 clean 14-session launches, 2026-06-06) and the platform robustness register
> (K1–K13) is audit-verified mostly fixed. See `ROSTER_ISSUES_2026-06-05.md` for the
> register and the audit verdict below.

## Audit verdict (K1–K13, vs current code, 2026-06-06)

| K | Status | Notes |
|---|---|---|
| K1 | ✅ FIXED (audit) | `475c002` SSE-survives-compaction; live this session |
| K2 | 🟡 contained | uuid still changes on resume, but auto-retire + re-slot + Guard-6 contain the harms; resume deprioritized in favor of fresh-start |
| K3a | ✅ FIXED (audit) | entanglement fence (`2addf5f`/`433fca6`/`70aace0`) |
| K3b | ⬜ alpha-close | **server-side bootstrap-overlap guard — see `tasks/k3b-bootstrap-overlap-guard/`** |
| K3c | 🟡 beta | name-collision: detection + delivery-by-id done; auto-suffix-at-registration deferred |
| K4 | ✅ FIXED (audit) | Guard-6 heartbeat-liveness; tests pass |
| K5 | ✅ FIXED (audit) | `chat_send` slot-heal canonical sender key; tests pass |
| K6 | ✅ FIXED (audit) | alive-guard + auto-retire + inert-denial |
| K7 | 🟡 beta | inbox notice shows 8-char sender prefix that won't resolve — needs a resolver change (accept a unique prefix) + full-id surfacing. P3. |
| K8 | 🟡 beta | parallel-master: manual-bootstrap + `chat_resume_master` contain it; formal master-lease deferred |
| K9 | 🟡 beta | context-saturation: surface context% in roster_progress + flag/compact a nudged agent >~85%. P3. |
| K10 | ➡️ Specter track | `specter_set_file_input` lying-success — NOT roster-core; tracked on the Specter track |
| K11 | ✅ FIXED (audit) | throttle-detect hook → `/throttle` escalation; tests pass (= task #13) |
| K12 | 🟡 beta | daemon perf: `my_chats` TTL cache shipped (`56b53b6`); further profiling deferred |
| K13 | ✅ FIXED (audit) | `set_creator`/`chat_resume_master` Phase B v2 + master-leave guard; tests pass |

## Beta backlog items (deferred, tracked)

### Roster-platform polish / hardening
- **K7 — notice sender-id resolvability** (P3): surface a resolvable handle on inbox notices; make `resolve_session_id` accept a unique 8-char prefix so a stray notice can be replied to.
- **K9 — context-saturation guard** (P3): surface context% in `roster_progress`; flag/auto-compact a nudged agent above ~85% before it degrades (prevents the K4-looking false-dead + K1 SSE-kill cascade).
- **K3c — name auto-suffix at registration**: `session_set_name` should reject or auto-suffix a friendly name already held by a live session (uniqueness at registration), beyond the current detect-and-surface.
- **K8 — formal master-lease arbitration**: a single "current master" lease per roster; a fresh session reads the latest handoff + checks for a live master before assuming the role. Stale handoffs auto-expire when a newer one lands.
- **K12 — daemon perf beyond the my_chats cache**: profile session_list / task-create latency at 100+ sessions; the 2s TTL cache (`56b53b6`) is a start, not the finish.

### Features (separate from the register)
- **#61 — Themis fail-open under daemon downtime (axis A)**
- **#60 — Mnemosyne lead-as-editor harvest v2**
- **#42 / #52 / #53 / #64** — lead write-gate, distill/lint polish, mnemosyne client wiring, master solo-investigate

### Other tracks
- **K10 — `specter_set_file_input` lying-success**: belongs on the Specter track, not the roster platform.

## Alpha-close scope (NOT in this backlog — do these to close alpha)
1. **K3b** — server-side bootstrap-overlap guard (`tasks/k3b-bootstrap-overlap-guard/IMPLEMENTATION.md`) — the one lived-damage item still structurally open.
2. **#14** — auto-BEGIN dispatch (`tasks/auto-begin-dispatch/IMPLEMENTATION.md`) — root fix for recurring roster-idle.
3. Cleanup: mark #13 (= K11) done; this backlog doc captures the rest.
