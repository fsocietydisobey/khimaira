"""Tiny helper for prompt assembly. Migrated from legacy cli/prompts.py."""

from __future__ import annotations


def build_prompt(*parts: str) -> str:
    """Join non-empty prompt parts with double newlines."""
    return "\n\n".join(p for p in parts if p)
