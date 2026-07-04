"""Grimoire Phase 1d — deterministic library organization (the housing pillar).

Phase 1 scope is deliberately SIMPLE: cheap, deterministic collection
assignment only. The LLM `organize_library()` pass (re-file/re-label an
already-collected-but-wrong guide, based on its own content, auto-applied)
is a LATER phase — not built here. This module's `derive_collection` is the
canonical rule; notebook_import.py imports it from here rather than
duplicating it, so import-time and organize-time assignment can never
disagree about which collection a given file belongs in.

Not a rubric: no quality/duplication/currency scoring. The only job is
keeping the library organized — every guide filed somewhere sensible.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from khimaira.monitor import notes


def derive_collection(path: Path, root: Path) -> str:
    """Deterministic collection-name derivation from a file's path relative
    to the import root: the immediate parent directory name, title-cased
    (e.g. `shared-docs/joseph/notes/foo.md` -> "Notes"; `shared-docs/sources/
    bar/baz.md` -> "Bar"). Falls back to "Uncategorized" for files directly
    under `root` with no parent subdirectory to name a collection after.

    Shared by notebook_import.py (assigns a collection at import time) and
    assign_deterministic below (the Phase 1 organize hook) — one rule, two
    callers, never duplicated.
    """
    try:
        rel_parent = path.parent.relative_to(root)
    except ValueError:
        rel_parent = path.parent
    parts = [p for p in rel_parent.parts if p not in (".", "")]
    if not parts:
        return "Uncategorized"
    return parts[-1].replace("-", " ").replace("_", " ").title()


def get_or_create_collection(title: str) -> dict[str, Any]:
    """Thin re-export of notes.get_or_create_collection — callers that
    reach for "the organizer" as the conceptual owner of collection
    creation can import it from here instead of reaching into notes.py
    directly."""
    return notes.get_or_create_collection(title)


def assign_deterministic(note_id: str, path: Path, root: Path) -> dict[str, Any]:
    """The Phase 1 organize hook: derive a collection from `path` (the
    source file's location) and file the note into it, stamping
    organized_at. Called right after import — cheap, no LLM call.

    Handles the obvious cases (a file's directory IS its topic) at ~zero
    cost. The later LLM organize_library() pass handles what this can't:
    already-collected-but-wrong, ambiguous naming, content-based re-filing."""
    collection = derive_collection(path, root)
    tab = get_or_create_collection(collection)
    return notes.mark_organized(note_id, tab_id=tab["id"])
