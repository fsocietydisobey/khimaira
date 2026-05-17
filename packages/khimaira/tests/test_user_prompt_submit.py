"""Tests for khimaira.hooks.user_prompt_submit — focused on _poll_missed_chat_events."""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SESSION_ID = "aaaaaaaa-0000-0000-0000-000000000001"
CHAT_ID = "chat-dfa8121d87b9"


@pytest.fixture
def hook_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    import khimaira.hooks.user_prompt_submit as m

    importlib.reload(m)
    return m


def _mock_urlopen(responses: list[dict]):
    """Return a context-manager mock that yields successive JSON payloads."""
    calls = iter(responses)

    class _Resp:
        def __init__(self, payload):
            self._data = json.dumps(payload).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def _urlopen(req, timeout=None):
        return _Resp(next(calls))

    return _urlopen


def test_poll_missed_chat_events_empty(hook_module):
    """No chats → empty string returned, no crash."""
    with patch(
        "khimaira.hooks.user_prompt_submit.urllib.request.urlopen",
        side_effect=_mock_urlopen([{"chats": []}]),
    ):
        result = hook_module._poll_missed_chat_events(SESSION_ID)
    assert result == ""


def test_poll_missed_chat_events_formats_correctly(hook_module):
    """Two new messages from another session → correct formatted block."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    ts1 = (now - timedelta(minutes=2)).isoformat()
    ts2 = (now - timedelta(minutes=1)).isoformat()

    chats_payload = {
        "chats": [{"chat_id": CHAT_ID, "title": "test chat", "my_state": "accepted"}]
    }
    messages_payload = {
        "messages": [
            {
                "kind": "msg",
                "event_id": "evt-001",
                "sender_id": "bbbbbbbb-0000-0000-0000-000000000002",
                "sender_name": "agent-2",
                "ts": ts1,
                "body": "first message",
            },
            {
                "kind": "msg",
                "event_id": "evt-002",
                "sender_id": "cccccccc-0000-0000-0000-000000000003",
                "sender_name": "agent-3",
                "ts": ts2,
                "body": "second message",
            },
        ]
    }

    with patch(
        "khimaira.hooks.user_prompt_submit.urllib.request.urlopen",
        side_effect=_mock_urlopen([chats_payload, messages_payload]),
    ):
        result = hook_module._poll_missed_chat_events(SESSION_ID)

    assert f"💬 MISSED CHAT EVENTS — {CHAT_ID} (2 new)" in result
    assert "agent-2" in result
    assert "first message" in result
    assert "agent-3" in result
    assert "second message" in result
