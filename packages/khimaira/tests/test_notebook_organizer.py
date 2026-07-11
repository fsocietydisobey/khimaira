"""Tests for khimaira.monitor.notebook_organizer (Grimoire Phase 1d).

Phase 1 scope only: deterministic collection assignment. No LLM calls in
this module yet (organize_library() is a later phase) — every test here is
a plain unit test, no mocking of claude -p needed.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
from pathlib import Path

import pytest


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0):
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self, input=None):
        return self._stdout, self._stderr


def _envelope(result_str: str) -> bytes:
    return json.dumps({"result": result_str, "session_id": "irrelevant"}).encode("utf-8")


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


# ---------------------------------------------------------------------------
# Grimoire Phase 2 — the LLM organize pass. `_invoke_claude`'s subprocess
# layer is mocked out (via notebook_pipeline.transform_note) — these tests
# exercise organize_library's own placement/filtering/scoping logic, not the
# real CLI (same convention as test_notebook_pipeline.py).
# ---------------------------------------------------------------------------


@pytest.fixture
def organizer_llm(organizer, monkeypatch):
    from khimaira.monitor import notebook_pipeline as pipeline_mod

    yield organizer, pipeline_mod


def _guide_with_abstract(notes_store, title, abstract, tags=None, tab_id=""):
    guide = notes_store.add_study_guide(f"# {title}\n\nbody", title=title, tab_id=tab_id)
    notes_store.set_study_guide_pipeline(
        guide["id"], {"abstract": abstract, "toc": [], "tags": tags or [], "entities": []}
    )
    return notes_store.get_note(guide["id"])


def _note_with_summary(notes_store, title, summary, tags=None, tab_id=""):
    note = notes_store.add_note(title, title=title, tab_id=tab_id)
    notes_store.set_pipeline(
        note["id"],
        {
            "summary": summary,
            "technical": "",
            "plain": "",
            "organized_md": "",
            "tags": tags or [],
            "entities": [],
        },
    )
    return notes_store.get_note(note["id"])


async def test_organize_library_empty_when_no_guides(organizer_llm):
    organizer, _pipeline_mod = organizer_llm
    result = await organizer.organize_library()
    assert result == {"considered": 0, "reassigned": [], "new_collections": []}


async def test_organize_library_places_guide_into_matching_existing_collection(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm
    existing = notes_store.add_tab(title="Architecture", kind="collection")
    guide = _guide_with_abstract(notes_store, "KG Overview", "Explains the knowledge graph.")

    async def fake_transform_note(content, *, instruction, schema):
        return {"placements": [{"note_id": guide["id"], "collection": "Architecture"}]}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert result["considered"] == 1
    assert result["reassigned"] == [guide["id"]]
    assert result["new_collections"] == []
    updated = notes_store.get_note(guide["id"])
    assert updated["tab_id"] == existing["id"]
    assert updated["organized_at"] is not None


async def test_organize_library_personal_context_stays_global_only(
    organizer_llm, notes_store, monkeypatch
):
    """Personal-context repo-scoping (2026-07-04,
    tasks/grimoire/PERSONAL-CONTEXT-SCOPING.md): organize_library's batch
    call spans potentially many repos at once — there is no single
    target_repo to scope to, so it deliberately passes NONE (global-only),
    even when an item in the batch shares a domain personal note's repo
    tag. This is a documented design choice, not a missed call site."""
    notes_store.add_note(
        "Jeevy-only rule.", tab_id=notes_store.PERSONAL_TAB_ID, repo="jeevy_portal"
    )
    guide = _guide_with_abstract(notes_store, "Jeevy Guide", "About jeevy.", tab_id="")
    notes_store.update_note(guide["id"], repo="jeevy_portal")
    captured_args = []

    async def fake_exec(*args, **kwargs):
        captured_args.append(args)
        return _FakeProc(_envelope(json.dumps({"placements": []})))

    from khimaira.monitor import notebook_pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod.asyncio, "create_subprocess_exec", fake_exec)
    await organizer_llm[0].organize_library()

    assert len(captured_args) == 1
    system_prompt_idx = captured_args[0].index("--append-system-prompt") + 1
    assert "Jeevy-only rule." not in captured_args[0][system_prompt_idx]


async def test_organize_library_creates_new_collection_when_llm_proposes_one(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm
    guide = _guide_with_abstract(notes_store, "Vector DB Internals", "How embeddings are stored.")

    async def fake_transform_note(content, *, instruction, schema):
        return {"placements": [{"note_id": guide["id"], "collection": "Retrieval"}]}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert result["reassigned"] == [guide["id"]]
    assert result["new_collections"] == ["Retrieval"]
    tabs = [t for t in notes_store.list_tabs() if t["kind"] == "collection"]
    assert any(t["title"] == "Retrieval" for t in tabs)


async def test_organize_library_no_op_stamps_organized_at_without_reassigning(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm
    tab = notes_store.add_tab(title="Onboarding", kind="collection")
    guide = _guide_with_abstract(notes_store, "Getting Started", "Intro guide.", tab_id=tab["id"])
    assert guide["organized_at"] is None

    async def fake_transform_note(content, *, instruction, schema):
        return {"placements": [{"note_id": guide["id"], "collection": "Onboarding"}]}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert result["reassigned"] == []
    updated = notes_store.get_note(guide["id"])
    assert updated["tab_id"] == tab["id"]
    assert updated["organized_at"] is not None


async def test_organize_library_discards_placements_for_unknown_note_ids(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm
    guide = _guide_with_abstract(notes_store, "Real Guide", "abstract")

    async def fake_transform_note(content, *, instruction, schema):
        return {
            "placements": [
                {"note_id": "nonexistent00", "collection": "Ghost"},
                {"note_id": guide["id"], "collection": "Somewhere"},
            ]
        }

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert result["reassigned"] == [guide["id"]]
    tabs = [t for t in notes_store.list_tabs() if t["kind"] == "collection"]
    assert not any(t["title"] == "Ghost" for t in tabs)


async def test_organize_library_scoped_to_note_ids_excludes_others(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm
    guide_a = _guide_with_abstract(notes_store, "Guide A", "abstract a")
    guide_b = _guide_with_abstract(notes_store, "Guide B", "abstract b")
    seen_content: list[str] = []

    async def fake_transform_note(content, *, instruction, schema):
        seen_content.append(content)
        return {"placements": [{"note_id": guide_a["id"], "collection": "Somewhere"}]}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library(note_ids=[guide_a["id"]])

    assert result["considered"] == 1
    assert guide_a["id"] in seen_content[0]
    assert guide_b["id"] not in seen_content[0]


async def test_organize_library_returns_gracefully_when_llm_fails(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm
    guide = _guide_with_abstract(notes_store, "Guide", "abstract")

    async def fake_transform_note(content, *, instruction, schema):
        return None

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert result == {"considered": 1, "reassigned": [], "new_collections": []}
    updated = notes_store.get_note(guide["id"])
    assert updated["organized_at"] is None


# ---------------------------------------------------------------------------
# Kind-aware organize_library (2026-07-04 addendum): notes AND guides in one
# engine — notes organize into folders, guides into collections, separate
# namespaces, a note's content signal is `summary` not `abstract`.
# ---------------------------------------------------------------------------


async def test_organize_library_files_a_note_into_a_folder_not_a_collection(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm
    note = _note_with_summary(notes_store, "My Note", "A note about widgets.")

    async def fake_transform_note(content, *, instruction, schema):
        return {"placements": [{"note_id": note["id"], "collection": "Widgets"}]}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert result["reassigned"] == [note["id"]]
    updated = notes_store.get_note(note["id"])
    tab = notes_store.get_tab(updated["tab_id"])
    assert tab["kind"] == "folder"
    assert tab["title"] == "Widgets"


async def test_organize_library_never_touches_priority(organizer_llm, notes_store, monkeypatch):
    """Priority flags (2026-07-04): user-owned, independent of the
    organizer's placement decisions — a reassignment must leave priority
    exactly as the user last set it."""
    organizer, pipeline_mod = organizer_llm
    note = _note_with_summary(notes_store, "My Note", "A note about widgets.")
    notes_store.update_note(note["id"], priority="urgent")

    async def fake_transform_note(content, *, instruction, schema):
        return {"placements": [{"note_id": note["id"], "collection": "Widgets"}]}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    await organizer.organize_library()

    assert notes_store.get_note(note["id"])["priority"] == "urgent"


async def test_organize_library_excludes_pinned_notes_from_prompt_and_reporting(
    organizer_llm, notes_store, monkeypatch
):
    """FILE-MANAGER (2026-07-04): a pinned note is excluded from
    organize_library's own prompt/considered-count entirely — not just
    prevented from actually moving."""
    organizer, pipeline_mod = organizer_llm
    pinned = _note_with_summary(notes_store, "Pinned Note", "About widgets.")
    notes_store.update_note(pinned["id"], pinned_placement=True)
    unpinned = _note_with_summary(notes_store, "Unpinned Note", "About gadgets.")

    captured_ids = []

    async def fake_transform_note(content, *, instruction, schema):
        captured_ids.append("Pinned Note" in content or pinned["id"] in content)
        return {"placements": [{"note_id": unpinned["id"], "collection": "Gadgets"}]}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert result["considered"] == 1  # only the unpinned note
    assert pinned["id"] not in result["reassigned"]


async def test_pin_survives_organize_sweep_and_reimport(organizer_llm, notes_store, monkeypatch):
    """THE required invariant test (closes Q4's side path): pin a guide,
    run organize_library() -> tab_id unchanged, THEN fire
    assign_deterministic (the re-import-equivalent path) -> tab_id STILL
    unchanged. The second half is what a naive impl (guard organize_library
    only, forget assign_deterministic/import) fails."""
    organizer, pipeline_mod = organizer_llm
    home_tab = notes_store.add_tab(title="Home", kind="collection")
    guide = _guide_with_abstract(
        notes_store, "Pinned Guide", "About widgets.", tab_id=home_tab["id"]
    )
    notes_store.update_note(guide["id"], pinned_placement=True)

    # Sweep: organize_library must not move it, even if the LLM proposes a
    # different collection.
    async def fake_transform_note(content, *, instruction, schema):
        return {"placements": [{"note_id": guide["id"], "collection": "Elsewhere"}]}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    await organizer.organize_library()
    assert notes_store.get_note(guide["id"])["tab_id"] == home_tab["id"]

    # Re-import-equivalent: assign_deterministic on the SAME (existing,
    # pinned) note_id, deriving a DIFFERENT collection from a path — must
    # also refuse to move it (the side path master's review specifically
    # flagged: mark_organized's own pin guard, not organize_library's filter,
    # is what closes this).
    other_root = Path("/shared-docs")
    other_path = other_root / "somewhere-else" / "guide.md"
    organizer.assign_deterministic(guide["id"], other_path, other_root)
    assert notes_store.get_note(guide["id"])["tab_id"] == home_tab["id"]


async def test_organize_library_note_blurb_uses_summary_not_abstract(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm
    _note_with_summary(notes_store, "My Note", "THE_SUMMARY_TEXT")
    seen_content = []

    async def fake_transform_note(content, *, instruction, schema):
        seen_content.append(content)
        return {"placements": []}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    await organizer.organize_library()

    assert "THE_SUMMARY_TEXT" in seen_content[0]


async def test_organize_library_handles_mixed_notes_and_guides_in_one_call(
    organizer_llm, notes_store, monkeypatch
):
    """ONE batched LLM call covers both kinds — the money-printer guard
    applies regardless of how many kinds are in the library."""
    organizer, pipeline_mod = organizer_llm
    note = _note_with_summary(notes_store, "A Note", "note summary")
    guide = _guide_with_abstract(notes_store, "A Guide", "guide abstract")
    call_count = 0

    async def fake_transform_note(content, *, instruction, schema):
        nonlocal call_count
        call_count += 1
        return {
            "placements": [
                {"note_id": note["id"], "collection": "Notes Folder"},
                {"note_id": guide["id"], "collection": "Guides Collection"},
            ]
        }

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert call_count == 1  # ONE batched call for both kinds
    assert result["considered"] == 2
    assert set(result["reassigned"]) == {note["id"], guide["id"]}

    note_tab = notes_store.get_tab(notes_store.get_note(note["id"])["tab_id"])
    guide_tab = notes_store.get_tab(notes_store.get_note(guide["id"])["tab_id"])
    assert note_tab["kind"] == "folder"
    assert guide_tab["kind"] == "collection"


async def test_organize_library_note_and_guide_can_reuse_same_name_in_different_namespaces(
    organizer_llm, notes_store, monkeypatch
):
    """A folder named "Widgets" and a collection named "Widgets" are
    DIFFERENT tabs — notes and guides never intermix, even on a name clash."""
    organizer, pipeline_mod = organizer_llm
    note = _note_with_summary(notes_store, "A Note", "note about widgets")
    guide = _guide_with_abstract(notes_store, "A Guide", "guide about widgets")

    async def fake_transform_note(content, *, instruction, schema):
        return {
            "placements": [
                {"note_id": note["id"], "collection": "Widgets"},
                {"note_id": guide["id"], "collection": "Widgets"},
            ]
        }

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    await organizer.organize_library()

    note_tab_id = notes_store.get_note(note["id"])["tab_id"]
    guide_tab_id = notes_store.get_note(guide["id"])["tab_id"]
    assert note_tab_id != guide_tab_id
    assert notes_store.get_tab(note_tab_id)["kind"] == "folder"
    assert notes_store.get_tab(guide_tab_id)["kind"] == "collection"


async def test_organize_library_ignores_unstructured_notes(organizer_llm, notes_store, monkeypatch):
    """A draft note (pipeline=None) has no content signal to organize by —
    must be excluded from the batch entirely, not sent to the LLM with an
    empty blurb."""
    organizer, pipeline_mod = organizer_llm
    notes_store.add_note("still a draft, never structured")
    called = []

    async def fake_transform_note(content, *, instruction, schema):
        called.append(1)
        return {"placements": []}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)
    result = await organizer.organize_library()

    assert result == {"considered": 0, "reassigned": [], "new_collections": []}
    assert called == []  # short-circuited before spending an LLM call


# ---------------------------------------------------------------------------
# organize_after_structuring — the post-structuring hook + its re-entrancy
# guard (defense-in-depth against the reaper-cascade bug class; see the
# function's docstring for the audit that the current write path is safe).
# ---------------------------------------------------------------------------


async def test_organize_after_structuring_calls_organize_library_scoped_to_note(
    organizer, monkeypatch
):
    calls: list[list[str] | None] = []

    async def fake_organize_library(note_ids=None):
        calls.append(note_ids)
        return {"considered": 1, "reassigned": [], "new_collections": []}

    monkeypatch.setattr(organizer, "organize_library", fake_organize_library)
    await organizer.organize_after_structuring("note123")

    assert calls == [["note123"]]


async def test_organize_after_structuring_swallows_errors(organizer, monkeypatch):
    async def failing_organize_library(note_ids=None):
        raise RuntimeError("boom")

    monkeypatch.setattr(organizer, "organize_library", failing_organize_library)
    await organizer.organize_after_structuring("note123")  # must not raise


async def test_organize_after_structuring_reentrancy_guard_skips_when_already_organizing(
    organizer, monkeypatch
):
    calls: list[list[str] | None] = []

    async def fake_organize_library(note_ids=None):
        calls.append(note_ids)
        return {"considered": 0, "reassigned": [], "new_collections": []}

    monkeypatch.setattr(organizer, "organize_library", fake_organize_library)
    organizer._ORGANIZING_NOTE_IDS.add("note123")
    try:
        await organizer.organize_after_structuring("note123")
    finally:
        organizer._ORGANIZING_NOTE_IDS.discard("note123")

    assert calls == []


async def test_organize_sweep_loop_returns_immediately_when_disabled(organizer, monkeypatch):
    monkeypatch.setattr(organizer, "_SWEEP_ENABLED", False)
    await organizer.organize_sweep_loop()  # must return, not hang


# ---------------------------------------------------------------------------
# Freeze-class invariant (2026-07-11) — same bug class + same live-caught
# instance as chat-102d8b5fd82f's kitty audit (test_kitty_freeze_class_
# invariant.py), but in the library-organize sweep instead of roster_recovery:
# organize_library() called notes.list_notes/list_tabs/get_or_create_folder
# (each a full synchronous JSONL reparse) directly on the event loop, once
# per reassigned item. On the hourly full-library sweep (note_ids=None) this
# pinned the daemon's MainThread for the entire duration — caught live via
# `py-spy dump` mid-incident (MainThread stuck in json.loads, called via
# organize_library -> get_or_create_folder -> list_tabs -> list_notes ->
# _fold_index -> _read_jsonl), not by inspection. Every existing test above
# mocks notes.list_notes/list_tabs implicitly through the real (but tiny,
# instant) isolated_state fixture, so none of them would have caught a
# missing asyncio.to_thread wrap — this test proves the CLASS (event loop
# stays live during the call), not just a return value.
# ---------------------------------------------------------------------------


async def test_organize_library_offloads_list_notes_without_blocking_loop(
    organizer_llm, notes_store, monkeypatch
):
    organizer, pipeline_mod = organizer_llm

    async def fake_transform_note(content, *, instruction, schema):
        return {"placements": []}

    monkeypatch.setattr(pipeline_mod, "transform_note", fake_transform_note)

    release = threading.Event()
    entered = threading.Event()
    real_list_notes = notes_store.list_notes

    def _blocking_list_notes(*args, **kwargs):
        entered.set()
        if not release.wait(timeout=5.0):
            raise AssertionError("test never released the blocking list_notes call")
        return real_list_notes(*args, **kwargs)

    monkeypatch.setattr(notes_store, "list_notes", _blocking_list_notes)
    try:
        task = asyncio.create_task(organizer.organize_library())

        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "organize_library never reached the list_notes call"

        # PROOF: the background thread is still parked inside list_notes
        # (release not yet set), yet the event loop keeps running other work.
        ticked = False
        for _ in range(5):
            await asyncio.sleep(0)
            ticked = True
        assert ticked
        assert not task.done(), (
            "organize_library completed while list_notes should still be "
            "blocked — this means the call ran SYNCHRONOUSLY on the event "
            "loop instead of being offloaded, i.e. the freeze-class bug is back"
        )

        release.set()
        result = await asyncio.wait_for(task, timeout=5.0)
        assert result == {"considered": 0, "reassigned": [], "new_collections": []}
    finally:
        release.set()  # in case of an early failure, don't leave the thread parked
