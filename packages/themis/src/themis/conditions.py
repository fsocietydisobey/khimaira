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


# Top-tier role prefixes for CONSULT_BEFORE_USER (IN-MASTER-4).
# Heuristic: session names starting with these prefixes are top-tier agents.
_TOP_TIER_PREFIXES = ("architect", "analyst", "critic", "verifier")
# Tool names that constitute a "consult" for top-tier detection purposes.
_CONSULT_TOOLS = frozenset(
    ["mcp__khimaira-chat__chat_send_to", "mcp__khimaira__session_log_question"]
)
# How many recent tool calls to inspect for top-tier consults.
_HISTORY_WINDOW = 10


def _is_top_tier_session(name: str) -> bool:
    """True if session name maps to a top-tier role.

    Handles bare names ("architect-1"), roster-prefixed names ("jp-architect-1"),
    and any depth of prefix (e.g. "acme-corp-analyst-2"). The rule: any segment
    of the dash-separated name that matches a top-tier role prefix is enough.
    """
    lower = name.lower()
    parts = lower.split("-")
    return any(part in _TOP_TIER_PREFIXES for part in parts)


def no_recent_top_tier_consult(payload: dict[str, Any]) -> bool:
    """True (violation) when no top-tier consult appears in recent tool calls.

    Inspects payload["recent_tool_calls"] — a list of recent tool-call dicts
    (up to the last _HISTORY_WINDOW entries), each shaped as:
      {"tool": str, "params": dict}

    For chat_send_to, params["to"] may be a str OR list[str] (production shape
    is list). For session_log_question, params["target_session_id"] is a str.

    Top-tier detection is segment-based: any dash-separated segment of the
    target name that matches a top-tier role prefix counts. This handles both
    bare names ("architect-1") and roster-prefixed names ("jp-architect-1").

    Fail-open: if the key is absent, returns False (can't confirm → don't fire).
    This preserves the invariant that missing payload never triggers a violation.

    Limitation: detection is name-based, not role-binding-based. A session with
    an opaque UUID will not be detected as top-tier.
    """
    recent = payload.get("recent_tool_calls")
    if recent is None:
        # Fail-open: no payload means we can't confirm the condition; don't fire.
        return False

    for call in recent[-_HISTORY_WINDOW:]:
        tool = call.get("tool", "")
        if tool not in _CONSULT_TOOLS:
            continue
        params = call.get("params", {})

        # Collect all target names from the call params.
        # chat_send_to: params["to"] is list[str] in production, str in some tests.
        # session_log_question: params["target_session_id"] is str.
        raw_to = params.get("to") or params.get("target_session_id") or ""
        if isinstance(raw_to, list):
            targets = [str(t) for t in raw_to if t]
        else:
            targets = [str(raw_to)] if raw_to else []

        if any(_is_top_tier_session(t) for t in targets):
            return False  # found a recent top-tier consult — condition not met

    return True  # no top-tier consult found — condition met, rule fires


# ---------------------------------------------------------------------------
# Registry — maps YAML condition name → function
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Any] = {
    "idle_agents_exist": idle_agents_exist,
    "chat_my_chats_not_called_this_turn": chat_my_chats_not_called_this_turn,
    "no_recent_top_tier_consult": no_recent_top_tier_consult,
}
