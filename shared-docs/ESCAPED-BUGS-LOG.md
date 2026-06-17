# ESCAPED BUGS LOG — khimaira live-test escape corpus

> Seed corpus for khimaira-debug (khimaira's own escapes). Each entry maps **(code-shape + mock-assumption) → escaped-bug-class → catching-test-pattern**. The training signal is the *seam* the green test was blind to. Append future escapes in the § "Schema for future entries" shape.
> Companion to the jeevy corpus (`/home/_3ntropy/work/jeevy_portal/shared-docs/ESCAPED-BUGS-LOG.md`). Same framework; this file holds khimaira-platform escapes (roster orchestration, Themis governance, monitor daemon, chat store).

## 1. Meta-class

**Green unit suite (or static config), broken live behavior — because the bug lives in a SEAM the unit tests mock past, or a PREMISE the static check never verifies at runtime.** Every escape below passed N/N green (or "rule exists in config") while the live path was broken. The tests aren't wrong; they're blind to the seam *by construction* — they mock the exact boundary where the defect lives (the real event a producer emits, the real payload, the real enforcement hook, the real runtime input).

**The meta-class is RECURSIVE and extends to GOVERNANCE, not just data flow.** A structural rule (Themis hint, lint gate) can itself escape if its *enforcement mechanism* is absent in some environment — "the rule exists in config" (static green) ≠ "the hook actually intercepts the tool call" (live-enforced). Standing invariant the corpus enforces: **every enforcement mechanism (test OR governance hook) must be verified to EXECUTE / INTERCEPT for real — presence-in-config is not enforcement.**

## 2. Escaped bugs

### themis-hook-dormant-standalone [enforcement-premise · environment-config]
- **Symptom (live):** a standalone-spawned agent session ("void", spawned by Joseph outside the roster bootstrap) ran `git stash` and `git checkout` on its own edits — state-changing git that the Themis rule IN-UNIVERSAL-1 ("agents never run state-changing git", shipped 2026-06-17) should STRUCTURALLY BLOCK. No block fired; the commands executed.
- **Root-cause (audit-grade):** IN-UNIVERSAL-1 is enforced by a Themis **PreToolUse hook** wired into the tool-call path only for sessions bootstrapped through the roster launch path. A session spawned standalone has no PreToolUse hook on its Bash tool-calls, so Themis never sees the `git` invocation — the rule is present in Themis config (static green) but **dormant at runtime** (no interceptor on this session's path). [evidence: audit-grade — the git commands were observed executing successfully in the standalone session that the rule names as forbidden.]
- **Why the test missed it:** the rule's guards (`test_role_convention_lint` + Themis rule-condition unit tests) assert the RULE EXISTS and its condition matches a state-changing-git tool-call — they exercise the rule object, assuming the hook is on the path. No test asserts the PreToolUse hook is actually WIRED + INTERCEPTING for a given session *class*. Static rule-existence ≠ live enforcement; the hook-present premise is unverified.
- **Catching-test:** an **assert-it-enforces** gate — a live probe per session class (roster-bootstrapped AND standalone) that issues a benign state-changing-git tool call and asserts Themis BLOCKS it; plus a startup/CI check that the Themis PreToolUse hook is registered on the tool-call path (premise-present), not merely that the rule is in config. **FORWARD.**

### formatter-premise-vs-file-reality [tooling-premise]
- **Symptom (live):** running `black <file>` on `roster_recovery.py` (the repo's "format every file you modify" rule) churned ~313 lines across the whole file and buried the real 98-line diff — even though the edit was 4 localized hunks. Same on `guard5.py` + `auto_dispatch.py`.
- **Root-cause (audit-grade):** these files carry **intentional manual formatting** (hand-split multi-arg calls, hand-wrapped strings) that NO black line-width preserves — `black -l 88 / -l 100 / -l 120 --check` each reports "would reformat" against the committed file. So the file's committed baseline is not any-black-clean; a blind whole-file format reflows the entire file, not just the new lines. The repo has no enforced formatter config (no `[tool.black]`, no `.pre-commit-config.yaml`) for these files. [evidence: audit-grade — verified `black -l {88,100,120} --check` all reformat the committed `git show HEAD:` baseline.]
- **Why the test missed it:** there is no test — "format every file" is a workflow rule, and it ASSUMES a formatter-clean baseline. The premise (the file is black-clean at the project width) is never checked; on a manually-formatted file the rule is actively harmful (churns the diff, can break intentional layout).
- **Catching-test:** **per-file formatter-width probe** before formatting — `black -l W --check <committed-file>`; if it reformats at every standard W, the file is manually formatted → hand-match the edit's style, do NOT run a whole-file formatter. Durable fix: a `.gitattributes` / pyproject exclude (or a `# fmt: off`-style marker) so CI never reformats these files, making the premise explicit. **FORWARD.**

### owed-verdict-obligation-dormant-gate-task [detector-premise]
- **Symptom (live):** muther's roster pipeline stalled silently — a work-task `done` with critic=`approve` but verifier owing `ship`; the verifier seat went idle and was NEVER woken to file its verdict → the task sat `done`-not-`approved` indefinitely. Joseph had to repeatedly tell the roster "X is idle" to get a nudge. (Motivated the entire idle-but-owing watchdog, 2026-06-17.)
- **Root-cause (audit-grade):** `_get_session_obligations` (api/chats.py) detected a reviewer owing a verdict ONLY via GATE-TASKS (a `gate_for` review-task, created only when `gate_required=True`). But **0 of 475 real tasks set `gate_required`** — `_auto_create_review_tasks` never fires, so no gate-task ever exists and that obligation branch is DORMANT. Real rosters gate via DIRECT `task_verdict` records on the work-task (`_committable_task_ids`: committable iff done+approve+ship). So an owing reviewer's obligation was never produced → `roster_recovery._process_window` never saw it → never kitty-woke the reviewer. [evidence: audit-grade — scanned ~/.local/state/khimaira/chats: 0/475 `gate_required`; 5 real `done` tasks with partial verdicts = the exact stall shape.]
- **Why the test missed it:** the obligation unit tests CONSTRUCTED gate-tasks (`gate_required=True`) — the exact mechanism real rosters never use. Green against an assumed input whose PREMISE (gate-tasks exist in prod) is dead. Same class as a detector keying on a field/event that never renders live (the plausible 1-line `chats.py:759` `done`-filter patch was a red herring — it'd change nothing because the branch it lives in never matches).
- **Catching-test:** build a room with a `done` work-task + critic `approve` + verifier `ship` MISSING (NO gate-task), assert `_get_session_obligations(verifier_session)` returns the owed-verdict obligation (returned `[]` before the fix). Premise-correct: uses the DIRECT-verdict path real rosters use, not the dormant gate-task wrapper. **ADDED 476f2b5** (test_direct_verdict_obligation.py, 7 tests; verified fires on the 5 real owing cases, 0 overlap with the 10 committable).

## 3. Forward test-strategy (layered by seam-class)
- **L1 — real-producer→consumer e2e** per surface (the real event/payload a producer emits, not an injected dict): catches entry-path, producer-mechanism, producer→payload seams.
- **L2 — real-store integration** (real chat JSONL / real DB, not a mocked store): catches logic seams the mock never runs.
- **L3 — contract checks** (every mocked field/column/symbol vs the real schema/runtime shape): catches mock-vs-schema + detector-premise-vs-runtime seams.
- **L4 — live-runtime exercise** (Specter-in-CI for UI; real-daemon for orchestration): catches environment-config + frontend seams.
- **L0 (meta) — assert-it-runs / assert-it-enforces gate**: the enforcement mechanism must EXECUTE (a test) or INTERCEPT (a governance hook) for real on the relevant path — assert `executed>0` / `blocked==true`, not just `failures==0` / `rule-in-config`. Catches the recursive escape where a catching-test silently no-ops (wrong driver, unreachable store) OR a structural rule is dormant (hook absent in some session class).

## 4. Schema for future entries
Append one block per escape, same shape — this is the corpus row a learner trains on:
```
### <bug-slug> [<seam-class>]
- **Symptom (live):** what was observed in real use (not the stack trace — the behavior)
- **Root-cause (audit-grade):** the specific mechanism — name the function / column / value / event / hook
- **Why the test missed it:** the SPECIFIC mock/static false-assumption (what it stubbed or assumed that diverged from runtime reality)
- **Catching-test:** the real test that exercises the seam + [ADDED <task-id> | FORWARD]
```
**Seam-class vocabulary** (extend as new classes appear): `entry-path` · `producer→event-payload` · `producer-mechanism` · `mock-vs-schema` · `SQL-logic` · `environment-config` · `contract` · `frontend-render` · `enforcement-premise` (a governance/detector rule whose enforcement mechanism — hook, interceptor — is absent in some runtime/environment, so the rule is present-in-config but dormant in practice) · `detector-premise-vs-runtime` (a detector keys on an input premise — field/string/symbol — that real runtime data never produces).
**Training signal:** `(code-shape + mock/static-assumption) → seam-class → catching-test-pattern`. A learner that sees "the test asserts the rule/detector EXISTS but mocks/assumes the hook-present or input-present premise" should predict the seam-class and propose the L0 assert-it-runs / assert-it-enforces gate.
