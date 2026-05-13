"""Tests for khimaira.hooks.subagent_stop — record subagent dispatches.

Coverage targets per repo CLAUDE.md "test the unhappy path before
shipping":
  - happy path: khimaira-* SubagentStop event → usage.jsonl row appended
  - unhappy: non-khimaira subagent_type → no record written
  - unhappy: missing transcript file → no crash, no record
  - unhappy: malformed stdin JSON → no crash, no record
  - round-trip: written record parses back as a valid UsageRecord
    with mode == "subagent"

Hook is invoked as a subprocess by Claude Code; tests exercise the
`main()` entry point directly by piping JSON to stdin via monkeypatch.
"""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path

import pytest

from khimaira_types import UsageRecord


# A realistic SubagentStop transcript line (from a 2026-05-13 session).
# Pruned to the fields the parser actually consults.
def _assistant_line(model: str, input_tokens: int, output_tokens: int,
                    cache_creation: int = 0, cache_read: int = 0) -> str:
    return json.dumps({
        "type": "assistant",
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cache_creation_input_tokens": cache_creation,
                "cache_read_input_tokens": cache_read,
            },
        },
    })


def _user_line(text: str = "what does X do?") -> str:
    return json.dumps({
        "type": "user",
        "message": {"role": "user", "content": text},
    })


@pytest.fixture
def hook_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Import khimaira.hooks.subagent_stop with XDG_STATE_HOME isolated.

    Reloaded per-test so module-level _LOG_DIR / _LOG_FILE pick up the
    tmp state-root.
    """
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.hooks import subagent_stop as mod
    importlib.reload(mod)
    yield mod, state_root
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(mod)


def _make_transcript(path: Path, lines: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _feed_stdin(monkeypatch: pytest.MonkeyPatch, payload: dict | str) -> None:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr("sys.stdin", io.StringIO(raw))


def _read_usage_rows(state_root: Path) -> list[dict]:
    log = state_root / "khimaira" / "usage.jsonl"
    if not log.is_file():
        return []
    return [json.loads(l) for l in log.read_text().splitlines() if l.strip()]


def test_happy_path_writes_one_record(hook_module, tmp_path, monkeypatch):
    """khimaira-factual stops → usage.jsonl gets one row with the right shape."""
    mod, state_root = hook_module
    transcript = tmp_path / "agent-abc.jsonl"
    _make_transcript(transcript, [
        _user_line(),
        _assistant_line("claude-haiku-4-5-20251001", input_tokens=3,
                        output_tokens=144, cache_creation=10965),
    ])
    _feed_stdin(monkeypatch, {
        "session_id": "parent-session-1",
        "agent_transcript_path": str(transcript),
        "cwd": "/tmp",
        "hook_event_name": "SubagentStop",
        "subagent_type": "khimaira-factual",
        "agent_id": "agent-abc",
    })

    assert mod.main() == 0

    rows = _read_usage_rows(state_root)
    assert len(rows) == 1
    r = rows[0]
    assert r["mode"] == "subagent"
    assert r["role"] == "khimaira-factual"
    assert r["model"] == "claude-haiku-4-5-20251001"
    assert r["runner"] == "claude"
    assert r["provider"] == "anthropic"
    # Cache tokens are now SEPARATE from input_tokens — cost math
    # uses the right multiplier for each bucket (#58).
    assert r["input_tokens"] == 3
    assert r["cache_creation_tokens"] == 10965
    assert r["cache_read_tokens"] == 0
    assert r["output_tokens"] == 144
    # Cost estimate non-zero (haiku has prices)
    assert r["estimated_cost_usd"] > 0
    assert r["task_id"] == "parent-session-1"


def test_non_khimaira_subagent_writes_nothing(hook_module, tmp_path, monkeypatch):
    """Explore / Plan / other built-in subagents are not our lane."""
    mod, state_root = hook_module
    transcript = tmp_path / "agent-xyz.jsonl"
    _make_transcript(transcript, [
        _user_line(),
        _assistant_line("claude-sonnet-4-6", input_tokens=100, output_tokens=200),
    ])
    _feed_stdin(monkeypatch, {
        "session_id": "parent-session-2",
        "agent_transcript_path": str(transcript),
        "subagent_type": "Explore",  # built-in, not khimaira-*
    })

    assert mod.main() == 0
    assert _read_usage_rows(state_root) == []


def test_missing_transcript_does_not_crash(hook_module, tmp_path, monkeypatch):
    """transcript_path pointing at a non-existent file → exit 0, no record."""
    mod, state_root = hook_module
    _feed_stdin(monkeypatch, {
        "session_id": "parent-session-3",
        "agent_transcript_path": str(tmp_path / "does-not-exist.jsonl"),
        "subagent_type": "khimaira-factual",
    })

    assert mod.main() == 0
    assert _read_usage_rows(state_root) == []


def test_malformed_stdin_does_not_crash(hook_module, monkeypatch):
    """Garbage on stdin → exit 0, no record."""
    mod, state_root = hook_module
    _feed_stdin(monkeypatch, "not-json-{{{")

    assert mod.main() == 0
    assert _read_usage_rows(state_root) == []


def test_empty_stdin_does_not_crash(hook_module, monkeypatch):
    """Empty stdin (e.g., piped from /dev/null) → exit 0, no record."""
    mod, state_root = hook_module
    _feed_stdin(monkeypatch, "")

    assert mod.main() == 0
    assert _read_usage_rows(state_root) == []


def test_transcript_with_no_assistant_turn_writes_nothing(hook_module, tmp_path, monkeypatch):
    """Subagent errored before producing output → nothing useful to record."""
    mod, state_root = hook_module
    transcript = tmp_path / "agent-empty.jsonl"
    _make_transcript(transcript, [_user_line()])
    _feed_stdin(monkeypatch, {
        "session_id": "parent",
        "agent_transcript_path": str(transcript),
        "subagent_type": "khimaira-research",
    })

    assert mod.main() == 0
    assert _read_usage_rows(state_root) == []


def test_multi_turn_transcript_sums_usage(hook_module, tmp_path, monkeypatch):
    """If a subagent has multiple assistant turns, usage sums across them
    and the model is captured from the last turn (single agent → same model).
    """
    mod, state_root = hook_module
    transcript = tmp_path / "agent-multi.jsonl"
    _make_transcript(transcript, [
        _user_line(),
        _assistant_line("claude-sonnet-4-6", input_tokens=50, output_tokens=100),
        _user_line("follow-up"),
        _assistant_line("claude-sonnet-4-6", input_tokens=20, output_tokens=80),
    ])
    _feed_stdin(monkeypatch, {
        "session_id": "p",
        "transcript_path": str(transcript),
        "subagent_type": "khimaira-research",
    })

    assert mod.main() == 0
    rows = _read_usage_rows(state_root)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 70
    assert rows[0]["output_tokens"] == 180


def test_round_trip_to_usage_record(hook_module, tmp_path, monkeypatch):
    """The hook-written JSON line parses back as a valid UsageRecord with
    mode == "subagent". Catches schema drift between hook and Pydantic model.
    """
    mod, state_root = hook_module
    transcript = tmp_path / "agent-rt.jsonl"
    _make_transcript(transcript, [
        _user_line(),
        _assistant_line("claude-opus-4-7", input_tokens=500, output_tokens=2000),
    ])
    _feed_stdin(monkeypatch, {
        "session_id": "parent",
        "agent_transcript_path": str(transcript),
        "subagent_type": "khimaira-deep-debug",
    })

    assert mod.main() == 0
    rows = _read_usage_rows(state_root)
    assert len(rows) == 1

    # Validate via Pydantic — same model the savings command reads through
    rec = UsageRecord.model_validate(rows[0])
    assert rec.mode == "subagent"
    assert rec.role == "khimaira-deep-debug"
    assert rec.model == "claude-opus-4-7"
    assert rec.input_tokens == 500
    assert rec.output_tokens == 2000


def test_agent_transcript_path_wins_over_transcript_path(hook_module, tmp_path, monkeypatch):
    """Regression — 2026-05-13 live verification revealed Claude Code's
    SubagentStop payload contains BOTH `transcript_path` (the PARENT
    session's transcript — millions of tokens) AND `agent_transcript_path`
    (the subagent's small transcript). The hook must read the latter,
    or the recorded usage matches the parent session and produces
    absurd costs (e.g. $421 for a one-sentence Haiku answer).
    """
    mod, state_root = hook_module
    # The "parent" transcript: large, on Opus (what we'd record by mistake)
    parent_transcript = tmp_path / "parent.jsonl"
    _make_transcript(parent_transcript, [
        _user_line(),
        _assistant_line("claude-opus-4-7", input_tokens=10_000_000,
                        output_tokens=100_000),
    ])
    # The "subagent" transcript: small, on Haiku (what we want)
    agent_transcript = tmp_path / "agent.jsonl"
    _make_transcript(agent_transcript, [
        _user_line(),
        _assistant_line("claude-haiku-4-5-20251001", input_tokens=20,
                        output_tokens=50),
    ])
    # Payload has BOTH fields, matching what Claude Code actually sends
    _feed_stdin(monkeypatch, {
        "session_id": "parent",
        "transcript_path": str(parent_transcript),
        "agent_transcript_path": str(agent_transcript),
        "subagent_type": "khimaira-factual",
    })

    assert mod.main() == 0
    rows = _read_usage_rows(state_root)
    assert len(rows) == 1
    # If the hook reads the wrong field, model would be opus and tokens
    # would be ~10M. If it reads the right field, model is haiku and
    # tokens are 70 total.
    assert rows[0]["model"] == "claude-haiku-4-5-20251001"
    assert rows[0]["input_tokens"] == 20
    assert rows[0]["output_tokens"] == 50


def test_falls_back_to_transcript_path_when_agent_path_absent(hook_module, tmp_path, monkeypatch):
    """If a future / older Claude Code version sends only `transcript_path`
    (no `agent_transcript_path`), fall back to it. Defensive — better to
    record approximate data than nothing."""
    mod, state_root = hook_module
    transcript = tmp_path / "agent.jsonl"
    _make_transcript(transcript, [
        _user_line(),
        _assistant_line("claude-haiku-4-5", input_tokens=10, output_tokens=20),
    ])
    _feed_stdin(monkeypatch, {
        "session_id": "parent",
        "transcript_path": str(transcript),  # only this field
        "subagent_type": "khimaira-factual",
    })

    assert mod.main() == 0
    rows = _read_usage_rows(state_root)
    assert len(rows) == 1
    assert rows[0]["input_tokens"] == 10


def test_subagent_type_field_alias(hook_module, tmp_path, monkeypatch):
    """Per Claude Code docs, the payload uses `subagent_type`; older docs
    mentioned `agent_type`. Hook accepts either as a defensive measure."""
    mod, state_root = hook_module
    transcript = tmp_path / "agent-alias.jsonl"
    _make_transcript(transcript, [
        _user_line(),
        _assistant_line("claude-haiku-4-5", input_tokens=10, output_tokens=20),
    ])
    _feed_stdin(monkeypatch, {
        "session_id": "parent",
        "agent_transcript_path": str(transcript),
        "agent_type": "khimaira-code-fast",  # alias-only path
    })

    assert mod.main() == 0
    rows = _read_usage_rows(state_root)
    assert len(rows) == 1
    assert rows[0]["role"] == "khimaira-code-fast"
