"""Tests for #66 Task A: context_inject.resolve_context().

Covers the acceptance criteria from the task spec:
- Happy path: AUTO+MANUAL merged, deduped, capped, manual wins
- Cap-ordering: >12 pointers → BUILTIN tail truncated, manual+AUTO survive
- Missing file → exact builtin; malformed YAML → builtin, no raise
- Unknown class → empty, no raise
- Dedup first-position-preserved
- Prose fields returned separately
- feature_docs ignored (schema-stale field)
- mtime cache: unchanged → no re-parse; changed → re-read
- Hook integration: cwd WITH context.yaml renders file pointers
- cwd WITHOUT context.yaml → builtin (pre-#66 parity)
"""

from __future__ import annotations

import json
import yaml
from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_ctx(tmp_path: Path, data: dict) -> Path:
    """Write .khimaira/context.yaml and return the cwd (tmp_path)."""
    ctx_dir = tmp_path / ".khimaira"
    ctx_dir.mkdir(parents=True, exist_ok=True)
    (ctx_dir / "context.yaml").write_text(yaml.dump(data), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# resolve_context unit tests
# ---------------------------------------------------------------------------

def test_no_context_yaml_returns_builtin(tmp_path):
    """cwd without a context.yaml → exact _BUILTIN_DEFAULTS[class]."""
    from khimaira.context_inject import resolve_context, _BUILTIN_DEFAULTS

    result = resolve_context(str(tmp_path), "architecture")
    builtin = _BUILTIN_DEFAULTS["architecture"]
    assert result["hint"] == builtin["hint"]
    assert result["pointers"] == builtin["pointers"][:12]


def test_auto_pointers_merged(tmp_path):
    """AUTO pointers from context.yaml are included in the result."""
    _write_ctx(tmp_path, {
        "classes": {
            "architecture": {
                "hint": "project-specific hint",
                "pointers": ["docs/ARCH.md", "src/core/"],
            }
        }
    })
    from khimaira.context_inject import resolve_context
    result = resolve_context(str(tmp_path), "architecture")
    assert "docs/ARCH.md" in result["pointers"]
    assert result["hint"] == "project-specific hint"


def test_manual_pointers_take_precedence(tmp_path):
    """MANUAL pointers appear before AUTO and BUILTIN."""
    _write_ctx(tmp_path, {
        "classes": {"architecture": {"pointers": ["auto-doc.md"]}},
        "manual": {"architecture": {"pointers": ["manual-cross-system.md"], "hint": "manual hint"}},
    })
    from khimaira.context_inject import resolve_context
    result = resolve_context(str(tmp_path), "architecture")
    ptrs = result["pointers"]
    assert "manual-cross-system.md" in ptrs
    assert "auto-doc.md" in ptrs
    assert ptrs.index("manual-cross-system.md") < ptrs.index("auto-doc.md"), (
        "MANUAL pointer must appear before AUTO pointer"
    )
    assert result["hint"] == "manual hint"


def test_cap_ordering_truncates_builtin_tail(tmp_path):
    """With >12 total pointers, the BUILTIN tail is truncated — manual+AUTO survive."""
    from khimaira.context_inject import resolve_context, _MAX_POINTERS

    # 3 manual + 3 auto + builtin (3 defaults) = 9 unique, under cap
    # Use a class with 3+ builtins and add many manual to exceed cap
    manual_ptrs = [f"manual-{i}.md" for i in range(8)]
    auto_ptrs = [f"auto-{i}.md" for i in range(5)]
    _write_ctx(tmp_path, {
        "classes": {"architecture": {"pointers": auto_ptrs}},
        "manual": {"architecture": {"pointers": manual_ptrs}},
    })
    result = resolve_context(str(tmp_path), "architecture")
    assert len(result["pointers"]) <= _MAX_POINTERS
    # All manual pointers must survive (they're first in merge order)
    for m in manual_ptrs[:_MAX_POINTERS]:
        assert m in result["pointers"], f"MANUAL pointer {m!r} should survive the cap"


def test_dedup_first_occurrence_wins(tmp_path):
    """A pointer in both MANUAL and BUILTIN keeps its MANUAL (first) position."""
    from khimaira.context_inject import _BUILTIN_DEFAULTS
    builtin_ptr = _BUILTIN_DEFAULTS["architecture"]["pointers"][0]  # e.g. "CLAUDE.md"

    _write_ctx(tmp_path, {
        "manual": {"architecture": {"pointers": [builtin_ptr, "other.md"]}},
    })
    from khimaira.context_inject import resolve_context
    result = resolve_context(str(tmp_path), "architecture")
    # builtin_ptr appears once, in MANUAL position (first)
    assert result["pointers"].count(builtin_ptr) == 1
    assert result["pointers"].index(builtin_ptr) == 0


def test_missing_class_in_yaml_returns_builtin(tmp_path):
    """A class not in context.yaml → falls back to builtin."""
    _write_ctx(tmp_path, {"classes": {"bugfix": {"pointers": ["tests/"]}}})
    from khimaira.context_inject import resolve_context, _BUILTIN_DEFAULTS
    result = resolve_context(str(tmp_path), "architecture")
    # Should fall through to builtin
    assert result["pointers"] == _BUILTIN_DEFAULTS["architecture"]["pointers"][:12]


def test_malformed_yaml_falls_back_to_builtin(tmp_path):
    """Malformed YAML → no raise, returns builtin."""
    ctx_dir = tmp_path / ".khimaira"
    ctx_dir.mkdir()
    (ctx_dir / "context.yaml").write_text("{{INVALID: - yaml: [", encoding="utf-8")
    from khimaira.context_inject import resolve_context, _BUILTIN_DEFAULTS
    result = resolve_context(str(tmp_path), "architecture")
    assert result["pointers"] == _BUILTIN_DEFAULTS["architecture"]["pointers"][:12]


def test_wrong_typed_pointers_coerced(tmp_path):
    """Non-list pointers → treated as empty, no raise."""
    _write_ctx(tmp_path, {
        "classes": {"architecture": {"pointers": "not-a-list"}}
    })
    from khimaira.context_inject import resolve_context, _BUILTIN_DEFAULTS
    result = resolve_context(str(tmp_path), "architecture")
    # AUTO pointers are empty → falls back to builtins only
    assert result["pointers"] == _BUILTIN_DEFAULTS["architecture"]["pointers"][:12]


def test_unknown_class_returns_empty(tmp_path):
    """Unknown prompt_class → returns empty structure, no raise."""
    from khimaira.context_inject import resolve_context
    result = resolve_context(str(tmp_path), "nonexistent_class")
    assert result["hint"] == ""
    assert result["pointers"] == []


def test_prose_fields_returned_separately(tmp_path):
    """dep_graph_note and framework are in prose dict, not in pointers."""
    _write_ctx(tmp_path, {
        "classes": {"architecture": {
            "pointers": ["src/"],
            "dep_graph_note": "dashboard imported by 7",
            "framework": "Next.js / RTK",
        }}
    })
    from khimaira.context_inject import resolve_context
    result = resolve_context(str(tmp_path), "architecture")
    # Prose fields in prose dict
    assert result["prose"].get("dep_graph_note") == "dashboard imported by 7"
    assert result["prose"].get("framework") == "Next.js / RTK"
    # NOT in pointers list
    for ptr in result["pointers"]:
        assert "dashboard imported" not in ptr
        assert "Next.js" not in ptr


def test_feature_docs_ignored(tmp_path):
    """feature_docs field in context.yaml is ignored (stale pre-v1 field)."""
    _write_ctx(tmp_path, {
        "classes": {"architecture": {
            "pointers": ["src/"],
            "feature_docs": "frontend/src/features/*/CLAUDE.md",
        }}
    })
    from khimaira.context_inject import resolve_context
    result = resolve_context(str(tmp_path), "architecture")
    for ptr in result["pointers"]:
        assert "feature_docs" not in ptr
        assert "*/CLAUDE.md" not in ptr


def test_mtime_cache_unchanged_file_no_reparse(tmp_path, monkeypatch):
    """Second call with same mtime → no re-parse (cached result)."""
    _write_ctx(tmp_path, {"classes": {"architecture": {"pointers": ["cached.md"]}}})
    import khimaira.context_inject as ci

    ci._YAML_CACHE.clear()
    parse_count = [0]
    real_safe_load = yaml.safe_load

    def counting_safe_load(s):
        parse_count[0] += 1
        return real_safe_load(s)

    monkeypatch.setattr(yaml, "safe_load", counting_safe_load)

    from khimaira.context_inject import resolve_context
    resolve_context(str(tmp_path), "architecture")
    resolve_context(str(tmp_path), "architecture")

    assert parse_count[0] == 1, "yaml.safe_load must be called exactly once for unchanged file"
    ci._YAML_CACHE.clear()


def test_mtime_cache_changed_file_rereads(tmp_path, monkeypatch):
    """Changed mtime → re-reads the file."""
    path = _write_ctx(tmp_path, {"classes": {"architecture": {"pointers": ["v1.md"]}}})
    import khimaira.context_inject as ci
    ci._YAML_CACHE.clear()

    from khimaira.context_inject import resolve_context
    r1 = resolve_context(str(path), "architecture")
    assert "v1.md" in r1["pointers"]

    # Update file
    import time
    time.sleep(0.01)
    _write_ctx(tmp_path, {"classes": {"architecture": {"pointers": ["v2.md"]}}})

    r2 = resolve_context(str(path), "architecture")
    assert "v2.md" in r2["pointers"]
    ci._YAML_CACHE.clear()


def test_manual_applies_to_bugfix_class(tmp_path):
    """manual.<class> applies to ALL classes including bugfix (not just architecture)."""
    _write_ctx(tmp_path, {
        "manual": {
            "bugfix": {
                "pointers": ["pytest backend/tests/", "npm run test -- <file>"],
                "hint": "project-specific test commands",
            }
        }
    })
    from khimaira.context_inject import resolve_context
    result = resolve_context(str(tmp_path), "bugfix")
    assert "pytest backend/tests/" in result["pointers"]
    assert result["hint"] == "project-specific test commands"


# ---------------------------------------------------------------------------
# Hook integration test
# ---------------------------------------------------------------------------

def test_hook_uses_context_yaml_pointers_when_present(tmp_path, monkeypatch):
    """Hook renders context.yaml architecture pointers (not hardcoded) with cwd set."""
    import os, sys, json, io
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    _write_ctx(tmp_path, {
        "classes": {
            "architecture": {
                "hint": "project arch hint",
                "pointers": ["PROJECT_ARCH.md"],
            }
        }
    })

    payload = json.dumps({
        "prompt": "how should we structure the auth module",
        "session_id": "test-session",
        "cwd": str(tmp_path),
    })

    from khimaira.hooks import user_prompt_submit as hook_mod
    import importlib
    importlib.reload(hook_mod)

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    hook_mod.main()

    output = captured.getvalue()
    assert "PROJECT_ARCH.md" in output, (
        "Hook must render context.yaml architecture pointers when they exist"
    )
    assert "project arch hint" in output, (
        "Hook must render context.yaml hint when provided"
    )


def test_hook_falls_back_to_builtin_without_context_yaml(tmp_path, monkeypatch):
    """Hook uses built-in defaults when cwd has no context.yaml."""
    import os, sys, json, io
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))

    payload = json.dumps({
        "prompt": "how should we structure the auth module",
        "session_id": "test-session",
        "cwd": str(tmp_path),
    })

    from khimaira.hooks import user_prompt_submit as hook_mod
    import importlib
    importlib.reload(hook_mod)

    captured = io.StringIO()
    monkeypatch.setattr(sys, "stdout", captured)
    monkeypatch.setattr(sys, "stdin", io.StringIO(payload))
    hook_mod.main()

    output = captured.getvalue()
    # Builtin includes CLAUDE.md or tasks/BUILD-PLAN.md
    assert "CLAUDE.md" in output or "BUILD-PLAN" in output, (
        "Hook must render builtin architecture pointers when no context.yaml exists"
    )
