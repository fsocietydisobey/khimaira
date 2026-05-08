"""Arbitrator — cross-model arbitration and synthesis.

The balancing force. Receives Stress Tester's verdict (restrictive) and ScopeAnalyzer's
proposals (generative), and arbitrates between them. Uses a DIFFERENT model
from the builder — no model judges its own output.

Produces a decision: which Stress Tester issues to address, which ScopeAnalyzer proposals
to accept, and an overall rationale. The decision feeds back into the
implementation loop.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.arbitrator")

ARBITRATOR_SYSTEM_PROMPT = """\
You are a senior engineering arbitrator. You receive two competing perspectives
on a piece of work:

1. **Stress Tester (the critic)** — found issues and wants changes
2. **ScopeAnalyzer (the advisor)** — proposes improvements and wants additions

Your job is to make the FINAL CALL on each item. You are the tiebreaker.

Decision rules:
- **Stress Tester blocker** → ALWAYS accept. Blockers must be fixed.
- **Stress Tester warning** → Accept if the issue is real and specific. Reject vague complaints.
- **ScopeAnalyzer proposal** → Accept if it's within scope, clearly valuable, and low effort.
  Reject scope creep, speculative improvements, and anything that's "nice to have" vs necessary.
- **Conflicting opinions** → Weigh specificity. Whoever cites specific files, functions,
  and concrete consequences wins. Vague arguments lose.

Be decisive. Don't hedge. For each item, say "accept" or "reject" with a one-line reason.

Finally, decide: does the implementation need rework (needs_rework: true) or is it
ready to proceed (needs_rework: false)? Only set needs_rework if there are accepted
Stress Tester blockers that haven't been fixed yet.
"""


class ArbitrationDecision(BaseModel):
    """Structured decision from the arbitrator."""

    accepted_changes: list[str] = Field(
        default_factory=list,
        description="Descriptions of accepted Stress Tester issues and ScopeAnalyzer proposals",
    )
    rejected_changes: list[str] = Field(
        default_factory=list,
        description="Descriptions of rejected items with reason",
    )
    rationale: str = Field(description="Overall reasoning for the decision")
    needs_rework: bool = Field(
        default=False,
        description="True if accepted blockers require implementation rework",
    )


def build_arbitrator_node(model: BaseChatModel):
    """Build a cross-model arbitration node.

    IMPORTANT: This should use a DIFFERENT model from the one that produced
    the implementation. If Claude built it, use Gemini to review (or vice versa).
    In practice, pass a different model instance than the one used for build nodes.

    Args:
        model: LangChain chat model (different from the builder model).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(ArbitrationDecision)

    async def arbitrator_node(state: OrchestratorState) -> dict:
        """Arbitrate between Stress Tester's restrictions and ScopeAnalyzer's expansions."""
        task = state.get("task", "")
        history = list(state.get("history", []))

        stress_test_verdict = state.get("stress_test_verdict") or {}
        scope_proposals = state.get("scope_proposals") or []

        # Build the arbitration context
        prompt_parts = [f"## Original task\n\n{task}"]

        # Format Stress Tester's issues
        issues = stress_test_verdict.get("issues", [])
        if issues:
            stress_test_section = "## Stress Tester's verdict\n\n"
            for i, issue in enumerate(issues, 1):
                stress_test_section += (
                    f"{i}. **[{issue.get('severity', '?')}]** ({issue.get('category', '?')}) "
                    f"{issue.get('description', '')}"
                )
                if issue.get("file"):
                    stress_test_section += f" — `{issue['file']}`"
                stress_test_section += "\n"
            prompt_parts.append(stress_test_section)
        else:
            prompt_parts.append("## Stress Tester's verdict\n\nNo issues found.")

        # Format ScopeAnalyzer's proposals
        if scope_proposals:
            scope_section = "## ScopeAnalyzer's proposals\n\n"
            for i, prop in enumerate(scope_proposals, 1):
                scope_section += (
                    f"{i}. **{prop.get('description', '')}** "
                    f"(effort: {prop.get('estimated_effort', '?')}) — "
                    f"{prop.get('rationale', '')}\n"
                )
            prompt_parts.append(scope_section)
        else:
            prompt_parts.append("## ScopeAnalyzer's proposals\n\nNo proposals.")

        # Include implementation context
        impl = state.get("implementation_result", "")
        if impl:
            prompt_parts.append(f"## Implementation output (for reference)\n\n{impl[:2000]}")

        messages = [
            SystemMessage(content=ARBITRATOR_SYSTEM_PROMPT),
            HumanMessage(content="\n\n".join(prompt_parts)),
        ]

        decision_raw = await structured_model.ainvoke(messages)
        assert isinstance(decision_raw, ArbitrationDecision)
        decision = decision_raw

        log.info(
            "decision: %d accepted, %d rejected, needs_rework=%s",
            len(decision.accepted_changes),
            len(decision.rejected_changes),
            decision.needs_rework,
        )

        decision_dict = {
            "accepted_changes": decision.accepted_changes,
            "rejected_changes": decision.rejected_changes,
            "rationale": decision.rationale,
            "needs_rework": decision.needs_rework,
        }

        # Build feedback for the next implementation cycle if rework needed
        feedback_parts = []
        if decision.accepted_changes:
            feedback_parts.append("## Accepted changes to address\n\n" +
                                  "\n".join(f"- {c}" for c in decision.accepted_changes))

        history_entry = (
            f"arbitrator: {len(decision.accepted_changes)} accepted, "
            f"{len(decision.rejected_changes)} rejected, "
            f"needs_rework={decision.needs_rework}"
        )

        # Bump the implementation-subgraph loop counter so the routing
        # function can self-bound after `max_phase_steps` iterations.
        # Without this, the loop runs until LangGraph's recursion_limit
        # forces termination.
        loop_step = state.get("implementation_loop_step", 0) + 1

        result: dict = {
            "arbitration_decision": decision_dict,
            "history": history + [history_entry],
            "implementation_loop_step": loop_step,
        }

        # If rework needed, append accepted changes to validation_feedback
        # so the implementation node sees them on retry
        if decision.needs_rework and feedback_parts:
            existing_feedback = state.get("validation_feedback", "")
            arbitration_feedback = "\n\n".join(feedback_parts)
            result["validation_feedback"] = (
                f"{existing_feedback}\n\n## Arbitrator decision\n\n{arbitration_feedback}"
                if existing_feedback else arbitration_feedback
            )
            result["handoff_type"] = "tests_failing"  # Force rework
        else:
            # Arbitrator decided to proceed — the arbitration IS the final
            # call. Clear any stale "tests_failing" set by stress_tester
            # earlier in this iteration so the routing function actually
            # exits the loop. The previous `elif != tests_failing` paranoia
            # caused infinite loops (handoff_type stuck at tests_failing
            # while needs_rework=False forced re-entry to implement via the
            # OR clause in _after_arbitrator).
            result["handoff_type"] = "ready_for_review"

        return result

    return arbitrator_node
