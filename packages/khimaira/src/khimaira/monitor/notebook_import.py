"""Grimoire Phase 1c — bulk-import study guides from a flat directory (the
jeevy roster's `~/work/jeevy_portal/shared-docs`, ~130-140 markdown files
today) into the notebook as a distinct kind.

Two-step, dry-run-first: `import_dir(root, dry_run=True)` (the default)
produces a manifest (path -> collection -> title) for human review and
writes NOTHING. Only `dry_run=False` actually creates notes — and even
then it's idempotent via `source_path` (notes.find_by_source_path), so
re-running an import (e.g. after new files land in shared-docs) only
imports what's new, never duplicates.

Collection assignment reuses notebook_organizer.derive_collection (the
SAME rule the ongoing organize hook uses) so a file imported here lands in
the collection an organize pass would independently assign it to.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from khimaira.log import get_logger
from khimaira.monitor import notebook_organizer, notebook_pipeline, notes

log = get_logger("monitor.notebook_import")

_GUIDE_EXTENSIONS = (".md", ".markdown")
_MAX_TITLE_SCAN_LINES = 20


def qualify(path: Path) -> bool:
    """Does this file look like an importable study guide? Markdown files
    only. Directory-vs-file filtering (skip dotfiles/hidden dirs) is the
    caller's glob's job, not this predicate's."""
    return path.is_file() and path.suffix.lower() in _GUIDE_EXTENSIONS


def derive_collection(path: Path, root: Path) -> str:
    """Re-exported from notebook_organizer — see its docstring. Kept as a
    module-level name here too so callers reaching for "the importer's
    collection rule" find it without needing to know it's actually owned
    by the organizer module."""
    return notebook_organizer.derive_collection(path, root)


def _derive_title(path: Path, text: str) -> str:
    """First markdown heading if present, else the filename (de-slugified).
    Never the first raw line (notes.py's _derive_title convention) — guides
    often open with frontmatter or a long lead paragraph, not a title line."""
    for line in text.splitlines()[:_MAX_TITLE_SCAN_LINES]:
        stripped = line.strip()
        if stripped.startswith("#"):
            heading = stripped.lstrip("#").strip()
            if heading:
                return heading
    return path.stem.replace("-", " ").replace("_", " ").title()


def import_dir(root: str | Path, *, repo: str = "", dry_run: bool = True) -> dict[str, Any]:
    """Scan `root` recursively for importable guides and either produce a
    dry-run manifest (default — writes NOTHING) or actually import them.

    Idempotent via source_path: a file whose absolute path already matches
    an existing note's source_path is skipped as "skipped_existing" (never
    silently dropped — it's reported in the manifest either way, so a
    re-run's manifest still accounts for every file under `root`).

    Each real (non-dry-run) import: creates the note (notes.add_study_guide),
    deterministically files it (notebook_organizer.assign_deterministic —
    stamps organized_at + tab_id in one write), then schedules its
    structuring pipeline (notebook_pipeline.schedule_pipeline) exactly like
    a UI paste — async, not blocking this call.

    Returns {"manifest": [...], "imported": [note_id, ...]} — `imported` is
    always [] when dry_run=True.
    """
    root_path = Path(root).expanduser().resolve()
    manifest: list[dict[str, Any]] = []
    imported: list[str] = []

    for path in sorted(root_path.rglob("*")):
        if not qualify(path):
            continue
        source_path = str(path.resolve())
        collection = derive_collection(path, root_path)
        existing = notes.find_by_source_path(source_path)
        if existing is not None:
            manifest.append(
                {
                    "path": source_path,
                    "collection": collection,
                    "title": existing["title"],
                    "source_path": source_path,
                    "status": "skipped_existing",
                    "note_id": existing["id"],
                }
            )
            continue

        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError as exc:
            log.warning("notebook_import: could not read %s: %s", path, exc)
            manifest.append(
                {
                    "path": source_path,
                    "collection": collection,
                    "title": path.stem,
                    "source_path": source_path,
                    "status": "unreadable",
                }
            )
            continue

        title = _derive_title(path, text)
        manifest.append(
            {
                "path": source_path,
                "collection": collection,
                "title": title,
                "source_path": source_path,
                "status": "would_import" if dry_run else "imported",
            }
        )

        if not dry_run:
            tab = notebook_organizer.get_or_create_collection(collection)
            record = notes.add_study_guide(
                text,
                tab_id=tab["id"],
                title=title,
                repo=repo or notes.GENERAL_REPO,
                source_path=source_path,
            )
            record = notes.mark_organized(record["id"], tab_id=tab["id"])
            notebook_pipeline.schedule_pipeline(record["id"])
            imported.append(record["id"])

    log.info(
        "notebook_import: scanned %s — %d file(s) in manifest, %d imported (dry_run=%s)",
        root_path,
        len(manifest),
        len(imported),
        dry_run,
    )
    return {"manifest": manifest, "imported": imported}
