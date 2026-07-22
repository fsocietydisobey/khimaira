---
name: intern
description: Run a task through the internal-roster pipeline (consultant designs → implementer builds → gatekeeper verdicts) via the Workflow tool. Use when the user types /intern <task> or asks to run a task "through the interns".
---

# /intern <task> — dispatch through the internal-roster pipeline

Run the given task through the three-stage internal roster: the
khimaira-internal-consultant produces a design plan, the
khimaira-internal-agent implements it, and the khimaira-internal-gatekeeper
returns an independent SHIP/HOLD verdict. All three run as subagents inside
THIS session — no kitty windows, no cross-session chat.

## Steps

1. If `$ARGUMENTS` is empty, ask the user for the task in one sentence — the
   workflow validates args and refuses an empty task.
2. Announce the dispatch in one line: which task, and that it runs
   consultant → implementer → gatekeeper.
3. Invoke the Workflow tool:

   ```
   Workflow({
     scriptPath: ".claude/workflows/internal-roster.js",
     args: "<the task, verbatim, with any file paths / constraints the user gave>"
   })
   ```

   Use `scriptPath` (not `name:`) — the script is repo-local, not registered.
   The workflow runs in the background; you'll get a task-notification when it
   completes. Keep working or wait as appropriate.
4. When the result arrives, report to the user:
   - the gatekeeper's verdict (SHIP or HOLD) and its reasoning, first;
   - the implementation summary (files touched, tests run);
   - the consultant's plan only if the user asks or the verdict is HOLD
     (it's the context for what went wrong).
5. On HOLD: do NOT auto-retry. Surface the gatekeeper's objections and let the
   user decide (fix inline, re-dispatch, or drop).

## Constraints carried by the pipeline itself

The task string you pass becomes fenced data inside the workflow
(`<<<WORKFLOW-DATA … WORKFLOW-DATA>>>`), so instruction-shaped text in the
task can't escape into workflow control flow. The implementer stage does not
commit; review and commit remain with this session and the user.
