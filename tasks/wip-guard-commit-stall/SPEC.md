# WIP-guard commit-stall — idle-despite-owed root cause

> Status: SPEC / deferred · 2026-06-20 · filed by khimaira-0 (master)
> Part of the idle-despite-owed audit. Distinct from the wake-actuator fixes (`ce7af6e`)
> and the dormant-obligation premise ([[project_obligation_gate_task_premise_dormant]]).

## Symptom (live, audit-grade)

muther-agent-2 sat IDLE holding **owed in_progress work** (JEEVY-601, real *uncommitted*
changes in the tree) at ~92% context; never reported done. The watchdog did NOT wake it.
Joseph caught it manually (2026-06-20 ~17:25, muther roster).

## Root cause (audit-grade — verified this session)

NOT a gate-classification gap (`_get_session_obligations` explicitly returns status
"pending **or in_progress**", so the in_progress task WAS gated wakeable), and NOT the
wake-actuator (that runs only after the gate fires).

The suppressor is the **`_session_has_recent_wip` "ALIVE-BUT-WORKING" guard**
(roster_recovery.py ~:1995–2011). After the gate passes, the daemon probes the owed-task's
**target-file mtimes**; if any changed within `_WIP_THRESHOLD_S` (900s/15min) it concludes
"editing-but-SSE-deaf → don't interrupt live work" and **skips the wake**. agent-2 had
just-finished **uncommitted** changes on exactly those target files → fresh mtimes →
`has_wip=True` → suppressed.

**The flaw:** the guard's premise "recent disk write = actively working" is false for a
session that just *finished* editing and stalled before commit/done-report. Fresh mtimes
from completed work are indistinguishable from active work → a done-but-uncommitted-idle
agent reads as alive-but-working and is suppressed for up to `_WIP_THRESHOLD_S` (then
mtimes go stale and it should self-wake; a human catches it inside that window).

The stale `session_list` status ("implementing", set at task-start, never cleared) is a
real *human/master* observability trap but did NOT cause the suppression — the daemon gate
keys on `last_active` mtime, not the status string.

## Fix direction (decide at build time)

Disambiguate **active-edit** from **finished-but-stalled** in the WIP guard:
- Require an **independent liveness signal** (live TUI spinner / process output / a
  heartbeat) rather than file-mtime freshness alone; OR
- Treat **"WIP files fresh BUT task ~complete AND no done-signal AND no recent
  non-file activity"** as a **commit-stall wake** (the agent owes a commit+report), not a
  work-in-progress skip; OR
- Shorten the conflation: require BOTH fresh mtimes AND recent session activity from a
  non-file source to classify ALIVE-BUT-WORKING.

## Test (class-level invariant)

A session holding an in_progress task with uncommitted changes on its target files but no
disk/process activity for > a short window, and no done-signal, MUST be woken (commit-stall),
NOT skipped as ALIVE-BUT-WORKING. Assert the wake fires for that state without interrupting
a genuinely-still-editing session (target files actively changing).

## Companion observability fix (Q2)

Don't trust self-reported `session_list` status for liveness — it's set once at task-start.
A status-based monitor (master per-turn sweep OR dashboard) should read `last_active` /
an independent signal, not the status string. Document in master.md per-turn-sweep duty.
