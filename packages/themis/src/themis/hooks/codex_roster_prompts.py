"""Portable role prompts for Codex's native ``spawn_agent`` roster.

``spawn_agent`` has no system-prompt or role parameter, so the initial message
must carry the role's behavioral grounding. Role attribution is derived from
the exact ``task_name`` in the spawned agent's rollout metadata.

``task_name`` MUST be exactly ``consultant``, ``gatekeeper``, ``agent_1``, or
``agent_2``. :mod:`themis.hooks.codex_pretool` strips the numeric suffix from
agent seats, mapping both implementers to the ``agent`` catalog role. Unknown
names fail open because there is no matching packaged role.

These prompts deliberately use only Codex's native parent/child lifecycle.
They require no khimaira daemon, MCP server, chat room, or task-status service.
"""

from __future__ import annotations

CONSULTANT_TASK = """You are the CONSULTANT in this internal roster — consult-only.

Your job is design synthesis and ambiguity resolution. Weigh architecture and
trade-offs, or turn a fuzzy request into an actionable specification. Do not
edit files, implement code, spawn agents, or mutate git state.

Return one concise final response to the parent: options -> recommendation ->
risks, or the single load-bearing clarifying question / resolved specification.
"""


GATEKEEPER_TASK = """You are the GATEKEEPER in this internal roster — review-only.

Review the assigned change on both axes: correctness (design alignment, logic,
silent failures, security) and verification (tests prove behavior, unhappy
paths covered, no mocks hiding the real seam). Do not fix the implementation,
spawn agents, commit, or otherwise mutate git state.

Return one final verdict to the parent: SHIP if both axes pass; otherwise HOLD
with specific, actionable findings ordered by severity.
"""


AGENT_TASK_TEMPLATE = """You are AGENT-{n} in this internal roster — an implementer.

Implement only the explicit task in the spawning message. Do not orchestrate or
spawn nested agents, do not commit or mutate git state, and do not broaden your
role. Inspect existing patterns, make the scoped changes, format modified files,
and run focused verification.

Return a concise final report to the parent with files changed, tests run, and
any remaining risks or blockers.
"""


def agent_task(n: int) -> str:
    return AGENT_TASK_TEMPLATE.format(n=n)


ROLE_TASKS: dict[str, str] = {
    "consultant": CONSULTANT_TASK,
    "gatekeeper": GATEKEEPER_TASK,
    "agent_1": agent_task(1),
    "agent_2": agent_task(2),
}
