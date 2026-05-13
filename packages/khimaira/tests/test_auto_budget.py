"""Tests for the per-project budget gate on `mcp__khimaira__auto`.

`mcp__khimaira__delegate` and `mcp__khimaira__auto` both accept an
optional `project` + `budget_usd` pair. When set, the dispatch
checks accumulated spend for that project label in the last 30 days
and refuses if the cap has been met or exceeded.

This is a LOOSE gate — one in-flight call can overshoot — but it
cuts off the next call cleanly without requiring atomic accounting.

Tests verify:
  - project_spend_usd reads + sums correctly from usage.jsonl
  - records older than the window are excluded
  - records from a different project are excluded
  - missing log file → 0.0 spend, no crash
  - budget without project label → refusal
  - budget with project under cap → dispatch proceeds (mocked)
  - budget with project over cap → dispatch refused before runner
"""

from __future__ import annotations

import importlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest


@pytest.fixture
def isolated_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root usage.jsonl at a tmp path so tests don't trash real data."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    from khimaira import usage as usage_mod
    importlib.reload(usage_mod)
    yield usage_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(usage_mod)


def _write_record(
    log_path: Path,
    *,
    project: str | None,
    cost: float,
    ts: datetime | None = None,
) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    rec = {
        "ts": (ts or datetime.now(timezone.utc)).isoformat(),
        "task_id": project,
        "runner": "claude",
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "role": "delegate",
        "input_tokens": 1000,
        "output_tokens": 500,
        "latency_s": 1.0,
        "estimated_cost_usd": cost,
        "source": "cli",
        "mode": "auto",
        "escalation_count": 0,
    }
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec) + "\n")


def test_project_spend_empty_log_returns_zero(isolated_usage):
    """No log file → 0.0, no crash."""
    assert isolated_usage.project_spend_usd("any-project") == 0.0


def test_project_spend_sums_matching_records(isolated_usage):
    """Two records for same project → sum is correct."""
    log = isolated_usage.log_file_path()
    _write_record(log, project="proj-a", cost=0.10)
    _write_record(log, project="proj-a", cost=0.25)
    _write_record(log, project="proj-b", cost=1.00)  # different project

    assert isolated_usage.project_spend_usd("proj-a") == pytest.approx(0.35)
    assert isolated_usage.project_spend_usd("proj-b") == pytest.approx(1.00)
    assert isolated_usage.project_spend_usd("proj-c") == 0.0  # no records


def test_project_spend_excludes_records_outside_window(isolated_usage):
    """A 60-day-old record is excluded from a 30-day spend query."""
    log = isolated_usage.log_file_path()
    now = datetime.now(timezone.utc)
    _write_record(log, project="proj-a", cost=0.10, ts=now - timedelta(hours=1))
    _write_record(log, project="proj-a", cost=5.00, ts=now - timedelta(days=60))

    assert isolated_usage.project_spend_usd("proj-a", days=30) == pytest.approx(0.10)
    assert isolated_usage.project_spend_usd("proj-a", days=90) == pytest.approx(5.10)


def test_project_spend_ignores_null_task_id(isolated_usage):
    """A record with task_id=None doesn't match any project label."""
    log = isolated_usage.log_file_path()
    _write_record(log, project=None, cost=1.00)
    _write_record(log, project="proj-a", cost=0.10)

    assert isolated_usage.project_spend_usd("proj-a") == pytest.approx(0.10)


def test_project_spend_empty_label_returns_zero(isolated_usage):
    """Empty project string → 0.0 (defensive; can't attribute to nothing)."""
    log = isolated_usage.log_file_path()
    _write_record(log, project="proj-a", cost=0.10)

    assert isolated_usage.project_spend_usd("") == 0.0


def test_project_spend_handles_corrupt_lines(isolated_usage):
    """A malformed JSONL line doesn't crash the sum."""
    log = isolated_usage.log_file_path()
    _write_record(log, project="proj-a", cost=0.10)
    with log.open("a") as f:
        f.write("not-json-{\n")
    _write_record(log, project="proj-a", cost=0.25)

    assert isolated_usage.project_spend_usd("proj-a") == pytest.approx(0.35)


# ----------------- _delegate_impl budget gate ----------------- #


@pytest.mark.asyncio
async def test_budget_without_project_refused(isolated_usage, monkeypatch):
    """Setting budget_usd without project is a usage error — we'd have
    nothing to attribute future spend to."""
    from khimaira.server import mcp as mcp_mod

    result = await mcp_mod._delegate_impl(
        "what is 2+2?",
        tier="haiku",
        timeout_s=30,
        project="",
        budget_usd=5.0,
    )
    assert "budget_usd requires project label" in result


@pytest.mark.asyncio
async def test_budget_over_cap_refuses_before_dispatch(isolated_usage, monkeypatch):
    """If accumulated spend ≥ cap, refuse immediately without invoking a runner.
    Test verifies the refusal happens BEFORE any runner subprocess by
    making the runner mock raise — if it gets called, the test fails loud."""
    log = isolated_usage.log_file_path()
    _write_record(log, project="proj-a", cost=10.00)  # over the $5 cap

    from khimaira.server import mcp as mcp_mod

    # Sentinel — if any runner gets touched, this fails the test
    def _runner_must_not_be_called(*a, **kw):
        raise AssertionError("runner should not have been invoked — budget gate failed")

    monkeypatch.setattr(
        "khimaira.dispatch.runners.get_runner", _runner_must_not_be_called
    )

    result = await mcp_mod._delegate_impl(
        "anything",
        tier="haiku",
        timeout_s=30,
        project="proj-a",
        budget_usd=5.0,
    )
    assert "budget exhausted" in result
    assert "proj-a" in result
    assert "$10.0000" in result or "$10.00" in result
    assert "$5.0000" in result or "$5.00" in result


@pytest.mark.asyncio
async def test_budget_under_cap_proceeds(isolated_usage, monkeypatch):
    """If spend < cap, dispatch is attempted. Mock the runner to capture
    that we got past the budget gate without actually shelling out."""
    log = isolated_usage.log_file_path()
    _write_record(log, project="proj-a", cost=1.00)  # well under $5

    from khimaira.server import mcp as mcp_mod

    class _FakeRunner:
        def is_available(self):
            return True

        async def run(self, prompt, model=None, timeout=None):
            class _Result:
                text = "fake answer"
                model = "claude-haiku-4-5"
                input_tokens = 10
                output_tokens = 20
                latency_s = 0.1

            return _Result()

    monkeypatch.setattr(
        "khimaira.dispatch.runners.get_runner",
        lambda runner: _FakeRunner(),
    )

    result = await mcp_mod._delegate_impl(
        "test",
        tier="haiku",
        timeout_s=30,
        project="proj-a",
        budget_usd=5.0,
    )
    assert "budget exhausted" not in result
    assert "fake answer" in result


@pytest.mark.asyncio
async def test_no_budget_no_gate(isolated_usage, monkeypatch):
    """budget_usd=None bypasses the gate entirely — backward compat with
    existing callers that don't set a budget."""
    from khimaira.server import mcp as mcp_mod

    class _FakeRunner:
        def is_available(self):
            return True

        async def run(self, prompt, model=None, timeout=None):
            class _Result:
                text = "ok"
                model = "claude-haiku-4-5"
                input_tokens = 1
                output_tokens = 1
                latency_s = 0.01

            return _Result()

    monkeypatch.setattr(
        "khimaira.dispatch.runners.get_runner",
        lambda runner: _FakeRunner(),
    )

    # No project, no budget — should just proceed
    result = await mcp_mod._delegate_impl(
        "test",
        tier="haiku",
        timeout_s=30,
        project="",
        budget_usd=None,
    )
    assert "budget" not in result.lower()
    assert "ok" in result
