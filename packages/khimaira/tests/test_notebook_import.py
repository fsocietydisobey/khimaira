"""Tests for khimaira.monitor.notebook_import (Grimoire Phase 1c).

Uses a disposable tmp_path directory tree standing in for
~/work/jeevy_portal/shared-docs — NEVER the real directory. Dry-run is the
default and is tested as the primary path; real import is tested against
the synthetic fixture only, never scheduling a real structuring pipeline
(schedule_pipeline is monkeypatched to a no-op — this module's own
responsibility ends at creating + filing the note; the pipeline is a
separate, already-tested concern).
"""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def notes_store(isolated_state, monkeypatch):
    from khimaira.monitor import notes as notes_mod

    importlib.reload(notes_mod)
    yield notes_mod
    importlib.reload(notes_mod)


@pytest.fixture
def importer(notes_store, monkeypatch):
    from khimaira.monitor import notebook_import as imp
    from khimaira.monitor import notebook_organizer as org
    from khimaira.monitor import notebook_pipeline as pipeline_mod

    importlib.reload(org)
    importlib.reload(imp)
    # Never actually schedule a claude -p structuring call from these tests
    # — notebook_pipeline's own scheduling/dispatch is covered in
    # test_notebook_pipeline.py; this module's job ends at create + file.
    monkeypatch.setattr(pipeline_mod, "schedule_pipeline", lambda note_id: None)
    yield imp


def _make_guide_tree(tmp_path):
    """onboarding/getting-started.md, onboarding/faq.md, architecture/kg.md,
    top-level.md (uncategorized), and a non-markdown file that must be
    skipped."""
    (tmp_path / "onboarding").mkdir()
    (tmp_path / "architecture").mkdir()
    (tmp_path / "onboarding" / "getting-started.md").write_text("# Getting Started\n\nWelcome.\n")
    (tmp_path / "onboarding" / "faq.md").write_text("# FAQ\n\nQuestions.\n")
    (tmp_path / "architecture" / "kg.md").write_text("# KG Overview\n\nHow it works.\n")
    (tmp_path / "top-level.md").write_text("# Top Level\n\nRoot doc.\n")
    (tmp_path / "notes.txt").write_text("not a guide")
    return tmp_path


def test_qualify_only_matches_markdown_files(importer, tmp_path):
    md = tmp_path / "a.md"
    md.write_text("x")
    txt = tmp_path / "b.txt"
    txt.write_text("x")
    directory = tmp_path / "subdir"
    directory.mkdir()
    assert importer.qualify(md) is True
    assert importer.qualify(txt) is False
    assert importer.qualify(directory) is False


@pytest.mark.parametrize(
    "filename",
    [
        "CHANGELOG.md",
        "changelog.md",
        "README.md",
        "readme.markdown",
        "STANDUP_NOTES.md",
        "standup-notes.md",
        "TODO.md",
        "todo.md",
    ],
)
def test_qualify_excludes_known_housekeeping_filenames(importer, tmp_path, filename):
    path = tmp_path / filename
    path.write_text("not a guide")
    assert importer.qualify(path) is False


def test_qualify_still_matches_a_real_guide_named_similarly(importer, tmp_path):
    """A guide whose title happens to contain one of the excluded words
    (but isn't a bare housekeeping filename) must still qualify — the
    exclusion is a STEM match, not a substring match."""
    path = tmp_path / "readme-driven-development.md"
    path.write_text("# Readme-Driven Development\n\nA real guide.\n")
    assert importer.qualify(path) is True


def test_derive_title_prefers_first_heading(importer, tmp_path):
    path = tmp_path / "x.md"
    assert importer._derive_title(path, "# Real Title\n\nbody") == "Real Title"


def test_derive_title_falls_back_to_filename_when_no_heading(importer, tmp_path):
    path = tmp_path / "my-cool-doc.md"
    assert importer._derive_title(path, "just a paragraph, no heading") == "My Cool Doc"


def test_import_dir_dry_run_writes_nothing(importer, notes_store, tmp_path):
    _make_guide_tree(tmp_path)
    result = importer.import_dir(tmp_path, dry_run=True)

    assert result["imported"] == []
    assert notes_store.list_notes(kind="study_guide") == []
    # 4 markdown files qualify; notes.txt is skipped entirely (not even manifested).
    assert len(result["manifest"]) == 4
    assert all(m["status"] == "would_import" for m in result["manifest"])


def test_import_dir_dry_run_manifest_shape(importer, notes_store, tmp_path):
    _make_guide_tree(tmp_path)
    result = importer.import_dir(tmp_path, dry_run=True)

    by_title = {m["title"]: m for m in result["manifest"]}
    assert by_title["Getting Started"]["collection"] == "Onboarding"
    assert by_title["FAQ"]["collection"] == "Onboarding"
    assert by_title["KG Overview"]["collection"] == "Architecture"
    assert by_title["Top Level"]["collection"] == "Uncategorized"
    for m in result["manifest"]:
        assert m["source_path"] == m["path"]


def test_import_dir_real_import_creates_notes(importer, notes_store, tmp_path):
    _make_guide_tree(tmp_path)
    result = importer.import_dir(tmp_path, repo="jeevy_portal", dry_run=False)

    assert len(result["imported"]) == 4
    guides = notes_store.list_notes(kind="study_guide")
    assert len(guides) == 4
    assert all(g["repo"] == "jeevy_portal" for g in guides)
    assert all(g["organized_at"] is not None for g in guides)
    assert all(g["source_path"] for g in guides)

    onboarding_guides = [
        g
        for g in guides
        if notes_store.get_tab(g["tab_id"], repo="jeevy_portal")["title"] == "Onboarding"
    ]
    assert len(onboarding_guides) == 2


def test_import_dir_isolates_same_collection_name_by_repo(importer, notes_store, tmp_path):
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    (root_a / "onboarding").mkdir(parents=True)
    (root_b / "onboarding").mkdir(parents=True)
    (root_a / "onboarding" / "guide.md").write_text("# A\n")
    (root_b / "onboarding" / "guide.md").write_text("# B\n")

    result_a = importer.import_dir(root_a, repo="repo-a", dry_run=False)
    result_b = importer.import_dir(root_b, repo="repo-b", dry_run=False)

    guide_a = notes_store.get_note(result_a["imported"][0])
    guide_b = notes_store.get_note(result_b["imported"][0])
    assert guide_a["tab_id"] != guide_b["tab_id"]
    tab_a = notes_store.get_tab(guide_a["tab_id"], repo="repo-a")
    tab_b = notes_store.get_tab(guide_b["tab_id"], repo="repo-b")
    assert (tab_a["title"], tab_a["repo"]) == ("Onboarding", "repo-a")
    assert (tab_b["title"], tab_b["repo"]) == ("Onboarding", "repo-b")


def test_import_dir_is_idempotent_on_rerun(importer, notes_store, tmp_path):
    _make_guide_tree(tmp_path)
    first = importer.import_dir(tmp_path, dry_run=False)
    assert len(first["imported"]) == 4

    second = importer.import_dir(tmp_path, dry_run=False)
    assert second["imported"] == []
    assert all(m["status"] == "skipped_existing" for m in second["manifest"])
    assert len(notes_store.list_notes(kind="study_guide")) == 4  # no duplicates


def test_import_dir_dry_run_reports_existing_as_skipped(importer, notes_store, tmp_path):
    """Even a dry-run manifest must reflect what a real import would do —
    files already imported show as skipped_existing, not would_import."""
    _make_guide_tree(tmp_path)
    importer.import_dir(tmp_path, dry_run=False)

    result = importer.import_dir(tmp_path, dry_run=True)
    assert result["imported"] == []
    assert all(m["status"] == "skipped_existing" for m in result["manifest"])


def test_import_dir_reports_unreadable_file(importer, notes_store, tmp_path, monkeypatch):
    """A file that fails to read (permissions, encoding, I/O error) must be
    reported in the manifest as "unreadable", not silently dropped or
    crash the whole scan."""
    from pathlib import Path

    (tmp_path / "bad.md").write_text("would be fine if it were readable")
    (tmp_path / "good.md").write_text("# Good\n\nfine")
    real_read_text = Path.read_text

    def flaky_read_text(self, *args, **kwargs):
        if self.name == "bad.md":
            raise OSError("simulated read failure")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)
    result = importer.import_dir(tmp_path, dry_run=True)

    bad_entry = next(m for m in result["manifest"] if m["path"].endswith("bad.md"))
    good_entry = next(m for m in result["manifest"] if m["path"].endswith("good.md"))
    assert bad_entry["status"] == "unreadable"
    assert good_entry["status"] == "would_import"


def test_import_dir_empty_directory_returns_empty_manifest(importer, notes_store, tmp_path):
    result = importer.import_dir(tmp_path, dry_run=True)
    assert result == {"manifest": [], "imported": []}


# ---------------------------------------------------------------------------
# export_note (Grimoire Phase 4) — the reversibility valve back to source_path.
# ---------------------------------------------------------------------------


def test_export_note_writes_to_source_path(importer, notes_store, tmp_path):
    src = tmp_path / "guide.md"
    src.write_text("# Original\n\noriginal body\n")
    guide = notes_store.add_study_guide("# Original\n\noriginal body\n", source_path=str(src))
    notes_store.update_note(guide["id"], raw_text="# Original\n\nrevised body\n")

    result = importer.export_note(guide["id"])

    assert result["path"] == str(src)
    assert src.read_text() == "# Original\n\nrevised body\n"
    assert result["bytes_written"] == len(b"# Original\n\nrevised body\n")


def test_export_note_explicit_path_overrides_source_path(importer, notes_store, tmp_path):
    original_src = tmp_path / "original.md"
    guide = notes_store.add_study_guide("body", source_path=str(original_src))
    target = tmp_path / "elsewhere" / "exported.md"

    result = importer.export_note(guide["id"], path=str(target))

    assert result["path"] == str(target)
    assert target.read_text() == "body"
    assert not original_src.exists()  # explicit path wins, source_path untouched


def test_export_note_creates_missing_parent_directories(importer, notes_store, tmp_path):
    guide = notes_store.add_study_guide("body")
    target = tmp_path / "new" / "nested" / "dir" / "guide.md"

    importer.export_note(guide["id"], path=str(target))

    assert target.read_text() == "body"


def test_export_note_rejects_non_guide_notes(importer, notes_store):
    note = notes_store.add_note("just a note")
    with pytest.raises(ValueError, match="not a study guide"):
        importer.export_note(note["id"])


def test_export_note_requires_source_path_or_explicit_path(importer, notes_store):
    guide = notes_store.add_study_guide("body")  # no source_path — authored directly
    with pytest.raises(ValueError, match="no source_path"):
        importer.export_note(guide["id"])


def test_export_note_unknown_note_id_raises(importer):
    with pytest.raises(ValueError, match="No note with id"):
        importer.export_note("no-such-note")


def test_export_note_is_idempotent(importer, notes_store, tmp_path):
    src = tmp_path / "guide.md"
    guide = notes_store.add_study_guide("stable content", source_path=str(src))

    first = importer.export_note(guide["id"])
    second = importer.export_note(guide["id"])

    assert first == second
    assert src.read_text() == "stable content"
