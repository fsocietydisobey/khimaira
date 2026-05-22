"""Named condition functions for Themis invariants.

Each function takes (payload: dict) and returns bool. Conditions are
AND-combined only — no OR/NOT support in Phase 1 (spec §"Condition shapes").

Adding a new condition: add a function here and register it in _REGISTRY.
The YAML references conditions by name (the dict key in _REGISTRY).

Phase 1 ships with exactly 2 conditions:
  - idle_agents_exist
  - chat_my_chats_not_called_this_turn

A DSL is deferred until 5+ named conditions OR a first OR/NOT need emerges.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def evaluate_condition(name: str, payload: dict[str, Any]) -> bool:
    """Evaluate a named condition against runtime payload. Returns False for unknown names."""
    fn = _REGISTRY.get(name)
    if fn is None:
        logger.warning("Unknown Themis condition %r — treating as False (rule will not fire)", name)
        return False
    try:
        return fn(payload)
    except Exception:
        logger.exception("Error evaluating condition %r — treating as False", name)
        return False


# ---------------------------------------------------------------------------
# Phase 1 condition implementations
# ---------------------------------------------------------------------------


def idle_agents_exist(payload: dict[str, Any]) -> bool:
    """True if any agent-role session is currently idle.

    Expects payload["idle_agents"] to be a list of session dicts with at least
    {"session_id": ..., "name": ..., "status": ..., "last_active": "<iso>"}.
    The daemon populates this from GET /api/sessions filtered to role=agent
    and status=idle, active within the last 30 minutes.

    If the key is absent (daemon didn't populate), returns False (fail-closed:
    don't fire the rule when we can't confirm agents are idle).
    """
    agents = payload.get("idle_agents")
    if agents is None:
        return False
    return len(agents) > 0


def chat_my_chats_not_called_this_turn(payload: dict[str, Any]) -> bool:
    """True if the session's SSE subscriber heartbeat predates the current turn.

    Expects:
      payload["subscriber_last_heartbeat"]: ISO-8601 timestamp of the last
        chat_my_chats call for this session (from GET /api/chats/subscribers/<id>).
      payload["turn_start_ts"]: ISO-8601 timestamp when the current turn began
        (recorded by UserPromptSubmit hook to ~/.local/state/khimaira/sessions/<id>/turn_start.txt).

    If either key is absent, returns False (fail-closed: can't confirm the
    condition without both timestamps).
    """
    heartbeat_str = payload.get("subscriber_last_heartbeat")
    turn_start_str = payload.get("turn_start_ts")
    if not heartbeat_str or not turn_start_str:
        return False
    try:
        heartbeat = datetime.fromisoformat(heartbeat_str)
        turn_start = datetime.fromisoformat(turn_start_str)
        # Normalize to UTC for comparison
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if turn_start.tzinfo is None:
            turn_start = turn_start.replace(tzinfo=timezone.utc)
        return heartbeat < turn_start
    except ValueError:
        logger.warning(
            "chat_my_chats_not_called_this_turn: could not parse timestamps "
            "heartbeat=%r turn_start=%r",
            heartbeat_str,
            turn_start_str,
        )
        return False


# ---------------------------------------------------------------------------
# Registry — maps YAML condition name → function
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Any] = {
    "idle_agents_exist": idle_agents_exist,
    "chat_my_chats_not_called_this_turn": chat_my_chats_not_called_this_turn,
}
