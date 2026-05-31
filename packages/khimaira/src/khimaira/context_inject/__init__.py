"""Dynamic context injection for the UserPromptSubmit hook (#66).

`resolve_context(cwd, prompt_class)` merges per-project context.yaml
(Scarlet-generated AUTO + human MANUAL sections) with built-in generic
defaults to produce per-prompt pointers + hint + prose.

Merge order = by VALUE, most-valuable first:
  1. MANUAL pointers (human-curated — highest value)
  2. AUTO   pointers (Scarlet-ranked by dep-graph centrality)
  3. BUILTIN defaults (generic — lowest value, truncated by cap)

The cap (_MAX_POINTERS) truncates the generic BUILTIN tail so the most
project-specific content always survives.  A misclassification can only add
or drop a pointer, never strip an actionable/safety signal (the default class
is "coordination" = full context, same as pre-#66).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

_MAX_POINTERS: int = 12
_CONTEXT_YAML_NAME: str = ".khimaira/context.yaml"

# Built-in defaults per prompt class.  These are the pre-#66 hardcoded values
# so a missing/corrupt context.yaml degrades gracefully to the old behaviour.
_BUILTIN_DEFAULTS: dict[str, dict[str, Any]] = {
    "architecture": {
        "hint": "🏛️ architecture/design prompt — read before proposing structure:",
        "pointers": [
            "CLAUDE.md",
            "tasks/BUILD-PLAN.md",
            "tasks/<name>/IMPLEMENTATION.md",
        ],
        "prose": {},
    },
    "bugfix": {
        "hint": (
            "🐛 bug-fix prompt — reproduce first, then trace the data flow to the root cause."
        ),
        "pointers": [
            "git log -p -S '<symbol>' <file>",
            "tests/<failing-test>",
        ],
        "prose": {},
    },
    "coordination": {
        "hint": "",
        "pointers": [],
        "prose": {},
    },
    "simple": {
        "hint": "",
        "pointers": [],
        "prose": {},
    },
}

_DEFAULT_RESULT: dict[str, Any] = {"hint": "", "pointers": [], "prose": {}}

# mtime cache: (absolute_path, st_mtime_ns) → parsed yaml dict
_YAML_CACHE: dict[tuple[str, int], dict[str, Any]] = {}


def _coerce_pointers(val: Any) -> list[str]:
    """Return a list of non-empty strings from val, or []."""
    if not isinstance(val, list):
        return []
    return [str(item) for item in val if item]


def _load_context_yaml(cwd: str) -> dict[str, Any] | None:
    """Load and cache .khimaira/context.yaml from cwd.  Returns None on any error."""
    try:
        path = Path(cwd) / _CONTEXT_YAML_NAME
        if not path.is_file():
            return None
        mtime_ns = path.stat().st_mtime_ns
        cache_key = (str(path.resolve()), mtime_ns)
        cached = _YAML_CACHE.get(cache_key)
        if cached is not None:
            return cached
        import yaml
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if not isinstance(data, dict):
            return None
        # Evict stale entries for this path (different mtime)
        stale = [k for k in _YAML_CACHE if k[0] == str(path.resolve()) and k[1] != mtime_ns]
        for k in stale:
            _YAML_CACHE.pop(k, None)
        _YAML_CACHE[cache_key] = data
        return data
    except Exception:
        return None


def resolve_context(
    cwd: str,
    prompt_class: str,
) -> dict[str, Any]:
    """Return {hint, pointers, prose} for (cwd, prompt_class).

    Merges MANUAL + AUTO (from .khimaira/context.yaml if present) with the
    built-in generic defaults.  Deduplicates first-occurrence-wins (so a
    pointer in MANUAL keeps the MANUAL position even if it also appears in
    BUILTIN).  Caps at _MAX_POINTERS — the cap truncates the BUILTIN tail so
    manual/AUTO content always survives.

    Fail-open: any yaml read error → returns the built-in default unchanged.
    """
    builtin = _BUILTIN_DEFAULTS.get(prompt_class, _DEFAULT_RESULT)
    builtin_ptrs: list[str] = list(builtin.get("pointers") or [])
    builtin_hint: str = builtin.get("hint") or ""

    yaml_data = _load_context_yaml(cwd)
    if yaml_data is None:
        return {
            "hint": builtin_hint,
            "pointers": builtin_ptrs[:_MAX_POINTERS],
            "prose": {},
        }

    # -- AUTO section --
    try:
        classes_section = yaml_data.get("classes") or {}
        class_entry = classes_section.get(prompt_class) if isinstance(classes_section, dict) else None
        if not isinstance(class_entry, dict):
            class_entry = {}
        auto_ptrs: list[str] = _coerce_pointers(class_entry.get("pointers"))
        auto_hint: str = str(class_entry.get("hint") or "").strip()
        # Prose fields (not paths — returned separately for the hook to render)
        prose: dict[str, str] = {}
        for field in ("dep_graph_note", "framework"):
            v = class_entry.get(field)
            if v:
                prose[field] = str(v)
        # Ignore feature_docs (frozen-v1 pre-expands it; stale instances may carry it)
    except Exception:
        auto_ptrs = []
        auto_hint = ""
        prose = {}

    # -- MANUAL section --
    try:
        manual_section = yaml_data.get("manual") or {}
        manual_class = manual_section.get(prompt_class) if isinstance(manual_section, dict) else None
        if not isinstance(manual_class, dict):
            manual_class = {}
        manual_ptrs: list[str] = _coerce_pointers(manual_class.get("pointers"))
        manual_hint: str = str(manual_class.get("hint") or "").strip()
    except Exception:
        manual_ptrs = []
        manual_hint = ""

    # -- Merge: MANUAL → AUTO → BUILTIN (most-valuable first) --
    merged: list[str] = []
    seen: set[str] = set()
    for ptr in manual_ptrs + auto_ptrs + builtin_ptrs:
        if ptr and ptr not in seen:
            seen.add(ptr)
            merged.append(ptr)

    # -- Cap: truncate the BUILTIN tail (last) --
    pointers = merged[:_MAX_POINTERS]

    # -- Hint: last-non-empty-wins (manual wins if present) --
    hint = builtin_hint
    if auto_hint:
        hint = auto_hint
    if manual_hint:
        hint = manual_hint

    return {"hint": hint, "pointers": pointers, "prose": prose}
