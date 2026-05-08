"""POB proposer — selects the highest-priority friction point and generates a tool spec.

Filters out rejected categories, enforces cool-down, and picks one tool per cycle.
Uses Haiku for spec generation.
"""

import time

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import BaseModel, Field

from chimera.core.state import OrchestratorState
from chimera.core.toolbuilder_memory import get_rejected_types, last_proposal_time
from chimera.log import get_logger

log = get_logger("node.toolbuilder_proposer")

PROPOSER_SYSTEM_PROMPT = """\
You are a tool specification writer. Given a friction point observed in a developer's
workflow, generate a precise specification for a tool that eliminates the friction.

## Rules

- The tool MUST go in scripts/, tools/, or .github/ — NEVER touch product code (src/)
- Keep it simple — one script or config file per tool
- Include clear usage instructions
- Name the tool descriptively (e.g., "quick-push" not "tool1")
"""

# Cool-down: minimum 1 hour between proposals
_COOLDOWN_SECONDS = 3600


class ToolSpec(BaseModel):
    """Specification for a tool to build."""

    name: str = Field(description="Tool name (e.g., 'quick-push')")
    category: str = Field(description="Friction category this addresses")
    description: str = Field(description="What the tool does")
    files_to_create: list[str] = Field(description="File paths to create (e.g., ['scripts/quick-push.sh'])")
    friction_addressed: str = Field(description="What problem this solves")
    evidence: str = Field(description="Data that triggered this proposal")
    usage: str = Field(description="How to use the tool")


def build_toolbuilder_proposer_node(model: BaseChatModel):
    """Build a proposer node that selects and specs a tool to build.

    Args:
        model: LangChain chat model (Haiku — fast, cheap).

    Returns:
        Async node function compatible with LangGraph StateGraph.
    """
    structured_model = model.with_structured_output(ToolSpec)

    async def toolbuilder_proposer_node(state: OrchestratorState) -> dict:
        """Select a friction point and generate a tool spec."""
        history = list(state.get("history", []))
        friction_points = state.get("toolbuilder_friction_points") or []

        if not friction_points:
            return {
                "toolbuilder_tool_spec": None,
                "history": history + ["toolbuilder_proposer: no friction points to address"],
            }

        # Cool-down check
        last_time = await last_proposal_time()
        if last_time and (time.time() - last_time) < _COOLDOWN_SECONDS:
            remaining = int(_COOLDOWN_SECONDS - (time.time() - last_time))
            log.info("POB proposer: cool-down active (%ds remaining)", remaining)
            return {
                "toolbuilder_tool_spec": None,
                "history": history + [f"toolbuilder_proposer: cool-down active ({remaining}s remaining)"],
            }

        # Filter out rejected categories
        rejected = await get_rejected_types()
        available = [fp for fp in friction_points if fp.get("category") not in rejected]

        if not available:
            log.info("POB proposer: all friction categories have been rejected")
            return {
                "toolbuilder_tool_spec": None,
                "history": history + ["toolbuilder_proposer: all categories rejected — nothing to propose"],
            }

        # Pick the highest priority (lowest number)
        available.sort(key=lambda fp: fp.get("priority", 99))
        selected = available[0]

        log.info("POB proposer: selected %s — %s", selected["category"], selected["description"])

        # Generate tool spec
        prompt = (
            f"## Friction Point\n\n"
            f"**Category:** {selected['category']}\n"
            f"**Description:** {selected['description']}\n"
            f"**Proposed solution:** {selected['proposed_solution']}\n"
            f"**Estimated time saved:** {selected['estimated_time_saved']}\n"
        )

        messages = [
            SystemMessage(content=PROPOSER_SYSTEM_PROMPT),
            HumanMessage(content=prompt),
        ]

        spec = await structured_model.ainvoke(messages)
        assert isinstance(spec, ToolSpec)

        # Validate paths
        allowed_dirs = ("scripts/", "tools/", ".github/")
        valid_files = [f for f in spec.files_to_create if any(f.startswith(d) for d in allowed_dirs)]

        if not valid_files:
            log.warning("POB proposer: spec has no valid file paths — rejected")
            return {
                "toolbuilder_tool_spec": None,
                "history": history + ["toolbuilder_proposer: spec rejected — invalid file paths"],
            }

        spec_dict = {
            "name": spec.name,
            "category": spec.category,
            "description": spec.description,
            "files_to_create": valid_files,
            "friction_addressed": spec.friction_addressed,
            "evidence": spec.evidence,
            "usage": spec.usage,
        }

        return {
            "toolbuilder_tool_spec": spec_dict,
            "history": history + [f"toolbuilder_proposer: spec ready — {spec.name} ({spec.category})"],
        }

    return toolbuilder_proposer_node
