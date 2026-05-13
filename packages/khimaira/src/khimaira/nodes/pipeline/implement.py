"""Implement node — writes IMPLEMENTATION.md and TODO.md for Cursor to execute.

Instead of writing code directly, this node produces detailed plan files
in tasks/<task-slug>/ that Cursor implements step-by-step using the
implement-task rule.
"""

import re

from khimaira.prompts.builder import build_prompt
from khimaira.dispatch.runners import run_claude
from khimaira.log import get_logger
from khimaira.core.state import OrchestratorState

log = get_logger("node.implement")

IMPLEMENT_SYSTEM_PROMPT = """\
You are a senior software engineer. Based on the architecture plan, create
a task folder and write two files inside it.

## Folder structure

Create the folder `tasks/{task_slug}/` in the project root, where `{task_slug}`
is a short kebab-case name derived from the task (e.g., "health-check-endpoint",
"error-recovery", "rate-limiting"). Then write these two files inside it:

## tasks/{task_slug}/IMPLEMENTATION.md

A detailed study guide with these sections:
1. **Context / Background** — What problem this solves and why.
2. **Current State** — What exists today, with relevant code snippets and file paths.
3. **Target Behavior** — Concrete expected outcome after the work.
4. **Technical Walkthrough** — Step-by-step changes. For each step: what file/function,
   what changes, why. Include before/after code snippets.
5. **File Map** — Table of every file changing with a one-line summary.
6. **Risks / Gotchas** — What could go wrong, breaking changes, edge cases.
7. **Verification** — Concrete test cases: input → expected output.

## tasks/{task_slug}/TODO.md

A checkbox list mirroring every actionable step in IMPLEMENTATION.md:
```
# TODO: <task title>

## Implementation
- [ ] Step 1 description
- [ ] Step 2 description

## Verification
- [ ] Test case 1
- [ ] Test case 2
```

Read the codebase to understand existing patterns. Be specific with file paths
and function names. The developer implementing this should be able to follow
the plan without asking questions.
"""


def _task_slug(task: str) -> str:
    """Convert a task description to a kebab-case slug."""
    # Take first 5 words, lowercase, replace non-alpha with hyphens
    words = re.sub(r'[^a-z0-9\s]', '', task.lower()).split()[:5]
    return "-".join(words) or "task"


def build_implement_node():
    """Build an implement node that writes IMPLEMENTATION.md + TODO.md.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def implement_node(state: OrchestratorState) -> dict:
        """Write tasks/<slug>/IMPLEMENTATION.md and TODO.md based on the architecture plan."""
        task = state.get("task", "")
        context = state.get("context", "")
        architecture_plan = state.get("architecture_plan", "")
        instructions = state.get("supervisor_instructions", "")
        node_calls = dict(state.get("node_calls", {}))
        history = list(state.get("history", []))

        # Track call count
        node_calls["implement"] = node_calls.get("implement", 0) + 1

        slug = _task_slug(task)
        log.info("starting (attempt=%d, task_slug=%s)", node_calls["implement"], slug)

        prompt = build_prompt(
            IMPLEMENT_SYSTEM_PROMPT.replace("{task_slug}", slug),
            f"## Task\n\n{task}",
            f"## Context\n\n{context}" if context else "",
            f"## Architecture Plan\n\n{architecture_plan}" if architecture_plan else "",
            f"## Additional instructions\n\n{instructions}" if instructions else "",
        )

        try:
            result = await run_claude(prompt, timeout=600, permission_mode="acceptEdits")
        except Exception as e:
            log.error("implement failed: %s", e)
            return {
                "node_failure": {
                    "node": "implement",
                    "error": str(e),
                    "attempt": node_calls["implement"],
                },
                "node_calls": node_calls,
                "history": history + [f"implement: failed (attempt {node_calls['implement']}): {e}"],
            }

        if result.startswith("Error:"):
            log.error("implement CLI error: %s", result)
            return {
                "node_failure": {
                    "node": "implement",
                    "error": result,
                    "attempt": node_calls["implement"],
                },
                "node_calls": node_calls,
                "history": history + [f"implement: failed (attempt {node_calls['implement']}): CLI error"],
            }

        return {
            "implementation_result": f"Plan written to tasks/{slug}/. Review IMPLEMENTATION.md and TODO.md, then say 'implement this' in Cursor.",
            "node_failure": {},
            "output_versions": [
                {"node": "implement", "attempt": node_calls["implement"], "content": result}
            ],
            "node_calls": node_calls,
            "history": history + [f"implement: wrote tasks/{slug}/IMPLEMENTATION.md + TODO.md (attempt {node_calls['implement']})"],
        }

    return implement_node
