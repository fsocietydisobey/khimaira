"""Tests for khimaira.monitor.notebook_organizer (Grimoire Phase 1d).

Phase 1 scope only: deterministic collection assignment. No LLM calls in
this module yet (organize_library() is a later phase) — every test here is
a plain unit test, no mocking of claude -p needed.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def notes_store(isolated_state, monkeypatch):
    from khimaira.monitor import notes as notes_mod

    importlib.reload(notes_mod)
    yield notes_mod
    importlib.reload(notes_mod)


@pytest.fixture
def organizer(notes_store):
    from khimaira.monitor import notebook_organizer as o

    importlib.reload(o)
    yield o


def test_derive_collection_uses_immediate_parent_dir(organizer):
    root = Path("/shared-docs")
    path = root / "joseph" / "notes" / "foo.md"
    assert organizer.derive_collection(path, root) == "Notes"


def test_derive_collection_titleizes_hyphens_and_underscores(organizer):
    root = Path("/shared-docs")
    path = root / "kg_data_model" / "bar.md"
    assert organizer.derive_collection(path, root) == "Kg Data Model"


def test_derive_collection_falls_back_to_uncategorized_for_root_level_files(organizer):
    root = Path("/shared-docs")
    path = root / "top-level.md"
    assert organizer.derive_collection(path, root) == "Uncategorized"


def test_derive_collection_handles_path_outside_root(organizer):
    """A path that isn't actually under `root` (e.g. a symlink resolved
    elsewhere) must not raise — falls back to using the real parent."""
    root = Path("/shared-docs")
    path = Path("/somewhere/else/file.md")
    # Should not raise; some deterministic string comes back.
    assert isinstance(organizer.derive_collection(path, root), str)


def test_get_or_create_collection_delegates_to_notes(organizer, notes_store):
    tab = organizer.get_or_create_collection("My Collection")
    assert tab["kind"] == "collection"
    again = organizer.get_or_create_collection("my collection")
    assert again["id"] == tab["id"]


def test_assign_deterministic_files_note_and_stamps_organized_at(organizer, notes_store):
    guide = notes_store.add_study_guide("body")
    root = Path("/shared-docs")
    path = root / "onboarding" / "getting-started.md"

    updated = organizer.assign_deterministic(guide["id"], path, root)

    assert updated["organized_at"] is not None
    tab = notes_store.get_tab(updated["tab_id"])
    assert tab["kind"] == "collection"
    assert tab["title"] == "Onboarding"


def test_assign_deterministic_reuses_existing_collection(organizer, notes_store):
    root = Path("/shared-docs")
    path_a = root / "onboarding" / "a.md"
    path_b = root / "onboarding" / "b.md"

    guide_a = notes_store.add_study_guide("a")
    guide_b = notes_store.add_study_guide("b")

    updated_a = organizer.assign_deterministic(guide_a["id"], path_a, root)
    updated_b = organizer.assign_deterministic(guide_b["id"], path_b, root)

    assert updated_a["tab_id"] == updated_b["tab_id"]
    collections = [t for t in notes_store.list_tabs() if t["kind"] == "collection"]
    assert len(collections) == 1
