"""Named condition functions for Themis invariants.

Each function takes (payload: dict) and returns bool. Conditions are
AND-combined only — no OR/NOT support in Phase 1 (spec §"Condition shapes").

Adding a new condition: add a function here and register it in _REGISTRY.
The YAML references conditions by name (the dict key in _REGISTRY).

Phase 1 ships with 7 conditions:
  - idle_agents_exist
  - chat_my_chats_not_called_this_turn
  - no_recent_top_tier_consult (IN-MASTER-4)
  - question_text_is_design_shaped (IN-MASTER-4 refinement)
  - idle_capacity_available (IN-MASTER-5)
  - no_recent_parallel_dispatch (IN-MASTER-5)
  - recent_dispatch_different_ctx (IN-MASTER-6)

A DSL is deferred until 5+ named conditions OR a first OR/NOT need emerges.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone
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
# IN-MASTER-4 refinement: question-shape detection
# ---------------------------------------------------------------------------

# Design-shape patterns — conservative (high-confidence only, prefer false negatives
# over false positives to preserve trust in the warn).
_DESIGN_SHAPE_PATTERNS = [
    re.compile(r"\bwhich approach\b", re.IGNORECASE),
    re.compile(r"\bshould (?:i|we) (?:use|adopt|pick|choose|implement)\b", re.IGNORECASE),
    re.compile(r"\b(?:option|approach)\s+(?:[Aa]|[Bb]|1|2)\s+(?:or|vs\.?|versus)\s+(?:[Aa]|[Bb]|1|2)\b", re.IGNORECASE),
    re.compile(r"\(\s*[Aa]\s*\)\s+.*\(\s*[Bb]\s*\)", re.IGNORECASE),
    re.compile(r"\b(?:trade.?off|architecture|scoping|coverage|design)\b.*\?", re.IGNORECASE),
    re.compile(r"\b(?:atomic|bundled|sibling|separate)\s+(?:task|fix|change)\b", re.IGNORECASE),
]

# Preference negation patterns — explicit user-preference signals that must NOT trigger.
_USER_PREFERENCE_PATTERNS = [
    re.compile(r"\b(?:which|what) (?:feature|color|theme|name|priority|task) (?:should|do you|would you)\b", re.IGNORECASE),
    re.compile(r"\b(?:do you want|would you prefer|what's your preference)\b", re.IGNORECASE),
    re.compile(r"\bauthor(?:ize|ization)\b", re.IGNORECASE),
    re.compile(r"\bdelete|remove|drop|push to (?:main|master|prod)", re.IGNORECASE),
]


def question_text_is_design_shaped(payload: dict[str, Any]) -> bool:
    """True (violation candidate) when the AskUserQuestion's question text matches
    design/architecture/scope/coverage patterns AND does NOT match user-preference
    or irreversible-authorization patterns.

    Conservative bias: false positives erode trust in IN-MASTER-4 warn faster than
    false negatives. Only high-confidence design-shape phrases match.

    Reads payload["tool_input"]["question"] (Claude Code AskUserQuestion schema).
    For multi-question calls (the schema supports a questions list), checks the
    first question's text only.

    Fail-open: absent question text returns False.
    """
    tool_input = payload.get("tool_input") or {}
    questions = tool_input.get("questions")
    if isinstance(questions, list) and questions:
        question_text = (questions[0] or {}).get("question", "") or ""
    else:
        question_text = tool_input.get("question", "") or ""

    if not question_text:
        return False

    for pat in _USER_PREFERENCE_PATTERNS:
        if pat.search(question_text):
            return False

    for pat in _DESIGN_SHAPE_PATTERNS:
        if pat.search(question_text):
            return True

    return False


# ---------------------------------------------------------------------------
# IN-MASTER-5 conditions (PARALLELIZE_INDEPENDENT_WORK)
# ---------------------------------------------------------------------------

# Minimum number of idle agents required to consider capacity "available".
# With < 2 idle, the current serial dispatch consumes all available agents.
_MIN_IDLE_FOR_CAPACITY = 2

# Tool names that constitute a task dispatch (IN-MASTER-5 scope).
_DISPATCH_TOOLS = frozenset(
    ["mcp__khimaira-chat__chat_task_create", "mcp__khimaira-chat__chat_send_to"]
)

# Window in seconds for detecting "batch" (parallel) dispatch.
_PARALLEL_WINDOW_S = 30


def idle_capacity_available(payload: dict[str, Any]) -> bool:
    """True if at least 2 idle agents exist in the master's current roster.

    Requires at least 2 (not just 1) so that a single-agent dispatch still
    leaves unused capacity — the scenario IN-MASTER-5 aims to flag.

    Reuses payload["idle_agents"] populated by the daemon (same key as
    idle_agents_exist). Fail-open: absent key returns False.
    """
    agents = payload.get("idle_agents")
    if agents is None:
        return False
    return len(agents) >= _MIN_IDLE_FOR_CAPACITY


def no_recent_parallel_dispatch(payload: dict[str, Any]) -> bool:
    """True (violation) when no parallel dispatch occurred in the last 30s.

    Inspects payload["recent_tool_calls"] for chat_task_create or chat_send_to
    calls within the last _PARALLEL_WINDOW_S seconds. If a prior dispatch
    exists in that window, master is already in batch mode — no violation.
    If this is the first (and only) dispatch in the window, it's serial mode —
    violation fires.

    Each entry is shaped as {"ts": "<iso>", "tool": str, "params": dict}.

    Fail-open: absent key returns False (can't confirm → don't fire).
    """
    recent = payload.get("recent_tool_calls")
    if recent is None:
        return False

    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(seconds=_PARALLEL_WINDOW_S)

    for call in recent:
        tool = call.get("tool", "")
        if tool not in _DISPATCH_TOOLS:
            continue
        ts_str = call.get("ts", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts >= cutoff:
            # Found a prior dispatch within the window — batch mode, no violation.
            return False

    # No prior dispatch found in the window — serial mode, violation fires.
    return True


_BATCH_WINDOW_S = 60
_CTX_ID_RE = re.compile(r"ctx-id:\s*(ctx-\S+)")


def recent_dispatch_different_ctx(payload: dict[str, Any]) -> bool:
    """True (violation) when a prior chat_task_create within _BATCH_WINDOW_S
    targets a different ctx-id than the current call.

    Catches the 'serial when could parallel' pattern: master fires task A
    (ctx-id ctx-foo), then within 60s fires task B (ctx-id ctx-bar). Same
    window of activity, different work units — could have been one parallel
    message. Distinct from IN-MASTER-5's no_recent_parallel_dispatch (which
    detects 'isolated single dispatch with no recent siblings').

    Body-parse approach: chat_task_create body is a markdown spec with a
    `ctx-id:` line near the top. Both calls must declare ctx-id for this
    check to fire — fail-open on absence.

    Fail-open: absent recent_tool_calls OR missing ctx-ids returns False.
    """
    recent = payload.get("recent_tool_calls")
    if recent is None:
        return False

    current_params = payload.get("tool_input", {}) or {}
    current_body = current_params.get("body", "") or ""
    m_cur = _CTX_ID_RE.search(current_body)
    if not m_cur:
        return False
    current_ctx = m_cur.group(1)

    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(seconds=_BATCH_WINDOW_S)

    for call in recent:
        if call.get("tool") != "mcp__khimaira-chat__chat_task_create":
            continue
        ts_str = call.get("ts", "")
        if not ts_str:
            continue
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if ts < cutoff:
            continue
        prior_body = (call.get("params") or {}).get("body", "") or ""
        m_prior = _CTX_ID_RE.search(prior_body)
        if m_prior and m_prior.group(1) != current_ctx:
            return True

    return False


# ---------------------------------------------------------------------------
# Registry — maps YAML condition name → function
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Any] = {
    "idle_agents_exist": idle_agents_exist,
    "chat_my_chats_not_called_this_turn": chat_my_chats_not_called_this_turn,
    "no_recent_top_tier_consult": no_recent_top_tier_consult,
    "idle_capacity_available": idle_capacity_available,
    "no_recent_parallel_dispatch": no_recent_parallel_dispatch,
    "recent_dispatch_different_ctx": recent_dispatch_different_ctx,
    "question_text_is_design_shaped": question_text_is_design_shaped,
}
