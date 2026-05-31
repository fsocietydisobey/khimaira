"""Tests for scarlet.semantic — 3-tier trust model (#66 / Scarlet-semantic)."""

from __future__ import annotations

import pytest

from scarlet.semantic import (
    PromotionError,
    SemanticBlock,
    TrustTier,
    analyze_semantic,
    promote_to_manual,
)


def test_trust_tiers_distinct():
    """AUTO / SUGGESTED / MANUAL are distinct string values."""
    assert TrustTier.AUTO != TrustTier.SUGGESTED
    assert TrustTier.SUGGESTED != TrustTier.MANUAL
    assert TrustTier.AUTO != TrustTier.MANUAL
    # str-valued for YAML/JSON friendliness
    assert TrustTier.AUTO.value == "AUTO"
    assert TrustTier.SUGGESTED.value == "SUGGESTED"
    assert TrustTier.MANUAL.value == "MANUAL"


def test_reviewed_by_gate():
    """analyze_semantic(analyzer=None) → SUGGESTED; promote_to_manual gates on reviewer."""
    block = analyze_semantic("vocabulary", "def foo(): pass", analyzer=None)
    assert block.tier is TrustTier.SUGGESTED

    # Empty reviewer → PromotionError
    with pytest.raises(PromotionError):
        promote_to_manual(block, "")

    # Whitespace-only reviewer → PromotionError
    with pytest.raises(PromotionError):
        promote_to_manual(block, "   ")

    # Valid reviewer → MANUAL
    promoted = promote_to_manual(block, "critic-1")
    assert promoted.tier is TrustTier.MANUAL
    assert promoted.reviewed_by == "critic-1"


def test_content_hash_cache_skip():
    """Same source + shared cache → analyzer called exactly once."""
    call_count = [0]

    def counting_analyzer(key: str, source: str) -> str:
        call_count[0] += 1
        return f"analyzed:{key}"

    cache: dict[str, str] = {}
    source = "def bar(): return 42"

    b1 = analyze_semantic("conventions", source, analyzer=counting_analyzer, cache=cache)
    b2 = analyze_semantic("conventions", source, analyzer=counting_analyzer, cache=cache)

    assert call_count[0] == 1, "analyzer must be called exactly once for unchanged source"
    assert b1.content == b2.content
    assert b1.source_hash == b2.source_hash
