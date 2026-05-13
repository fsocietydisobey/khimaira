"""PDE worker — parallel worker for PDE.

Thin wrapper around Claude CLI that executes a single task with scoped
file access. Each agent owns a set of files and should only modify those.
"""

from khimaira.prompts.builder import build_prompt
from khimaira.dispatch.runners import run_claude
from khimaira.log import get_logger
from khimaira.core.state import OrchestratorState

log = get_logger("node.swarm_agent")

SWARM_AGENT_PROMPT = """\
You are a focused implementation agent. Execute ONLY the task described below.

## Rules

- ONLY modify the files listed in your scope. Do not touch other files.
- Make the minimum change necessary to complete the task.
- If the task is unclear, make a reasonable choice and note it.
- Do NOT add features, refactor, or "improve" code beyond the task scope.
"""


def build_swarm_worker_node():
    """Build a swarm agent node for parallel execution.

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def pde_worker_node(state: OrchestratorState) -> dict:
        """Execute a single scoped task."""
        # Read task details from the Send() payload
        task_id = state.get("task", "unknown")  # Overloaded in Send payload
        description = state.get("context", "")  # Task description via context
        instructions = state.get("supervisor_instructions", "")

        history = list(state.get("history", []))

        log.info("agent starting: %s", task_id[:60])

        prompt = build_prompt(
            SWARM_AGENT_PROMPT,
            f"## Task: {task_id}\n\n{description}",
            f"## Instructions\n\n{instructions}" if instructions else "",
        )

        try:
            result = await run_claude(prompt, timeout=300, permission_mode="acceptEdits")

            log.info("agent completed: %s", task_id[:60])

            return {
                "swarm_results": [{
                    "task_id": task_id,
                    "status": "success",
                    "output": result[:2000],
                }],
                "history": history + [f"swarm_agent: completed {task_id}"],
            }

        except Exception as e:
            log.error("agent failed: %s — %s", task_id[:60], e)
            return {
                "swarm_results": [{
                    "task_id": task_id,
                    "status": "failed",
                    "error": str(e),
                }],
                "history": history + [f"swarm_agent: failed {task_id} — {e}"],
            }

    return pde_worker_node
