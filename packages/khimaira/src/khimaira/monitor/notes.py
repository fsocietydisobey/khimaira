"""AI-notebook — JSONL note + tab store (Phase 1a).

Storage: ~/.local/state/khimaira/notebook/
  - notes/<id>.json   — full note body, atomic-rename overwrite per mutation
  - index.jsonl       — append-only stub log (id, tab_id, title, status,
                         timestamps, deleted); folded to latest-per-id for
                         cheap listing without opening every note file
  - tabs.jsonl        — append-only tab record log, same fold convention

Mirrors khimaira.monitor.chats/sessions conventions: private path helpers →
_append/_read → public verbs; atomic tmp+rename writes; _BASE_DIR derived
from XDG_STATE_HOME at module load.

The note body is the source of truth for a single note (get/update/delete
read+write it directly); index.jsonl is a derived, append-only projection
used only for cheap listing. Tabs are lightweight records; a tab's note_ids
are derived by grouping live notes on tab_id, not stored redundantly.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from khimaira.log import get_logger
from khimaira.monitor.sessions import _append_jsonl, _read_jsonl

log = get_logger("monitor.notes")

_VALID_STATUSES = frozenset({"draft", "processed", "promoted", "failed"})
_NOTE_MUTABLE_FIELDS = frozenset({"title", "tab_id", "raw_text", "status", "links", "repo"})
_DEFAULT_TAB_ID = "default"
_DEFAULT_REPO = "khimaira"

# "General" — a repo value meaning "no codebase to validate against" (cross-
# cutting notes). revalidate_note() and answer_question()'s code-grounding
# both skip entirely for this repo — see notebook_pipeline.py.
GENERAL_REPO = "general"

# Well-known tab_id for the Personal/Behavior folder (Joseph, 2026-07-03):
# notes here are behavioral CONTEXT injected into every LLM call, not
# answerable knowledge content — never embedded, never surfaced as an ask
# source, never auto-structured. See notebook_pipeline._personal_context
# and api/notebook.py's create_note.
PERSONAL_TAB_ID = "personal"


def _base_dir() -> Path:
    xdg = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    return xdg / "khimaira" / "notebook"


def _notes_dir() -> Path:
    return _base_dir() / "notes"


def _index_path() -> Path:
    return _base_dir() / "index.jsonl"


def _tabs_path() -> Path:
    return _base_dir() / "tabs.jsonl"


def _note_path(note_id: str) -> Path:
    return _notes_dir() / f"{note_id}.json"


def _ensure_dirs() -> None:
    _notes_dir().mkdir(parents=True, exist_ok=True)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _derive_title(raw_text: str) -> str:
    first_line = raw_text.strip().splitlines()[0] if raw_text.strip() else ""
    first_line = first_line.strip()
    if not first_line:
        return "Untitled note"
    return first_line[:80]


def _write_note_atomic(note_id: str, record: dict[str, Any]) -> None:
    _ensure_dirs()
    path = _note_path(note_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2), encoding="utf-8")
    tmp.replace(path)


def _read_note_file(note_id: str) -> dict[str, Any] | None:
    path = _note_path(note_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _index_stub(record: dict[str, Any], *, deleted: bool = False) -> dict[str, Any]:
    """Listing projection of a note. Carries raw_text + pipeline + training
    (not just id/title/status) so GET /notes can render note cards directly —
    no N+1 get_note() round trip per listed note. history is summarized to a
    count (not the full array) to keep the listing cheap; full history is
    available via get_note()."""
    return {
        "id": record["id"],
        "tab_id": record["tab_id"],
        "title": record["title"],
        "status": record["status"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "raw_text": record["raw_text"],
        "pipeline": record["pipeline"],
        "training": record["training"],
        "repo": record.get("repo", _DEFAULT_REPO),
        "last_validated_at": record.get("last_validated_at"),
        "validated_git_sha": record.get("validated_git_sha"),
        "history_count": len(record.get("history") or []),
        "deleted": deleted,
    }


def _fold_index() -> dict[str, dict[str, Any]]:
    """Fold index.jsonl to the latest stub per note id, dropping deleted ones."""
    folded: dict[str, dict[str, Any]] = {}
    for line in _read_jsonl(_index_path()):
        note_id = line.get("id")
        if not note_id:
            continue
        folded[note_id] = line
    return {nid: stub for nid, stub in folded.items() if not stub.get("deleted")}


def _fold_tabs() -> dict[str, dict[str, Any]]:
    """Fold tabs.jsonl to the latest record per tab id, dropping deleted ones."""
    folded: dict[str, dict[str, Any]] = {}
    for line in _read_jsonl(_tabs_path()):
        tab_id = line.get("id")
        if not tab_id:
            continue
        folded[tab_id] = line
    return {tid: rec for tid, rec in folded.items() if not rec.get("deleted")}


# ---------------------------------------------------------------------------
# Public API — notes
# ---------------------------------------------------------------------------


def add_note(raw_text: str, tab_id: str = "", title: str = "", repo: str = "") -> dict[str, Any]:
    """Create a draft note. `pipeline` is null until the transform runs (Phase 1c).

    `repo`: which codebase this note is validated against (north-star:
    code is source of truth, notes are re-validated caches of it). Defaults
    to _DEFAULT_REPO ("khimaira") — must match a project name khimaira's
    discovery registry resolves to a filesystem path, or revalidate_note()
    can't find anchor files to check against.
    """
    note_id = _new_id()
    now = _now_iso()
    record: dict[str, Any] = {
        "id": note_id,
        "created_at": now,
        "updated_at": now,
        "title": title or _derive_title(raw_text),
        "tab_id": tab_id or _DEFAULT_TAB_ID,
        "raw_text": raw_text,
        "status": "draft",
        "pipeline": None,
        "embedding_id": None,
        "training": {
            "promoted": False,
            "promoted_at": None,
            "domain": "khimaira:notes",
            "distilled_pairs": 0,
        },
        "links": [],
        "repo": repo or _DEFAULT_REPO,
        "history": [],
        "last_validated_at": None,
        "validated_git_sha": None,
    }
    _write_note_atomic(note_id, record)
    _append_jsonl(_index_path(), _index_stub(record))
    log.info("notes: added %s in tab=%s repo=%s", note_id, record["tab_id"], record["repo"])
    return record


def get_note(note_id: str) -> dict[str, Any]:
    record = _read_note_file(note_id)
    if record is None:
        raise ValueError(f"No note with id={note_id!r}. Use list_notes() to see available notes.")
    return record


def list_notes(tab_id: str | None = None, repo: str | None = None) -> list[dict[str, Any]]:
    """Newest-created first. Sorted by created_at (not updated_at) so a
    revalidate/heal pass — which only bumps updated_at — doesn't reshuffle
    the list out from under someone reading it.

    `repo`, when given, scopes to that repo PLUS GENERAL_REPO (the "no
    codebase" bucket for cross-cutting notes always stays visible alongside
    whichever project is in view). `repo=None` returns everything — the
    "All projects" view."""
    stubs = list(_fold_index().values())
    if tab_id is not None:
        stubs = [s for s in stubs if s["tab_id"] == tab_id]
    if repo is not None:
        stubs = [s for s in stubs if s.get("repo") in (repo, GENERAL_REPO)]
    stubs.sort(key=lambda s: s["created_at"], reverse=True)
    return stubs


def update_note(note_id: str, **fields: Any) -> dict[str, Any]:
    """Edit a note. Accepts title/tab_id/raw_text/status/links/repo, plus a
    `pipeline` kwarg treated as a partial patch merged onto the existing
    pipeline dict (manual edits to summary/technical/plain/etc).

    Changing `repo` to a different value re-anchors future validation: the
    old validated_git_sha means nothing against a different repo's git
    history, so it's cleared along with last_validated_at, forcing a full
    re-check (not a heal-vs-stale-sha comparison) on the next revalidate."""
    record = get_note(note_id)
    pipeline_patch = fields.pop("pipeline", None)
    unknown = set(fields) - _NOTE_MUTABLE_FIELDS
    if unknown:
        raise ValueError(
            f"Unknown note field(s): {sorted(unknown)}. "
            f"Mutable fields: {sorted(_NOTE_MUTABLE_FIELDS)} (+ 'pipeline' patch)."
        )
    if "status" in fields and fields["status"] not in _VALID_STATUSES:
        raise ValueError(
            f"Invalid status {fields['status']!r}; must be one of {sorted(_VALID_STATUSES)}."
        )
    if "repo" in fields and fields["repo"] != record.get("repo"):
        record["validated_git_sha"] = None
        record["last_validated_at"] = None
    record.update(fields)
    if pipeline_patch is not None:
        merged = dict(record.get("pipeline") or {})
        merged.update(pipeline_patch)
        record["pipeline"] = merged
    record["updated_at"] = _now_iso()
    _write_note_atomic(note_id, record)
    _append_jsonl(_index_path(), _index_stub(record))
    return record


def set_pipeline(
    note_id: str, pipeline: dict[str, Any], *, title: str | None = None
) -> dict[str, Any]:
    """Full replace of the pipeline dict (called by the Phase 1c transform
    on completion) and marks the note processed. `title` (LLM-generated,
    Joseph 2026-07-03) replaces the raw-text-derived placeholder title when
    given — the display title everywhere (list rows, grid cards, reader
    header, @-mention label)."""
    record = get_note(note_id)
    record["pipeline"] = pipeline
    if title:
        record["title"] = title
    record["status"] = "processed"
    record["updated_at"] = _now_iso()
    _write_note_atomic(note_id, record)
    _append_jsonl(_index_path(), _index_stub(record))
    return record


def apply_validation(
    note_id: str,
    *,
    git_sha: str,
    new_pipeline: dict[str, Any] | None = None,
    title: str | None = None,
) -> dict[str, Any]:
    """Record a revalidate_note() pass (north-star self-healing core).

    `new_pipeline=None` — the note was checked and found CURRENT (staleness
    gate skip, or the LLM confirmed it unchanged vs live code): just stamps
    `last_validated_at`/`validated_git_sha`, no history churn.

    `new_pipeline={...}` — the note was HEALED: the OUTGOING pipeline (plus
    the validation stamp it was checked under) is pushed to `history` before
    the new one replaces it, so prior versions are never lost. `raw_text` is
    never touched either way — it's the immutable original paste.

    `title`, when given, replaces the note's display title regardless of
    `new_pipeline` — a title backfill happens on ANY revalidate pass that
    reaches the LLM, not just a heal (Joseph, 2026-07-03).
    """
    record = get_note(note_id)
    now = _now_iso()
    if new_pipeline is not None:
        history_entry = {
            "pipeline": record["pipeline"],
            "replaced_at": now,
            "validated_git_sha": record.get("validated_git_sha"),
        }
        record.setdefault("history", []).append(history_entry)
        record["pipeline"] = new_pipeline
        record["status"] = "processed"
    if title:
        record["title"] = title
    record["last_validated_at"] = now
    record["validated_git_sha"] = git_sha
    record["updated_at"] = now
    _write_note_atomic(note_id, record)
    _append_jsonl(_index_path(), _index_stub(record))
    return record


def promote_note(note_id: str) -> dict[str, Any]:
    """Curated promotion — mark training.promoted=True. Human gate only;
    never auto-promoted."""
    record = get_note(note_id)
    now = _now_iso()
    record["training"]["promoted"] = True
    record["training"]["promoted_at"] = now
    record["status"] = "promoted"
    record["updated_at"] = now
    _write_note_atomic(note_id, record)
    _append_jsonl(_index_path(), _index_stub(record))
    return record


def delete_note(note_id: str) -> dict[str, Any]:
    record = get_note(note_id)
    _note_path(note_id).unlink(missing_ok=True)
    _append_jsonl(_index_path(), _index_stub(record, deleted=True))
    log.info("notes: deleted %s", note_id)
    return {"id": note_id, "deleted": True}


# ---------------------------------------------------------------------------
# One-time backfill (Joseph, 2026-07-03): drop pre-fix spurious "heals".
#
# Before notebook_pipeline.RevalidationOutput's explicit `unchanged` field,
# revalidate_note() inferred a heal from dict-equality between the model's
# regenerated JSON and the prior pipeline — but real LLM output is never
# byte-identical across two generations even when nothing substantive
# changed, so nearly every LLM re-check got mis-flagged as a heal. The
# signature of that specific bug: summary/technical/plain/tags/entities
# identical, only organized_md drifted (wording noise). This backfill
# removes exactly that signature from existing history — a real heal
# always changes more than just organized_md, so genuine heals are untouched.
# ---------------------------------------------------------------------------

_SUBSTANCE_FIELDS = ("summary", "technical", "plain", "tags", "entities")


def _same_substance_different_organized_md(a: dict[str, Any], b: dict[str, Any]) -> bool:
    return all(a.get(k) == b.get(k) for k in _SUBSTANCE_FIELDS) and a.get(
        "organized_md"
    ) != b.get("organized_md")


def backfill_drop_spurious_heals(note_id: str) -> dict[str, Any]:
    """Drops any history entry whose archived pipeline differs from the
    NEXT version in the chain (or the current pipeline, for the last
    entry) ONLY in organized_md. Idempotent — a no-op on already-clean
    history."""
    record = get_note(note_id)
    history = record.get("history") or []
    if not history:
        return record

    kept = []
    for i, entry in enumerate(history):
        next_pipeline = (
            history[i + 1]["pipeline"] if i + 1 < len(history) else record.get("pipeline") or {}
        )
        if _same_substance_different_organized_md(entry.get("pipeline") or {}, next_pipeline):
            continue  # spurious wording-only "heal" — drop
        kept.append(entry)

    if len(kept) == len(history):
        return record

    record["history"] = kept
    _write_note_atomic(note_id, record)
    _append_jsonl(_index_path(), _index_stub(record))
    log.info(
        "notes: backfill dropped %d spurious heal entr%s from %s",
        len(history) - len(kept),
        "y" if len(history) - len(kept) == 1 else "ies",
        note_id,
    )
    return record


def backfill_drop_spurious_heals_all() -> list[str]:
    """Runs backfill_drop_spurious_heals() across every note. Returns ids of
    notes that were actually changed. Cheap (pure JSON diffing, no LLM/git
    calls) — safe to call on every daemon startup."""
    changed: list[str] = []
    for stub in list_notes():
        if not stub.get("history_count"):
            continue
        before = stub["history_count"]
        updated = backfill_drop_spurious_heals(stub["id"])
        if len(updated.get("history") or []) != before:
            changed.append(stub["id"])
    return changed


# ---------------------------------------------------------------------------
# Public API — tabs
# ---------------------------------------------------------------------------


def add_tab(title: str = "") -> dict[str, Any]:
    _ensure_dirs()
    tab_id = _new_id()
    now = _now_iso()
    record = {
        "id": tab_id,
        "title": title or f"Tab {tab_id[:6]}",
        "created_at": now,
        "updated_at": now,
        "deleted": False,
    }
    _append_jsonl(_tabs_path(), record)
    return _with_note_ids(record)


def get_tab(tab_id: str) -> dict[str, Any]:
    folded = _fold_tabs()
    record = folded.get(tab_id)
    if record is None:
        raise ValueError(f"No tab with id={tab_id!r}. Use list_tabs() to see available tabs.")
    return _with_note_ids(record)


def update_tab(tab_id: str, **fields: Any) -> dict[str, Any]:
    _ensure_dirs()
    existing = get_tab(tab_id)
    existing.pop("note_ids", None)
    unknown = set(fields) - {"title"}
    if unknown:
        raise ValueError(f"Unknown tab field(s): {sorted(unknown)}. Mutable fields: ['title'].")
    existing.update(fields)
    existing["updated_at"] = _now_iso()
    existing["deleted"] = False
    _append_jsonl(_tabs_path(), existing)
    return _with_note_ids(existing)


def list_tabs() -> list[dict[str, Any]]:
    tabs = [_with_note_ids(rec) for rec in _fold_tabs().values()]
    tabs.sort(key=lambda t: t["created_at"])
    return tabs


def _with_note_ids(tab_record: dict[str, Any]) -> dict[str, Any]:
    out = dict(tab_record)
    out["note_ids"] = [n["id"] for n in list_notes(tab_id=tab_record["id"])]
    return out
