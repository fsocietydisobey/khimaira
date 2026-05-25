"""Tests for themis.conditions — each named condition function."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from themis.conditions import (
    chat_my_chats_not_called_this_turn,
    evaluate_condition,
    idle_agents_exist,
    idle_capacity_available,
    no_recent_parallel_dispatch,
    no_recent_top_tier_consult,
    recent_dispatch_different_ctx,
)
from themis.data import Severity
from themis.engine import evaluate


class TestIdleAgentsExist:
    def test_returns_true_when_agents_idle(self):
        payload = {"idle_agents": [{"session_id": "x", "name": "agent-1", "status": "idle"}]}
        assert idle_agents_exist(payload) is True

    def test_returns_false_when_empty_list(self):
        assert idle_agents_exist({"idle_agents": []}) is False

    def test_returns_false_when_key_absent(self):
        assert idle_agents_exist({}) is False

    def test_returns_false_when_none(self):
        assert idle_agents_exist({"idle_agents": None}) is False

    def test_multiple_agents(self):
        payload = {
            "idle_agents": [
                {"session_id": "a", "name": "agent-1"},
                {"session_id": "b", "name": "agent-2"},
            ]
        }
        assert idle_agents_exist(payload) is True


class TestChatMyChatsNotCalledThisTurn:
    def _now(self) -> str:
        return datetime.now(tz=timezone.utc).isoformat()

    def _past(self, seconds: int = 60) -> str:
        return (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds)).isoformat()

    def _future(self, seconds: int = 60) -> str:
        return (datetime.now(tz=timezone.utc) + timedelta(seconds=seconds)).isoformat()

    def test_heartbeat_before_turn_returns_true(self):
        payload = {
            "subscriber_last_heartbeat": self._past(120),
            "turn_start_ts": self._past(60),
        }
        assert chat_my_chats_not_called_this_turn(payload) is True

    def test_heartbeat_after_turn_start_returns_false(self):
        payload = {
            "subscriber_last_heartbeat": self._past(10),
            "turn_start_ts": self._past(60),
        }
        assert chat_my_chats_not_called_this_turn(payload) is False

    def test_missing_heartbeat_returns_false(self):
        payload = {"turn_start_ts": self._past(30)}
        assert chat_my_chats_not_called_this_turn(payload) is False

    def test_missing_turn_start_returns_false(self):
        payload = {"subscriber_last_heartbeat": self._past(30)}
        assert chat_my_chats_not_called_this_turn(payload) is False

    def test_both_missing_returns_false(self):
        assert chat_my_chats_not_called_this_turn({}) is False

    def test_invalid_timestamp_returns_false(self):
        payload = {
            "subscriber_last_heartbeat": "not-a-timestamp",
            "turn_start_ts": self._past(30),
        }
        assert chat_my_chats_not_called_this_turn(payload) is False

    def test_naive_datetimes_handled(self):
        # Naive datetime (no tzinfo) should work — treated as UTC
        payload = {
            "subscriber_last_heartbeat": "2026-05-21T10:00:00",
            "turn_start_ts": "2026-05-21T11:00:00",
        }
        assert chat_my_chats_not_called_this_turn(payload) is True


class TestNoRecentTopTierConsult:
    def test_returns_true_with_no_consult(self):
        """Empty tool-call history → True (violation: no top-tier consult found)."""
        payload = {"recent_tool_calls": []}
        assert no_recent_top_tier_consult(payload) is True

    def test_returns_false_with_architect_consult(self):
        """chat_send_to targeting architect-1 (list shape) → False (consult found)."""
        payload = {
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira-chat__chat_send_to",
                    "params": {"to": ["architect-1"], "body": "design question"},
                }
            ]
        }
        assert no_recent_top_tier_consult(payload) is False

    def test_returns_false_with_critic_consult(self):
        """chat_send_to targeting critic-1 (list shape) → False (critic is top-tier)."""
        payload = {
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira-chat__chat_send_to",
                    "params": {"to": ["critic-1"], "body": "review this"},
                }
            ]
        }
        assert no_recent_top_tier_consult(payload) is False

    def test_returns_true_with_only_agent_consult(self):
        """chat_send_to targeting agent-2 (list shape) → True (agent not top-tier)."""
        payload = {
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira-chat__chat_send_to",
                    "params": {"to": ["agent-2"], "body": "task"},
                }
            ]
        }
        assert no_recent_top_tier_consult(payload) is True

    def test_returns_false_when_key_absent(self):
        """Absent recent_tool_calls → False (fail-open: can't confirm, don't fire)."""
        assert no_recent_top_tier_consult({}) is False

    def test_returns_false_with_session_log_question_to_verifier(self):
        """session_log_question targeting verifier-1 also counts as a top-tier consult."""
        payload = {
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira__session_log_question",
                    "params": {"target_session_id": "verifier-1", "text": "coverage check"},
                }
            ]
        }
        assert no_recent_top_tier_consult(payload) is False

    def test_returns_false_with_to_list_containing_top_tier(self):
        """to list with multiple members — top-tier member found → False (no violation)."""
        payload = {
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira-chat__chat_send_to",
                    "params": {"to": ["architect-1", "joseph-bot"], "body": "q"},
                }
            ]
        }
        assert no_recent_top_tier_consult(payload) is False

    def test_returns_true_with_to_list_no_top_tier(self):
        """to list with only non-top-tier members → True (violation fires)."""
        payload = {
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira-chat__chat_send_to",
                    "params": {"to": ["agent-2"], "body": "task"},
                }
            ]
        }
        assert no_recent_top_tier_consult(payload) is True

    def test_returns_false_with_jp_prefixed_architect(self):
        """jp-architect-1 (roster-prefixed) → False (segment detection handles prefix)."""
        payload = {
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira-chat__chat_send_to",
                    "params": {"to": ["jp-architect-1"], "body": "design q"},
                }
            ]
        }
        assert no_recent_top_tier_consult(payload) is False


class TestIdleCapacityAvailable:
    def _agent(self, name: str) -> dict:
        return {"session_id": name, "name": name, "status": "idle"}

    def test_returns_true_with_2_idle_agents(self):
        payload = {"idle_agents": [self._agent("agent-1"), self._agent("agent-2")]}
        assert idle_capacity_available(payload) is True

    def test_returns_true_with_more_than_2_idle_agents(self):
        payload = {
            "idle_agents": [self._agent("agent-1"), self._agent("agent-2"), self._agent("agent-3")]
        }
        assert idle_capacity_available(payload) is True

    def test_returns_false_with_1_idle_agent(self):
        payload = {"idle_agents": [self._agent("agent-1")]}
        assert idle_capacity_available(payload) is False

    def test_returns_false_with_no_idle_agents(self):
        assert idle_capacity_available({"idle_agents": []}) is False

    def test_returns_false_when_key_absent(self):
        assert idle_capacity_available({}) is False

    def test_returns_false_when_none(self):
        assert idle_capacity_available({"idle_agents": None}) is False


class TestNoRecentParallelDispatch:
    def _ts(self, seconds_ago: float = 0) -> str:
        return (datetime.now(tz=timezone.utc) - timedelta(seconds=seconds_ago)).isoformat()

    def _dispatch_call(self, seconds_ago: float = 5, tool: str = "mcp__khimaira-chat__chat_task_create") -> dict:
        return {"ts": self._ts(seconds_ago), "tool": tool, "params": {"body": "task"}}

    def test_returns_true_when_no_recent_dispatch(self):
        """No prior dispatch in recent_tool_calls → serial mode → violation fires."""
        payload = {"recent_tool_calls": []}
        assert no_recent_parallel_dispatch(payload) is True

    def test_returns_true_when_only_non_dispatch_tools(self):
        """Only non-dispatch tools in recent history → serial mode → violation fires."""
        payload = {
            "recent_tool_calls": [
                {"ts": self._ts(10), "tool": "Read", "params": {}},
                {"ts": self._ts(5), "tool": "AskUserQuestion", "params": {}},
            ]
        }
        assert no_recent_parallel_dispatch(payload) is True

    def test_returns_false_when_recent_chat_task_create(self):
        """Prior chat_task_create within 30s → batch mode → no violation."""
        payload = {"recent_tool_calls": [self._dispatch_call(seconds_ago=10)]}
        assert no_recent_parallel_dispatch(payload) is False

    def test_returns_false_when_recent_chat_send_to(self):
        """Prior chat_send_to within 30s → batch mode → no violation."""
        payload = {
            "recent_tool_calls": [
                self._dispatch_call(seconds_ago=8, tool="mcp__khimaira-chat__chat_send_to")
            ]
        }
        assert no_recent_parallel_dispatch(payload) is False

    def test_returns_true_when_dispatch_outside_window(self):
        """Prior dispatch outside 30s window → treated as stale → violation fires."""
        payload = {"recent_tool_calls": [self._dispatch_call(seconds_ago=60)]}
        assert no_recent_parallel_dispatch(payload) is True

    def test_returns_false_when_key_absent(self):
        """Absent recent_tool_calls → fail-open → no violation."""
        assert no_recent_parallel_dispatch({}) is False

    def test_returns_false_when_recent_tool_calls_is_none(self):
        """None value for recent_tool_calls → fail-open → no violation."""
        assert no_recent_parallel_dispatch({"recent_tool_calls": None}) is False

    def test_skips_entries_with_missing_ts(self):
        """Entry missing ts field → skipped, no false positive."""
        payload = {
            "recent_tool_calls": [
                {"tool": "mcp__khimaira-chat__chat_task_create", "params": {}}
            ]
        }
        assert no_recent_parallel_dispatch(payload) is True


class TestINMASTER5Integration:
    """Integration: IN-MASTER-5 fires via evaluate() with real YAML + conditions."""

    def _idle_agent(self, name: str) -> dict:
        return {"session_id": name, "name": name, "status": "idle"}

    def test_fires_when_serial_dispatch_with_idle_capacity(self):
        """chat_task_create + 2 idle agents + no recent dispatch → IN-MASTER-5 warn."""
        conditions_payload = {
            "idle_agents": [self._idle_agent("agent-1"), self._idle_agent("agent-2")],
            "recent_tool_calls": [],  # no prior dispatch in window
        }
        result = evaluate("master", "mcp__khimaira-chat__chat_task_create", {}, conditions_payload=conditions_payload)
        assert result.ok is False
        assert result.violation.rule_id == "IN-MASTER-5"
        assert result.violation.severity == Severity.WARN

    def test_does_not_fire_when_recent_parallel_dispatch(self):
        """chat_task_create + prior dispatch within 30s → batch mode → no violation."""
        recent_ts = (datetime.now(tz=timezone.utc) - timedelta(seconds=10)).isoformat()
        conditions_payload = {
            "idle_agents": [self._idle_agent("agent-1"), self._idle_agent("agent-2")],
            "recent_tool_calls": [
                {"ts": recent_ts, "tool": "mcp__khimaira-chat__chat_task_create", "params": {}}
            ],
        }
        result = evaluate("master", "mcp__khimaira-chat__chat_task_create", {}, conditions_payload=conditions_payload)
        assert result.ok is True

    def test_does_not_fire_with_only_1_idle_agent(self):
        """chat_task_create + only 1 idle agent → no spare capacity → no violation."""
        conditions_payload = {
            "idle_agents": [self._idle_agent("agent-1")],
            "recent_tool_calls": [],
        }
        result = evaluate("master", "mcp__khimaira-chat__chat_task_create", {}, conditions_payload=conditions_payload)
        assert result.ok is True


class TestEvaluateCondition:
    def test_known_condition_dispatches(self):
        payload = {"idle_agents": [{"session_id": "x"}]}
        assert evaluate_condition("idle_agents_exist", payload) is True

    def test_unknown_condition_returns_false(self):
        assert evaluate_condition("no_such_condition_xyz", {}) is False

    def test_exception_in_condition_returns_false(self):
        # Deliberately pass a non-dict payload to trigger an internal error
        # (chat_my_chats_not_called_this_turn will try .get() on None)
        assert evaluate_condition("idle_agents_exist", None) is False  # type: ignore[arg-type]


class TestRecentDispatchDifferentCtx:
    def test_fires_on_pair_with_different_ctx(self):
        """Two chat_task_create within 60s with different ctx-ids → fires."""
        payload = {
            "tool_input": {"body": "ctx-id: ctx-bar\nsome other content"},
            "recent_tool_calls": [
                {
                    "ts": (datetime.now(tz=timezone.utc) - timedelta(seconds=10)).isoformat(),
                    "tool": "mcp__khimaira-chat__chat_task_create",
                    "params": {"body": "ctx-id: ctx-foo\nprior content"},
                }
            ],
        }
        assert recent_dispatch_different_ctx(payload) is True

    def test_skips_same_ctx(self):
        """Two chat_task_create with SAME ctx-id (retry) → does not fire."""
        payload = {
            "tool_input": {"body": "ctx-id: ctx-foo\nretry attempt"},
            "recent_tool_calls": [
                {
                    "ts": (datetime.now(tz=timezone.utc) - timedelta(seconds=10)).isoformat(),
                    "tool": "mcp__khimaira-chat__chat_task_create",
                    "params": {"body": "ctx-id: ctx-foo\noriginal"},
                }
            ],
        }
        assert recent_dispatch_different_ctx(payload) is False

    def test_skips_outside_window(self):
        """Prior dispatch >60s ago → does not fire."""
        payload = {
            "tool_input": {"body": "ctx-id: ctx-bar"},
            "recent_tool_calls": [
                {
                    "ts": (datetime.now(tz=timezone.utc) - timedelta(seconds=90)).isoformat(),
                    "tool": "mcp__khimaira-chat__chat_task_create",
                    "params": {"body": "ctx-id: ctx-foo"},
                }
            ],
        }
        assert recent_dispatch_different_ctx(payload) is False
