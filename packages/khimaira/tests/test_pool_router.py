"""Pool router contract tests.

Covers the auto-mode model selection pipeline:
  load_registry → auto_pool → filter on runner availability →
  filter on capability subset → sort by cost → pick cheapest.

Tests the contract, not the implementation — derive_required_caps()
internals can change, but the same TaskClassification must keep
landing on the same kind of model.
"""

from __future__ import annotations

import pytest
from khimaira_types import TaskClassification

from khimaira.dispatch.pool_router import (
    PoolDecision,
    derive_required_caps,
    select_from_pool,
)
from khimaira.dispatch.registry import ModelCost, ModelEntry


def _classification(
    *,
    task_type: str = "classify",
    complexity_tier: str = "trivial",
    thinking_level: str = "none",
    confidence: float = 0.9,
) -> TaskClassification:
    """Build a TaskClassification for tests. Defaults to a trivial classify
    task (the cheapest end of the spectrum)."""
    return TaskClassification(
        task_type=task_type,  # type: ignore[arg-type]
        complexity_tier=complexity_tier,  # type: ignore[arg-type]
        thinking_level=thinking_level,  # type: ignore[arg-type]
        recommended_runner="claude",
        recommended_model="claude-haiku-4-5",
        thinking_budget_tokens=0,
        estimated_cost_usd_max=0.1,
        reasoning="test",
        confidence=confidence,
    )


def _entry(
    id: str,
    runner: str,
    capabilities: tuple[str, ...],
    input_cost: float,
    output_cost: float,
    enabled: bool = True,
) -> ModelEntry:
    """Build a ModelEntry inline."""
    return ModelEntry(
        id=id,
        runner=runner,
        capabilities=capabilities,
        cost_per_1m=ModelCost(input=input_cost, output=output_cost),
        subscription="test",
        enabled_for_auto=enabled,
    )


def _all_available(name: str) -> bool:
    return True


def _only_claude_available(name: str) -> bool:
    return name == "claude"


# -------------------- derive_required_caps -------------------- #


def test_required_caps_trivial_classify_is_empty():
    """Trivial classify needs no special caps — keeps the pool wide."""
    caps = derive_required_caps(_classification())
    assert caps == frozenset()


def test_required_caps_code_task_requires_code():
    caps = derive_required_caps(_classification(task_type="implement"))
    assert "code" in caps


def test_required_caps_complex_requires_deep_reasoning():
    caps = derive_required_caps(_classification(complexity_tier="complex"))
    assert "deep-reasoning" in caps
    # 'reasoning' should be dropped when 'deep-reasoning' is present
    # (subset test stays clean; deep-reasoning implies reasoning)
    assert "reasoning" not in caps


def test_required_caps_high_thinking_overrides_to_deep_reasoning():
    caps = derive_required_caps(
        _classification(complexity_tier="medium", thinking_level="high"),
    )
    assert "deep-reasoning" in caps


# -------------------- select_from_pool: happy paths -------------------- #


def test_picks_cheapest_eligible_model():
    """All else equal, the cheapest model that covers required caps wins."""
    registry = [
        _entry("expensive", "claude", ("code",), 15.0, 75.0),
        _entry("cheap", "claude", ("code",), 1.0, 5.0),
        _entry("midprice", "claude", ("code",), 3.0, 15.0),
    ]
    c = _classification(task_type="implement")
    d = select_from_pool(c, registry=registry, runner_available=_all_available)

    assert not d.refused
    assert d.chosen is not None
    assert d.chosen.id == "cheap"


def test_top_2_reflects_ranking():
    """Audit field — top_2 must list cheapest first, second cheapest second."""
    registry = [
        _entry("a", "claude", ("code",), 15.0, 75.0),
        _entry("b", "claude", ("code",), 1.0, 5.0),
        _entry("c", "claude", ("code",), 3.0, 15.0),
    ]
    d = select_from_pool(
        _classification(task_type="implement"),
        registry=registry,
        runner_available=_all_available,
    )
    assert [t[0] for t in d.top_2] == ["b", "c"]


# -------------------- select_from_pool: rejection paths -------------------- #


def test_runner_unavailable_skips_entry_with_audit_reason():
    """When ollama is down, ollama entries get rejected with a clear reason."""
    registry = [
        _entry("local", "ollama", ("code",), 0.0, 0.0),
        _entry("cloud", "claude", ("code",), 3.0, 15.0),
    ]
    d = select_from_pool(
        _classification(task_type="implement"),
        registry=registry,
        runner_available=_only_claude_available,
    )
    assert d.chosen is not None
    assert d.chosen.id == "cloud"
    assert "local" in d.rejected
    assert "runner-unavailable" in d.rejected["local"]


def test_missing_capability_rejects_with_audit_reason():
    """Cheap factual-only models get rejected for code tasks."""
    registry = [
        _entry("factual_only", "claude", ("factual",), 1.0, 5.0),
        _entry("code_capable", "claude", ("code",), 3.0, 15.0),
    ]
    d = select_from_pool(
        _classification(task_type="implement"),
        registry=registry,
        runner_available=_all_available,
    )
    assert d.chosen is not None
    assert d.chosen.id == "code_capable"
    assert "factual_only" in d.rejected
    assert "missing-caps" in d.rejected["factual_only"]
    assert "code" in d.rejected["factual_only"]


def test_disabled_for_auto_excluded_from_pool():
    """An entry with enabled_for_auto=False shouldn't be picked even if
    it's the cheapest. The user opted it out."""
    registry = [
        _entry("opted_out", "claude", ("code",), 0.1, 0.1, enabled=False),
        _entry("opted_in", "claude", ("code",), 3.0, 15.0, enabled=True),
    ]
    d = select_from_pool(
        _classification(task_type="implement"),
        registry=registry,
        runner_available=_all_available,
    )
    assert d.chosen is not None
    assert d.chosen.id == "opted_in"
    # opted_out wasn't in the pool at all — shouldn't appear in rejected either
    assert "opted_out" not in d.rejected


# -------------------- select_from_pool: refusal paths -------------------- #


def test_empty_pool_refuses_with_clear_message():
    """Every entry enabled_for_auto=false → refusal points the user at
    `khimaira models enable`."""
    registry = [
        _entry("a", "claude", ("code",), 1.0, 5.0, enabled=False),
        _entry("b", "claude", ("code",), 1.0, 5.0, enabled=False),
    ]
    d = select_from_pool(
        _classification(task_type="implement"),
        registry=registry,
        runner_available=_all_available,
    )
    assert d.refused
    assert d.chosen is None
    assert "khimaira models enable" in (d.refusal_reason or "")


def test_no_runner_available_refuses():
    """Nothing installed → refuse with install hint."""
    registry = [
        _entry("a", "claude", ("code",), 1.0, 5.0),
    ]
    d = select_from_pool(
        _classification(task_type="implement"),
        registry=registry,
        runner_available=lambda _: False,
    )
    assert d.refused
    assert "Install" in (d.refusal_reason or "")


def test_no_eligible_caps_refuses_with_hint():
    """All available models lack the required cap → tell the user to
    enable a more-capable model."""
    registry = [
        _entry("weak", "claude", ("factual",), 0.5, 1.0),
    ]
    d = select_from_pool(
        _classification(task_type="architect", complexity_tier="complex"),
        registry=registry,
        runner_available=_all_available,
    )
    assert d.refused
    assert "khimaira models" in (d.refusal_reason or "")


# -------------------- audit dict shape -------------------- #


def test_audit_dict_includes_all_required_fields():
    """to_audit_dict must surface every field needed for `khimaira usage
    savings --audit` post-hoc review."""
    registry = [_entry("a", "claude", ("code",), 1.0, 5.0)]
    d = select_from_pool(
        _classification(task_type="implement"),
        registry=registry,
        runner_available=_all_available,
    )
    audit = d.to_audit_dict()
    for key in (
        "chosen_id",
        "chosen_runner",
        "required_caps",
        "classifier_confidence",
        "pool_size",
        "available_size",
        "eligible_size",
        "top_2",
        "rejected_reasons",
        "refused",
    ):
        assert key in audit, f"missing audit field: {key}"
