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


# Sessions at turn ≥ BOOTSTRAP_GRACE_TURNS that have never stamped a
# heartbeat are flagged as never-registered (IN-MASTER-1).  Sessions
# younger than this are assumed to be genuinely bootstrapping.
BOOTSTRAP_GRACE_TURNS: int = 3


def _get_session_tool_call_count(session_id: str) -> int | None:
    """Count entries in the session's tool_calls.jsonl as a turn-age proxy.

    Returns None if the file is absent (caller treats as bootstrapping).
    Reads the XDG_STATE_HOME path directly to avoid a cross-package import.
    """
    try:
        import os
        from pathlib import Path

        state_root = Path(
            os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")
        )
        tool_calls_path = (
            state_root / "khimaira" / "sessions" / session_id / "tool_calls.jsonl"
        )
        if not tool_calls_path.is_file():
            return None
        with tool_calls_path.open(encoding="utf-8") as f:
            return sum(1 for line in f if line.strip())
    except Exception:
        return None


def chat_my_chats_not_called_this_turn(payload: dict[str, Any]) -> bool:
    """True only if the heartbeat predates this turn AND no chat_my_chats in recent_tool_calls.

    Two-path pass:
      1. Fresh heartbeat (>= turn_start) → False (session is subscribed).
      2. Stale heartbeat BUT chat_my_chats appears in recent_tool_calls with
         ts >= turn_start → False (session self-healed this turn via GET /chats).

    Violation (True) only when BOTH conditions hold: heartbeat is stale AND
    no chat_my_chats was called this turn.

    Never-registered path: absent heartbeat + session old enough (tool call count
    >= BOOTSTRAP_GRACE_TURNS) → True (never subscribed, not bootstrapping).

    Expects:
      payload["session_id"]: the session being checked (used for never-registered path).
      payload["subscriber_last_heartbeat"]: ISO-8601 timestamp of last heartbeat.
      payload["turn_start_ts"]: ISO-8601 turn start (UserPromptSubmit hook).
      payload["recent_tool_calls"]: list of {ts, tool, params} dicts (optional).

    Fail-open: absent heartbeat or turn_start → False unless never-registered.
    Absent recent_tool_calls → fall through to heartbeat-only check.
    """
    heartbeat_str = payload.get("subscriber_last_heartbeat")
    turn_start_str = payload.get("turn_start_ts")
    if not heartbeat_str or not turn_start_str:
        # Never-registered path: session has been active long enough but never stamped.
        session_id = payload.get("session_id")
        if session_id:
            tool_count = _get_session_tool_call_count(session_id)
            if tool_count is not None and tool_count >= BOOTSTRAP_GRACE_TURNS:
                return True  # never registered — flag as violation
        return False  # genuinely bootstrapping or can't determine age
    try:
        heartbeat = datetime.fromisoformat(heartbeat_str)
        turn_start = datetime.fromisoformat(turn_start_str)
        # Normalize to UTC for comparison
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        if turn_start.tzinfo is None:
            turn_start = turn_start.replace(tzinfo=timezone.utc)
        if heartbeat >= turn_start:
            return False  # heartbeat is fresh — no violation
        # Heartbeat is stale. Check recent_tool_calls for a chat_my_chats this turn.
        recent = payload.get("recent_tool_calls")
        if recent:
            for call in recent:
                tool = call.get("tool", "")
                if "chat_my_chats" not in tool:
                    continue
                ts_str = call.get("ts")
                if not ts_str:
                    continue
                try:
                    ts = datetime.fromisoformat(ts_str)
                    if ts.tzinfo is None:
                        ts = ts.replace(tzinfo=timezone.utc)
                    if ts >= turn_start:
                        return False  # session called chat_my_chats this turn
                except ValueError:
                    continue
        return True
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


def done_report_missing_branch_declaration(payload: dict[str, Any]) -> bool:
    """True (violation) when chat_task_update with status=done has a note
    missing required branch declaration fields.

    Required fields per agent.md done-report template (2026-05-26):
    - branch:
    - worktree:
    - merge_intent:

    Catches the JEEVY-543 silent-strand failure mode: agent posts done
    without declaring git state; master can't audit arc-end coherence.

    Note-parse approach: chat_task_update note is markdown with field lines
    like `branch: foo`. All three field labels must appear.

    Fail-open: absent note, status != done, or non-task-update tool → False.
    """
    tool_name = payload.get("tool_name", "")
    if tool_name != "mcp__khimaira-chat__chat_task_update":
        return False

    tool_input = payload.get("tool_input") or {}
    status = tool_input.get("new_status", "") or tool_input.get("status", "")
    if status != "done":
        return False

    note = tool_input.get("note", "") or ""
    if not note:
        return False

    # All three labels must appear (case-sensitive — these are conventional)
    required_labels = ["branch:", "worktree:", "merge_intent:"]
    return not all(label in note for label in required_labels)


def assignee_not_ready(payload: dict[str, Any]) -> bool:
    """True when the task's assignee is NOT fully ready to start.

    Used by IN-MASTER-8 (BEGIN_BEFORE_READY) to warn master before firing BEGIN
    to an unprepared assignee. "Ready" requires ALL of:
      - state == accepted in the chat
      - subscriber_last_heartbeat >= turn_start_ts (SSE is fresh this turn)
      - a ✅ ready [task-id] message from the assignee in recent chat events

    Fail-open: if assignee_readiness is absent from payload → False (don't fire).
    This is a warn rule; the missing-payload case is not a crisis.
    """
    readiness = payload.get("assignee_readiness")
    if not readiness:
        return False
    # Fire (True = not ready) when ANY component is False
    return not (
        readiness.get("accepted", False)
        and readiness.get("heartbeat_fresh", False)
        and readiness.get("ready_ack", False)
    )


def file_is_code(payload: dict[str, Any]) -> bool:
    """True if the tool's file_path has a code-file extension.

    Used by IN-MASTER-7 (NO_DIRECT_CODE_IMPLEMENTATION) to gate master
    on Edit/Write calls that target production source files. Returns False
    for non-edit tools or absent file_path.

    Fail-open: if tool_input is absent or file_path is empty → False (don't fire).
    """
    tool_input = payload.get("tool_input") or {}
    file_path = tool_input.get("file_path") or ""
    if not file_path:
        return False
    _CODE_EXTENSIONS = frozenset({
        ".py", ".ts", ".tsx", ".js", ".jsx", ".vue", ".svelte",
        ".go", ".rs", ".rb", ".java", ".kt", ".swift", ".c", ".cpp",
        ".h", ".hpp", ".cs", ".php", ".sh", ".bash",
    })
    import os
    _ext = os.path.splitext(file_path)[-1].lower()
    return _ext in _CODE_EXTENSIONS


# ---------------------------------------------------------------------------
# Registry — maps YAML condition name → function
# ---------------------------------------------------------------------------

def path_touched_by_other_live_session(payload: dict[str, Any]) -> bool:
    """True when another live session recently touched one of the files being edited.

    Used by IN-AGENT-N PATH_CONTENTION (Guard-2) to warn before concurrent edits.
    Reads payload["concurrent_touchers"] — a list of {session_id, session_name,
    touch_ts, file_path} dicts populated by the Guard-2 enrichment in themis_check.
    Non-empty list = contention detected → rule fires (warn).

    Fail-open: absent/malformed key → False (no warn).
    """
    touchers = payload.get("concurrent_touchers")
    if not isinstance(touchers, list):
        return False
    return len(touchers) > 0


_REGISTRY: dict[str, Any] = {
    "idle_agents_exist": idle_agents_exist,
    "chat_my_chats_not_called_this_turn": chat_my_chats_not_called_this_turn,
    "no_recent_top_tier_consult": no_recent_top_tier_consult,
    "idle_capacity_available": idle_capacity_available,
    "no_recent_parallel_dispatch": no_recent_parallel_dispatch,
    "recent_dispatch_different_ctx": recent_dispatch_different_ctx,
    "question_text_is_design_shaped": question_text_is_design_shaped,
    "done_report_missing_branch_declaration": done_report_missing_branch_declaration,
    "file_is_code": file_is_code,
    "assignee_not_ready": assignee_not_ready,
    "path_touched_by_other_live_session": path_touched_by_other_live_session,
}

# gate_verdicts_incomplete registered below (defined after _REGISTRY to avoid forward-ref)


_GATE_SENTINEL = object()  # distinguishes "key not in payload" from "key=None"


def gate_verdicts_incomplete(payload: dict[str, Any]) -> bool:
    """True when the current task's commit gate is NOT satisfied.

    Gate-satisfied = the enrichment's `committable` flag (single source of truth):
    lean N-distinct-gatekeeper-ship OR legacy critic-APPROVE + verifier-SHIP.

    Implements B3's Joseph-ruled semantics (exact tri-state):
      - no gate_verdicts key in payload → False (fail-open; enrichment didn't run)
      - gate_verdicts is None → False (no active task → ad-hoc commit; allow)
      - "absent" → True (BLOCK: task found, no verdicts yet)
      - "error" → True (BLOCK: verdict lookup failed; fail toward loud/recoverable)
      - dict present + committable=True → False (allow)
      - dict present + committable=False → True (BLOCK: gate not satisfied)

    The daemon-unreachable case is NOT handled here — themis_pretool.py
    fail-opens on URLError before reaching this condition (D7 / #61 axis-A).
    """
    gate = payload.get("gate_verdicts", _GATE_SENTINEL)
    if gate is _GATE_SENTINEL:
        return False  # enrichment didn't run (wrong tool/role) → fail-open
    if gate is None:
        return False  # no active task → ad-hoc commit/approve → allow
    if gate == "absent":
        return True   # task found but no verdicts → block
    if gate == "error":
        return True   # enrichment error → fail closed (Joseph's ruling)
    if isinstance(gate, dict):
        # SINGLE SOURCE OF TRUTH: `committable` (chats._is_committable) covers BOTH the
        # lean N-distinct-gatekeeper-ship gate AND the legacy critic-approve+verifier-ship
        # dual. Fall back to the legacy pair only for a pre-change payload that predates
        # the `committable` field (defensive; in-process enrichment always emits it now).
        if "committable" in gate:
            return not gate["committable"]
        return not (gate.get("critic_approved") and gate.get("verifier_shipped"))
    return True  # unknown shape → fail closed

_REGISTRY["gate_verdicts_incomplete"] = gate_verdicts_incomplete


_ASSIGN_SENTINEL = object()


def agent_edit_without_assigned_task(payload: dict[str, Any]) -> bool:
    """True (warn) when an agent edits files while holding NO in_progress assigned task.

    The self-dispatch / BEGIN-gate-jump footgun (jeevy-agent-2, 2026-06-21): an eager
    agent treats visible-needed-work in the roster chat as a cue to edit BEFORE master
    assigns a task and signals start. Reads payload['has_in_progress_assignment'] (bool)
    set by the IN-AGENT-7 enrichment for edit tools. Tri-state, fail-open:
      - key absent → enrichment didn't run (non-edit tool / error) → False (no warn)
      - True  → agent holds an active assignment → False (no warn — legitimate work)
      - False → no active assignment → True (WARN: propose + wait for the task)

    WARN, not block: autonomous agent initiative is a WANTED capability — this nudges
    the agent to route its initiative through assignment + gate, it does not lock the
    agent to propose-only. Master still captures good unsolicited work into a gated task.
    """
    v = payload.get("has_in_progress_assignment", _ASSIGN_SENTINEL)
    if v is _ASSIGN_SENTINEL:
        return False  # enrichment didn't run → fail-open (no warn)
    return v is False


_REGISTRY["agent_edit_without_assigned_task"] = agent_edit_without_assigned_task
