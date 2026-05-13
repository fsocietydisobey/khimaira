"""Research node — shells out to Gemini CLI for deep exploration.

Gemini CLI runs from the project root with native codebase access.
Incorporates supervisor instructions and validation feedback on retries.
"""

from khimaira.prompts.builder import build_prompt
from khimaira.dispatch.runners import run_gemini
from khimaira.log import get_logger
from khimaira.prompts import RESEARCH_SYSTEM_PROMPT
from khimaira.core.state import OrchestratorState

log = get_logger("node.research")


def build_research_node():
    """Build a research node that uses Gemini CLI.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def research_node(state: OrchestratorState) -> dict:
        """Run Gemini CLI research and return findings.

        Works in two modes:
        - Sequential: called normally with full state
        - Parallel: called via Send() with a partial payload containing
          task, context, supervisor_instructions, parallel_task_topic
        """
        task = state.get("task", "")
        context = state.get("context", "")
        instructions = state.get("supervisor_instructions", "")
        feedback = state.get("validation_feedback", "")
        topic = state.get("parallel_task_topic", "")
        node_calls = dict(state.get("node_calls", {}))
        history = list(state.get("history", []))

        # Track call count
        node_calls["research"] = node_calls.get("research", 0) + 1

        # Parallel sub-task: prepend topic context to instructions
        if topic:
            instructions = f"[Research sub-task: {topic}]\n\n{instructions}"

        log.info("starting (attempt=%d, topic=%s)", node_calls["research"], topic or "sequential")

        prompt = build_prompt(
            RESEARCH_SYSTEM_PROMPT,
            task,
            f"## Context\n\n{context}" if context else "",
            f"## Supervisor instructions\n\n{instructions}" if instructions else "",
            f"## Previous feedback to address\n\n{feedback}" if feedback else "",
        )

        try:
            findings = await run_gemini(prompt, timeout=600)
        except Exception as e:
            log.error("research failed: %s", e)
            label = topic or "sequential"
            return {
                "node_failure": {
                    "node": "research",
                    "error": str(e),
                    "attempt": node_calls["research"],
                    "topic": label,
                },
                "node_calls": node_calls,
                "history": history + [f"research: failed (topic: {label}): {e}"],
            }

        if findings.startswith("Error:"):
            log.error("research CLI error: %s", findings)
            label = topic or "sequential"
            return {
                "node_failure": {
                    "node": "research",
                    "error": findings,
                    "attempt": node_calls["research"],
                    "topic": label,
                },
                "node_calls": node_calls,
                "history": history + [f"research: failed (topic: {label}): CLI error"],
            }

        label = topic or "sequential"
        out: dict = {
            "node_failure": {},
            "output_versions": [
                {
                    "node": "research",
                    "attempt": node_calls["research"],
                    "topic": label,
                    "content": findings,
                }
            ],
            "node_calls": node_calls,
            "history": history + [f"research: completed (topic: {label})"],
        }
        # Only write research_findings in sequential mode; parallel branches write to
        # output_versions only — merge_research is the single writer to research_findings.
        if not topic:
            out["research_findings"] = findings
        return out

    return research_node
