"""Grimoire Phase 1d/2 — library organization (the housing pillar).

Phase 1 (`derive_collection`/`assign_deterministic`) is deliberately SIMPLE:
cheap, deterministic collection assignment from a file's path — handles the
obvious cases (a file's directory IS its topic) at ~zero cost. Phase 2 adds
the LLM pass: `organize_library()` re-files/re-labels notes AND guides based
on their OWN CONTENT (title/summary-or-abstract/tags), not their current
placement — this is what fixes an already-filed-but-wrong item, or one
authored directly (no source path to derive a location from at all).

KIND-AWARE, one engine (2026-07-04 addendum): guides organize into
COLLECTIONS (tab kind="collection"); regular notes organize into FOLDERS
(tab kind="folder") — separate namespaces so the two never intermix in the
tab filter bar, mirroring the split that already existed for deterministic
Phase 1 filing. A guide's content signal is `pipeline.abstract`; a note's is
`pipeline.summary` — everything else (the LLM call, the re-file/no-op logic,
the re-entrancy guard, the sweep) is shared, kind-branching only on where to
look up content and which get_or_create_* / existing-name list to use.

Not a rubric: no quality/duplication/currency scoring. The only job is
keeping the library organized — every item filed somewhere sensible. Guide
currency (is the content still accurate vs the code) is a SEPARATE concern
— see notebook_pipeline.revalidate_note's study-guide branch.

Money-printer guard: `organize_library()` makes exactly ONE batched LLM call
per invocation, whether scoped to one item (the post-structuring hook) or
the whole library (the periodic sweep) — never a per-item fan-out.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from khimaira.log import get_logger
from khimaira.monitor import notes

log = get_logger("monitor.notebook_organizer")


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


def get_or_create_folder(title: str) -> dict[str, Any]:
    """Thin re-export of notes.get_or_create_folder — the note-side sibling
    of get_or_create_collection above."""
    return notes.get_or_create_folder(title)


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


# ---------------------------------------------------------------------------
# Phase 2 — the LLM organize pass. Content-based, not path-based: re-files
# a guide (existing or a proposed new collection) purely from its own
# title/abstract/tags, independent of where it currently sits or how it was
# imported. Auto-applies (re-filing a tab_id is reversible — Joseph: "just
# fix it", not a proposal queue).
# ---------------------------------------------------------------------------

_ORGANIZE_INSTRUCTION = (
    "You organize a library of notes and study guides into folders and "
    "collections. For EACH item listed below, decide where it belongs — "
    "reuse an EXISTING name from the appropriate list (guide collections "
    "for study guides, note folders for notes — each item's listing tells "
    "you which) if it genuinely fits, or propose a new, short, Title-Case "
    "name if none does. Base the decision ONLY on the item's own content "
    "(title/summary-or-abstract/tags), never on its current placement. "
    "Output ONLY a JSON object, no prose, no markdown fence, with key "
    "`placements`: an array of {note_id, collection} objects, exactly one "
    "per item listed — `collection` is the folder name for a note or the "
    "collection name for a guide, whichever applies to that item."
)

_MAX_BLURB_CHARS_IN_PROMPT = 300


class GuidePlacement(BaseModel):
    note_id: str
    collection: str


class OrganizeOutput(BaseModel):
    placements: list[GuidePlacement]


def _organizable_kind(record: dict[str, Any]) -> str | None:
    """Which namespace (kind) an item organizes into, or None if this kind
    isn't organizable at all (e.g. "personal" notes are excluded upstream by
    never having a pipeline — see organize_library's own filter)."""
    kind = record.get("kind")
    return kind if kind in ("note", "study_guide") else None


def _content_blurb(record: dict[str, Any]) -> str:
    """The content signal fed to the LLM: a guide's `abstract`, a note's
    `summary` — the two kinds' pipelines don't share a key name for "the
    short human-readable gist," so this is the one kind-branch the prompt
    formatting needs."""
    pipeline = record.get("pipeline") or {}
    key = "abstract" if record.get("kind") == "study_guide" else "summary"
    return (pipeline.get(key) or "")[:_MAX_BLURB_CHARS_IN_PROMPT]


def _format_item_for_prompt(record: dict[str, Any], current_title: str) -> str:
    pipeline = record.get("pipeline") or {}
    tags = pipeline.get("tags") or []
    kind_label = "guide" if record.get("kind") == "study_guide" else "note"
    return (
        f"- note_id={record['id']} kind={kind_label} title={record.get('title', '?')!r} "
        f"blurb={_content_blurb(record)!r} tags={tags!r} current_location={current_title!r}"
    )


async def organize_library(note_ids: list[str] | None = None) -> dict[str, Any]:
    """Run ONE batched LLM organize pass over notes AND study guides.

    `note_ids=None` (the periodic sweep's call): considers every organizable
    item in the library — the full drift-correction pass, including
    proposing new folders/collections when a content cluster emerges.

    `note_ids=[...]` (the post-structuring hook's call, either kind): scopes
    the items UNDER CONSIDERATION to just those ids, keeping the prompt
    small and the cost independent of library size — this is what lets a
    freshly authored/imported item get an LLM-judged placement immediately
    without turning every single creation into an O(library-size) call (the
    money-printer risk the sweep-only design would otherwise avoid but a
    naive per-item hook would reintroduce).

    Only STRUCTURED items are organizable (pipeline must be set — nothing to
    judge content from otherwise); this also excludes personal-tab notes,
    which are never structured at all (see api/notebook.py's create_note).

    FILE-MANAGER (2026-07-04): also excludes `pinned_placement=True` notes —
    a manual move is a durable override the organizer must never churn.
    This keeps a pinned item out of the LLM prompt (cost) and out of
    `reassigned`/considered reporting entirely; `notes.mark_organized`
    ALSO refuses a tab_id change for a pinned note as a structural
    backstop (covers assign_deterministic and notebook_import's own
    re-file, which don't go through this filter at all).

    Re-files (mark_organized with a new tab_id) only when the LLM's chosen
    location differs from the item's current one; otherwise just stamps
    organized_at (checked, still correctly placed). Unknown/hallucinated
    note_ids in the model's response are silently discarded — the LLM never
    gets write access beyond the items it was actually shown.

    Returns {"considered": int, "reassigned": [note_id, ...], "new_collections": [name, ...]}
    ("new_collections" covers both new folders and new collections — one
    counter, since callers only care "did a new location get created").
    Fails open: any LLM/parse failure logs a warning and returns considered=N,
    reassigned=[] — never raises, never corrupts placement on a bad response.
    """
    from khimaira.monitor import notebook_pipeline

    all_notes = notes.list_notes()
    organizable = [
        n
        for n in all_notes
        if n.get("pipeline") and _organizable_kind(n) and not n.get("pinned_placement")
    ]
    targets = [n for n in organizable if note_ids is None or n["id"] in note_ids]
    if not targets:
        return {"considered": 0, "reassigned": [], "new_collections": []}

    tabs = notes.list_tabs()
    tabs_by_id = {t["id"]: t for t in tabs}
    existing_collections = sorted({t["title"] for t in tabs if t.get("kind") == "collection"})
    existing_folders = sorted({t["title"] for t in tabs if t.get("kind") == "folder"})

    lines = [
        f"Existing guide collections: {existing_collections or '(none yet)'}",
        f"Existing note folders: {existing_folders or '(none yet)'}",
        "",
    ]
    for record in targets:
        current_title = tabs_by_id.get(record["tab_id"], {}).get("title", "?")
        lines.append(_format_item_for_prompt(record, current_title))
    content = "\n".join(lines)

    result = await notebook_pipeline.transform_note(
        content, instruction=_ORGANIZE_INSTRUCTION, schema=OrganizeOutput
    )
    if result is None:
        log.warning(
            "notebook_organizer: organize_library failed to parse after retry (considered=%d)",
            len(targets),
        )
        return {"considered": len(targets), "reassigned": [], "new_collections": []}

    valid_by_id = {n["id"]: n for n in targets}
    existing_lower_by_kind = {
        "study_guide": {c.lower() for c in existing_collections},
        "note": {f.lower() for f in existing_folders},
    }
    reassigned: list[str] = []
    new_collections: list[str] = []
    for placement in result["placements"]:
        note_id = placement.get("note_id")
        location = (placement.get("collection") or "").strip()
        record = valid_by_id.get(note_id)
        if record is None or not location:
            continue  # hallucinated/unknown note_id or empty location — discard

        is_guide = record["kind"] == "study_guide"
        tab = get_or_create_collection(location) if is_guide else get_or_create_folder(location)
        if tab["id"] == record["tab_id"]:
            notes.mark_organized(note_id)  # still correctly placed — just refresh the check
            continue
        notes.mark_organized(note_id, tab_id=tab["id"])
        reassigned.append(note_id)
        if location.lower() not in existing_lower_by_kind[record["kind"]]:
            new_collections.append(location)

    return {
        "considered": len(targets),
        "reassigned": reassigned,
        "new_collections": new_collections,
    }


# Notes currently mid-organize — defense-in-depth against a re-entrant call
# for the same note while one is already in flight. AUDIT (2026-07-04):
# mark_organized() above only ever touches tab_id/organized_at, never
# raw_text, and is called directly (module-to-module), never through the
# PATCH /notes/{id} API route — the ONLY place that reschedules the
# structuring pipeline is that route's own `if "raw_text" in fields` branch
# (api/notebook.py). So this hook's write-back is SAFE against the
# reaper-cascade class (organize -> write -> re-fire pipeline -> organize ->
# ...) as things stand today. This latch is precautionary, not a fix for an
# observed recursion — it guards against a FUTURE caller accidentally
# routing an organize re-file through a path that does touch raw_text.
_ORGANIZING_NOTE_IDS: set[str] = set()


async def organize_after_structuring(note_id: str) -> None:
    """Hook fired from notebook_pipeline.trigger_study_guide_pipeline right
    after a guide's abstract/tags land (Grimoire Phase 2) — gives a freshly
    authored/imported guide an LLM-judged collection placement immediately,
    on top of Phase 1's deterministic-only assign (which has nothing to work
    with for a guide authored directly via notebook_create_study_guide,
    since there's no source path to derive a collection from).

    Fail-open: any error is logged and swallowed — an organize failure must
    never surface as (or block on) a structuring failure."""
    if note_id in _ORGANIZING_NOTE_IDS:
        return
    _ORGANIZING_NOTE_IDS.add(note_id)
    try:
        await organize_library(note_ids=[note_id])
    except Exception:
        log.exception("notebook_organizer: organize_after_structuring(%s) failed", note_id)
    finally:
        _ORGANIZING_NOTE_IDS.discard(note_id)


# ---------------------------------------------------------------------------
# The periodic sweep — drift self-correction across the whole library.
# ---------------------------------------------------------------------------

_SWEEP_ENABLED = os.environ.get("KHIMAIRA_NOTEBOOK_ORGANIZE_SWEEP", "1") != "0"
_SWEEP_INTERVAL_S = float(os.environ.get("KHIMAIRA_NOTEBOOK_ORGANIZE_SWEEP_S", "3600"))


async def organize_sweep_loop() -> None:
    """Background loop (started via server.py's `_spawn`, mirroring
    registry_gc_loop's shape): every `_SWEEP_INTERVAL_S`, runs ONE batched
    organize_library() pass over the ENTIRE library so drift (guides that
    have quietly become mislabeled, or whose right collection only becomes
    obvious once siblings accumulate) self-corrects even without a create/
    edit event to trigger it. Disable via KHIMAIRA_NOTEBOOK_ORGANIZE_SWEEP=0."""
    if not _SWEEP_ENABLED:
        log.info("notebook_organizer: sweep disabled via KHIMAIRA_NOTEBOOK_ORGANIZE_SWEEP=0")
        return
    log.info("notebook_organizer: sweep loop started (interval=%ds)", _SWEEP_INTERVAL_S)
    while True:
        try:
            result = await organize_library()
            if result["reassigned"]:
                log.info(
                    "notebook_organizer: sweep reassigned %d guide(s), %d new collection(s)",
                    len(result["reassigned"]),
                    len(result["new_collections"]),
                )
        except Exception:
            log.exception("notebook_organizer: sweep error")
        await asyncio.sleep(_SWEEP_INTERVAL_S)
