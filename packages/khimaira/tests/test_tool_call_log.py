"""Tests for sessions.log_tool_call + recent_tool_calls (SLICE-D).

Verifies the ring-buffer semantics that feed the Themis IN-MASTER-4 rule:
  - Each call is appended with the correct shape ({ts, tool, params}).
  - recent_tool_calls returns at most `limit` entries, newest-last.
  - Fresh session returns [].
  - Cap at 100: logging 150 calls leaves exactly 100 on disk (oldest dropped).
"""

from __future__ import annotations

import json

import pytest


def test_log_tool_call_appends_to_jsonl(isolated_state):
    """log_tool_call writes a {ts, tool, params} entry to tool_calls.jsonl."""
    sid = "test-session-tool-log"
    isolated_state.log_tool_call(sid, "Edit", {"file_path": "/tmp/foo.py"})

    path = isolated_state._session_dir(sid) / "tool_calls.jsonl"
    assert path.exists()
    entries = [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["tool"] == "Edit"
    assert entry["params"] == {"file_path": "/tmp/foo.py"}
    assert "ts" in entry


def test_recent_tool_calls_returns_last_n_in_order(isolated_state):
    """Log 30 calls; recent_tool_calls(limit=20) returns the last 20, oldest-first."""
    sid = "test-session-recent"
    for i in range(30):
        isolated_state.log_tool_call(sid, f"Tool{i}", {"i": i})

    result = isolated_state.recent_tool_calls(sid, limit=20)
    assert len(result) == 20
    # Entries should be oldest-first within the returned window (last 20 of 30).
    params_i = [e["params"]["i"] for e in result]
    assert params_i == list(range(10, 30))


def test_recent_tool_calls_empty_for_no_history(isolated_state):
    """A fresh session with no tool_calls.jsonl returns []."""
    sid = "test-session-empty"
    result = isolated_state.recent_tool_calls(sid)
    assert result == []


def test_log_tool_call_truncates_at_100(isolated_state):
    """After logging 150 calls, tool_calls.jsonl contains exactly the last 100."""
    sid = "test-session-cap"
    for i in range(150):
        isolated_state.log_tool_call(sid, "Noop", {"i": i})

    path = isolated_state._session_dir(sid) / "tool_calls.jsonl"
    lines = [ln for ln in path.read_text().splitlines() if ln.strip()]
    assert len(lines) == 100

    # Verify the kept entries are the LAST 100 (i=50..149).
    entries = [json.loads(ln) for ln in lines]
    params_i = [e["params"]["i"] for e in entries]
    assert params_i == list(range(50, 150))
