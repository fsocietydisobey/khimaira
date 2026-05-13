"""Context resolver — Pillar 1 of khimaira.

`resolve_context(task) → ContextBundle` answers *"what files actually
matter for this task?"* before anything hits the LLM. Reduces prompt
tokens 5–10× compared to "paste the whole codebase" workflows.

Three resolution sources, weighted and merged:
  - Séance (semantic vector search) — when the seance package's library
    API is available; otherwise falls back to grep
  - Scarlet (codebase cartography) — when scarlet is available; otherwise
    reads existing CLAUDE.md files
  - Filesystem heuristics — recently modified files, README, top-level entry points

The resolver is designed to ALWAYS produce some result, even when no
perception tools are installed. Quality scales with what's available;
the interface doesn't change.
"""

from .resolver import resolve_context

__all__ = ["resolve_context"]
