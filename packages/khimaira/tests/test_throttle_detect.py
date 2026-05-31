"""Tests for #13b-heavy terminal-overload detection (throttle_detect).

The load-bearing case is AC2 — a recovered turn (mid-loop 529s followed by a
trailing success) must NOT be flagged. A false positive here re-pokes/alerts
on a session that's working fine.
"""

from __future__ import annotations

import json
from pathlib import Path

from khimaira.hooks.throttle_detect import (
    detect_terminal_overload,
    _is_overload_529,
    _is_success,
)


def _overload_529(retry_attempt: int = 1, should_retry: str = "true") -> dict:
    """A real-shaped 529 overload api_error record."""
    return {
        "type": "system",
        "subtype": "api_error",
        "level": "error",
        "error": {
            "status": 529,
            "headers": {"x-should-retry": should_retry},
            "error": {
                "type": "error",
                "error": {"type": "overloaded_error", "message": "Overloaded. Retry."},
            },
            "type": "overloaded_error",
        },
        "retryAttempt": retry_attempt,
        "maxRetries": 10,
        "timestamp": f"2026-05-31T02:0{retry_attempt}:00.000Z",
    }


def _assistant() -> dict:
    return {"type": "assistant", "message": {"role": "assistant", "content": "done"}}


def _turn_duration() -> dict:
    return {"type": "system", "subtype": "turn_duration", "durationMs": 4200}


def _user_tool_result() -> dict:
    # CC writes tool results as type=="user" — this is NOT a success signal.
    return {"type": "user", "message": {"role": "user", "content": "tool_result"}}


def _auth_error() -> dict:
    return {
        "type": "system",
        "subtype": "api_error",
        "level": "error",
        "error": {
            "status": 401,
            "headers": {"x-should-retry": "false"},
            "type": "authentication_error",
        },
        "retryAttempt": 0,
        "maxRetries": 10,
        "timestamp": "2026-05-31T02:09:00.000Z",
    }


def _write_transcript(path: Path, records: list[dict]) -> str:
    path.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    return str(path)


# ─── AC1: terminal 529 storm → detected ──────────────────────────────────


def test_terminal_overload_detected(tmp_path):
    """Turn ends on an unrecovered 529 storm (no trailing success)."""
    records = [_user_tool_result()] + [_overload_529(i) for i in range(1, 11)]
    p = _write_transcript(tmp_path / "t.jsonl", records)

    verdict = detect_terminal_overload(p)

    assert verdict is not None
    assert verdict["terminal"] is True
    assert verdict["overload_count"] == 10
    assert verdict["retry_attempt"] == 10
    assert verdict["max_retries"] == 10
    assert verdict["message"] == "Overloaded. Retry."


# ─── AC2: recovered turn → NOT detected (critical false-positive guard) ───


def test_recovered_with_trailing_assistant_not_detected(tmp_path):
    """Mid-loop 529s but a trailing assistant record → CC recovered."""
    records = [_overload_529(i) for i in range(1, 6)] + [_assistant(), _turn_duration()]
    p = _write_transcript(tmp_path / "t.jsonl", records)

    assert detect_terminal_overload(p) is None


def test_recovered_with_trailing_turn_duration_not_detected(tmp_path):
    """turn_duration after the storm also means the turn completed."""
    records = [_overload_529(i) for i in range(1, 4)] + [_turn_duration()]
    p = _write_transcript(tmp_path / "t.jsonl", records)

    assert detect_terminal_overload(p) is None


def test_trailing_user_tool_result_does_not_count_as_recovery(tmp_path):
    """A type==user (tool-result) record after the 529 is NOT a success —
    detection must still fire (else real exhaustions get masked)."""
    records = [_overload_529(i) for i in range(1, 11)] + [_user_tool_result()]
    p = _write_transcript(tmp_path / "t.jsonl", records)

    verdict = detect_terminal_overload(p)
    assert verdict is not None and verdict["terminal"] is True


# ─── AC3: auth/billing error at turn end → NOT detected ───────────────────


def test_auth_error_not_detected(tmp_path):
    """A non-529 api_error (auth) at turn end is not a transient overload."""
    records = [_assistant(), _auth_error()]
    p = _write_transcript(tmp_path / "t.jsonl", records)

    assert detect_terminal_overload(p) is None


def test_529_without_should_retry_not_detected(tmp_path):
    """A 529 whose x-should-retry header is 'false' is not the retryable
    overload class — don't flag it."""
    records = [_overload_529(1, should_retry="false")]
    p = _write_transcript(tmp_path / "t.jsonl", records)

    assert detect_terminal_overload(p) is None


# ─── AC4: normal success → NOT detected ───────────────────────────────────


def test_normal_turn_not_detected(tmp_path):
    records = [_user_tool_result(), _assistant(), _turn_duration()]
    p = _write_transcript(tmp_path / "t.jsonl", records)

    assert detect_terminal_overload(p) is None


# ─── edge cases ───────────────────────────────────────────────────────────


def test_missing_path_returns_none():
    assert detect_terminal_overload(None) is None
    assert detect_terminal_overload("") is None
    assert detect_terminal_overload("/nonexistent/path.jsonl") is None


def test_empty_transcript_returns_none(tmp_path):
    p = _write_transcript(tmp_path / "t.jsonl", [])
    assert detect_terminal_overload(p) is None


def test_corrupt_lines_skipped(tmp_path):
    """Unparseable lines are skipped; a valid terminal 529 still detected."""
    p = tmp_path / "t.jsonl"
    p.write_text(
        "{not json\n" + json.dumps(_overload_529(10)) + "\n", encoding="utf-8"
    )
    verdict = detect_terminal_overload(str(p))
    assert verdict is not None and verdict["terminal"] is True


def test_tail_truncation_drops_partial_first_line(tmp_path):
    """With a tiny tail budget the partial leading line is dropped but the
    trailing terminal 529 is still found."""
    records = [_assistant()] * 50 + [_overload_529(i) for i in range(1, 11)]
    p = _write_transcript(tmp_path / "t.jsonl", records)

    verdict = detect_terminal_overload(p, tail_bytes=2048)
    assert verdict is not None and verdict["terminal"] is True


# ─── predicate unit checks ────────────────────────────────────────────────


def test_is_overload_529_predicate():
    assert _is_overload_529(_overload_529(1)) is True
    assert _is_overload_529(_auth_error()) is False
    assert _is_overload_529(_assistant()) is False
    assert _is_overload_529({"type": "system", "subtype": "api_error",
                             "error": {"status": 529}}) is True  # lenient: no headers


def test_is_success_predicate():
    assert _is_success(_assistant()) is True
    assert _is_success(_turn_duration()) is True
    assert _is_success(_user_tool_result()) is False
    assert _is_success(_overload_529(1)) is False
