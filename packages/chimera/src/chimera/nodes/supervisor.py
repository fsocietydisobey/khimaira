"""Supervisor node — central decision-maker in the hub-and-spoke pattern.

After every node execution, the graph returns to the supervisor. It inspects
the full state (task, history, scores, findings, plan) and decides:
- Which node to call next
- What instructions to give that node
- Whether to terminate

Uses Pydantic structured output for type-safe, validated decisions.
"""

from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("supervisor")

SUPERVISOR_SYSTEM_PROMPT = """\
You are a supervisor orchestrating a software engineering pipeline. You inspect
the current state after each step and decide what to do next.

## Available nodes

- **research**: Deep domain/technology exploration using Gemini CLI. Use when
  the problem domain isn't well understood, you need to explore options, or
  gather information before designing.
- **architect**: Design and planning using Claude Code CLI with full codebase
  access. Use when the problem is understood but needs a concrete plan with
  file paths, functions, and step-by-step changes.
- **human_review**: Pause for human approval. Use BEFORE implement — the human
  must review and approve the architecture plan before any code is written.
  ALWAYS route to human_review after the architecture plan is validated.
  Never skip this step. Never go directly from architect/validator to implement.
- **implement**: Code implementation using Claude Code CLI with full read/write
  codebase access. Use ONLY after human_review has approved the plan.
- **gemini_assist**: Debugging and analysis using Gemini CLI with full codebase
  access. Use when a node has failed repeatedly and needs external analysis,
  or when the user explicitly asks Gemini to help debug or research an issue.
  Gemini can read codebase files, research patterns, and suggest fixes.
- **validator**: Quality check on the most recent output. Use after research
  or architect to verify the output is complete, specific, and actionable.
  Do NOT validate after implement — implementation is the final step.
- **finish**: The pipeline is complete. Use when implementation is done, or
  when the task has been fully addressed.

## Decision rules

1. For unknown domains or exploratory tasks: research first.
2. For tasks needing design: architect (optionally after research).
3. For simple/clear tasks: architect first, then human_review, then implement.
4. After research or architect: validate the output quality.
5. If the validator reports low quality: retry the node with the feedback.
6. After architect is validated (good score): ALWAYS route to human_review.
7. After human_review approved: route to implement.
8. After human_review rejected: route back to architect with the human's feedback.
9. After implement: finish (do not validate implementation).
10. Never call the same node more than 3 times total.
11. If stuck (repeated low scores or failures), route to gemini_assist for debugging help.
12. If gemini_assist provides findings, use them to retry the failed node with better context.

## Parallel research (fan-out)

When you choose `research` as next_step, you can optionally fan out into
multiple parallel sub-tasks. Populate `parallel_tasks` with a list of dicts:
- `topic`: short label (e.g., "auth-patterns", "caching-strategies")
- `instructions`: specific instructions for that sub-task

Use parallel research when the task has 2-4 independent knowledge gaps that
can be explored simultaneously. Do NOT fan out for a single focused question
— use sequential research with `instructions` instead.

Good candidates for fan-out:
- Evaluating multiple competing technologies
- Researching different aspects of a system (API design + data model + auth)
- Comparing frameworks or libraries

Keep it to 2-4 parallel tasks max. Leave `parallel_tasks` empty for sequential.

## State inspection

Look at the full state to make your decision:
- `task`: What the user asked for
- `history`: What has happened so far (node → result summaries)
- `research_findings`: Research output (if any)
- `architecture_plan`: Architecture output (if any)
- `implementation_result`: Implementation output (if any)
- `validation_score`: Last validation score (0.0-1.0, if any)
- `validation_feedback`: Last validation feedback (if any)
- `node_calls`: How many times each node has been called
- `human_review_status`: "approved" or "rejected" (if human review has happened)
- `human_feedback`: Feedback from the human reviewer (if any)
"""


class ParallelTask(BaseModel):
    """A single sub-task for parallel fan-out research."""

    topic: str = Field(description="Short label for this sub-task (e.g., 'caching-strategies').")
    instructions: str = Field(description="Specific research instructions for this sub-task.")


class RouterDecision(BaseModel):
    """Structured decision from the supervisor."""

    next_step: Literal["research", "architect", "human_review", "implement", "validator", "gemini_assist", "finish"] = (
        Field(description="The next node to execute, or 'finish' to terminate.")
    )
    rationale: str = Field(
        description="One sentence explaining why this node was chosen."
    )
    instructions: str = Field(
        default="",
        description="Brief, focused instructions for the next node (1-2 sentences). State WHAT to do, not HOW to explore. Do NOT tell the node to inspect or explore the codebase.",
    )
    parallel_tasks: list[ParallelTask] = Field(
        default_factory=list,
        description=(
            "Optional. If next_step is 'research' and you want PARALLEL research, "
            "provide 2-4 sub-tasks. Leave empty for sequential research."
        ),
    )


def build_supervisor_node(model: BaseChatModel):
    """Build a supervisor node that uses structured output for routing decisions.

    Args:
        model: LangChain chat model (cheap/fast — Haiku recommended).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(RouterDecision)

    async def supervisor_node(state: OrchestratorState) -> dict:
        """Inspect state and decide the next step."""
        import time as _time
        _t0 = _time.monotonic()
        task = state.get("task", "")
        context = state.get("context", "")
        history = state.get("history", [])
        node_calls = state.get("node_calls", {})

        # Build a state summary for the supervisor to inspect
        state_summary_parts = [f"## Task\n\n{task}"]

        if context:
            state_summary_parts.append(f"## Context\n\n{context}")

        # Show what's been done
        if history:
            state_summary_parts.append(
                "## History\n\n" + "\n".join(f"- {h}" for h in history)
            )

        # Show node call counts
        if node_calls:
            counts = ", ".join(f"{k}: {v}" for k, v in node_calls.items())
            state_summary_parts.append(f"## Node call counts\n\n{counts}")

        # Show current outputs (truncated)
        findings = state.get("research_findings", "")
        if findings:
            state_summary_parts.append(
                f"## Research findings (truncated)\n\n{findings[:1000]}..."
                if len(findings) > 1000
                else f"## Research findings\n\n{findings}"
            )

        plan = state.get("architecture_plan", "")
        if plan:
            state_summary_parts.append(
                f"## Architecture plan (truncated)\n\n{plan[:1000]}..."
                if len(plan) > 1000
                else f"## Architecture plan\n\n{plan}"
            )

        impl = state.get("implementation_result", "")
        if impl:
            state_summary_parts.append(
                f"## Implementation result (truncated)\n\n{impl[:500]}..."
                if len(impl) > 500
                else f"## Implementation result\n\n{impl}"
            )

        # Show validation results
        v_score = state.get("validation_score")
        v_feedback = state.get("validation_feedback", "")
        if v_score is not None:
            state_summary_parts.append(
                f"## Last validation\n\n"
                f"**Score:** {v_score:.2f}\n"
                f"**Feedback:** {v_feedback}"
            )

        # Show node failure if present
        node_failure = state.get("node_failure", {})
        if node_failure:
            failed_node = node_failure.get("node", "?")
            error = node_failure.get("error", "unknown")
            attempt = node_failure.get("attempt", "?")
            state_summary_parts.append(
                f"## Node failure\n\n"
                f"**{failed_node}** failed on attempt {attempt}: {error}\n"
                f"Decide: retry (if under max retries), skip to another node, or finish."
            )

        # Show human review status
        review_status = state.get("human_review_status", "")
        human_feedback = state.get("human_feedback", "")
        if review_status:
            review_section = f"## Human review\n\n**Status:** {review_status}"
            if human_feedback:
                review_section += f"\n**Feedback:** {human_feedback}"
            state_summary_parts.append(review_section)

        state_summary = "\n\n".join(state_summary_parts)
        log.debug("state summary: %d chars", len(state_summary))
        _t1 = _time.monotonic()
        log.info("building state summary took %.1fs", _t1 - _t0)

        messages = [
            SystemMessage(content=SUPERVISOR_SYSTEM_PROMPT),
            HumanMessage(content=state_summary),
        ]

        decision: RouterDecision = await structured_model.ainvoke(messages)
        _t2 = _time.monotonic()
        log.info("decision: %s — %s (LLM call: %.1fs, total: %.1fs)", decision.next_step, decision.rationale, _t2 - _t1, _t2 - _t0)

        # Convert parallel tasks to dicts for state storage
        parallel_tasks = [
            {"topic": pt.topic, "instructions": pt.instructions}
            for pt in decision.parallel_tasks
        ]

        history_entry = f"supervisor → {decision.next_step}: {decision.rationale}"
        if parallel_tasks:
            topics = ", ".join(pt["topic"] for pt in parallel_tasks)
            history_entry += f" [fan-out: {topics}]"

        return {
            "next_node": decision.next_step,
            "supervisor_rationale": decision.rationale,
            "supervisor_instructions": decision.instructions,
            "parallel_tasks": parallel_tasks,
            "history": history + [history_entry],
        }

    return supervisor_node
