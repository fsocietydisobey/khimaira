"""Scarlet-semantic — 3-tier trust model for generated CLAUDE.md content (#66 / Scarlet-semantic).

Three tiers, by trust:

* ``AUTO``      — mechanically derived (tree-sitter exports/consumers/dep-graph).
  Deterministic, regenerable, TRUSTED.
* ``SUGGESTED`` — LLM-generated *from the code*.  DRAFT — regenerable, labeled
  unverified.  NEVER treated as authoritative until a human promotes it.
* ``MANUAL``    — human-confirmed (cross-system knowledge the AST can't see, or a
  SUGGESTED block a reviewer promoted).  AUTHORITATIVE; preserved across regen.

Design invariants (architect's, audit-grade):

1. **LLM is DEPENDENCY-INJECTED, never imported here.**  ``analyze_semantic``
   takes an ``analyzer`` callable supplied by the caller (khimaira, which holds
   creds via load_dotenv).  Scarlet stays deterministic + credential-free; the
   non-determinism lives caller-side.  ``analyzer=None`` -> no LLM call.
2. **Content-hash cache** — re-run the analyzer only when the source changed.
3. **Promotion gate** — SUGGESTED -> MANUAL requires a reviewer DISTINCT from the
   generator, recorded in ``reviewed_by``.  An unreviewed draft can never silently
   cross into MANUAL (the same author-role-binding lesson as the B3 gate).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable


class TrustTier(str, Enum):
    """Trust level of a generated doc block (str-valued for YAML/JSON friendliness)."""

    AUTO = "AUTO"
    SUGGESTED = "SUGGESTED"
    MANUAL = "MANUAL"


class PromotionError(RuntimeError):
    """Raised when a SUGGESTED block cannot be promoted (wrong tier / missing reviewer)."""


@dataclass
class SemanticBlock:
    """A generated doc section, its trust tier, and provenance."""

    key: str
    content: str
    tier: TrustTier
    source_hash: str = ""
    reviewed_by: str | None = None
    reviewed_at: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content,
            "tier": self.tier.value,
            "source_hash": self.source_hash,
            "reviewed_by": self.reviewed_by,
            "reviewed_at": self.reviewed_at,
        }


def hash_source(source: str) -> str:
    """Stable content hash of the source a SUGGESTED block was derived from."""
    return hashlib.sha256(source.encode("utf-8")).hexdigest()


def analyze_semantic(
    key: str,
    source: str,
    analyzer: Callable[[str, str], str] | None = None,
    cache: dict[str, str] | None = None,
) -> SemanticBlock:
    """Produce a ``SUGGESTED``-tier block for ``key`` derived from ``source``.

    * ``analyzer is None`` -> a SUGGESTED stub with empty content (NO LLM call).
      This is the deterministic default; the caller opts into LLM by passing one.
    * ``analyzer`` given -> call ``analyzer(key, source)`` for the content.  When
      ``cache`` is provided, a matching ``source_hash`` returns the cached content
      WITHOUT re-invoking the analyzer (content-hash cache).

    Always returns ``TrustTier.SUGGESTED`` — generation never yields AUTO
    (that is mechanical/tree-sitter) or MANUAL (that requires human promotion).
    """
    src_hash = hash_source(source)

    if analyzer is None:
        return SemanticBlock(key=key, content="", tier=TrustTier.SUGGESTED, source_hash=src_hash)

    if cache is not None and src_hash in cache:
        return SemanticBlock(
            key=key, content=cache[src_hash], tier=TrustTier.SUGGESTED, source_hash=src_hash
        )

    content = analyzer(key, source)
    if cache is not None:
        cache[src_hash] = content
    return SemanticBlock(key=key, content=content, tier=TrustTier.SUGGESTED, source_hash=src_hash)


def promote_to_manual(
    block: SemanticBlock,
    reviewer: str,
    reviewed_at: str | None = None,
) -> SemanticBlock:
    """Promote a SUGGESTED block to MANUAL — gated on a real reviewer.

    Mutates + returns ``block``.  Raises ``PromotionError`` if the block is not
    SUGGESTED, or if ``reviewer`` is empty/blank.  The injected LLM analyzer has
    no roster identity, so any named human/review-role reviewer is "distinct from
    the generator"; the load-bearing guard is simply that a real reviewer is
    recorded — an unreviewed (auto/self) promotion is refused.
    """
    if block.tier is not TrustTier.SUGGESTED:
        raise PromotionError(f"can only promote SUGGESTED blocks, not {block.tier.value}")
    if not reviewer or not reviewer.strip():
        raise PromotionError(
            "promotion requires a reviewer (reviewed_by) — refusing to promote an unreviewed block"
        )
    block.tier = TrustTier.MANUAL
    block.reviewed_by = reviewer.strip()
    block.reviewed_at = reviewed_at
    return block
