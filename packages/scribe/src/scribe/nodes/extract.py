"""Action / decision / participant extraction — routed via khimaira auto-router.

Like summarize, this is text-only post-processing of the transcript.
Routes through the pool router for cheapest competent model. Records
usage with `mode="auto"` so it lands in `khimaira usage savings`.
"""

import json

from scribe.state import MeetingState


_PROMPT = (
    "Analyze this meeting transcript and extract the following as JSON:\n\n"
    "```json\n"
    "{\n"
    '  "action_items": ["Action item with owner if mentioned"],\n'
    '  "decisions": ["Decision that was made"],\n'
    '  "participants": ["Name or Speaker label"]\n'
    "}\n"
    "```\n\n"
    "Rules:\n"
    "- Action items should be specific and actionable\n"
    "- Include the responsible person if mentioned\n"
    "- Decisions should capture what was agreed upon\n"
    "- List all identifiable participants\n"
    "- Return ONLY valid JSON, no markdown fences\n\n"
    "## Transcript\n\n"
)


async def extract_actions(state: MeetingState) -> dict:
    """Extract action items, decisions, and participants via khimaira auto-router."""
    transcript = state["transcript"]

    try:
        from khimaira.server.mcp import _delegate_impl
    except ImportError:
        return {"action_items": [], "decisions": [], "participants": []}

    # See summarize.py for the gemini-runner workaround rationale —
    # pinning to claude-haiku until tasks/gemini-runner-bug is resolved.
    raw = await _delegate_impl(
        _PROMPT + transcript,
        tier="haiku",
        timeout_s=120,
        project=state.get("task_id") or "",
    )
    if raw.startswith("_(via "):
        try:
            raw = raw.split("\n\n", 1)[1]
        except IndexError:
            pass

    try:
        text = raw.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1]
            text = text.rsplit("```", 1)[0]
        data = json.loads(text)
    except (json.JSONDecodeError, IndexError):
        data = {"action_items": [], "decisions": [], "participants": []}

    return {
        "action_items": data.get("action_items", []),
        "decisions": data.get("decisions", []),
        "participants": data.get("participants", []),
    }
