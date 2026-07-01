# Directed-wake path — audit (Phase A) + Phase B plan

> Author: khimaira-void-1 · 2026-06-28 · Joseph-greenlit audit, prereq for the #4
> directed-private chat-cost fix. Phase A = live observation (daemon NOT touched,
> livyatan NOT touched). Phase B (the clean directed-DM end-to-end) is HELD until
> Joseph's ~7pm primary reset, then run against ONE throwaway session.

## Acceptance test (the one question)

"Does the daemon wake an idle, directed-owed agent end-to-end — ZERO human nudges,
NO flood?" YES → directed-private is safe to build. NO → daemon-side bug to fix first.

## Phase A verdict: QUALIFIED — mechanism SAFE, but a LIVE directed-efficacy FAILURE found

The wake mechanism is fundamentally healthy — detection, actuation, and the
kitty-RC-under-systemd suspect are all SAFE, no flood, zero human nudges. BUT the
directed end-state is NOT clean: window 44 is a **live, audit-grade directed-DM
efficacy failure** (corrected finding — see §window-44). So the acceptance test is
currently a **NO on the end-state link**, not merely "unproven." The directed wake
FIRES reliably; it does not TERMINATE.

## 4-link enumeration (evidence quality tagged)

### 1. Detection — `_session_has_directed_unanswered` (roster_recovery.py:1237) — ✅ SAFE [audit-grade]
- Logic: directed = `to=[me]` OR `@<my_name>` in body, newer than the session's own
  last post in that chat; excludes self + SYSTEM. Correctly ignores undirected chatter.
- Unit tests pass both directions (`pytest -k directed` → 6 passed).
- Live: WAKE-SKIP-DIAG lines show `directed_chat:False` for idle livyatan seats holding
  1–23 UNDIRECTED inbound msgs — correct non-fire (the over-wake fix works).

### 2. Actuation — `KHIMAIRA_ROSTER_WAKE_INJECT` (roster_recovery.py:173, default on) — ✅ SAFE [audit-grade]
- `=1` confirmed in the RUNNING daemon process env (`/proc/<MainPID>/environ`), not just
  settings.json.
- "wake injected to window …" fires repeatedly; ZERO "WOULD-WAKE … injection disabled".

### 3. kitty-RC-under-systemd (the prime suspect) — ✅ SAFE [audit-grade]
- Daemon IS under systemd (`khimaira-monitor.service`, active) with NO inherited
  `KITTY_LISTEN_ON` — the exact regression condition.
- The discovery fix (`_resolve_listen_socket`, roster_recovery.py:214) works: the
  post-#39-restart daemon logged "discovered kitty control socket unix:/tmp/kitty-6285"
  at startup; direct probe `kitty @ --to=unix:/tmp/kitty-6285 ls` → rc=0, lists all 17
  windows incl. the livyatan roster; ZERO `kitty @ … failed` / `/dev/tty` errors in 6h.
- Conclusion: the suspect is HEALTHY right now (socket present + discovered + driving injects).

### 4. End-state — ⚠️ SPLIT
- **inject→submit→turn: ✅ SAFE [audit-grade].** Read window 44's buffer (read-only
  `kitty @ get-text`): the injected "⏰ resume: call chat_my_chats…" was SUBMITTED, the
  critic took a turn and called chat_my_chats.
- **directed-DM efficacy (woken agent SEES + ACTS on the directed msg): ❌ BROKEN [audit-grade, live].**
  CORRECTED: window 44 IS directed-triggered (read-only signal probe: `directed_unanswered=True`,
  obligations=0, unread_inbox=False, pending_task/invite=False). So the directed wake fires +
  the agent wakes + takes a turn — but it reports "no new activity" and never POSTS a reply,
  so `_session_has_directed_unanswered` (keyed on the session's own-post-since) stays True →
  re-woken every cooldown forever. The directed wake FIRES but does not TERMINATE. Phase B
  must isolate WHY the woken agent doesn't act (stale/already-seen DM? not surfaced by
  chat_my_chats? wind-down interaction?).

## No-flood / zero-human (the other half of the acceptance test)
- NO FLOOD ✅ — 6 wake injects in 90 min, all to one stuck window, cooldown-spaced; ZERO
  `← khimaira-chat` backlog-flood signature in 6h. Storm guards (cooldown/debounce/stagger)
  working.
- ZERO HUMAN NUDGES ✅ — the daemon auto-wakes unaided.

## Window-44 re-wake loop (separate finding — see report below)
Window 44 (livyatan-critic-1, 28fd211f) is re-woken every cooldown: wakes → chat_my_chats
→ "Standing by idle — no new activity" → re-idles → re-woken. A real bug AND a live backup
drain.

**Root cause (read-only signal probe, audit-grade) — NOT #39, NOT #32:**
`_get_session_obligations(28fd211f)` = 0 (so NOT the #39 owed-verdict path), unread_inbox =
False, pending_task/invite = False — but `_session_has_directed_unanswered` = **True**. So the
trigger is the DIRECTED signal: a directed msg (to=[critic]/@critic) is newer than the
critic's own last post. The critic wakes, takes a turn, but does NOT post a reply in that
chat → the signal (keyed on own-post-since) never clears → infinite cooldown re-wake.

This is the **directed-wake self-termination gap**: detection treats "the session posted in
this chat since" as proof-of-handling. An agent that wakes and HANDLES a directed ask without
posting a chat reply (or that judges a stale/already-seen DM as "nothing to do") loops
forever. It is the SAME failure class the directed-private build would hit at scale, so it's
the load-bearing thing Phase B must characterize.

**The actual DM (full body, read 2026-06-28):** chat-ddbb5b4bd5e8, from livyatan master,
to=[verifier 16d0b9d0, critic 28fd211f], 19s after critic-1's last own post. It asks critic-1
to `post chat_task_verdict(verdict="approve")` on task-32d4e88980b1 + task-77f87bc5a851
("formality — verify-task findings, done + acted on"). So this is a **REAL owed verdict (case
B), NOT a phantom** — but `_get_session_obligations(critic-1)=0` does NOT register it. So a
verify-task terminal verdict requested via a directed chat msg is invisible to the obligation
system; ONLY the directed @mention wakes the seat, and the woken critic (seeing no NEW chat
activity it recognizes as actionable) never posts → loops. Two compounding gaps:
  1. **obligation-detection gap** (#39/#32-adjacent): chat-requested terminal verdicts on
     verify-tasks aren't surfaced by `_get_session_obligations` (0 obligations despite a real
     owed verdict).
  2. **directed-wake self-termination gap**: the directed signal keys on own-post-since, so
     even after the wake fires the loop can't end until the agent POSTS — which it won't if it
     doesn't recognize the ask.
  3. **MSG-vs-action gap (sharpening of #2)**: `_session_has_directed_unanswered` counts only
     `kind==MSG` as the session's "own post". `chat_task_verdict` writes a `TASK_VERDICT`, not
     a MSG — so even if critic-1 DOES the owed work (files both verdicts), the directed signal
     stays True and the loop continues. Self-termination requires an actual chat MSG reply, not
     just the substantive action. The eventual fix should likely treat ANY own-authored entry
     (MSG, verdict, task-update) since the directed msg as "answered", OR clear on the
     specifically-requested action.
Surgical fix for the live drain: critic-1 must (1) post the 2 approve verdicts AND (2) post a
one-line chat MSG reply in chat-ddbb5b4bd5e8 — verdicts resolve the ask, the MSG clears the
loop.

EXECUTION STATUS (2026-06-28 ~14:37): master GO'd option A (resolve). My scripted kitty
keystroke-inject into window 44 was DENIED by Claude Code's auto-mode permission classifier
(agents can't send-text/send-key into another window without a Bash permission allow). Handed
to Joseph/livyatan-master to drive critic-1's post. Drain still live pending that. Will verify
`_session_has_directed_unanswered(critic-1)=False` once it posts.

## Phase B plan (run post-reset, against ONE throwaway)
1. Spin ONE throwaway idle session (master coordinates). Never touch livyatan.
2. Post a directed DM to it (`to=[throwaway_id]` and/or `@<name>`), leave it idle.
3. Observe, audit-grade:
   a. daemon computes `directed_chat:True` for it (journal / wake-skip-diag absence),
   b. "wake injected to window <throwaway>" fires (no human nudge),
   c. the throwaway TAKES A TURN and **actually acts on the DM** — NOT "no new activity".
4. Watch for the flood failure mode (a `← khimaira-chat:` backlog dump on inject = BROKEN).
5. YES on (a)(b)(c) + no flood → acceptance test PASS → directed-private safe to build.
   Any miss → that link is the daemon-side bug to fix before the build.
