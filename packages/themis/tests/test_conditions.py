"""Tests for themis.conditions — each named condition function."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from themis.conditions import (
    chat_my_chats_not_called_this_turn,
    evaluate_condition,
    idle_agents_exist,
)


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
