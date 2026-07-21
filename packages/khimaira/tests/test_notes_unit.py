"""Round-trip + unhappy-path coverage for khimaira.monitor.notes (Phase 1a)."""

from __future__ import annotations

import importlib
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest


@pytest.fixture
def notes_store(isolated_state, monkeypatch):
    """Re-root the notes store on the same tmp XDG_STATE_HOME as isolated_state."""
    from khimaira.monitor import notes as notes_mod

    importlib.reload(notes_mod)
    yield notes_mod
    importlib.reload(notes_mod)


def test_add_and_get_note_round_trip(notes_store):
    tab = notes_store.add_tab(title="proj-a")
    note = notes_store.add_note("some raw pasted text", tab_id=tab["id"], title="My note")
    fetched = notes_store.get_note(note["id"])
    assert fetched["raw_text"] == "some raw pasted text"
    assert fetched["title"] == "My note"
    assert fetched["tab_id"] == tab["id"]
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


# ---------------------------------------------------------------------------
# Sensitive / credential-safe notes (2026-07-04)
# ---------------------------------------------------------------------------


def test_add_note_defaults_not_sensitive(notes_store):
    note = notes_store.add_note("raw")
    assert note["sensitive"] is False
    assert note["llm_text"] is None
    assert note["redactions"] is None


def test_add_note_sensitive_computes_redacted_twin(notes_store):
    secret = "sk-ant-" + "a" * 30
    note = notes_store.add_note(f"API_KEY={secret}", sensitive=True)
    assert note["sensitive"] is True
    assert note["raw_text"] == f"API_KEY={secret}"  # REAL text unchanged
    assert secret not in note["llm_text"]
    assert len(note["redactions"]) >= 1
    assert all(secret not in r["placeholder"] for r in note["redactions"])


def test_add_note_sensitive_auto_derived_title_never_contains_the_secret(notes_store):
    """Regression: _derive_title takes the literal FIRST LINE of content —
    when the secret IS the first line, an auto-derived title would leak it
    verbatim into _index_stub/list views/ask-synthesis headers, all of
    which read `title` unredacted (title is a display label, not gated by
    llm_view). Auto-derivation must use the redacted twin when sensitive."""
    secret = "sk-ant-" + "z" * 30
    note = notes_store.add_note(f"key: {secret}", sensitive=True)
    assert secret not in note["title"]


def test_add_note_sensitive_explicit_title_is_used_as_is(notes_store):
    """An EXPLICITLY given title is trusted verbatim regardless of
    sensitivity — only AUTO-derivation is redirected through the redacted
    twin; a human typing a title is their own call, same as raw_text."""
    secret = "sk-ant-" + "y" * 30
    note = notes_store.add_note(f"key: {secret}", title="My Credential Note", sensitive=True)
    assert note["title"] == "My Credential Note"


def test_add_study_guide_sensitive_computes_redacted_twin(notes_store):
    secret = "sk-ant-" + "b" * 30
    guide = notes_store.add_study_guide(f"# Guide\n\nAPI_KEY={secret}", sensitive=True)
    assert guide["sensitive"] is True
    assert secret not in guide["llm_text"]
    assert guide["raw_text"] == f"# Guide\n\nAPI_KEY={secret}"


def test_llm_view_returns_raw_text_when_not_sensitive(notes_store):
    note = notes_store.add_note("hello world")
    assert notes_store.llm_view(note) == "hello world"


def test_llm_view_returns_redacted_twin_when_sensitive(notes_store):
    secret = "sk-ant-" + "c" * 30
    note = notes_store.add_note(f"key: {secret}", sensitive=True)
    view = notes_store.llm_view(note)
    assert secret not in view
    assert view == note["llm_text"]


def test_llm_view_empty_string_when_sensitive_but_llm_text_missing(notes_store):
    """Defensive: a malformed/legacy record with sensitive=True but no
    llm_text must fail SAFE (empty string), never fall through to raw_text."""
    record = {"sensitive": True, "raw_text": "should never appear", "llm_text": None}
    assert notes_store.llm_view(record) == ""


def test_update_note_raw_text_change_re_redacts_sensitive_note(notes_store):
    old_secret = "sk-ant-" + "d" * 30
    new_secret = "sk-ant-" + "e" * 30
    note = notes_store.add_note(f"key: {old_secret}", sensitive=True)

    updated = notes_store.update_note(note["id"], raw_text=f"key: {new_secret}")

    assert old_secret not in updated["llm_text"]
    assert new_secret not in updated["llm_text"]  # re-redacted against the NEW text


def test_update_note_flipping_sensitive_on_computes_redaction(notes_store):
    secret = "sk-ant-" + "f" * 30
    note = notes_store.add_note(f"key: {secret}")
    assert note["llm_text"] is None

    updated = notes_store.update_note(note["id"], sensitive=True)

    assert updated["sensitive"] is True
    assert secret not in updated["llm_text"]


def test_update_note_flipping_sensitive_off_clears_redaction(notes_store):
    secret = "sk-ant-" + "g" * 30
    note = notes_store.add_note(f"key: {secret}", sensitive=True)

    updated = notes_store.update_note(note["id"], sensitive=False)

    assert updated["sensitive"] is False
    assert updated["llm_text"] is None
    assert updated["redactions"] is None
    assert notes_store.llm_view(updated) == f"key: {secret}"  # falls through to raw_text


def test_update_note_raw_text_unchanged_does_not_recompute_redaction(notes_store):
    secret = "sk-ant-" + "h" * 30
    note = notes_store.add_note(f"key: {secret}", sensitive=True)
    original_llm_text = note["llm_text"]

    updated = notes_store.update_note(note["id"], title="renamed")

    assert updated["llm_text"] == original_llm_text  # unchanged, not recomputed


def test_index_stub_masks_raw_text_for_sensitive_notes(notes_store):
    secret = "sk-ant-" + "i" * 30
    notes_store.add_note(f"key: {secret}", sensitive=True)

    listed = notes_store.list_notes()
    assert len(listed) == 1
    assert secret not in listed[0]["raw_text"]
    assert listed[0]["sensitive"] is True


def test_index_stub_does_not_mask_raw_text_for_non_sensitive_notes(notes_store):
    notes_store.add_note("plain content")
    listed = notes_store.list_notes()
    assert listed[0]["raw_text"] == "plain content"
    assert listed[0]["sensitive"] is False


def test_get_note_returns_real_raw_text_for_sensitive_note(notes_store):
    """The single-note reader fetch (get_note, NOT list_notes) always
    returns the REAL raw_text — only bulk list/search results are masked."""
    secret = "sk-ant-" + "j" * 30
    note = notes_store.add_note(f"key: {secret}", sensitive=True)
    fetched = notes_store.get_note(note["id"])
    assert fetched["raw_text"] == f"key: {secret}"


def test_promote_note_refuses_sensitive_notes(notes_store):
    note = notes_store.add_note("secret stuff", sensitive=True)
    with pytest.raises(ValueError, match="sensitive"):
        notes_store.promote_note(note["id"])


def test_promote_note_still_works_for_non_sensitive_notes(notes_store):
    note = notes_store.add_note("ordinary note")
    promoted = notes_store.promote_note(note["id"])
    assert promoted["training"]["promoted"] is True


def test_unpromote_note_reverses_promote(notes_store):
    note = notes_store.add_note("ordinary note")
    notes_store.promote_note(note["id"])
    restored = notes_store.unpromote_note(note["id"])
    assert restored["training"]["promoted"] is False
    assert restored["training"]["promoted_at"] is None
    assert restored["status"] == "processed"


def test_unpromote_note_restores_archived_to_resolved_lifecycle(notes_store):
    note = notes_store.add_note("problem text")
    notes_store.add_resolution(note["id"], "fixed it")
    notes_store.promote_note(note["id"])
    assert notes_store.derive_lifecycle(notes_store.get_note(note["id"])) == "archived"
    notes_store.unpromote_note(note["id"])
    assert notes_store.derive_lifecycle(notes_store.get_note(note["id"])) == "resolved"


def test_unpromote_note_preserves_resolution(notes_store):
    note = notes_store.add_note("problem text")
    notes_store.add_resolution(note["id"], "fixed it")
    notes_store.promote_note(note["id"])
    restored = notes_store.unpromote_note(note["id"])
    assert restored["resolution"] == "fixed it"


# ---------------------------------------------------------------------------
# Priority flags (2026-07-04)
# ---------------------------------------------------------------------------


def test_add_note_defaults_priority_normal(notes_store):
    note = notes_store.add_note("raw")
    assert note["priority"] == "normal"


def test_add_study_guide_defaults_priority_normal(notes_store):
    guide = notes_store.add_study_guide("# G\n\nbody")
    assert guide["priority"] == "normal"


def test_update_note_priority_round_trips(notes_store):
    note = notes_store.add_note("raw")
    updated = notes_store.update_note(note["id"], priority="urgent")
    assert updated["priority"] == "urgent"
    assert notes_store.get_note(note["id"])["priority"] == "urgent"


def test_update_note_rejects_invalid_priority(notes_store):
    note = notes_store.add_note("raw")
    with pytest.raises(ValueError, match="Invalid priority"):
        notes_store.update_note(note["id"], priority="critical")


def test_list_notes_priority_filter(notes_store):
    tab = notes_store.add_tab(title="t1")
    notes_store.add_note("a", tab_id=tab["id"])
    urgent = notes_store.add_note("b", tab_id=tab["id"])
    notes_store.update_note(urgent["id"], priority="urgent")

    urgent_only = notes_store.list_notes(priority="urgent")
    assert len(urgent_only) == 1
    assert urgent_only[0]["id"] == urgent["id"]

    all_notes = notes_store.list_notes()
    assert len(all_notes) == 2


def test_index_stub_carries_priority(notes_store):
    note = notes_store.add_note("raw")
    notes_store.update_note(note["id"], priority="high")
    listed = notes_store.list_notes()
    assert listed[0]["priority"] == "high"


# ---------------------------------------------------------------------------
# Testing-workflow status (2026-07-07)
# ---------------------------------------------------------------------------


def test_add_note_defaults_test_status_untested(notes_store):
    note = notes_store.add_note("raw")
    assert note["test_status"] == "untested"


def test_add_study_guide_defaults_test_status_untested(notes_store):
    guide = notes_store.add_study_guide("# G\n\nbody")
    assert guide["test_status"] == "untested"


def test_update_note_test_status_round_trips(notes_store):
    note = notes_store.add_note("raw")
    updated = notes_store.update_note(note["id"], test_status="in_review")
    assert updated["test_status"] == "in_review"
    assert notes_store.get_note(note["id"])["test_status"] == "in_review"


def test_update_note_rejects_invalid_test_status(notes_store):
    note = notes_store.add_note("raw")
    with pytest.raises(ValueError, match="Invalid test_status"):
        notes_store.update_note(note["id"], test_status="done")


def test_list_notes_test_status_filter(notes_store):
    tab = notes_store.add_tab(title="t1")
    notes_store.add_note("a", tab_id=tab["id"])
    tested = notes_store.add_note("b", tab_id=tab["id"])
    notes_store.update_note(tested["id"], test_status="tested")

    tested_only = notes_store.list_notes(test_status="tested")
    assert len(tested_only) == 1
    assert tested_only[0]["id"] == tested["id"]

    all_notes = notes_store.list_notes()
    assert len(all_notes) == 2


def test_index_stub_carries_test_status(notes_store):
    note = notes_store.add_note("raw")
    notes_store.update_note(note["id"], test_status="needs_testing")
    listed = notes_store.list_notes()
    assert listed[0]["test_status"] == "needs_testing"


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


def test_backfill_handles_raw_text_revision_entries_without_crashing(notes_store):
    """Grimoire Phase 4 regression: backfill_drop_spurious_heals assumed
    EVERY history entry has a "pipeline" key (history[i+1]["pipeline"]) —
    a raw_text-revision entry (update_note's new snapshot) has no such key
    and must not raise KeyError, must never be treated as a spurious heal,
    and must not corrupt an adjacent pipeline-heal entry's own evaluation."""
    note = notes_store.add_note("v1")
    notes_store.set_pipeline(note["id"], _BASE_PIPELINE)
    notes_store.apply_validation(note["id"], git_sha="sha1", new_pipeline=None)
    # A spurious wording-only heal (organized_md only) — should be dropped...
    notes_store.apply_validation(
        note["id"],
        git_sha="sha2",
        new_pipeline={**_BASE_PIPELINE, "organized_md": "# reworded, same substance"},
    )
    # ...followed by a raw_text edit, which appends a DIFFERENT-shaped entry.
    notes_store.update_note(note["id"], raw_text="v2")
    before = notes_store.get_note(note["id"])
    assert len(before["history"]) == 2
    assert "pipeline" in before["history"][0]
    assert "raw_text" in before["history"][1]

    cleaned = notes_store.backfill_drop_spurious_heals(note["id"])  # must not raise

    assert len(cleaned["history"]) == 1
    assert cleaned["history"][0]["raw_text"] == "v1"  # the raw_text entry always kept
    assert cleaned["raw_text"] == "v2"


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
    tab = notes_store.add_tab(title="t1")
    note = notes_store.add_note("full text here", tab_id=tab["id"])
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
    listed = notes_store.list_notes(tab_id=tab["id"])
    assert listed[0]["raw_text"] == "full text here"
    assert listed[0]["pipeline"]["summary"] == "s"
    assert listed[0]["training"]["promoted"] is False


def test_list_notes_filters_by_tab_and_sorts_recent_first(notes_store):
    tab1 = notes_store.add_tab(title="tab1")
    tab2 = notes_store.add_tab(title="tab2")
    a = notes_store.add_note("a", tab_id=tab1["id"])
    notes_store.add_note("b", tab_id=tab2["id"])
    notes_store.update_note(a["id"], title="a-updated")
    listed = notes_store.list_notes(tab_id=tab1["id"])
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
    tab1 = notes_store.add_tab(title="t1")
    tab2 = notes_store.add_tab(title="t2")
    note = notes_store.add_note("raw", tab_id=tab1["id"])
    updated = notes_store.update_note(note["id"], title="new title", tab_id=tab2["id"])
    assert updated["title"] == "new title"
    assert updated["tab_id"] == tab2["id"]
    refetched = notes_store.get_note(note["id"])
    assert refetched["title"] == "new title"
    assert refetched["tab_id"] == tab2["id"]


def test_update_note_raw_text_change_snapshots_prior_version(notes_store):
    """Grimoire Phase 4: a raw_text-changing edit is otherwise a lossy
    overwrite of the deliverable — the OUTGOING text must land in history
    BEFORE it's replaced."""
    note = notes_store.add_note("original text")
    updated = notes_store.update_note(note["id"], raw_text="revised text")

    assert updated["raw_text"] == "revised text"
    assert len(updated["history"]) == 1
    assert updated["history"][0]["raw_text"] == "original text"
    assert "replaced_at" in updated["history"][0]
    # Distinguished from a pipeline-heal entry by key shape, not a "kind" tag.
    assert "pipeline" not in updated["history"][0]


def test_update_note_raw_text_same_value_does_not_snapshot(notes_store):
    """Resubmitting the SAME raw_text (e.g. a no-op save) must not bloat
    history with an identical snapshot."""
    note = notes_store.add_note("same text")
    updated = notes_store.update_note(note["id"], raw_text="same text")
    assert updated["history"] == []


def test_update_note_raw_text_snapshots_accumulate_across_edits(notes_store):
    note = notes_store.add_note("v1")
    notes_store.update_note(note["id"], raw_text="v2")
    updated = notes_store.update_note(note["id"], raw_text="v3")

    assert updated["raw_text"] == "v3"
    assert [h["raw_text"] for h in updated["history"]] == ["v1", "v2"]


def test_update_note_raw_text_snapshot_applies_to_study_guides_too(notes_store):
    """The safety net applies to the KIND of note a REVISE Apply targets —
    a guide's raw_text is exactly what's at risk of a lossy overwrite."""
    guide = notes_store.add_study_guide("# Guide\n\noriginal body")
    updated = notes_store.update_note(guide["id"], raw_text="# Guide\n\nrevised body")

    assert updated["raw_text"] == "# Guide\n\nrevised body"
    assert len(updated["history"]) == 1
    assert updated["history"][0]["raw_text"] == "# Guide\n\noriginal body"


def test_update_note_raw_text_snapshot_and_pipeline_heal_history_coexist(notes_store):
    """A note with BOTH kinds of history entry (a pipeline heal from
    apply_validation, then a raw_text edit) must not confuse the two — each
    keeps its own shape in the same list."""
    note = notes_store.add_note("v1")
    notes_store.set_pipeline(note["id"], {"summary": "old summary"})
    notes_store.apply_validation(note["id"], git_sha="sha1", new_pipeline=None)
    notes_store.apply_validation(
        note["id"], git_sha="sha2", new_pipeline={"summary": "new summary"}
    )
    updated = notes_store.update_note(note["id"], raw_text="v2")

    assert len(updated["history"]) == 2
    assert updated["history"][0]["pipeline"] == {"summary": "old summary"}
    assert "raw_text" not in updated["history"][0]
    assert updated["history"][1]["raw_text"] == "v1"
    assert "pipeline" not in updated["history"][1]


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
    tab = notes_store.add_tab(title="t1")
    note = notes_store.add_note("raw", tab_id=tab["id"])
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


def test_derive_lifecycle_archived_after_resolution_and_promotion(notes_store):
    note = notes_store.add_note("raw")
    resolved = notes_store.add_resolution(note["id"], "fixed it")
    assert notes_store.derive_lifecycle(resolved) == "resolved"

    promoted = notes_store.promote_note(note["id"])
    assert notes_store.derive_lifecycle(promoted) == "archived"


def test_derive_lifecycle_promoted_without_resolution_is_reviewed(notes_store):
    note = notes_store.add_note("raw")
    promoted = notes_store.promote_note(note["id"])
    assert notes_store.derive_lifecycle(promoted) == "reviewed"


def test_list_notes_includes_resolution_fields_and_lifecycle(notes_store):
    tab = notes_store.add_tab(title="t1")
    note = notes_store.add_note("raw", tab_id=tab["id"])
    notes_store.add_resolution(note["id"], "fixed it", resolved_by="agent-1")
    listed = notes_store.list_notes(tab_id=tab["id"])
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
    other_tab = notes_store.add_tab(title="other")
    n1 = notes_store.add_note("a", tab_id=tab["id"])
    n2 = notes_store.add_note("b", tab_id=tab["id"])
    notes_store.add_note("c", tab_id=other_tab["id"])
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
    original = notes_store.add_tab(title="original", kind="collection")
    guide = notes_store.add_study_guide("body", tab_id=original["id"])
    updated = notes_store.mark_organized(guide["id"])
    assert updated["tab_id"] == original["id"]
    assert updated["organized_at"] is not None


def test_mark_organized_refiles_when_tab_id_given(notes_store):
    original = notes_store.add_tab(title="original", kind="collection")
    destination = notes_store.add_tab(title="new", kind="collection")
    guide = notes_store.add_study_guide("body", tab_id=original["id"])
    updated = notes_store.mark_organized(guide["id"], tab_id=destination["id"])
    assert updated["tab_id"] == destination["id"]


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


def test_get_or_create_folder_creates_when_absent(notes_store):
    tab = notes_store.get_or_create_folder("New Folder")
    assert tab["kind"] == "folder"
    assert tab["title"] == "New Folder"


def test_get_or_create_folder_reuses_existing_case_insensitive(notes_store):
    first = notes_store.get_or_create_folder("My Folder")
    second = notes_store.get_or_create_folder("my folder")
    assert first["id"] == second["id"]
    assert len([t for t in notes_store.list_tabs() if t["kind"] == "folder"]) == 1


def test_get_or_create_folder_does_not_match_collections(notes_store):
    notes_store.add_tab(title="Ambiguous Name", kind="collection")
    folder = notes_store.get_or_create_folder("Ambiguous Name")
    assert folder["kind"] == "folder"


# ---------------------------------------------------------------------------
# FILE-MANAGER (2026-07-04) — tab hierarchy (parent_id) + pinned_placement/starred
# ---------------------------------------------------------------------------


def test_list_tabs_defaults_parent_id_for_legacy_tab_records(notes_store):
    """A tab record written before `parent_id` existed must read as root
    (None) — not crash. Mirrors the existing kind-defaulting test."""
    tab = notes_store.add_tab(title="legacy")
    folded = notes_store._fold_tabs()
    raw = dict(folded[tab["id"]])
    del raw["parent_id"]
    out = notes_store._with_note_ids(raw)
    assert out["parent_id"] is None


def test_add_tab_with_parent_id_round_trips(notes_store):
    root = notes_store.add_tab(title="Root", kind="collection")
    child = notes_store.add_tab(title="Child", kind="collection", parent_id=root["id"])
    assert child["parent_id"] == root["id"]
    assert notes_store.get_tab(child["id"])["parent_id"] == root["id"]


def test_add_tab_dangling_parent_id_raises(notes_store):
    with pytest.raises(ValueError, match="No tab with id"):
        notes_store.add_tab(title="x", parent_id="no-such-tab")


def test_add_tab_cross_kind_parent_raises_tab_validation_error(notes_store):
    """Homogeneous subtree: a folder cannot nest under a collection parent."""
    collection = notes_store.add_tab(title="Coll", kind="collection")
    with pytest.raises(notes_store.TabValidationError, match="don't nest"):
        notes_store.add_tab(title="Sub", kind="folder", parent_id=collection["id"])


def test_add_tab_sibling_title_collision_raises(notes_store):
    parent = notes_store.add_tab(title="Parent", kind="collection")
    notes_store.add_tab(title="API", kind="collection", parent_id=parent["id"])
    with pytest.raises(notes_store.TabValidationError, match="already exists under this parent"):
        notes_store.add_tab(title="api", kind="collection", parent_id=parent["id"])


def test_add_tab_same_title_different_parents_is_fine(notes_store):
    parent_a = notes_store.add_tab(title="A", kind="collection")
    parent_b = notes_store.add_tab(title="B", kind="collection")
    child_a = notes_store.add_tab(title="API", kind="collection", parent_id=parent_a["id"])
    child_b = notes_store.add_tab(title="API", kind="collection", parent_id=parent_b["id"])
    assert child_a["id"] != child_b["id"]


def test_update_tab_reparent_round_trip(notes_store):
    root = notes_store.add_tab(title="Root", kind="collection")
    child = notes_store.add_tab(title="Child", kind="collection")
    updated = notes_store.update_tab(child["id"], parent_id=root["id"])
    assert updated["parent_id"] == root["id"]


def test_update_tab_reparent_to_root_is_fine(notes_store):
    root = notes_store.add_tab(title="Root", kind="collection")
    child = notes_store.add_tab(title="Child", kind="collection", parent_id=root["id"])
    updated = notes_store.update_tab(child["id"], parent_id=None)
    assert updated["parent_id"] is None


def test_update_tab_self_parent_raises(notes_store):
    tab = notes_store.add_tab(title="x")
    with pytest.raises(notes_store.TabValidationError, match="cannot be its own parent"):
        notes_store.update_tab(tab["id"], parent_id=tab["id"])


def test_update_tab_reparent_dangling_parent_raises(notes_store):
    tab = notes_store.add_tab(title="x")
    with pytest.raises(ValueError, match="No tab with id"):
        notes_store.update_tab(tab["id"], parent_id="no-such-tab")


def test_update_tab_reparent_cross_kind_raises(notes_store):
    collection = notes_store.add_tab(title="Coll", kind="collection")
    folder = notes_store.add_tab(title="Fold", kind="folder")
    with pytest.raises(notes_store.TabValidationError, match="don't nest"):
        notes_store.update_tab(folder["id"], parent_id=collection["id"])


def test_update_tab_reparent_direct_cycle_raises(notes_store):
    """A tab cannot become its own parent's parent — the simplest cycle."""
    a = notes_store.add_tab(title="A", kind="collection")
    b = notes_store.add_tab(title="B", kind="collection", parent_id=a["id"])
    with pytest.raises(notes_store.TabValidationError, match="cycle"):
        notes_store.update_tab(a["id"], parent_id=b["id"])


def test_update_tab_reparent_deep_cycle_raises(notes_store):
    """A -> B -> C; reparenting A under C (A's own grandchild) must be
    rejected — proves the ancestor walk isn't just checking direct parent."""
    a = notes_store.add_tab(title="A", kind="collection")
    b = notes_store.add_tab(title="B", kind="collection", parent_id=a["id"])
    c = notes_store.add_tab(title="C", kind="collection", parent_id=b["id"])
    with pytest.raises(notes_store.TabValidationError, match="cycle"):
        notes_store.update_tab(a["id"], parent_id=c["id"])


def test_update_tab_reparent_sibling_collision_raises(notes_store):
    parent = notes_store.add_tab(title="Parent", kind="collection")
    notes_store.add_tab(title="API", kind="collection", parent_id=parent["id"])
    other_root_tab = notes_store.add_tab(title="API", kind="collection")
    with pytest.raises(notes_store.TabValidationError, match="already exists under this parent"):
        notes_store.update_tab(other_root_tab["id"], parent_id=parent["id"])


def test_get_or_create_collection_parent_scoped_sibling_isolation(notes_store):
    """THE required invariant test (closes the whole Q6 class): two
    "API" collections under DIFFERENT parents must be DISTINCT tabs, and
    get_or_create must resolve each call to the tab actually shown under
    that parent — not an arbitrary same-named sibling elsewhere in the tree."""
    parent_1 = notes_store.add_tab(title="Backend", kind="collection")
    parent_2 = notes_store.add_tab(title="Frontend", kind="collection")

    api_1 = notes_store.get_or_create_collection("API", parent_id=parent_1["id"])
    api_2 = notes_store.get_or_create_collection("API", parent_id=parent_2["id"])

    assert api_1["id"] != api_2["id"]
    assert api_1["parent_id"] == parent_1["id"]
    assert api_2["parent_id"] == parent_2["id"]

    # A guide filed "into API" under parent_1 must land in api_1, not api_2.
    guide = notes_store.add_study_guide("# G\n\nbody")
    refiled = notes_store.mark_organized(guide["id"], tab_id=api_1["id"])
    assert refiled["tab_id"] == api_1["id"]
    assert guide["id"] in notes_store.get_tab(api_1["id"])["note_ids"]
    assert guide["id"] not in notes_store.get_tab(api_2["id"])["note_ids"]

    # Re-calling get_or_create for parent_2's "API" must NOT return api_1.
    again = notes_store.get_or_create_collection("api", parent_id=parent_2["id"])
    assert again["id"] == api_2["id"]


def test_delete_tab_unknown_id_raises(notes_store):
    with pytest.raises(ValueError, match="No tab with id"):
        notes_store.delete_tab("no-such-tab")


def test_delete_tab_loses_nothing(notes_store):
    """THE required invariant test: parent -> child collections, each with
    a member guide, delete the parent -> (a) no infinite loop, (b) child
    tab reparented (to root, since parent was at root), (c) every member
    note resolves to a live tab_id (list_notes count unchanged, no dead
    tab_id)."""
    parent = notes_store.add_tab(title="Parent", kind="collection")
    child = notes_store.add_tab(title="Child", kind="collection", parent_id=parent["id"])
    guide_in_parent = notes_store.add_study_guide("# P\n\nbody")
    notes_store.mark_organized(guide_in_parent["id"], tab_id=parent["id"])
    guide_in_child = notes_store.add_study_guide("# C\n\nbody")
    notes_store.mark_organized(guide_in_child["id"], tab_id=child["id"])

    before_count = len(notes_store.list_notes())
    result = notes_store.delete_tab(parent["id"])
    assert result == {"id": parent["id"], "deleted": True}

    # (a) no infinite loop already proven by reaching this line.
    # (b) child tab reparented to root (parent's own parent, which was root).
    refetched_child = notes_store.get_tab(child["id"])
    assert refetched_child["parent_id"] is None

    # (c) every member note resolves to a live, non-dead tab_id.
    assert len(notes_store.list_notes()) == before_count
    all_tab_ids = {t["id"] for t in notes_store.list_tabs()}
    for note_id in (guide_in_parent["id"], guide_in_child["id"]):
        note = notes_store.get_note(note_id)
        assert (
            note["tab_id"] == notes_store.PERSONAL_TAB_ID
            or note["tab_id"] in all_tab_ids
            or (note["tab_id"] == "default")
        )

    # The parent's direct member specifically must have been re-filed to
    # the default tab (parent had no parent of its own — root).
    assert notes_store.get_note(guide_in_parent["id"])["tab_id"] == "default"
    # The child's own member note is untouched (child tab still exists).
    assert notes_store.get_note(guide_in_child["id"])["tab_id"] == child["id"]

    with pytest.raises(ValueError, match="No tab with id"):
        notes_store.get_tab(parent["id"])


def test_delete_tab_deep_reparents_children_one_level_not_to_root(notes_store):
    """A -> B -> C; deleting B reparents C to A (B's own parent), not
    flattened all the way to root — proves the "one level up" behavior,
    not just "always root."""
    a = notes_store.add_tab(title="A", kind="collection")
    b = notes_store.add_tab(title="B", kind="collection", parent_id=a["id"])
    c = notes_store.add_tab(title="C", kind="collection", parent_id=b["id"])

    notes_store.delete_tab(b["id"])

    assert notes_store.get_tab(c["id"])["parent_id"] == a["id"]


def test_delete_tab_unpins_relocated_notes(notes_store):
    """A note pinned to a deleted tab must be un-pinned on relocation — a
    pin to a tab that no longer exists is meaningless; the organizer should
    be free to reclaim it."""
    tab = notes_store.add_tab(title="Doomed")
    note = notes_store.add_note("body", tab_id=tab["id"])
    notes_store.update_note(note["id"], pinned_placement=True)

    notes_store.delete_tab(tab["id"])

    updated = notes_store.get_note(note["id"])
    assert updated["tab_id"] == "default"
    assert updated["pinned_placement"] is False


def test_delete_tab_sibling_name_collision_at_destination_does_not_crash(notes_store):
    """Edge case found via audit: deleting a parent whose child shares a
    title with an EXISTING tab at the destination must not crash the whole
    delete (a rare cosmetic naming collision is a far better failure mode
    than losing the child tab or aborting mid-delete)."""
    root = notes_store.add_tab(title="Root", kind="collection")
    # Pre-existing sibling already at the destination (root).
    notes_store.add_tab(title="Sub", kind="collection")
    parent = notes_store.add_tab(title="Parent", kind="collection", parent_id=root["id"])
    child = notes_store.add_tab(title="Sub", kind="collection", parent_id=parent["id"])

    # Must not raise.
    notes_store.delete_tab(parent["id"])

    assert notes_store.get_tab(child["id"])["parent_id"] == root["id"]


def test_mark_organized_pinned_note_keeps_tab_id(notes_store):
    """Pin respected at the mark_organized chokepoint itself — closes the
    whole class in one place regardless of caller."""
    tab_a = notes_store.add_tab(title="A")
    tab_b = notes_store.add_tab(title="B")
    guide = notes_store.add_study_guide("# G\n\nbody", tab_id=tab_a["id"])
    notes_store.update_note(guide["id"], pinned_placement=True)

    result = notes_store.mark_organized(guide["id"], tab_id=tab_b["id"])

    assert result["tab_id"] == tab_a["id"]
    assert result["organized_at"] is not None


def test_mark_organized_unpinned_note_still_relocates(notes_store):
    tab_a = notes_store.add_tab(title="A")
    tab_b = notes_store.add_tab(title="B")
    guide = notes_store.add_study_guide("# G\n\nbody", tab_id=tab_a["id"])

    result = notes_store.mark_organized(guide["id"], tab_id=tab_b["id"])

    assert result["tab_id"] == tab_b["id"]


def test_list_notes_starred_filter(notes_store):
    starred_note = notes_store.add_note("a")
    notes_store.update_note(starred_note["id"], starred=True)
    notes_store.add_note("b")

    starred_only = notes_store.list_notes(starred=True)
    assert [n["id"] for n in starred_only] == [starred_note["id"]]

    unstarred_only = notes_store.list_notes(starred=False)
    assert starred_note["id"] not in [n["id"] for n in unstarred_only]


def test_list_notes_archived_filter(notes_store):
    active = notes_store.add_note("active")
    resolved = notes_store.add_note("resolved")
    notes_store.add_resolution(resolved["id"], "fixed")
    archived = notes_store.add_note("archived")
    notes_store.add_resolution(archived["id"], "fixed")
    notes_store.promote_note(archived["id"])

    archived_only = notes_store.list_notes(archived=True)
    assert [note["id"] for note in archived_only] == [archived["id"]]

    active_only = notes_store.list_notes(archived=False)
    assert {note["id"] for note in active_only} == {active["id"], resolved["id"]}

    all_notes = notes_store.list_notes(archived=None)
    assert {note["id"] for note in all_notes} == {
        active["id"],
        resolved["id"],
        archived["id"],
    }


def test_list_notes_archived_filter_rederives_stale_persisted_lifecycle(notes_store):
    note = notes_store.add_note("legacy archived")
    notes_store.add_resolution(note["id"], "fixed")
    promoted = notes_store.promote_note(note["id"])

    stale_stub = {
        **notes_store._index_stub(promoted),
        "lifecycle": "resolved",
    }
    notes_store._append_jsonl(notes_store._index_path(), stale_stub)
    assert notes_store._fold_index()[note["id"]]["lifecycle"] == "resolved"

    archived = notes_store.list_notes(archived=True)
    assert [record["id"] for record in archived] == [note["id"]]
    assert archived[0]["lifecycle"] == "archived"


def test_update_note_pinned_placement_and_starred_round_trip(notes_store):
    note = notes_store.add_note("body")
    assert note["pinned_placement"] is False
    assert note["starred"] is False

    updated = notes_store.update_note(note["id"], pinned_placement=True, starred=True)
    assert updated["pinned_placement"] is True
    assert updated["starred"] is True

    stub = next(s for s in notes_store.list_notes() if s["id"] == note["id"])
    assert stub["pinned_placement"] is True
    assert stub["starred"] is True


# ---------------------------------------------------------------------------
# Index self-compaction (2026-07-11) — live production incident: index.jsonl
# is append-only and _index_stub carries full raw_text, so a note touched N
# times over its life keeps N copies of its own content on disk even though
# only the latest is ever read. A 70MB/3700-line index (folding to ~150 live
# notes) made every list_notes() call a multi-second synchronous parse. Fold
# is provably lossless (already discards everything but the latest per id),
# so _fold_index persists that fold back to disk once raw lines meaningfully
# exceed the folded count. These tests prove: (1) it fires past the
# threshold and preserves exact content, (2) it does NOT fire under it (no
# gratuitous writes on a healthy library), (3) a concurrent append during
# compaction is never dropped — the actual failure mode a naive read-fold-
# then-blind-rewrite would have under the real production write pattern
# (many concurrent add_resolution calls, per this session's stress test).
# ---------------------------------------------------------------------------


def test_fold_index_compacts_past_threshold_and_preserves_latest_state(notes_store):
    note = notes_store.add_note("v1")
    for i in range(notes_store._COMPACT_EXCESS_LINES + 5):
        notes_store.update_note(note["id"], title=f"v{i}")

    raw_before = notes_store._read_jsonl(notes_store._index_path())
    assert len(raw_before) > notes_store._COMPACT_EXCESS_LINES  # bloated, pre-compaction

    folded = notes_store._fold_index()

    raw_after = notes_store._read_jsonl(notes_store._index_path())
    assert len(raw_after) == 1  # compacted to exactly the one surviving note
    assert folded[note["id"]]["title"] == f"v{notes_store._COMPACT_EXCESS_LINES + 4}"
    # get_note (reads notes/<id>.json directly, unaffected by index compaction)
    # still agrees — compaction never touched the source-of-truth note body.
    assert notes_store.get_note(note["id"])["title"] == f"v{notes_store._COMPACT_EXCESS_LINES + 4}"


def test_fold_index_does_not_compact_under_threshold(notes_store):
    note = notes_store.add_note("v1")
    notes_store.update_note(note["id"], title="v2")  # 2 raw lines, well under threshold

    raw_before = notes_store._read_jsonl(notes_store._index_path())
    notes_store._fold_index()
    raw_after = notes_store._read_jsonl(notes_store._index_path())

    assert len(raw_after) == len(raw_before) == 2  # untouched — no gratuitous rewrite


def test_compact_index_survives_concurrent_append(notes_store, monkeypatch):
    """The race a naive compact (read -> fold -> blind rewrite, no lock)
    would lose: an _append_index_stub landing between the read and the
    rename. Real _INDEX_LOCK sharing between the two must prevent it."""
    note_a = notes_store.add_note("a")
    for i in range(notes_store._COMPACT_EXCESS_LINES + 5):
        notes_store.update_note(note_a["id"], title=f"a{i}")

    real_read_jsonl = notes_store._read_jsonl
    entered_compaction = threading.Event()
    index_reads = {"count": 0}

    def _gated_read_jsonl(path):
        result = real_read_jsonl(path)
        if path == notes_store._index_path():
            index_reads["count"] += 1
            # The FIRST call is _fold_index's own outer bloat-check read,
            # before _INDEX_LOCK is even acquired — pausing there proves
            # nothing about the lock. Gate the SECOND call: the fresh read
            # inside _compact_index_locked, taken while (correctly) holding
            # _INDEX_LOCK. A bounded sleep here (not an event the appender
            # must complete first — that would deadlock against the lock
            # the appender needs) opens exactly the race window a
            # concurrent append must not be able to land in un-serialized:
            # in the FIXED impl the appender blocks on _INDEX_LOCK for this
            # whole window; in a BROKEN (unlocked-append) impl it lands
            # immediately, right before this call's rewrite clobbers it.
            if index_reads["count"] == 2:
                entered_compaction.set()
                time.sleep(0.2)
        return result

    monkeypatch.setattr(notes_store, "_read_jsonl", _gated_read_jsonl)

    note_b_holder: list[dict] = []

    def _concurrent_append():
        entered_compaction.wait(timeout=5.0)
        note_b_holder.append(notes_store.add_note("b"))

    t = threading.Thread(target=_concurrent_append)
    t.start()
    try:
        folded = notes_store._fold_index()
    finally:
        t.join(timeout=5.0)

    assert note_b_holder, "concurrent append thread never ran"
    note_b = note_b_holder[0]
    # The concurrent append must survive — either folded directly into this
    # call's result, or present on disk for the next read to pick up.
    on_disk = notes_store._fold_index()
    assert note_b["id"] in folded or note_b["id"] in on_disk


# ---------------------------------------------------------------------------
# Tickets (2026-07-11) — local mirror of Linear issues, kind="ticket".
# Mirrors the study_guide test section above: own kind, own defaults, own
# validation, plus the read-only-synced-field + resync-idempotency
# invariants that make this a distinct sub-kind rather than a bare note.
# ---------------------------------------------------------------------------


def test_add_ticket_sets_kind_and_defaults(notes_store):
    ticket = notes_store.add_ticket("Fix the reaper race")
    assert ticket["kind"] == "ticket"
    assert ticket["title"] == "Fix the reaper race"
    assert ticket["raw_text"] == ""
    assert ticket["state"] == "Backlog"
    assert ticket["linear_priority"] == 0
    assert ticket["assignee"] is None
    assert ticket["labels"] == []
    assert ticket["project"] == ""
    assert ticket["parent_id"] is None
    assert ticket["origin"] == "local-created"
    assert ticket["linear_ref"] is None
    assert ticket["sync_state"] == "local-only"
    assert ticket["comments"] == []
    assert ticket["repo"] == notes_store.GENERAL_REPO


def test_add_ticket_local_created_needs_no_linear_ref(notes_store):
    ticket = notes_store.add_ticket("Local idea", origin="local-created")
    assert ticket["linear_ref"] is None
    assert ticket["sync_state"] == "local-only"


def test_add_ticket_linear_pulled_requires_linear_ref(notes_store):
    with pytest.raises(ValueError, match="require linear_ref"):
        notes_store.add_ticket("Mirrored", origin="linear-pulled")


def test_add_ticket_linear_pulled_defaults_sync_state_synced(notes_store):
    ticket = notes_store.add_ticket("Mirrored", origin="linear-pulled", linear_ref="LIN-1")
    assert ticket["sync_state"] == "synced"
    assert ticket["origin"] == "linear-pulled"


def test_add_ticket_rejects_invalid_state(notes_store):
    with pytest.raises(ValueError, match="Invalid ticket state"):
        notes_store.add_ticket("X", state="Nonsense")


def test_add_ticket_rejects_invalid_linear_priority(notes_store):
    with pytest.raises(ValueError, match="Invalid ticket linear_priority"):
        notes_store.add_ticket("X", linear_priority=9)


def test_add_ticket_rejects_invalid_origin(notes_store):
    with pytest.raises(ValueError, match="Invalid ticket origin"):
        notes_store.add_ticket("X", origin="bogus")


def test_add_ticket_priority_field_untouched_from_ticket_linear_priority(notes_store):
    """The base `priority` field (str enum, notebook_list's own filter) must
    stay independent of `linear_priority` (int, Linear's own scale) — no
    collision, no cross-contamination (chat-102d8b5fd82f task-8191e0a1672b)."""
    ticket = notes_store.add_ticket("X", linear_priority=4)
    assert ticket["priority"] == notes_store._DEFAULT_PRIORITY
    assert ticket["linear_priority"] == 4


def test_list_tickets_excludes_notes_and_guides(notes_store):
    ticket = notes_store.add_ticket("A ticket")
    notes_store.add_note("A note")
    notes_store.add_study_guide("A guide")

    tickets = notes_store.list_tickets()
    assert [t["id"] for t in tickets] == [ticket["id"]]


def test_list_tickets_project_filter(notes_store):
    a = notes_store.add_ticket("A", project="Langgraph")
    notes_store.add_ticket("B", project="Other")

    scoped = notes_store.list_tickets(project="Langgraph")
    assert [t["id"] for t in scoped] == [a["id"]]


def test_list_tickets_state_filter(notes_store):
    a = notes_store.add_ticket("A", state="In Progress")
    notes_store.add_ticket("B", state="Backlog")

    scoped = notes_store.list_tickets(state="In Progress")
    assert [t["id"] for t in scoped] == [a["id"]]


def test_list_tickets_assignee_filter_matches_id_or_name(notes_store):
    a = notes_store.add_ticket("A", assignee={"id": "u1", "name": "Priya"})
    notes_store.add_ticket("B", assignee={"id": "u2", "name": "Sam"})

    by_name = notes_store.list_tickets(assignee="priya")  # case-insensitive
    assert [t["id"] for t in by_name] == [a["id"]]

    by_id = notes_store.list_tickets(assignee="u1")
    assert [t["id"] for t in by_id] == [a["id"]]


def test_list_tickets_label_filter(notes_store):
    a = notes_store.add_ticket("A", labels=["bug", "frontend"])
    notes_store.add_ticket("B", labels=["docs"])

    scoped = notes_store.list_tickets(label="bug")
    assert [t["id"] for t in scoped] == [a["id"]]


def test_update_ticket_local_created_edits_synced_fields(notes_store):
    ticket = notes_store.add_ticket("Local", origin="local-created")
    updated = notes_store.update_ticket(ticket["id"], title="Renamed", state="In Progress")
    assert updated["title"] == "Renamed"
    assert updated["state"] == "In Progress"


def test_update_ticket_linear_pulled_rejects_synced_field(notes_store):
    ticket = notes_store.add_ticket("Mirrored", origin="linear-pulled", linear_ref="LIN-2")
    with pytest.raises(ValueError, match="read-only"):
        notes_store.update_ticket(ticket["id"], title="Local rename attempt")


def test_update_ticket_linear_pulled_still_allows_tab_id(notes_store):
    """Filing (tab_id) is always a local concern, even on a mirrored ticket."""
    ticket = notes_store.add_ticket("Mirrored", origin="linear-pulled", linear_ref="LIN-3")
    tab = notes_store.add_tab(title="My tickets", repo=notes_store.GENERAL_REPO)
    updated = notes_store.update_ticket(ticket["id"], tab_id=tab["id"])
    assert updated["tab_id"] == tab["id"]


def test_update_ticket_rejects_unknown_field(notes_store):
    ticket = notes_store.add_ticket("X")
    with pytest.raises(ValueError, match="Unknown ticket field"):
        notes_store.update_ticket(ticket["id"], bogus_field="y")


def test_update_ticket_rejects_non_ticket_note(notes_store):
    note = notes_store.add_note("just a note")
    with pytest.raises(ValueError, match="is not a ticket"):
        notes_store.update_ticket(note["id"], title="x")


def test_add_ticket_comment_round_trip(notes_store):
    ticket = notes_store.add_ticket("X")
    updated = notes_store.add_ticket_comment(ticket["id"], "Looks good", author="reviewer-1")
    assert len(updated["comments"]) == 1
    assert updated["comments"][0]["text"] == "Looks good"
    assert updated["comments"][0]["author"] == "reviewer-1"


def test_add_ticket_comment_allowed_on_linear_pulled(notes_store):
    """Comments are local annotations — never a synced field, so allowed
    regardless of origin."""
    ticket = notes_store.add_ticket("Mirrored", origin="linear-pulled", linear_ref="LIN-4")
    updated = notes_store.add_ticket_comment(ticket["id"], "local note", author="me")
    assert len(updated["comments"]) == 1


def test_add_ticket_comment_rejects_empty_text(notes_store):
    ticket = notes_store.add_ticket("X")
    with pytest.raises(ValueError, match="non-empty text"):
        notes_store.add_ticket_comment(ticket["id"], "   ")


def test_find_ticket_by_linear_ref_finds_and_misses(notes_store):
    ticket = notes_store.add_ticket("Mirrored", origin="linear-pulled", linear_ref="LIN-5")
    found = notes_store.find_ticket_by_linear_ref("LIN-5")
    assert found is not None
    assert found["id"] == ticket["id"]
    assert notes_store.find_ticket_by_linear_ref("does-not-exist") is None


def test_upsert_ticket_from_linear_creates_on_first_pull(notes_store):
    mapped = {"linear_ref": "LIN-10", "title": "New issue", "state": "Todo"}
    record, created = notes_store.upsert_ticket_from_linear(mapped, project="Langgraph")
    assert created is True
    assert record["kind"] == "ticket"
    assert record["origin"] == "linear-pulled"
    assert record["title"] == "New issue"
    assert record["state"] == "Todo"
    assert record["project"] == "Langgraph"
    assert record["sync_state"] == "synced"


def test_upsert_ticket_from_linear_requires_linear_ref(notes_store):
    with pytest.raises(ValueError, match="requires mapped\\['linear_ref'\\]"):
        notes_store.upsert_ticket_from_linear({"title": "no ref"}, project="Langgraph")


def test_upsert_ticket_from_linear_is_idempotent_no_duplicates(notes_store):
    """Resync idempotency — the core invariant task-8191e0a1672b asked for:
    running the same mapped issue twice must never create a second ticket,
    and the second run must only update, not re-create."""
    mapped = {"linear_ref": "LIN-11", "title": "Original title", "state": "Backlog"}
    first, first_created = notes_store.upsert_ticket_from_linear(mapped, project="Langgraph")
    assert first_created is True

    updated_mapped = {"linear_ref": "LIN-11", "title": "Renamed upstream", "state": "In Progress"}
    second, second_created = notes_store.upsert_ticket_from_linear(
        updated_mapped, project="Langgraph"
    )
    assert second_created is False
    assert second["id"] == first["id"]
    assert second["title"] == "Renamed upstream"
    assert second["state"] == "In Progress"

    all_tickets = notes_store.list_tickets(project="Langgraph")
    assert len(all_tickets) == 1, (
        "resync must never create a duplicate ticket for the same linear_ref"
    )


def test_upsert_ticket_from_linear_partial_map_leaves_other_fields_untouched(notes_store):
    """A resync entry missing a key (e.g. no `labels` in this pull's payload)
    must not clear that field on the existing ticket — only keys actually
    present in `mapped` are applied."""
    first_mapped = {"linear_ref": "LIN-12", "title": "T", "labels": ["bug"]}
    notes_store.upsert_ticket_from_linear(first_mapped, project="Langgraph")

    partial_mapped = {"linear_ref": "LIN-12", "title": "T renamed"}  # no "labels" key
    second, _ = notes_store.upsert_ticket_from_linear(partial_mapped, project="Langgraph")
    assert second["title"] == "T renamed"
    assert second["labels"] == ["bug"], "missing key in the resync payload must not clear it"


def test_upsert_ticket_from_linear_never_touches_local_annotations(notes_store):
    """resolution/comments/tab_id are local — a resync must never overwrite
    them, no matter how many times it runs."""
    mapped = {"linear_ref": "LIN-13", "title": "T"}
    record, _ = notes_store.upsert_ticket_from_linear(mapped, project="Langgraph")
    notes_store.add_ticket_comment(record["id"], "local note")
    notes_store.add_resolution(record["id"], "worked around it")
    tab = notes_store.add_tab(title="Filed", repo=notes_store.GENERAL_REPO)
    notes_store.update_ticket(record["id"], tab_id=tab["id"])

    resynced, created = notes_store.upsert_ticket_from_linear(mapped, project="Langgraph")
    assert created is False
    assert len(resynced["comments"]) == 1
    assert resynced["resolution"] == "worked around it"
    assert resynced["tab_id"] == tab["id"]


def test_index_stub_projects_ticket_fields(notes_store):
    ticket = notes_store.add_ticket(
        "X", state="In Progress", linear_priority=2, project="Langgraph", labels=["bug"]
    )
    stub = notes_store._index_stub(ticket)
    assert stub["state"] == "In Progress"
    assert stub["linear_priority"] == 2
    assert stub["project"] == "Langgraph"
    assert stub["labels"] == ["bug"]
    assert stub["comments_count"] == 0


def test_index_stub_ticket_fields_harmless_on_notes(notes_store):
    """A regular note's stub must carry the same ticket-only keys (defaulted
    None/[]/0), not KeyError — the projection is unconditional across kinds."""
    note = notes_store.add_note("regular note")
    stub = notes_store._index_stub(note)
    assert stub["state"] is None
    assert stub["labels"] == []
    assert stub["comments_count"] == 0


# ---------------------------------------------------------------------------
# Repository-scoped tabs + legacy migration
# ---------------------------------------------------------------------------


def test_tabs_are_repo_scoped_with_intentional_general_visibility(notes_store):
    tab_a = notes_store.add_tab("A", repo="repo-a")
    tab_b = notes_store.add_tab("B", repo="repo-b")
    general = notes_store.add_tab("General", repo=notes_store.GENERAL_REPO)
    assert {tab["id"] for tab in notes_store.list_tabs(repo="repo-a")} == {
        tab_a["id"],
        general["id"],
    }
    assert tab_b["id"] not in {tab["id"] for tab in notes_store.list_tabs(repo="repo-a")}
    with pytest.raises(ValueError, match="No tab"):
        notes_store.get_tab(general["id"], repo="repo-a")
    assert notes_store.get_tab(general["id"], repo=notes_store.GENERAL_REPO)["id"] == general["id"]


def test_sibling_uniqueness_is_scoped_by_repo(notes_store):
    notes_store.add_tab("Root", repo="repo-a")
    notes_store.add_tab("Root", repo="repo-b")
    with pytest.raises(notes_store.TabValidationError, match="already exists"):
        notes_store.add_tab("root", repo="repo-a")


def test_sibling_uniqueness_treats_general_repo_as_universal(notes_store):
    notes_store.add_tab("Testing", repo=notes_store.GENERAL_REPO)
    with pytest.raises(notes_store.TabValidationError, match="already exists"):
        notes_store.add_tab("testing", repo="repo-a")


def test_sibling_uniqueness_general_repo_blocked_by_any_existing_project_tab(notes_store):
    notes_store.add_tab("Testing", repo="repo-a")
    with pytest.raises(notes_store.TabValidationError, match="already exists"):
        notes_store.add_tab("testing", repo=notes_store.GENERAL_REPO)


def test_sibling_uniqueness_two_specific_repos_do_not_collide(notes_store):
    notes_store.add_tab("Testing", repo="repo-a")
    created = notes_store.add_tab("Testing", repo="repo-b")
    assert created["title"] == "Testing"


def test_cross_repo_tab_access_parent_and_mutation_are_not_found(notes_store):
    tab_b = notes_store.add_tab("B", repo="repo-b")
    original = dict(notes_store.get_tab(tab_b["id"], repo="repo-b"))
    for action in (
        lambda: notes_store.get_tab(tab_b["id"], repo="repo-a"),
        lambda: notes_store.update_tab(tab_b["id"], repo="repo-a", title="changed"),
        lambda: notes_store.delete_tab(tab_b["id"], repo="repo-a"),
        lambda: notes_store.add_tab("child", parent_id=tab_b["id"], repo="repo-a"),
    ):
        with pytest.raises(ValueError, match="No tab"):
            action()
    assert notes_store.get_tab(tab_b["id"], repo="repo-b") == original


def test_delete_and_note_ids_walk_exact_repo_only(notes_store):
    tab_a = notes_store.add_tab("A", repo="repo-a")
    tab_b = notes_store.add_tab("B", repo="repo-b")
    note_a = notes_store.add_note("a", tab_id=tab_a["id"], repo="repo-a")
    note_b = notes_store.add_note("b", tab_id=tab_b["id"], repo="repo-b")
    assert notes_store.get_tab(tab_a["id"], repo="repo-a")["note_ids"] == [note_a["id"]]
    notes_store.delete_tab(tab_a["id"], repo="repo-a")
    assert notes_store.get_note(note_a["id"])["tab_id"] == notes_store._DEFAULT_TAB_ID
    assert notes_store.get_note(note_b["id"])["tab_id"] == tab_b["id"]
    assert notes_store.get_tab(tab_b["id"], repo="repo-b")["note_ids"] == [note_b["id"]]


def test_note_assignment_and_repo_change_require_exact_destination(notes_store):
    tab_a = notes_store.add_tab("A", repo="repo-a")
    tab_b = notes_store.add_tab("B", repo="repo-b")
    with pytest.raises(ValueError, match="No tab"):
        notes_store.add_note("wrong", tab_id=tab_b["id"], repo="repo-a")
    note = notes_store.add_note("a", tab_id=tab_a["id"], repo="repo-a")
    with pytest.raises(notes_store.TabValidationError, match="destination tab_id"):
        notes_store.update_note(note["id"], repo="repo-b")
    with pytest.raises(ValueError, match="No tab"):
        notes_store.update_note(note["id"], repo="repo-b", tab_id=tab_a["id"])
    moved = notes_store.update_note(note["id"], repo="repo-b", tab_id=tab_b["id"])
    assert (moved["repo"], moved["tab_id"]) == ("repo-b", tab_b["id"])


def test_kind_change_rejects_existing_incompatible_children(notes_store):
    parent = notes_store.add_tab("parent", kind="folder", repo="repo-a")
    notes_store.add_tab("child", kind="folder", parent_id=parent["id"], repo="repo-a")
    with pytest.raises(notes_store.TabValidationError, match="direct children"):
        notes_store.update_tab(parent["id"], repo="repo-a", kind="collection")


def _seed_legacy_tab(notes_store, tab_id, title, parent_id=None, **metadata):
    notes_store._ensure_dirs()
    now = notes_store._now_iso()
    record = {
        "id": tab_id,
        "title": title,
        "kind": "folder",
        "parent_id": parent_id,
        "created_at": now,
        "updated_at": now,
        "deleted": False,
        **metadata,
    }
    notes_store._append_tab_record(record)
    return record


def _seed_kindless_tab(notes_store, tab_id, title, *, repo, parent_id=None, **metadata):
    notes_store._ensure_dirs()
    now = notes_store._now_iso()
    record = {
        "id": tab_id,
        "title": title,
        "parent_id": parent_id,
        "repo": repo,
        "created_at": now,
        "updated_at": now,
        "deleted": False,
        **metadata,
    }
    notes_store._append_tab_record(record)
    return record


def _seed_legacy_tab_note(notes_store, tab_id, repo, text):
    note = notes_store.add_note(text, repo=repo)
    note["tab_id"] = tab_id
    notes_store._write_note_atomic(note["id"], note)
    notes_store._append_index_stub(note)
    # add_note initializes the store before writing. Restore the seeded tab
    # rows to their pre-migration shape so the explicit migration run below
    # sees the intended legacy fixture rather than that eager quarantine.
    for tab in notes_store._fold_tabs().values():
        legacy = dict(tab)
        legacy.pop("repo", None)
        notes_store._append_tab_record(legacy)
    return note


def _rerun_tab_repo_migration(notes_store):
    notes_store._tab_repo_migration_path().unlink(missing_ok=True)
    notes_store._MIGRATED_BASE_DIRS.clear()
    notes_store.initialize_tab_repo_migration()


def _rerun_tab_kind_migration(notes_store):
    notes_store._tab_kind_migration_path().unlink(missing_ok=True)
    notes_store._KIND_MIGRATED_BASE_DIRS.clear()
    notes_store.initialize_tab_kind_migration()


def test_legacy_kindless_tab_blocks_duplicate_sibling(notes_store):
    # Complete migrations first so this specifically proves the central
    # read-time normalization, not the backfill that would also repair it.
    notes_store.initialize_tab_repo_migration()
    legacy = _seed_kindless_tab(
        notes_store,
        "legacy-research",
        "research",
        repo="jeevy_portal",
    )

    with pytest.raises(notes_store.TabValidationError, match="already exists"):
        notes_store.add_tab("Research", repo="jeevy_portal")

    assert notes_store._fold_tabs()[legacy["id"]]["kind"] == "folder"
    assert "kind" not in notes_store._fold_tabs_raw()[legacy["id"]]


def test_get_or_create_folder_reuses_legacy_kindless_tab(notes_store):
    notes_store.initialize_tab_repo_migration()
    legacy = _seed_kindless_tab(
        notes_store,
        "legacy-research",
        "Research",
        repo="jeevy_portal",
    )

    reused = notes_store.get_or_create_folder("research", repo="jeevy_portal")

    assert reused["id"] == legacy["id"]
    assert (
        len(
            [
                tab
                for tab in notes_store._fold_tabs().values()
                if tab["repo"] == "jeevy_portal" and tab["title"].lower() == "research"
            ]
        )
        == 1
    )


def test_tab_kind_migration_backfills_only_missing_and_is_idempotent(notes_store):
    notes_store.initialize_tab_repo_migration()
    missing = _seed_kindless_tab(
        notes_store,
        "missing-kind",
        "Legacy folder",
        repo="repo-a",
        marker="preserved",
    )
    explicit_folder = _seed_legacy_tab(
        notes_store,
        "explicit-folder",
        "Folder",
        repo="repo-a",
        marker="folder-kept",
    )
    explicit_collection = {
        **_seed_legacy_tab(
            notes_store,
            "explicit-collection",
            "Collection",
            repo="repo-a",
            marker="collection-kept",
        ),
        "kind": "collection",
    }
    notes_store._append_tab_record(explicit_collection)

    _rerun_tab_kind_migration(notes_store)

    raw = notes_store._fold_tabs_raw()
    assert raw[missing["id"]]["kind"] == "folder"
    assert raw[missing["id"]]["marker"] == "preserved"
    assert raw[explicit_folder["id"]]["kind"] == "folder"
    assert raw[explicit_folder["id"]]["marker"] == "folder-kept"
    assert raw[explicit_collection["id"]]["kind"] == "collection"
    assert raw[explicit_collection["id"]]["marker"] == "collection-kept"

    stable_tabs = notes_store._tabs_path().read_bytes()
    stable_marker = notes_store._tab_kind_migration_path().read_bytes()
    notes_store._KIND_MIGRATED_BASE_DIRS.clear()
    notes_store.initialize_tab_kind_migration()
    assert notes_store._tabs_path().read_bytes() == stable_tabs
    assert notes_store._tab_kind_migration_path().read_bytes() == stable_marker


def test_tab_repo_migration_single_repo_stamps_whole_tree(notes_store):
    root = _seed_legacy_tab(notes_store, "legacy-root", "root", custom="kept")
    child = _seed_legacy_tab(notes_store, "legacy-child", "child", root["id"])
    note = _seed_legacy_tab_note(notes_store, child["id"], "repo-a", "a")
    _rerun_tab_repo_migration(notes_store)
    assert notes_store.get_tab(root["id"], repo="repo-a")["custom"] == "kept"
    assert notes_store.get_tab(child["id"], repo="repo-a")["parent_id"] == root["id"]
    assert notes_store.get_note(note["id"])["tab_id"] == child["id"]


def test_tab_repo_migration_mixed_repos_clones_component_and_remaps_notes(notes_store):
    root = _seed_legacy_tab(notes_store, "mixed-root", "root")
    child = _seed_legacy_tab(notes_store, "mixed-child", "child", root["id"])
    note_a = _seed_legacy_tab_note(notes_store, root["id"], "repo-a", "a")
    note_b = _seed_legacy_tab_note(notes_store, child["id"], "repo-b", "b")
    _rerun_tab_repo_migration(notes_store)
    a_tabs = {tab["title"]: tab for tab in notes_store.list_tabs(repo="repo-a")}
    b_tabs = {tab["title"]: tab for tab in notes_store.list_tabs(repo="repo-b")}
    assert a_tabs["root"]["id"] == root["id"]
    assert b_tabs["root"]["id"] != root["id"]
    assert b_tabs["child"]["parent_id"] == b_tabs["root"]["id"]
    assert notes_store.get_note(note_a["id"])["tab_id"] == a_tabs["root"]["id"]
    assert notes_store.get_note(note_b["id"])["tab_id"] == b_tabs["child"]["id"]


def test_tab_repo_migration_zero_note_component_is_quarantined(notes_store):
    legacy = _seed_legacy_tab(notes_store, "empty-legacy", "empty")
    _rerun_tab_repo_migration(notes_store)
    unscoped = {tab["id"]: tab for tab in notes_store.list_tabs()}
    assert unscoped[legacy["id"]]["repo"] is None
    assert legacy["id"] not in {tab["id"] for tab in notes_store.list_tabs(repo="repo-a")}
    with pytest.raises(ValueError, match="No tab"):
        notes_store.get_tab(legacy["id"], repo="repo-a")


def test_tab_repo_migration_general_replay_and_compaction_preserve_metadata(notes_store):
    root = _seed_legacy_tab(notes_store, "general-root", "root", marker="preserved")
    general_note = _seed_legacy_tab_note(notes_store, root["id"], notes_store.GENERAL_REPO, "g")
    repo_note = _seed_legacy_tab_note(notes_store, root["id"], "repo-z", "z")
    _rerun_tab_repo_migration(notes_store)
    first_tabs = notes_store._tabs_path().read_text(encoding="utf-8")
    first_index = notes_store._index_path().read_text(encoding="utf-8")
    general_tab = notes_store.get_tab(root["id"], repo=notes_store.GENERAL_REPO)
    repo_tab = next(tab for tab in notes_store.list_tabs(repo="repo-z") if tab["repo"] == "repo-z")
    assert general_tab["marker"] == "preserved"
    assert notes_store.get_note(general_note["id"])["tab_id"] == general_tab["id"]
    assert notes_store.get_note(repo_note["id"])["tab_id"] == repo_tab["id"]
    notes_store._MIGRATED_BASE_DIRS.clear()
    notes_store.initialize_tab_repo_migration()
    assert notes_store._tabs_path().read_text(encoding="utf-8") == first_tabs
    assert notes_store._index_path().read_text(encoding="utf-8") == first_index
    with notes_store._TABS_LOCK:
        notes_store._compact_tabs_locked()
    assert notes_store.get_tab(root["id"], repo=notes_store.GENERAL_REPO)["marker"] == "preserved"


def test_tab_repo_migration_resumes_a_partially_applied_durable_plan(notes_store):
    root = _seed_legacy_tab(notes_store, "resume-root", "root")
    note = _seed_legacy_tab_note(notes_store, root["id"], "repo-a", "a")
    notes_store._tab_repo_migration_path().unlink(missing_ok=True)
    notes_store._MIGRATED_BASE_DIRS.clear()
    plan = notes_store._build_tab_repo_migration_plan()
    notes_store._atomic_write_json(notes_store._tab_repo_migration_path(), plan)
    # Simulate a crash after the first append but before note moves / marker completion.
    notes_store._append_tab_record(plan["tabs"][0])

    notes_store.initialize_tab_repo_migration()

    assert notes_store.get_tab(root["id"], repo="repo-a")["id"] == root["id"]
    assert notes_store.get_note(note["id"])["tab_id"] == root["id"]
    marker = __import__("json").loads(
        notes_store._tab_repo_migration_path().read_text(encoding="utf-8")
    )
    assert marker["status"] == "complete"


def test_add_tab_from_worker_thread_after_main_thread_migration(notes_store):
    notes_store.initialize_tab_repo_migration()
    with ThreadPoolExecutor(max_workers=1) as executor:
        tab = executor.submit(notes_store.add_tab, "threaded", repo="repo-a").result(timeout=2)
    assert notes_store.get_tab(tab["id"], repo="repo-a")["title"] == "threaded"


def test_tab_repo_migration_replay_repairs_index_after_note_file_only_crash(notes_store):
    root = _seed_legacy_tab(notes_store, "file-only-root", "root")
    _seed_legacy_tab_note(notes_store, root["id"], "repo-a", "a")
    note_b = _seed_legacy_tab_note(notes_store, root["id"], "repo-b", "b")
    notes_store._tab_repo_migration_path().unlink(missing_ok=True)
    notes_store._MIGRATED_BASE_DIRS.clear()
    plan = notes_store._build_tab_repo_migration_plan()
    target_tab_id = plan["note_tabs"][note_b["id"]]
    assert target_tab_id != root["id"]
    notes_store._atomic_write_json(notes_store._tab_repo_migration_path(), plan)

    # Exact crash boundary: the note file reached its clone, but the process
    # died before appending the corresponding index projection.
    file_record = notes_store._read_note_file(note_b["id"])
    file_record["tab_id"] = target_tab_id
    notes_store._write_note_atomic(note_b["id"], file_record)
    assert notes_store._fold_index()[note_b["id"]]["tab_id"] == root["id"]

    notes_store.initialize_tab_repo_migration()

    assert notes_store.get_note(note_b["id"])["tab_id"] == target_tab_id
    assert notes_store._fold_index()[note_b["id"]]["tab_id"] == target_tab_id
    marker = __import__("json").loads(
        notes_store._tab_repo_migration_path().read_text(encoding="utf-8")
    )
    assert marker["status"] == "complete"
    stable_bytes = (
        notes_store._tabs_path().read_bytes(),
        notes_store._index_path().read_bytes(),
        notes_store._tab_repo_migration_path().read_bytes(),
    )
    notes_store._MIGRATED_BASE_DIRS.clear()
    notes_store.initialize_tab_repo_migration()
    assert (
        notes_store._tabs_path().read_bytes(),
        notes_store._index_path().read_bytes(),
        notes_store._tab_repo_migration_path().read_bytes(),
    ) == stable_bytes
