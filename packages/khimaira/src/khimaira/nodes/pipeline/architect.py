"""Architect node — shells out to Claude Code CLI for design/planning.

Claude Code reads the codebase, designs a plan, and self-corrects by
verifying file paths and function names against the actual code.
"""

from khimaira.prompts.builder import build_prompt
from khimaira.dispatch.runners import run_claude
from khimaira.log import get_logger
from khimaira.core.state import OrchestratorState

# Graph nodes return the plan as text in state — no file writing.
# The standalone architect MCP tool uses ARCHITECT_SYSTEM_PROMPT which writes files.
GRAPH_ARCHITECT_PROMPT = """\
You are a senior software architect. Design a detailed implementation plan.

## How you work

1. Read the relevant codebase files to understand existing patterns.
2. Design the approach — what changes, where, why, in what order.
3. Identify risks, edge cases, and dependencies.
4. Before finalizing, verify that every file path and function name you
   reference actually exists in the codebase. Fix any hallucinated paths.

## Output format

Return a structured plan with:
- **Context** — what problem this solves and why it matters.
- **Approach** — high-level strategy (1-3 sentences).
- **Steps** — numbered, each with: file/function, what changes, why.
  Include before/after code snippets where helpful.
- **Risks** — what could go wrong, how to mitigate.
- **Verification** — how to confirm it works.

Be specific. Use actual file paths and function names. Don't hand-wave.
Do NOT write any files — just return the plan as text.
"""

log = get_logger("node.architect")


def build_architect_node():
    """Build an architect node that uses Claude Code CLI.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def architect_node(state: OrchestratorState) -> dict:
        """Run Claude Code architecture and return the plan."""
        import time as _time
        _t0 = _time.monotonic()
        task = state.get("task", "")
        context = state.get("context", "")
        research_findings = state.get("research_findings", "")
        instructions = state.get("supervisor_instructions", "")
        feedback = state.get("validation_feedback", "")
        node_calls = dict(state.get("node_calls", {}))
        history = list(state.get("history", []))

        # Track call count
        node_calls["architect"] = node_calls.get("architect", 0) + 1

        log.info("starting (attempt=%d, prompt_build_time=%.1fs)", node_calls["architect"], _time.monotonic() - _t0)

        prompt = build_prompt(
            GRAPH_ARCHITECT_PROMPT,
            task,
            f"## Context\n\n{context}" if context else "",
            f"## Research Findings\n\n{research_findings}" if research_findings else "",
            f"## Supervisor instructions\n\n{instructions}" if instructions else "",
            f"## Previous feedback to address\n\n{feedback}" if feedback else "",
        )

        log.info("prompt built (%d chars), calling claude...", len(prompt))
        try:
            plan = await run_claude(prompt, timeout=600)
            log.info("claude returned (%d chars, %.1fs total)", len(plan), _time.monotonic() - _t0)
        except Exception as e:
            log.error("architect failed (%.1fs): %s", _time.monotonic() - _t0, e)
            return {
                "node_failure": {
                    "node": "architect",
                    "error": str(e),
                    "attempt": node_calls["architect"],
                },
                "node_calls": node_calls,
                "history": history + [f"architect: failed (attempt {node_calls['architect']}): {e}"],
            }

        if plan.startswith("Error:"):
            log.error("architect CLI error: %s", plan)
            return {
                "node_failure": {
                    "node": "architect",
                    "error": plan,
                    "attempt": node_calls["architect"],
                },
                "node_calls": node_calls,
                "history": history + [f"architect: failed (attempt {node_calls['architect']}): CLI error"],
            }

        return {
            "architecture_plan": plan,
            "node_failure": {},
            "output_versions": [
                {"node": "architect", "attempt": node_calls["architect"], "content": plan}
            ],
            "node_calls": node_calls,
            "history": history + [f"architect: completed (attempt {node_calls['architect']})"],
        }

    return architect_node
