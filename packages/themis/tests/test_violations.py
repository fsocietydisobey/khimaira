"""Tests for themis.violations — JSONL round-trip, GC, and compaction."""

from __future__ import annotations

import gzip
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from themis.data import ViolationRecord
from themis.violations import (
    _COMPACT_THRESHOLD_BYTES,
    append_violation,
    compact_if_needed,
    read_violations,
)


def _make_record(
    session_id: str = "sess-abc",
    session_name: str = "agent-1",
    role: str = "agent",
    rule_id: str = "IN-AGENT-2",
    tool_name: str = "Bash",
    tool_use_id: str = "toolu_001",
    decision: str = "blocked",
    ts: str | None = None,
    cwd: str = "/home/user/project",
) -> ViolationRecord:
    if ts is None:
        ts = datetime.now(tz=timezone.utc).isoformat()
    return ViolationRecord(
        ts=ts,
        session_id=session_id,
        session_name=session_name,
        role=role,
        rule_id=rule_id,
        tool_name=tool_name,
        tool_use_id=tool_use_id,
        tool_input_summary='{"command": "git commit --no-verify"}',
        decision=decision,
        cwd=cwd,
    )


def _ts_days_ago(days: int) -> str:
    return (datetime.now(tz=timezone.utc) - timedelta(days=days)).isoformat()


class TestAppendAndRead:
    def test_round_trip(self, violations_path: Path):
        rec = _make_record(session_id="s1", rule_id="IN-1")
        append_violation(rec, path=violations_path)
        results = read_violations(path=violations_path)
        assert len(results) == 1
        assert results[0].session_id == "s1"
        assert results[0].rule_id == "IN-1"

    def test_multiple_records_appended(self, violations_path: Path):
        for i in range(5):
            append_violation(_make_record(session_id=f"s{i}", rule_id=f"IN-{i}"), path=violations_path)
        results = read_violations(path=violations_path)
        assert len(results) == 5

    def test_empty_file_returns_empty_list(self, violations_path: Path):
        results = read_violations(path=violations_path)
        assert results == []

    def test_nonexistent_file_returns_empty_list(self, tmp_path: Path):
        p = tmp_path / "nonexistent.jsonl"
        assert read_violations(path=p) == []

    def test_most_recent_first(self, violations_path: Path):
        older_ts = _ts_days_ago(2)
        newer_ts = _ts_days_ago(0)
        append_violation(_make_record(session_id="old", ts=older_ts), path=violations_path)
        append_violation(_make_record(session_id="new", ts=newer_ts), path=violations_path)
        results = read_violations(path=violations_path)
        assert results[0].session_id == "new"

    def test_limit_honored(self, violations_path: Path):
        for i in range(10):
            append_violation(_make_record(session_id=f"s{i}"), path=violations_path)
        results = read_violations(limit=3, path=violations_path)
        assert len(results) == 3

    def test_filter_by_session_id(self, violations_path: Path):
        append_violation(_make_record(session_id="alpha"), path=violations_path)
        append_violation(_make_record(session_id="beta"), path=violations_path)
        results = read_violations(session_id="alpha", path=violations_path)
        assert len(results) == 1
        assert results[0].session_id == "alpha"

    def test_filter_by_role(self, violations_path: Path):
        append_violation(_make_record(role="intake"), path=violations_path)
        append_violation(_make_record(role="agent"), path=violations_path)
        results = read_violations(role="intake", path=violations_path)
        assert len(results) == 1
        assert results[0].role == "intake"

    def test_filter_by_since(self, violations_path: Path):
        old = _ts_days_ago(10)
        recent = _ts_days_ago(1)
        append_violation(_make_record(session_id="old", ts=old), path=violations_path)
        append_violation(_make_record(session_id="new", ts=recent), path=violations_path)
        since = _ts_days_ago(5)
        results = read_violations(since=since, path=violations_path)
        assert len(results) == 1
        assert results[0].session_id == "new"

    def test_creates_parent_dirs(self, tmp_path: Path):
        deep = tmp_path / "a" / "b" / "c" / "violations.jsonl"
        append_violation(_make_record(), path=deep)
        assert deep.exists()

    def test_corrupt_line_skipped(self, violations_path: Path):
        violations_path.parent.mkdir(parents=True, exist_ok=True)
        violations_path.write_text('{"ts": "bad"\n{"completely": "wrong"\n')
        # Should not crash, just return empty
        results = read_violations(path=violations_path)
        assert results == []


class TestCompaction:
    def test_no_compaction_below_threshold(self, violations_path: Path):
        append_violation(_make_record(), path=violations_path)
        ran = compact_if_needed(path=violations_path)
        assert ran is False

    def test_force_compaction_runs(self, violations_path: Path):
        append_violation(_make_record(), path=violations_path)
        ran = compact_if_needed(path=violations_path, force=True)
        assert ran is True

    def test_compaction_creates_archive(self, violations_path: Path):
        append_violation(_make_record(), path=violations_path)
        compact_if_needed(path=violations_path, force=True)
        archives = list(violations_path.parent.glob("themis_violations.*.jsonl.gz"))
        assert len(archives) == 1

    def test_archive_is_valid_gzip(self, violations_path: Path):
        rec = _make_record(session_id="archived-session")
        append_violation(rec, path=violations_path)
        compact_if_needed(path=violations_path, force=True)
        archives = list(violations_path.parent.glob("themis_violations.*.jsonl.gz"))
        with gzip.open(archives[0], "rt") as f:
            content = f.read()
        assert "archived-session" in content

    def test_compaction_removes_expired_entries(self, violations_path: Path):
        old_ts = _ts_days_ago(35)
        recent_ts = _ts_days_ago(1)
        for _ in range(3):
            append_violation(_make_record(session_id="old", ts=old_ts), path=violations_path)
        append_violation(_make_record(session_id="recent", ts=recent_ts), path=violations_path)
        compact_if_needed(path=violations_path, force=True)
        remaining = read_violations(path=violations_path)
        assert len(remaining) == 1
        assert remaining[0].session_id == "recent"

    def test_compaction_keeps_all_recent_entries(self, violations_path: Path):
        for i in range(5):
            append_violation(_make_record(session_id=f"s{i}", ts=_ts_days_ago(i)), path=violations_path)
        compact_if_needed(path=violations_path, force=True)
        remaining = read_violations(path=violations_path)
        assert len(remaining) == 5

    def test_all_expired_drops_all(self, violations_path: Path):
        for i in range(3):
            append_violation(_make_record(ts=_ts_days_ago(40)), path=violations_path)
        compact_if_needed(path=violations_path, force=True)
        remaining = read_violations(path=violations_path)
        assert remaining == []

    def test_compaction_triggered_at_threshold(self, violations_path: Path, monkeypatch):
        # Patch threshold to 0 so any write triggers compaction
        monkeypatch.setattr("themis.violations._COMPACT_THRESHOLD_BYTES", 0)
        append_violation(_make_record(), path=violations_path)
        # A second append should trigger compaction automatically
        append_violation(_make_record(), path=violations_path)
        archives = list(violations_path.parent.glob("themis_violations.*.jsonl.gz"))
        assert len(archives) >= 1

    def test_nonexistent_file_compaction_is_noop(self, tmp_path: Path):
        p = tmp_path / "nonexistent.jsonl"
        ran = compact_if_needed(path=p, force=True)
        assert ran is False
