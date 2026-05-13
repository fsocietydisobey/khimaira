"""Gemini assist node — uses Gemini CLI for debugging and research assistance.

Called when a domain node fails repeatedly or when the supervisor explicitly
requests Gemini's help analyzing an issue. Gemini has codebase access and
can research patterns, debug errors, and suggest fixes.
"""

from khimaira.prompts.builder import build_prompt
from khimaira.dispatch.runners import run_gemini
from khimaira.log import get_logger
from khimaira.core.state import OrchestratorState

log = get_logger("node.gemini_assist")

GEMINI_ASSIST_PROMPT = """\
You are a senior technical advisor helping debug and resolve issues in a
software project. You have full codebase access via Gemini CLI.

## How you work

1. Read the error details and context provided.
2. Explore the relevant codebase files to understand the current state.
3. Research patterns, best practices, or library documentation as needed.
4. Return structured, actionable findings.

## Output format

Return your analysis with:
- **Root cause** — what's going wrong and why.
- **Relevant code** — key snippets from the codebase with file paths.
- **Suggested fix** — concrete steps to resolve the issue.
- **Alternative approaches** — if the current approach is flawed, suggest better ones.

Be specific. Reference actual file paths and code from the codebase.
"""


def build_gemini_assist_node():
    """Build a Gemini assist node for debugging and research.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def gemini_assist_node(state: OrchestratorState) -> dict:
        """Run Gemini CLI to debug issues or research solutions."""
        task = state.get("task", "")
        context = state.get("context", "")
        instructions = state.get("supervisor_instructions", "")
        node_failure = state.get("node_failure", {})
        architecture_plan = state.get("architecture_plan", "")
        research_findings = state.get("research_findings", "")
        node_calls = dict(state.get("node_calls", {}))
        history = list(state.get("history", []))

        node_calls["gemini_assist"] = node_calls.get("gemini_assist", 0) + 1

        log.info("starting (attempt=%d)", node_calls["gemini_assist"])

        # Build context from failure info and current state
        failure_context = ""
        if node_failure:
            failed_node = node_failure.get("node", "unknown")
            error = node_failure.get("error", "unknown")
            attempt = node_failure.get("attempt", "?")
            failure_context = (
                f"## Failed Node\n\n"
                f"**{failed_node}** failed on attempt {attempt}:\n"
                f"```\n{error}\n```"
            )

        prompt = build_prompt(
            GEMINI_ASSIST_PROMPT,
            f"## Task\n\n{task}",
            f"## Supervisor instructions\n\n{instructions}" if instructions else "",
            failure_context,
            f"## Current architecture plan\n\n{architecture_plan}" if architecture_plan else "",
            f"## Research findings so far\n\n{research_findings}" if research_findings else "",
            f"## Additional context\n\n{context}" if context else "",
        )

        try:
            findings = await run_gemini(prompt, timeout=600)
        except Exception as e:
            log.error("gemini_assist failed: %s", e)
            return {
                "node_failure": {
                    "node": "gemini_assist",
                    "error": str(e),
                    "attempt": node_calls["gemini_assist"],
                },
                "node_calls": node_calls,
                "history": history + [f"gemini_assist: failed: {e}"],
            }

        if findings.startswith("Error:"):
            log.error("gemini_assist CLI error: %s", findings)
            return {
                "node_failure": {
                    "node": "gemini_assist",
                    "error": findings,
                    "attempt": node_calls["gemini_assist"],
                },
                "node_calls": node_calls,
                "history": history + [f"gemini_assist: CLI error"],
            }

        log.info("gemini_assist completed (%d chars)", len(findings))

        return {
            "research_findings": findings,
            "node_failure": {},
            "output_versions": [
                {
                    "node": "gemini_assist",
                    "attempt": node_calls["gemini_assist"],
                    "content": findings,
                }
            ],
            "node_calls": node_calls,
            "history": history + [f"gemini_assist: completed (attempt {node_calls['gemini_assist']})"],
        }

    return gemini_assist_node
