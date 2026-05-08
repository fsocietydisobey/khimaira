"""Human-in-the-loop review node — pauses the graph for human approval.

Uses LangGraph's interrupt() to pause execution and surface the current
architecture plan (or other reviewable output) to the caller. The graph
resumes when the caller invokes Command(resume=...) with an approval
or rejection + feedback.

The interrupt/resume cycle:
    1. human_review node runs, calls interrupt(review_payload)
    2. Graph pauses, returns the payload to the MCP tool
    3. MCP tool returns the plan to the IDE with "waiting for approval"
    4. User calls approve(thread_id, feedback?) MCP tool
    5. approve() calls graph.ainvoke(Command(resume={"decision": "approved/rejected", ...}))
    6. human_review node re-executes from the top, interrupt() returns the resume value
    7. Node writes human_review_status and human_feedback to state
    8. Graph continues — supervisor sees the review result and routes accordingly
"""

from langgraph.types import interrupt

from chimera.core.state import OrchestratorState


def build_human_review_node():
    """Build a human review node that pauses for approval via interrupt().

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """

    async def human_review_node(state: OrchestratorState) -> dict:
        """Pause the graph and wait for human approval.

        On first execution, interrupt() raises GraphInterrupt with the
        review payload. On resume, interrupt() returns the resume value
        (the human's decision).
        """
        history = list(state.get("history", []))
        plan = state.get("architecture_plan", "")
        task = state.get("task", "")

        # Build the review payload — this is what the caller sees
        review_payload = {
            "type": "human_review",
            "message": "Architecture plan ready for review. Approve to proceed to implementation, or reject with feedback.",
            "task": task,
            "architecture_plan": plan,
        }

        # interrupt() pauses execution on first call.
        # On resume, it returns the value from Command(resume=...).
        response = interrupt(review_payload)

        # --- Execution resumes here after Command(resume=...) ---
        decision = response.get("decision", "approved")
        feedback = response.get("feedback", "")

        if decision == "approved":
            status_entry = "human_review: approved"
            if feedback:
                status_entry += f" — {feedback}"
        else:
            status_entry = f"human_review: rejected — {feedback}"

        return {
            "human_review_status": decision,
            "human_feedback": feedback,
            "history": history + [status_entry],
        }

    return human_review_node
