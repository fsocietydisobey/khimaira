"""Compatibility re-exports for portable Themis Codex roster prompts."""

from themis.hooks.codex_roster_prompts import (
    AGENT_TASK_TEMPLATE,
    CONSULTANT_TASK,
    GATEKEEPER_TASK,
    ROLE_TASKS,
    agent_task,
)

__all__ = [
    "AGENT_TASK_TEMPLATE",
    "CONSULTANT_TASK",
    "GATEKEEPER_TASK",
    "ROLE_TASKS",
    "agent_task",
]
