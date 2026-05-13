"""Tests for prompt-caching awareness in cost estimates (#58).

Anthropic bills cache_creation at ~1.25x base input price and cache_read
at ~0.10x base input. Folding all three token classes into a single
"input_tokens" bucket charged at full input rate over-counts the cost.

estimate_cost now takes optional cache_creation_tokens / cache_read_tokens
kwargs. Callers without the breakdown (most CLI runners) pass 0 and
get the legacy single-bucket math. Callers with the breakdown (the
SubagentStop hook reading Anthropic transcripts) get the right math.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_usage(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    from khimaira import usage as usage_mod
    importlib.reload(usage_mod)
    yield usage_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(usage_mod)


def test_estimate_cost_legacy_signature_unchanged(isolated_usage):
    """Callers that don't pass cache kwargs get the old math."""
    # Haiku: $0.8/$4 per M
    # 1000 in + 500 out = 1000*0.8/1M + 500*4/1M = 0.0008 + 0.002 = 0.0028
    cost = isolated_usage.estimate_cost("claude-haiku-4-5", 1000, 500)
    assert cost == pytest.approx(0.0028)


def test_estimate_cost_zero_cache_matches_legacy(isolated_usage):
    """Passing 0 for both cache kwargs is a no-op vs the legacy form."""
    a = isolated_usage.estimate_cost("claude-haiku-4-5", 1000, 500)
    b = isolated_usage.estimate_cost(
        "claude-haiku-4-5", 1000, 500,
        cache_creation_tokens=0, cache_read_tokens=0,
    )
    assert a == b == pytest.approx(0.0028)


def test_cache_creation_billed_at_1_25x_input(isolated_usage):
    """1000 cache-creation tokens on Haiku = 1000 * 0.8 * 1.25 / 1M = 0.001."""
    cost = isolated_usage.estimate_cost(
        "claude-haiku-4-5", 0, 0, cache_creation_tokens=1000, cache_read_tokens=0,
    )
    assert cost == pytest.approx(0.001)


def test_cache_read_billed_at_0_10x_input(isolated_usage):
    """10000 cache-read tokens on Haiku = 10000 * 0.8 * 0.10 / 1M = 0.0008."""
    cost = isolated_usage.estimate_cost(
        "claude-haiku-4-5", 0, 0, cache_creation_tokens=0, cache_read_tokens=10000,
    )
    assert cost == pytest.approx(0.0008)


def test_all_four_buckets_sum_correctly(isolated_usage):
    """Realistic breakdown: 3 fresh input, 10965 cache_creation, 144 output, 0 cache_read.
    (This is the actual khimaira-factual dispatch from earlier today's session.)

    Expected: 3*0.8/1M + 144*4/1M + 10965*0.8*1.25/1M + 0
            = 2.4e-6 + 5.76e-4 + 1.0965e-2
            = 0.01154184
    """
    cost = isolated_usage.estimate_cost(
        "claude-haiku-4-5-20251001",
        input_tokens=3,
        output_tokens=144,
        cache_creation_tokens=10965,
        cache_read_tokens=0,
    )
    expected = (3 * 0.8 + 144 * 4.0 + 10965 * 0.8 * 1.25) / 1_000_000
    assert cost == pytest.approx(expected)


def test_unknown_model_returns_zero_with_cache_tokens(isolated_usage):
    """Cache pricing only applies when the model is in the price table.
    Unknown models still return 0 (no over-bill on uncatalogued models)."""
    cost = isolated_usage.estimate_cost(
        "some-random-future-model-id",
        100, 50,
        cache_creation_tokens=1000, cache_read_tokens=2000,
    )
    assert cost == 0.0


def test_cache_pricing_vs_legacy_folding_diverges(isolated_usage):
    """The whole reason for #58: folding cache tokens into input_tokens
    over-counts the cost. Verify the new math produces a SMALLER number
    than the legacy fold-everything-in approach for a cache-heavy workload."""
    # Legacy (broken): 10000 cache-creation + 5000 cache-read folded into
    # input_tokens — charged at full $0.8/M.
    legacy_total_input = 0 + 10000 + 5000  # input + cache_creation + cache_read
    legacy_cost = isolated_usage.estimate_cost(
        "claude-haiku-4-5", legacy_total_input, 100,
    )

    # New (correct): cache buckets get their multipliers.
    correct_cost = isolated_usage.estimate_cost(
        "claude-haiku-4-5", 0, 100,
        cache_creation_tokens=10000,
        cache_read_tokens=5000,
    )

    # Cache-heavy workload should cost LESS under correct accounting
    assert correct_cost < legacy_cost
    # And specifically:
    # legacy: (15000*0.8 + 100*4) / 1M = 0.0124
    # correct: (100*4 + 10000*0.8*1.25 + 5000*0.8*0.10) / 1M
    #        = (400 + 10000 + 400) / 1M = 0.0108
    assert legacy_cost == pytest.approx(0.0124)
    assert correct_cost == pytest.approx(0.0108)


def test_recorder_persists_cache_token_fields(isolated_usage, tmp_path):
    """A record with cache_creation + cache_read written via the recorder
    round-trips back as a valid UsageRecord with those fields populated."""
    import asyncio, json
    from khimaira_types import UsageRecord

    async def _do_record():
        await isolated_usage.get_recorder().record(
            runner="claude",
            provider="anthropic",
            model="claude-haiku-4-5",
            input_tokens=10,
            output_tokens=20,
            latency_s=0.5,
            mode="subagent",
            cache_creation_tokens=5000,
            cache_read_tokens=1000,
        )

    asyncio.run(_do_record())

    raw = isolated_usage.log_file_path().read_text().strip()
    rec = UsageRecord.model_validate(json.loads(raw))
    assert rec.cache_creation_tokens == 5000
    assert rec.cache_read_tokens == 1000
    # Cost should reflect the cache pricing
    # input_tokens=10, output=20, cache_creation=5000, cache_read=1000
    # = (10*0.8 + 20*4 + 5000*0.8*1.25 + 1000*0.8*0.10) / 1M
    # = (8 + 80 + 5000 + 80) / 1M = 0.005168
    assert rec.estimated_cost_usd == pytest.approx(0.005168)
