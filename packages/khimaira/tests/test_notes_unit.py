"""Round-trip + unhappy-path coverage for khimaira.monitor.notes (Phase 1a)."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def notes_store(isolated_state, monkeypatch):
    """Re-root the notes store on the same tmp XDG_STATE_HOME as isolated_state."""
    from khimaira.monitor import notes as notes_mod

    importlib.reload(notes_mod)
    yield notes_mod
    importlib.reload(notes_mod)


def test_add_and_get_note_round_trip(notes_store):
    note = notes_store.add_note("some raw pasted text", tab_id="proj-a", title="My note")
    fetched = notes_store.get_note(note["id"])
    assert fetched["raw_text"] == "some raw pasted text"
    assert fetched["title"] == "My note"
    assert fetched["tab_id"] == "proj-a"
    assert fetched["status"] == "draft"
    assert fetched["pipeline"] is None
    assert fetched["training"]["promoted"] is False


def test_add_note_derives_title_and_default_tab(notes_store):
    note = notes_store.add_note("first line here\nmore text")
    assert note["title"] == "first line here"
    assert note["tab_id"] == "default"


def test_list_notes_empty_store_returns_empty_list(notes_store):
    assert notes_store.list_notes() == []


def test_list_notes_includes_raw_text_and_pipeline(notes_store):
    """Listing must carry full render data (raw_text/pipeline/training) —
    the frontend renders note cards straight from the list response,
    no per-note get_note() round trip."""
    note = notes_store.add_note("full text here", tab_id="t1")
    notes_store.set_pipeline(
        note["id"],
        {
            "summary": "s",
            "technical": "t",
            "plain": "p",
            "organized_md": "m",
            "tags": [],
            "entities": [],
        },
    )
    listed = notes_store.list_notes(tab_id="t1")
    assert listed[0]["raw_text"] == "full text here"
    assert listed[0]["pipeline"]["summary"] == "s"
    assert listed[0]["training"]["promoted"] is False


def test_list_notes_filters_by_tab_and_sorts_recent_first(notes_store):
    a = notes_store.add_note("a", tab_id="tab1")
    notes_store.add_note("b", tab_id="tab2")
    notes_store.update_note(a["id"], title="a-updated")
    listed = notes_store.list_notes(tab_id="tab1")
    assert [n["id"] for n in listed] == [a["id"]]
    all_notes = notes_store.list_notes()
    assert len(all_notes) == 2


def test_update_note_round_trip(notes_store):
    note = notes_store.add_note("raw", tab_id="t1")
    updated = notes_store.update_note(note["id"], title="new title", tab_id="t2")
    assert updated["title"] == "new title"
    assert updated["tab_id"] == "t2"
    refetched = notes_store.get_note(note["id"])
    assert refetched["title"] == "new title"
    assert refetched["tab_id"] == "t2"


def test_update_note_rejects_unknown_field(notes_store):
    note = notes_store.add_note("raw")
    with pytest.raises(ValueError, match="Unknown note field"):
        notes_store.update_note(note["id"], not_a_real_field="x")


def test_update_note_rejects_invalid_status(notes_store):
    note = notes_store.add_note("raw")
    with pytest.raises(ValueError, match="Invalid status"):
        notes_store.update_note(note["id"], status="not-a-status")


def test_update_note_pipeline_patch_merges(notes_store):
    note = notes_store.add_note("raw")
    notes_store.set_pipeline(note["id"], {"summary": "s1", "tags": ["a"]})
    updated = notes_store.update_note(note["id"], pipeline={"summary": "edited"})
    assert updated["pipeline"]["summary"] == "edited"
    assert updated["pipeline"]["tags"] == ["a"]


def test_set_pipeline_round_trip_marks_processed(notes_store):
    note = notes_store.add_note("raw")
    pipeline = {
        "summary": "TL;DR",
        "technical": "tech",
        "plain": "plain",
        "organized_md": "# md",
        "tags": ["x"],
        "entities": ["y"],
    }
    updated = notes_store.set_pipeline(note["id"], pipeline)
    assert updated["status"] == "processed"
    assert updated["pipeline"] == pipeline
    refetched = notes_store.get_note(note["id"])
    assert refetched["pipeline"] == pipeline
    assert refetched["status"] == "processed"


def test_promote_note_round_trip(notes_store):
    note = notes_store.add_note("raw")
    promoted = notes_store.promote_note(note["id"])
    assert promoted["training"]["promoted"] is True
    assert promoted["training"]["promoted_at"] is not None
    assert promoted["status"] == "promoted"
    refetched = notes_store.get_note(note["id"])
    assert refetched["training"]["promoted"] is True


def test_delete_note_round_trip(notes_store):
    note = notes_store.add_note("raw", tab_id="t1")
    result = notes_store.delete_note(note["id"])
    assert result == {"id": note["id"], "deleted": True}
    with pytest.raises(ValueError, match="No note with id"):
        notes_store.get_note(note["id"])
    assert notes_store.list_notes() == []


def test_get_note_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No note with id"):
        notes_store.get_note("no-such-note")


def test_update_note_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No note with id"):
        notes_store.update_note("no-such-note", title="x")


def test_delete_note_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No note with id"):
        notes_store.delete_note("no-such-note")


def test_set_pipeline_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No note with id"):
        notes_store.set_pipeline("no-such-note", {})


def test_index_survives_corrupt_line(notes_store):
    """A corrupt line in index.jsonl is skipped, not fatal (mirrors
    sessions._read_jsonl's existing corrupt-line tolerance)."""
    good = notes_store.add_note("kept")
    index_path = notes_store._index_path()
    with index_path.open("a", encoding="utf-8") as f:
        f.write("{not valid json\n")
    listed = notes_store.list_notes()
    assert [n["id"] for n in listed] == [good["id"]]


# ---------------------------------------------------------------------------
# Tabs
# ---------------------------------------------------------------------------


def test_add_and_get_tab_round_trip(notes_store):
    tab = notes_store.add_tab(title="My Tab")
    fetched = notes_store.get_tab(tab["id"])
    assert fetched["title"] == "My Tab"
    assert fetched["note_ids"] == []


def test_add_tab_default_title(notes_store):
    tab = notes_store.add_tab()
    assert tab["title"].startswith("Tab ")


def test_list_tabs_empty_store_returns_empty_list(notes_store):
    assert notes_store.list_tabs() == []


def test_tab_note_ids_derived_from_live_notes(notes_store):
    tab = notes_store.add_tab(title="grouped")
    n1 = notes_store.add_note("a", tab_id=tab["id"])
    n2 = notes_store.add_note("b", tab_id=tab["id"])
    notes_store.add_note("c", tab_id="other-tab")
    fetched = notes_store.get_tab(tab["id"])
    assert set(fetched["note_ids"]) == {n1["id"], n2["id"]}
    notes_store.delete_note(n1["id"])
    refetched = notes_store.get_tab(tab["id"])
    assert refetched["note_ids"] == [n2["id"]]


def test_update_tab_round_trip(notes_store):
    tab = notes_store.add_tab(title="old")
    updated = notes_store.update_tab(tab["id"], title="new")
    assert updated["title"] == "new"
    refetched = notes_store.get_tab(tab["id"])
    assert refetched["title"] == "new"


def test_update_tab_rejects_unknown_field(notes_store):
    tab = notes_store.add_tab()
    with pytest.raises(ValueError, match="Unknown tab field"):
        notes_store.update_tab(tab["id"], not_real="x")


def test_get_tab_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No tab with id"):
        notes_store.get_tab("no-such-tab")


def test_update_tab_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No tab with id"):
        notes_store.update_tab("no-such-tab", title="x")
