"""End-to-end tests for `mcp__khimaira__delegate` and `mcp__khimaira__auto`.

Unit tests (test_pool_router.py, test_auto_budget.py) cover the routing
and budget logic with mocked runners. These tests exercise the FULL
dispatch path against a real CLI runner subprocess — classifier →
pool router → runner.run() → usage.jsonl write — so we catch regressions
that mocks miss:
  - the runner's CLI invocation actually works on this machine
  - tokens come back populated (not zero or None)
  - usage.jsonl gets a real row with the right shape
  - mode="auto" / "explicit-tier" land correctly

Strategy: probe `available_runners()` at module import time, run each
test against whichever cheapest runner exists. Ollama is preferred
(local, free, fast); claude is next; we skip everything else to keep
the test suite cheap on CI.

CI typically has no runners installed → every test in this file skips
cleanly via `@pytest.mark.integration` + the runner-availability check.

Run locally with: `pytest -m integration packages/khimaira/tests/test_delegate_auto_e2e.py`
"""

from __future__ import annotations

import importlib
import json
import os
from pathlib import Path

import pytest


pytestmark = pytest.mark.integration


def _runner_available(name: str) -> bool:
    """Cheap check — does the runner's CLI exist on PATH?"""
    from khimaira.dispatch.runners import get_runner

    try:
        return get_runner(name).is_available()
    except Exception:
        return False


def _pick_cheapest_available_runner() -> tuple[str, str] | None:
    """Return (runner_name, tier) for the cheapest runner installed,
    or None if no runner is available."""
    candidates = [
        ("ollama", "local"),
        ("claude", "haiku"),
        ("gemini", "flash"),
    ]
    for runner_name, tier in candidates:
        if _runner_available(runner_name):
            return runner_name, tier
    return None


_PICK = _pick_cheapest_available_runner()
_HAS_ANY_RUNNER = _PICK is not None


@pytest.fixture
def isolated_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root usage.jsonl so E2E tests don't pollute real usage data."""
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    from khimaira import usage as usage_mod

    importlib.reload(usage_mod)
    yield usage_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(usage_mod)


def _read_usage_records(log_path: Path) -> list[dict]:
    if not log_path.is_file():
        return []
    return [json.loads(l) for l in log_path.read_text().splitlines() if l.strip()]


# ---------------- E2E: explicit-tier path ---------------- #


@pytest.mark.skipif(not _HAS_ANY_RUNNER, reason="no runner installed on this machine")
async def test_delegate_explicit_tier_e2e(isolated_usage):
    """Real dispatch on the cheapest installed runner → answer comes back +
    usage.jsonl gets one row with mode='explicit-tier'."""
    from khimaira.server.mcp import _delegate_impl

    _, tier = _PICK
    result = await _delegate_impl(
        "Reply with exactly the word: pong",
        tier=tier,
        timeout_s=60,
    )

    # Result includes the dispatch header + the model's answer
    assert "❌" not in result, f"dispatch failed: {result}"
    assert "mode=explicit-tier" in result

    # Exactly one usage record landed
    rows = _read_usage_records(isolated_usage.log_file_path())
    assert len(rows) == 1
    r = rows[0]
    assert r["mode"] == "explicit-tier"
    assert r["model"]  # non-empty
    assert r["input_tokens"] > 0 or r["output_tokens"] > 0  # at least one direction


@pytest.mark.skipif(not _HAS_ANY_RUNNER, reason="no runner installed on this machine")
async def test_delegate_with_project_label_e2e(isolated_usage):
    """Project label flows into task_id; budget gate is permissive when
    cap is far above zero."""
    from khimaira.server.mcp import _delegate_impl

    _, tier = _PICK
    result = await _delegate_impl(
        "Reply with the word: pong",
        tier=tier,
        timeout_s=60,
        project="e2e-test-proj",
        budget_usd=1000.0,  # large cap, won't trigger refusal
    )

    assert "❌" not in result, f"dispatch failed: {result}"
    rows = _read_usage_records(isolated_usage.log_file_path())
    assert len(rows) == 1
    assert rows[0]["task_id"] == "e2e-test-proj"


@pytest.mark.skipif(not _HAS_ANY_RUNNER, reason="no runner installed on this machine")
async def test_delegate_budget_exhausted_refuses_e2e(isolated_usage):
    """Pre-seed usage.jsonl with project spend > cap; verify dispatch is
    refused before the runner is invoked. Refusal must NOT produce a new
    usage row (no dispatch happened)."""
    log = isolated_usage.log_file_path()
    log.parent.mkdir(parents=True, exist_ok=True)

    # Seed: $10 already spent on this project
    from datetime import datetime, timezone

    seed = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "task_id": "e2e-budget-test",
        "runner": "claude",
        "provider": "anthropic",
        "model": "claude-haiku-4-5",
        "role": "delegate",
        "input_tokens": 1000,
        "output_tokens": 500,
        "latency_s": 1.0,
        "estimated_cost_usd": 10.0,
        "source": "cli",
        "mode": "auto",
        "escalation_count": 0,
    }
    with log.open("a") as f:
        f.write(json.dumps(seed) + "\n")

    from khimaira.server.mcp import _delegate_impl

    _, tier = _PICK
    result = await _delegate_impl(
        "test",
        tier=tier,
        timeout_s=60,
        project="e2e-budget-test",
        budget_usd=5.0,  # less than seeded $10
    )

    assert "budget exhausted" in result
    # Still only the seeded row; no new dispatch happened
    rows = _read_usage_records(log)
    assert len(rows) == 1


# ---------------- E2E: auto path (classifier + pool router) ---------------- #


@pytest.mark.skipif(
    not _runner_available("claude") and not _runner_available("gemini"),
    reason="auto path needs claude or gemini for the classifier",
)
async def test_auto_e2e(isolated_usage):
    """Full auto path: classifier picks tier → pool router picks model →
    runner dispatches → usage.jsonl shows mode='auto'."""
    from khimaira.server.mcp import _delegate_impl

    result = await _delegate_impl(
        "What does the @classmethod decorator do in Python? Reply in one sentence.",
        tier="auto",
        timeout_s=90,
    )

    if "❌ auto routing refused" in result:
        # Pool may not have any enabled-for-auto models on this machine
        pytest.skip(f"auto pool refused dispatch: {result}")

    assert "❌" not in result, f"dispatch failed: {result}"
    assert "mode=auto" in result

    rows = _read_usage_records(isolated_usage.log_file_path())
    assert len(rows) == 1
    assert rows[0]["mode"] == "auto"
    assert rows[0]["model"]


# ---------------- E2E: error paths (runner unavailable) ---------------- #


async def test_delegate_unknown_tier_refuses_without_runner_call(isolated_usage):
    """Refusal-before-dispatch path. Doesn't need any runner installed —
    refusal happens at the tier-map lookup. So no skip marker."""
    from khimaira.server.mcp import _delegate_impl

    result = await _delegate_impl(
        "anything",
        tier="bogus-tier-name",
        timeout_s=30,
    )
    assert "unknown tier" in result
    # No usage record on a refusal
    assert _read_usage_records(isolated_usage.log_file_path()) == []
