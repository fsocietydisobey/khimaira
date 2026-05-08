"""Stress Tester — adversarial validator that actively tries to break the code.

Unlike the passive critic (which scores and gates), Stress Tester is an active adversary.
It doesn't ask "is this good enough?" — it asks "how can I break this?"

Produces a structured verdict with categorized issues at blocker/warning/note severity.
Any blocker forces a rework. Warnings are passed to Arbitrator for arbitration.
"""

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.log import get_logger
from chimera.core.state import OrchestratorState

log = get_logger("node.stress_tester")

STRESS_TESTER_SYSTEM_PROMPT = """\
You are an adversarial code reviewer. Your job is to BREAK things, not approve them.

Assume the code is wrong and prove it. Look for:

1. **Correctness**: Logic errors, off-by-one, missing edge cases, unhandled exceptions
2. **Security**: Injection vulnerabilities, unvalidated input, exposed secrets, unsafe deserialization
3. **Hallucination**: File paths or function names that don't exist in the codebase
4. **Scope creep**: Changes not requested by the original task or architecture plan
5. **Breaking changes**: Modified interfaces, removed exports, changed signatures without updating callers

For each issue found, classify severity:
- **blocker**: Must be fixed before proceeding. Security holes, logic errors, broken interfaces.
- **warning**: Should be fixed but not critical. Missing edge cases, style issues, minor scope creep.
- **note**: Observation only. Potential future improvement, not a current problem.

Be harsh. Be specific. Reference exact file paths and line numbers when possible.
If you find nothing wrong, say so — but try hard to find something first.
"""


class Issue(BaseModel):
    """A single issue found by Stress Tester."""

    description: str = Field(description="What's wrong — be specific")
    file: str = Field(default="", description="File path if applicable")
    severity: str = Field(description="blocker | warning | note")
    category: str = Field(
        description="correctness | security | hallucination | scope_creep | breaking_change"
    )


class StressTestVerdict(BaseModel):
    """Structured verdict from the adversarial validator."""

    issues: list[Issue] = Field(default_factory=list, description="All issues found")
    recommendation: str = Field(
        description="pass | fail | pass_with_warnings"
    )
    summary: str = Field(description="One-sentence summary of the verdict")


def build_stress_tester_node(model: BaseChatModel):
    """Build an adversarial validator node.

    Args:
        model: LangChain chat model (Haiku recommended for cost, Sonnet for depth).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(StressTestVerdict)

    async def stress_tester_node(state: OrchestratorState) -> dict:
        """Adversarially review the most recent output."""
        task = state.get("task", "")
        history = list(state.get("history", []))

        # Determine what to attack
        plan = state.get("architecture_plan", "")
        impl = state.get("implementation_result", "")
        findings = state.get("research_findings", "")

        if impl:
            target_type = "implementation"
            target_content = impl
        elif plan:
            target_type = "architecture plan"
            target_content = plan
        elif findings:
            target_type = "research findings"
            target_content = findings
        else:
            log.info("nothing to review, passing")
            return {
                "stress_test_verdict": {"issues": [], "recommendation": "pass", "summary": "Nothing to review"},
                "history": history + ["stress_tester: nothing to review, passing"],
            }

        prompt = (
            f"## Original task\n\n{task}\n\n"
            f"## Output to attack ({target_type})\n\n{target_content}"
        )

        # Include the architecture plan as context when reviewing implementation
        if target_type == "implementation" and plan:
            prompt += f"\n\n## Architecture plan (for scope comparison)\n\n{plan}"

        messages = [
            SystemMessage(content=STRESS_TESTER_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        verdict_raw = await structured_model.ainvoke(messages)
        assert isinstance(verdict_raw, StressTestVerdict)
        verdict = verdict_raw

        blockers = [i for i in verdict.issues if i.severity == "blocker"]
        warnings = [i for i in verdict.issues if i.severity == "warning"]
        notes = [i for i in verdict.issues if i.severity == "note"]

        log.info(
            "verdict: %s (%d blockers, %d warnings, %d notes)",
            verdict.recommendation, len(blockers), len(warnings), len(notes),
        )

        # Determine handoff
        if blockers:
            handoff = "tests_failing"  # Force rework
        else:
            handoff = ""  # Let Arbitrator decide

        verdict_dict = {
            "issues": [i.model_dump() for i in verdict.issues],
            "recommendation": verdict.recommendation,
            "summary": verdict.summary,
        }

        history_entry = (
            f"stress_tester: {verdict.recommendation} — "
            f"{len(blockers)} blockers, {len(warnings)} warnings, {len(notes)} notes"
        )
        if verdict.summary:
            history_entry += f" — {verdict.summary}"

        result: dict = {
            "stress_test_verdict": verdict_dict,
            "history": history + [history_entry],
        }
        if handoff:
            result["handoff_type"] = handoff

        return result

    return stress_tester_node
