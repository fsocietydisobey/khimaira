"""Invariant guards for SPR-4 phase routing and tool execution.

These guards enforce safety constraints:
- No implementation without an approved plan
- No execution without human approval (when required)
- Max step limits per phase
"""

from chimera.core.state import OrchestratorState


def require_plan_approved(state: OrchestratorState) -> bool:
    """Check that the architecture plan has been approved by the critic."""
    return bool(state.get("plan_approved", False))


def require_human_approved(state: OrchestratorState) -> bool:
    """Check that a human has approved the plan."""
    return bool(state.get("human_approved", False))


def phase_steps_remaining(state: OrchestratorState) -> bool:
    """Check that the current phase hasn't exceeded its step limit."""
    step = state.get("phase_step", 0)
    max_steps = state.get("max_phase_steps", 5)
    return step < max_steps
