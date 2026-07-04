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


def test_add_note_defaults_repo_and_north_star_fields(notes_store):
    note = notes_store.add_note("raw")
    assert note["repo"] == "khimaira"
    assert note["history"] == []
    assert note["last_validated_at"] is None
    assert note["validated_git_sha"] is None


def test_add_note_repo_override(notes_store):
    note = notes_store.add_note("raw", repo="jeevy_portal")
    assert note["repo"] == "jeevy_portal"


def test_apply_validation_current_no_history_churn(notes_store):
    note = notes_store.add_note("raw")
    notes_store.set_pipeline(note["id"], {"summary": "s1"})
    updated = notes_store.apply_validation(note["id"], git_sha="abc123", new_pipeline=None)
    assert updated["last_validated_at"] is not None
    assert updated["validated_git_sha"] == "abc123"
    assert updated["pipeline"] == {"summary": "s1"}
    assert updated["history"] == []


def test_apply_validation_heal_pushes_history(notes_store):
    note = notes_store.add_note("raw")
    notes_store.set_pipeline(note["id"], {"summary": "old"})
    notes_store.apply_validation(note["id"], git_sha="sha1", new_pipeline=None)

    healed = notes_store.apply_validation(
        note["id"], git_sha="sha2", new_pipeline={"summary": "new"}
    )
    assert healed["pipeline"] == {"summary": "new"}
    assert healed["validated_git_sha"] == "sha2"
    assert len(healed["history"]) == 1
    assert healed["history"][0]["pipeline"] == {"summary": "old"}
    assert healed["history"][0]["validated_git_sha"] == "sha1"
    # raw_text is never touched by validation.
    assert healed["raw_text"] == "raw"


def test_apply_validation_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No note with id"):
        notes_store.apply_validation("no-such-note", git_sha="abc")


_BASE_PIPELINE = {
    "summary": "s",
    "technical": "t",
    "plain": "p",
    "organized_md": "# original",
    "tags": ["a"],
    "entities": ["b"],
}


def test_backfill_drops_spurious_wording_only_heal(notes_store):
    """Regression for the pre-fix dict-equality bug: a history entry whose
    pipeline differs from the current one ONLY in organized_md is spurious
    wording noise, not a real heal — must be dropped."""
    note = notes_store.add_note("raw")
    notes_store.set_pipeline(note["id"], _BASE_PIPELINE)
    notes_store.apply_validation(note["id"], git_sha="sha1", new_pipeline=None)
    # Simulate the old bug: only organized_md drifted, everything else identical.
    notes_store.apply_validation(
        note["id"],
        git_sha="sha2",
        new_pipeline={**_BASE_PIPELINE, "organized_md": "# reworded, same substance"},
    )
    assert len(notes_store.get_note(note["id"])["history"]) == 1

    cleaned = notes_store.backfill_drop_spurious_heals(note["id"])
    assert cleaned["history"] == []
    assert cleaned["pipeline"]["organized_md"] == "# reworded, same substance"  # current untouched


def test_backfill_keeps_real_heal(notes_store):
    """A heal that changes actual substance (not just organized_md wording)
    must survive the backfill untouched."""
    note = notes_store.add_note("raw")
    notes_store.set_pipeline(note["id"], _BASE_PIPELINE)
    notes_store.apply_validation(note["id"], git_sha="sha1", new_pipeline=None)
    notes_store.apply_validation(
        note["id"], git_sha="sha2", new_pipeline={**_BASE_PIPELINE, "summary": "genuinely new"}
    )

    cleaned = notes_store.backfill_drop_spurious_heals(note["id"])
    assert len(cleaned["history"]) == 1
    assert cleaned["history"][0]["pipeline"] == _BASE_PIPELINE


def test_backfill_is_idempotent_on_clean_history(notes_store):
    note = notes_store.add_note("raw")
    notes_store.set_pipeline(note["id"], _BASE_PIPELINE)
    notes_store.apply_validation(note["id"], git_sha="sha1", new_pipeline=None)

    first = notes_store.backfill_drop_spurious_heals(note["id"])
    second = notes_store.backfill_drop_spurious_heals(note["id"])
    assert first == second
    assert first["history"] == []


def test_backfill_all_reports_only_changed_notes(notes_store):
    spurious = notes_store.add_note("raw")
    notes_store.set_pipeline(spurious["id"], _BASE_PIPELINE)
    notes_store.apply_validation(spurious["id"], git_sha="sha1", new_pipeline=None)
    notes_store.apply_validation(
        spurious["id"],
        git_sha="sha2",
        new_pipeline={**_BASE_PIPELINE, "organized_md": "# reworded only"},
    )

    clean = notes_store.add_note("other raw")
    notes_store.set_pipeline(clean["id"], _BASE_PIPELINE)
    notes_store.apply_validation(clean["id"], git_sha="sha1", new_pipeline=None)
    notes_store.apply_validation(
        clean["id"], git_sha="sha2", new_pipeline={**_BASE_PIPELINE, "summary": "real change"}
    )

    changed = notes_store.backfill_drop_spurious_heals_all()
    assert changed == [spurious["id"]]


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


def test_list_notes_sorts_by_created_at_not_updated_at(notes_store):
    """Regression: sorting by updated_at reshuffled the list every time an
    older note got touched by a revalidate/heal pass (only updated_at
    changes). Joseph wants stable newest-created-first ordering.

    Stamps created_at explicitly (rather than relying on real wall-clock
    gaps between two add_note() calls) per the no-wall-clock-timing rule —
    two calls microseconds apart could otherwise tie."""
    a = notes_store.add_note("a")
    b = notes_store.add_note("b")
    record_a = notes_store.get_note(a["id"])
    record_b = notes_store.get_note(b["id"])
    record_a["created_at"] = "2026-01-01T00:00:00+00:00"
    record_b["created_at"] = "2026-01-02T00:00:00+00:00"
    notes_store._write_note_atomic(a["id"], record_a)
    notes_store._write_note_atomic(b["id"], record_b)
    notes_store._append_jsonl(notes_store._index_path(), notes_store._index_stub(record_a))
    notes_store._append_jsonl(notes_store._index_path(), notes_store._index_stub(record_b))

    # Touch `a` after stamping — updated_at(a) > created_at(b) now, but
    # creation order must still win.
    notes_store.update_note(a["id"], title="a-touched")
    listed = notes_store.list_notes()
    assert [n["id"] for n in listed] == [b["id"], a["id"]]


def test_list_notes_repo_filter_includes_general_bucket(notes_store):
    """repo=None is the "All projects" view; repo=<x> scopes to that repo
    PLUS the General bucket (cross-cutting notes always stay visible)."""
    khimaira_note = notes_store.add_note("a", repo="khimaira")
    jeevy_note = notes_store.add_note("b", repo="jeevy_portal")
    general_note = notes_store.add_note("c", repo=notes_store.GENERAL_REPO)

    scoped = notes_store.list_notes(repo="khimaira")
    assert {n["id"] for n in scoped} == {khimaira_note["id"], general_note["id"]}

    all_projects = notes_store.list_notes()
    assert {n["id"] for n in all_projects} == {
        khimaira_note["id"],
        jeevy_note["id"],
        general_note["id"],
    }


def test_update_note_repo_change_resets_validation_state(notes_store):
    """Changing repo re-anchors future validation — the old validated_git_sha
    is meaningless against a different repo's git history."""
    note = notes_store.add_note("raw", repo="khimaira")
    notes_store.apply_validation(note["id"], git_sha="deadbeef")
    validated = notes_store.get_note(note["id"])
    assert validated["validated_git_sha"] == "deadbeef"
    assert validated["last_validated_at"] is not None

    updated = notes_store.update_note(note["id"], repo="jeevy_portal")
    assert updated["validated_git_sha"] is None
    assert updated["last_validated_at"] is None
    assert updated["repo"] == "jeevy_portal"


def test_update_note_same_repo_keeps_validation_state(notes_store):
    """Setting repo to its CURRENT value (e.g. an unrelated field edit that
    happens to pass repo through) must not spuriously reset validation."""
    note = notes_store.add_note("raw", repo="khimaira")
    notes_store.apply_validation(note["id"], git_sha="deadbeef")

    updated = notes_store.update_note(note["id"], repo="khimaira", title="new title")
    assert updated["validated_git_sha"] == "deadbeef"


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


def test_set_pipeline_promotes_title_when_given(notes_store):
    note = notes_store.add_note("raw", title="truncated first line")
    pipeline = {
        "summary": "TL;DR",
        "technical": "tech",
        "plain": "plain",
        "organized_md": "# md",
        "tags": ["x"],
        "entities": ["y"],
    }
    updated = notes_store.set_pipeline(note["id"], pipeline, title="A proper generated title")
    assert updated["title"] == "A proper generated title"
    assert updated["pipeline"] == pipeline  # title never lands inside the pipeline dict


def test_set_pipeline_keeps_existing_title_when_none_given(notes_store):
    note = notes_store.add_note("raw", title="keep me")
    updated = notes_store.set_pipeline(note["id"], {})
    assert updated["title"] == "keep me"


def test_apply_validation_backfills_title_independent_of_new_pipeline(notes_store):
    """title is applied whenever given, regardless of whether new_pipeline
    is None (unchanged) or a real heal — the two are independent knobs."""
    note = notes_store.add_note("raw", title="old title")
    unchanged = notes_store.apply_validation(note["id"], git_sha="sha1", title="fresh title 1")
    assert unchanged["title"] == "fresh title 1"
    assert unchanged["history"] == []

    healed = notes_store.apply_validation(
        note["id"], git_sha="sha2", new_pipeline={"summary": "new"}, title="fresh title 2"
    )
    assert healed["title"] == "fresh title 2"
    assert len(healed["history"]) == 1


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


# ---------------------------------------------------------------------------
# Resolution + lifecycle (v2 roster loop)
# ---------------------------------------------------------------------------


def test_add_note_defaults_resolution_fields(notes_store):
    note = notes_store.add_note("raw")
    assert note["resolution"] == ""
    assert note["resolved_by"] == ""
    assert note["resolved_at"] is None


def test_add_resolution_round_trip(notes_store):
    note = notes_store.add_note("raw")
    resolved = notes_store.add_resolution(note["id"], "did the fix", resolved_by="agent-1")
    assert resolved["resolution"] == "did the fix"
    assert resolved["resolved_by"] == "agent-1"
    assert resolved["resolved_at"] is not None
    refetched = notes_store.get_note(note["id"])
    assert refetched["resolution"] == "did the fix"
    assert refetched["resolved_by"] == "agent-1"
    assert refetched["resolved_at"] is not None
    # additive-only — raw_text and pipeline untouched
    assert refetched["raw_text"] == "raw"
    assert refetched["pipeline"] is None


def test_add_resolution_defaults_resolved_by(notes_store):
    note = notes_store.add_note("raw")
    resolved = notes_store.add_resolution(note["id"], "fixed it")
    assert resolved["resolved_by"] == ""


def test_add_resolution_empty_string_clears_fields(notes_store):
    note = notes_store.add_note("raw")
    notes_store.add_resolution(note["id"], "fixed it", resolved_by="agent-1")
    cleared = notes_store.add_resolution(note["id"], "", resolved_by="ignored")
    assert cleared["resolution"] == ""
    assert cleared["resolved_by"] == ""
    assert cleared["resolved_at"] is None


def test_add_resolution_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No note with id"):
        notes_store.add_resolution("no-such-note", "fixed it")


def test_derive_lifecycle_captured_by_default(notes_store):
    note = notes_store.add_note("raw")
    assert notes_store.derive_lifecycle(note) == "captured"


def test_derive_lifecycle_reviewed_after_pipeline(notes_store):
    note = notes_store.add_note("raw")
    processed = notes_store.set_pipeline(note["id"], {"summary": "s"})
    assert notes_store.derive_lifecycle(processed) == "reviewed"


def test_derive_lifecycle_resolved_once_resolution_lands(notes_store):
    note = notes_store.add_note("raw")
    notes_store.set_pipeline(note["id"], {"summary": "s"})
    resolved = notes_store.add_resolution(note["id"], "fixed it")
    assert notes_store.derive_lifecycle(resolved) == "resolved"


def test_list_notes_includes_resolution_fields_and_lifecycle(notes_store):
    note = notes_store.add_note("raw", tab_id="t1")
    notes_store.add_resolution(note["id"], "fixed it", resolved_by="agent-1")
    listed = notes_store.list_notes(tab_id="t1")
    assert listed[0]["resolution"] == "fixed it"
    assert listed[0]["resolved_by"] == "agent-1"
    assert listed[0]["resolved_at"] is not None
    assert listed[0]["lifecycle"] == "resolved"


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


# ---------------------------------------------------------------------------
# Grimoire (2026-07-04): study guides — a distinct KIND of note, housed +
# rendered rather than re-expressed. LOAD-BEARING INVARIANT tested
# throughout: raw_text is never touched by anything in this module.
# ---------------------------------------------------------------------------


def test_add_study_guide_sets_kind_and_defaults(notes_store):
    guide = notes_store.add_study_guide("# Guide\n\nBody text.")
    assert guide["kind"] == "study_guide"
    assert guide["source_path"] is None
    assert guide["organized_at"] is None
    assert guide["pipeline"] is None
    assert guide["status"] == "draft"
    assert guide["raw_text"] == "# Guide\n\nBody text."


def test_add_study_guide_records_source_path(notes_store):
    guide = notes_store.add_study_guide("body", source_path="/tmp/foo.md")
    assert guide["source_path"] == "/tmp/foo.md"


def test_add_note_defaults_kind_to_note(notes_store):
    note = notes_store.add_note("raw")
    assert note["kind"] == "note"
    assert note["source_path"] is None
    assert note["organized_at"] is None


def test_index_stub_defaults_kind_for_legacy_records(notes_store):
    """A note record written before `kind` existed (no key in the JSON at
    all) must default to "note" when read through the index projection —
    not crash, not silently become a "study_guide"."""
    note = notes_store.add_note("raw")
    legacy_record = notes_store.get_note(note["id"])
    del legacy_record["kind"]
    stub = notes_store._index_stub(legacy_record)
    assert stub["kind"] == "note"


def test_set_study_guide_pipeline_never_touches_title(notes_store):
    guide = notes_store.add_study_guide("body", title="Original Title")
    pipeline = {"abstract": "a", "toc": [], "tags": [], "entities": []}
    updated = notes_store.set_study_guide_pipeline(guide["id"], pipeline)
    assert updated["title"] == "Original Title"
    assert updated["pipeline"] == pipeline
    assert updated["status"] == "processed"
    assert updated["structured_at"] is not None


def test_derive_lifecycle_study_guide_housed_then_organized(notes_store):
    guide = notes_store.add_study_guide("body")
    assert notes_store.derive_lifecycle(guide) == "housed"

    organized = notes_store.mark_organized(guide["id"])
    assert notes_store.derive_lifecycle(organized) == "organized"


def test_derive_lifecycle_study_guide_ignores_resolution_fields(notes_store):
    """A guide's lifecycle must never fall through to the note
    captured/reviewed/resolved chain, even if resolution fields somehow
    got set on it."""
    guide = notes_store.add_study_guide("body")
    guide["resolution"] = "should not matter"
    assert notes_store.derive_lifecycle(guide) == "housed"


def test_mark_organized_stamps_without_refiling_when_tab_id_omitted(notes_store):
    guide = notes_store.add_study_guide("body", tab_id="original-tab")
    updated = notes_store.mark_organized(guide["id"])
    assert updated["tab_id"] == "original-tab"
    assert updated["organized_at"] is not None


def test_mark_organized_refiles_when_tab_id_given(notes_store):
    guide = notes_store.add_study_guide("body", tab_id="original-tab")
    updated = notes_store.mark_organized(guide["id"], tab_id="new-tab")
    assert updated["tab_id"] == "new-tab"


def test_find_by_source_path_finds_existing(notes_store):
    guide = notes_store.add_study_guide("body", source_path="/tmp/x.md")
    found = notes_store.find_by_source_path("/tmp/x.md")
    assert found is not None
    assert found["id"] == guide["id"]


def test_find_by_source_path_returns_none_when_missing(notes_store):
    assert notes_store.find_by_source_path("/tmp/does-not-exist.md") is None


def test_list_notes_kind_filter(notes_store):
    note = notes_store.add_note("raw")
    guide = notes_store.add_study_guide("body")

    notes_only = notes_store.list_notes(kind="note")
    assert [n["id"] for n in notes_only] == [note["id"]]

    guides_only = notes_store.list_notes(kind="study_guide")
    assert [n["id"] for n in guides_only] == [guide["id"]]

    both = notes_store.list_notes()
    assert {n["id"] for n in both} == {note["id"], guide["id"]}


def test_add_tab_defaults_kind_folder(notes_store):
    tab = notes_store.add_tab(title="My Tab")
    assert tab["kind"] == "folder"


def test_add_tab_kind_collection(notes_store):
    tab = notes_store.add_tab(title="My Collection", kind="collection")
    assert tab["kind"] == "collection"


def test_add_tab_invalid_kind_raises(notes_store):
    with pytest.raises(ValueError, match="Invalid tab kind"):
        notes_store.add_tab(title="x", kind="not-a-real-kind")


def test_update_tab_kind_is_mutable(notes_store):
    tab = notes_store.add_tab(title="x")
    updated = notes_store.update_tab(tab["id"], kind="collection")
    assert updated["kind"] == "collection"


def test_update_tab_invalid_kind_raises(notes_store):
    tab = notes_store.add_tab(title="x")
    with pytest.raises(ValueError, match="Invalid tab kind"):
        notes_store.update_tab(tab["id"], kind="bogus")


def test_list_tabs_defaults_kind_for_legacy_tab_records(notes_store):
    """A tab record written before `kind` existed must default to "folder"
    when read — not crash."""
    tab = notes_store.add_tab(title="legacy")
    # Simulate a pre-migration tab record with no "kind" key at all.
    folded = notes_store._fold_tabs()
    raw = dict(folded[tab["id"]])
    del raw["kind"]
    out = notes_store._with_note_ids(raw)
    assert out["kind"] == "folder"


def test_get_or_create_collection_creates_when_absent(notes_store):
    tab = notes_store.get_or_create_collection("New Collection")
    assert tab["kind"] == "collection"
    assert tab["title"] == "New Collection"


def test_get_or_create_collection_reuses_existing_case_insensitive(notes_store):
    first = notes_store.get_or_create_collection("My Collection")
    second = notes_store.get_or_create_collection("my collection")
    assert first["id"] == second["id"]
    assert len([t for t in notes_store.list_tabs() if t["kind"] == "collection"]) == 1


def test_get_or_create_collection_does_not_match_folders(notes_store):
    """A "folder"-kind tab with the same title must NOT be mistaken for a
    matching collection — kind is part of the lookup key."""
    notes_store.add_tab(title="Ambiguous Name", kind="folder")
    collection = notes_store.get_or_create_collection("Ambiguous Name")
    assert collection["kind"] == "collection"
    kinds = {t["kind"] for t in notes_store.list_tabs() if t["title"] == "Ambiguous Name"}
    assert kinds == {"folder", "collection"}
