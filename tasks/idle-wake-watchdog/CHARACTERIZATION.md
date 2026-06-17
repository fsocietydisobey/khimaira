# Idle-but-owing wake watchdog: RESOLVED (3 paths) 2026-06-17

> Status: **RESOLVED + DEPLOYED-PENDING-BOUNCE.** All three paths committed to main
> (Path 1 `476f2b5`, Path 2 `18092e2`, Path 3 `f71787c`); live once the monitor daemon
> is bounced. Closes the muther 2026-06-17 roster-orchestration gap: a turn-gated
> session (incl. the master) is not woken to ACT when an unconsumed event/obligation is
> addressed to it, so the pipeline stalls silently until a human notices.

## The bug class — turn-gated unconsumed-event

Every Claude Code session is **turn-gated**: it acts only when a human prompt or a
daemon window-inject gives it a turn. A chat message / inbox notice / owed verdict /
task completion delivered over SSE lands in the session's context but does **not**
trigger a turn — it sits unconsumed until something actuates the session.

Waking was a **patchwork of special cases** (roster_recovery wakes a worker on
master-assign; auto_dispatch wakes the master on its own idle-backlog). The general
class — *"an idle session owes/was-pinged but nothing wakes it"* — had four open holes,
each closed by one path below.

### Three layers (don't conflate them)

| Layer | Mechanism | Status |
|---|---|---|
| **Delivery** (move data) | SSE (`chat_my_chats` subscriber) | already correct — untouched |
| **Actuation** (give the agent a turn) | kitty `send-text` window inject (`roster_recovery._inject_text_and_submit`) | the ONLY thing that wakes a turn-gated CLI agent; SSE/WS cannot |
| **Detection** (decide who needs waking) | periodic sweep + event bookkeeping | a TIMER is irreducible — a stall is *silence*, and silence only surfaces on a clock |

All three paths extend **detection**, reuse the existing **actuation**, and leave
**delivery** untouched. None touch the #18-freeze-prone `auto_dispatch_loop` (Path 3's
only auto_dispatch edit is an additive param on a helper — the loop + its sleep are
byte-identical).

## The three paths + their premise-reversals

The leverage this dig produced: **three plausible fixes, each falsified by checking the
mechanism against real runtime data before building** (the same discipline that cracked
#18). The reversals are the durable lesson.

### Path 1 — owed-verdict obligation (`476f2b5`)
- **Hole:** an idle reviewer owing a verdict on a `done` task is never woken → pipeline
  stalls (the muther incident: task `done`, critic=approve, verifier owes ship).
- **PLAUSIBLE FIX (FALSIFIED):** `_get_session_obligations` filters tasks to
  pending/in_progress at `api/chats.py:759`, dropping `done` — "just include `done`."
- **REVERSAL (audit, real state):** `gate_required=True` on **0 of 475** real tasks, so
  the role-class gate-task branch the filter feeds is **dormant** — including `done`
  changes nothing. Real rosters record verdicts **directly** on the work-task
  (`_committable_task_ids`: `done AND approve AND ship`), no gate-task wrapper.
- **REAL FIX:** a direct-verdict obligation mirroring `_committable_task_ids` — a
  reviewer-role MEMBER owes a verdict iff a `done` work-task already carries ≥1 verdict
  (proof it's under review) and that member's slot is empty. Scoped per-chat via
  `member_roles`. Consumed by `roster_recovery._process_window`'s existing wake gate.

### Path 2 — idle-session unread-inbox + unconsumed-peer-chat wake (`18092e2`)
- **Hole:** an idle session (incl. master) with an unread notice or a peer chat-reply,
  but no task-obligation, is never woken ("Joseph had to tell master void replied").
- **PLAUSIBLE FIX (FALSIFIED):** wake on chat-**cursor** lag (messages after the
  session's cursor).
- **REVERSAL:** the cursor advances on SSE **delivery** (`api/chats.py:1732`), and an
  idle session keeps its SSE subscriber alive → cursor-lag ≈ 0 *exactly* when we must
  wake (peer replied → delivered → master still turn-gated). The cursor is
  **safe-but-useless**.
- **REAL FIX (two OR-gate signals behind the existing idle>5min + per-window cooldown):**
  - **A — `_session_has_unread_inbox`**: `pending_notes(mark_read=False)` peek; healthy
    sessions drain their inbox each turn, so it's non-zero only on an SSE-deaf idle one.
  - **B — `_session_has_unconsumed_chat`**: an inbound (non-self, non-SYSTEM) message
    with **ts > last_active** (the last observable action). Chat receipt writes the CHAT
    dir, never the session dir, so `last_active` isn't polluted by delivery — robust
    whether or not the cursor advances while idle. Clock-skew guard
    (`_TS_SKEW_EPSILON_S=2.0`) because ts (daemon ISO) and last_active (fs mtime) are two
    clocks.

### Path 3 — Guard-5 stall → master-window wake (`f71787c`)
- **Hole:** Guard-5 detects a stalled gate (stale >8min + ≥2 idle) but only POSTS A
  NOTICE — which doesn't wake a turn-gated master; the notice sits unread.
- **DESIGN (master-only, deliberate):** Guard-5's target priority is same-role-peer →
  master → coordinator. A same-role-peer target OWES the verdict → already woken by
  **Path 1** via `_process_window`. So Path 3 closes only the **master** hole (the stall
  isn't the master's own obligation, so Path 1 can't see it).
- **FIX:** `_guard5_escalate`, when `member_roles[target]==master`, calls
  `auto_dispatch._maybe_wake_idle_master(..., wake_text=<stall-specific>)` — **reusing**
  that actuator (roster_recovery discovery + inject + per-master 300s cooldown + 180s
  idle-min + busy + unreachable-escalate), NOT a parallel one. A new optional `wake_text`
  param carries the stall message; absent it, behavior is unchanged.

## Test contract (the acceptance gate for each path)

Storm-safety is proven, not assumed: **fire when it should AND stay silent on a quiet
roster** — a watchdog that storms (or silently no-ops) is worse than none.

- **Path 1** (`test_direct_verdict_obligation.py`, 7): fires for owed critic + owed
  verifier (+ "changes"=acted); silent on committable / ungated-no-verdict / non-member
  / pending. Real-data: fires on the 5 genuine owing cases, 0 overlap with 10 committable.
- **Path 2** (`test_idle_wake_signals.py`, 9): both signals fire; silent on
  drained-inbox / old / self / system / non-member / sub-epsilon-skew. **Zero-wakes
  proof**: scanned all 29 real sessions → 0 trips either signal.
- **Path 3** (`test_guard5_master_wake_bridge.py`, 4): `wake_text` override injected;
  cooldown bounds to one wake; active master (<180s idle) not woken; bridge routes only
  when target is master.
- Regression: 269 across guard5 + bridge + direct_verdict + idle_wake + roster_recovery
  + chats_api. (Pre-existing `test_auto_dispatch.py` full-suite flakes are module-state
  pollution, unaffected — file passes 26/26 in isolation.)

## Seam captures (tooling-premise findings, folded in per request)

Two escapes captured to `shared-docs/ESCAPED-BUGS-LOG.md` + mnemosyne
`escaped-bugs:khimaira` as we went. Both are the **static-green ≠ live-true** class.

### `themis-hook-dormant-standalone` [enforcement-premise]
- The Themis rule IN-UNIVERSAL-1 ("agents never run state-changing git") is enforced by a
  PreToolUse hook wired only for roster-bootstrapped sessions. A standalone-spawned
  helper (this "void" session) has no such hook → it ran `git stash`/`checkout` freely;
  the rule is present-in-config but **dormant at runtime**.
- **FORWARD catching-test:** an *assert-it-ENFORCES* probe per session class — issue a
  benign state-changing-git tool call and assert Themis BLOCKS it; CI/startup check that
  the PreToolUse hook is registered on the tool-call path (not just that the rule exists).

### `formatter-premise-vs-file-reality` [tooling-premise]
- "Format every file you modify" assumes a formatter-clean baseline. `roster_recovery.py`,
  `guard5.py`, and `auto_dispatch.py` carry **intentional manual formatting** that NO
  black width (88/100/120) preserves — blind `black <file>` churns ~300 lines and buries
  the real diff. Caught mid-build (a 313-line churn on roster_recovery); recovered via
  read-only `git show HEAD:` + `cp` + hand-reapply (Read-not-git kept).
- **FORWARD catching-test:** a per-file formatter-width check before formatting (does
  `black -l W --check` leave the committed file unchanged at some W?), OR a
  `.gitattributes`/pyproject exclude so CI never reformats these manually-formatted files.

## Deployment + env knobs

In main, **not live until the daemon is bounced** (it runs old code in memory). muther's
roster is mid-KG-arc; bounce timing is being confirmed with Joseph to avoid flushing the
in-memory heartbeat buffer mid-critical-work.

| Knob | Default | Effect |
|---|---|---|
| `KHIMAIRA_ROSTER_IDLE_MIN_S` | 300 | idle gate before any wake |
| `KHIMAIRA_TS_SKEW_EPSILON_S` | 2.0 | Path-2 clock-skew margin (ts > last_active + ε) |
| `KHIMAIRA_MASTER_WAKE_COOLDOWN_S` | 300 | per-master wake cooldown (Path 3 reuses) |
| `KHIMAIRA_MASTER_WAKE_IDLE_S` | 180 | master idle-min before stall wake |
| (existing) per-`(window,"wake")` 300s `_DEBOUNCE` | — | per-window wake cooldown (Paths 1+2) |

## Files touched

- `monitor/api/chats.py` — Path 1 direct-verdict obligation (`_get_session_obligations`).
- `monitor/roster_recovery.py` — Path 2 `_session_has_unread_inbox` +
  `_session_has_unconsumed_chat` + `_iso_to_epoch` + OR-gate wiring.
- `monitor/guard5.py` — Path 3 `_wake_master_window_on_stall` + escalation bridge call.
- `monitor/auto_dispatch.py` — Path 3 additive `wake_text` param on
  `_maybe_wake_idle_master` (the loop + sleep are byte-identical — no #18 exposure).
- tests: `test_direct_verdict_obligation.py`, `test_idle_wake_signals.py`,
  `test_guard5_master_wake_bridge.py`.
