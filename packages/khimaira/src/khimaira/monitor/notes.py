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
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from khimaira.log import get_logger
from khimaira.monitor.notebook_redaction import redact_secrets
from khimaira.monitor.sessions import _append_jsonl, _read_jsonl

log = get_logger("monitor.notes")

_VALID_STATUSES = frozenset({"draft", "processed", "promoted", "failed"})
_NOTE_MUTABLE_FIELDS = frozenset(
    {
        "title",
        "tab_id",
        "raw_text",
        "status",
        "links",
        "repo",
        "sensitive",
        "priority",
        "pinned_placement",
        "starred",
        "test_status",
    }
)
_DEFAULT_TAB_ID = "default"
_DEFAULT_REPO = "khimaira"

# Sensitive notes (2026-07-04): user-set priority is INDEPENDENT of status
# (lifecycle) — importance, not workflow state. Mirrors _VALID_STATUSES
# exactly. The LLM organizer must never touch this (see notebook_organizer's
# mark_organized — it only ever sets tab_id/organized_at).
_VALID_PRIORITIES = frozenset({"low", "normal", "high", "urgent"})
_DEFAULT_PRIORITY = "normal"

# Testing-workflow status (2026-07-07, Joseph): a HUMAN-set label distinct
# from every other status-like field — `status` is the automated structuring
# pipeline's lifecycle, `lifecycle` (derive_lifecycle, below) is a read-only
# projection of status/resolution/kind, neither has anything to do with
# whether a human has verified a note's content. Mirrors _VALID_PRIORITIES
# exactly (independent field, own default, own mutable-fields entry).
# Notes-only by design decision — study guides keep their own housed/
# organized lifecycle for a different concept (library placement); guide
# records still carry this field (for schema uniformity across add_note/
# add_study_guide) but no UI surface exposes it for guides.
_VALID_TEST_STATUSES = frozenset({"untested", "needs_testing", "in_review", "tested"})
_DEFAULT_TEST_STATUS = "untested"

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

# Grimoire (2026-07-04): a study guide is a distinct KIND of note — a
# finished, human-authored deliverable to be HOUSED + RENDERED, not
# re-expressed into the note pipeline's summary/technical/plain triple.
# LOAD-BEARING INVARIANT: raw_text (the guide body) is the human deliverable
# and is NEVER LLM-rewritten (except an explicit, human-approved research
# REVISE — a later phase). Every derived artifact (pipeline={abstract, toc,
# tags, entities}, collection, currency drift) sits alongside raw_text,
# never in place of it. See add_study_guide / set_study_guide_pipeline.
_VALID_KINDS = frozenset({"note", "study_guide"})
_DEFAULT_KIND = "note"

# Tab kind: "folder" groups regular notes; "collection" groups study guides
# in the Library view. A collection IS a tab — no separate store.
_VALID_TAB_KINDS = frozenset({"folder", "collection"})
_DEFAULT_TAB_KIND = "folder"


class TabValidationError(ValueError):
    """FILE-MANAGER (2026-07-04): a tab create/reparent request that's
    structurally invalid (cycle, cross-kind nesting, sibling-name collision)
    — distinct from a plain ValueError (unknown tab_id / dangling parent_id,
    still raised by get_tab as-is) so API routes can map it to 422 (bad
    input) instead of 404 (not found). Subclasses ValueError so any existing
    generic `except ValueError` catch still works unchanged."""


# Sensitive / credential-safe notes (2026-07-04): a place to paste content
# containing real secrets (API keys, connection strings) that the notebook
# still summarizes/organizes/embeds/chats-about normally — but no LLM call,
# embedding, cross-note context blob, or training export may ever see the
# actual secret VALUES. `raw_text` stays the real content (human-only,
# readable/copyable in the reader); `llm_text` is a redacted twin computed
# ONCE at write time (see notebook_redaction.redact_secrets) whenever
# `sensitive=True`; `redactions` records {placeholder, kind} pairs for the
# UI's "what got hidden" panel — NEVER the masked value itself.
#
# `llm_view` is THE choke point: every egress site that used to read
# `record["raw_text"]` directly (structuring, organizer, embedding, chat/
# research, personal-context concatenation, training export) now reads
# `llm_view(record)` instead — this is what makes the boundary structural
# (one accessor, audited call sites) rather than prompt-enforced. The model
# can't leak what it never receives.
def llm_view(record: dict[str, Any]) -> str:
    """The text an LLM/embedding/cross-note-context/training call is allowed
    to see for this note: the redacted twin when sensitive, else the real
    raw_text unchanged."""
    if record.get("sensitive"):
        return record.get("llm_text") or ""
    return record.get("raw_text", "")


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


def derive_lifecycle(record: dict[str, Any]) -> str:
    """captured -> reviewed -> resolved, derived (not stored). A note is
    "resolved" once a resolution has been attached (see `add_resolution`) —
    that's the training-quality gate: a problem earns training status by
    being worked to completion, not by being merely structured. "reviewed"
    means the structuring pipeline ran (status processed/promoted) but no
    resolution has landed yet; anything else (draft/failed) is "captured".

    Study guides (kind="study_guide") get a DIFFERENT lifecycle — they're
    finished deliverables to house, not problems to resolve, so
    "resolution" doesn't apply: "housed" (imported/created, not yet
    organized) -> "organized" (`organized_at` is set, i.e. the organizer
    has placed/checked it in a real collection)."""
    if record.get("kind") == "study_guide":
        return "organized" if record.get("organized_at") else "housed"
    if record.get("resolution"):
        return "resolved"
    if record.get("status") in ("processed", "promoted"):
        return "reviewed"
    return "captured"


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


_SENSITIVE_LIST_PLACEHOLDER = "[sensitive note — open it to view the real content]"


def _index_stub(record: dict[str, Any], *, deleted: bool = False) -> dict[str, Any]:
    """Listing projection of a note. Carries raw_text + pipeline + training
    (not just id/title/status) so GET /notes can render note cards directly —
    no N+1 get_note() round trip per listed note. history is summarized to a
    count (not the full array) to keep the listing cheap; full history is
    available via get_note().

    Sensitive notes (2026-07-04): `raw_text` is REPLACED with a placeholder
    here — list/search results must never carry a sensitive note's real
    content in bulk (get_note(), the single-note reader fetch, still returns
    the real raw_text unchanged)."""
    is_sensitive = record.get("sensitive", False)
    return {
        "id": record["id"],
        "tab_id": record["tab_id"],
        "title": record["title"],
        "status": record["status"],
        "created_at": record["created_at"],
        "updated_at": record["updated_at"],
        "raw_text": _SENSITIVE_LIST_PLACEHOLDER if is_sensitive else record["raw_text"],
        "pipeline": record["pipeline"],
        "training": record["training"],
        "repo": record.get("repo", _DEFAULT_REPO),
        "last_validated_at": record.get("last_validated_at"),
        "validated_git_sha": record.get("validated_git_sha"),
        "structured_at": record.get("structured_at"),
        "history_count": len(record.get("history") or []),
        "resolution": record.get("resolution", ""),
        "resolved_by": record.get("resolved_by", ""),
        "resolved_at": record.get("resolved_at"),
        "kind": record.get("kind", _DEFAULT_KIND),
        "source_path": record.get("source_path"),
        "organized_at": record.get("organized_at"),
        "sensitive": is_sensitive,
        "redactions": record.get("redactions"),
        "priority": record.get("priority", _DEFAULT_PRIORITY),
        "test_status": record.get("test_status", _DEFAULT_TEST_STATUS),
        "lifecycle": derive_lifecycle(record),
        "deleted": deleted,
        "pinned_placement": record.get("pinned_placement", False),
        "starred": record.get("starred", False),
    }


_INDEX_LOCK = threading.Lock()
_TABS_LOCK = threading.Lock()

# Compact once the raw line count exceeds the folded (deduped) count by this
# many stale entries. Small/lightly-churned libraries never cross this and
# never pay a compaction write; a library under heavy churn (bulk resolution
# passes, repeated organize sweeps) self-heals on the next read instead of
# growing unboundedly. See _fold_index's docstring for the incident this closes.
_COMPACT_EXCESS_LINES = 50


def _append_index_stub(record: dict[str, Any], *, deleted: bool = False) -> None:
    """Append a stub to index.jsonl. MUST be the only append path — sharing
    _INDEX_LOCK with _compact_index_locked is what makes compaction safe
    under concurrent writers (see _fold_index's docstring)."""
    with _INDEX_LOCK:
        _append_jsonl(_index_path(), _index_stub(record, deleted=deleted))


def _append_tab_record(record: dict[str, Any]) -> None:
    """Append a record to tabs.jsonl. Same lock-sharing contract as
    _append_index_stub, for _compact_tabs_locked."""
    with _TABS_LOCK:
        _append_jsonl(_tabs_path(), record)


def _atomic_replace_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    """Rewrite `path` to contain exactly `records`, one JSON object per line.
    Unique-per-call tmp name (pid + thread + random) — a fixed name races
    under concurrent writers (see sessions._atomic_write_json's docstring
    for the confirmed failure mode this pattern closes)."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    tmp.replace(path)


def _compact_index_locked() -> list[dict[str, Any]]:
    """Rewrite index.jsonl to exactly the folded (latest-per-id, non-deleted)
    state. MUST be called while holding _INDEX_LOCK, and does its OWN fresh
    read under that lock — a concurrent _append_index_stub can only land
    strictly before or after this read (both threads share _INDEX_LOCK), so
    an in-flight append can never be silently dropped by the rewrite.
    Returns the survivor stubs actually written, so callers don't need a
    third read to get fresh state."""
    raw = _read_jsonl(_index_path())
    folded: dict[str, dict[str, Any]] = {}
    for line in raw:
        note_id = line.get("id")
        if note_id:
            folded[note_id] = line
    survivors = [stub for stub in folded.values() if not stub.get("deleted")]
    _atomic_replace_jsonl(_index_path(), survivors)
    log.info("notes: compacted index.jsonl %d -> %d line(s)", len(raw), len(survivors))
    return survivors


def _compact_tabs_locked() -> list[dict[str, Any]]:
    """tabs.jsonl sibling of _compact_index_locked — same contract, guarded
    by _TABS_LOCK instead."""
    raw = _read_jsonl(_tabs_path())
    folded: dict[str, dict[str, Any]] = {}
    for line in raw:
        tab_id = line.get("id")
        if tab_id:
            folded[tab_id] = line
    survivors = [rec for rec in folded.values() if not rec.get("deleted")]
    _atomic_replace_jsonl(_tabs_path(), survivors)
    log.info("notes: compacted tabs.jsonl %d -> %d line(s)", len(raw), len(survivors))
    return survivors


def _fold_index() -> dict[str, dict[str, Any]]:
    """Fold index.jsonl to the latest stub per note id, dropping deleted ones.

    Self-compacting (2026-07-11): index.jsonl is append-only — every note
    mutation appends a FULL new stub, raw_text included (see _index_stub's
    docstring — the stub carries raw_text so listing avoids an N+1 get_note()
    per item), so a note touched N times over its life carries N copies of
    its own content in the file even though only the LATEST is ever read.
    Confirmed live (2026-07-11 production incident): a 70MB/3700-line index
    folding to ~150 live notes made every list_notes()/list_tabs() call a
    multi-second synchronous parse, which is what made organize_library's
    freeze-class bug (see notebook_organizer.py) catastrophic instead of
    just slow, and degraded the daemon under any bulk-write workload (a
    stress test bulk-adding resolutions) even with that fix in place.

    Folding is provably lossless — the fold below already discards every-
    thing but the latest entry per id — so once the raw line count mean-
    ingfully exceeds the folded count, persist the fold back to disk. No
    behavior changes for callers; this is pure garbage collection of
    already-dead history that was never read again."""
    raw = _read_jsonl(_index_path())
    folded: dict[str, dict[str, Any]] = {}
    for line in raw:
        note_id = line.get("id")
        if not note_id:
            continue
        folded[note_id] = line

    if len(raw) - len(folded) > _COMPACT_EXCESS_LINES:
        with _INDEX_LOCK:
            survivors = _compact_index_locked()
        return {stub["id"]: stub for stub in survivors}

    return {nid: stub for nid, stub in folded.items() if not stub.get("deleted")}


def _fold_tabs() -> dict[str, dict[str, Any]]:
    """Fold tabs.jsonl to the latest record per tab id, dropping deleted
    ones. Self-compacting sibling of _fold_index — same rationale, much
    smaller file today, but the same append-only growth pattern applies."""
    raw = _read_jsonl(_tabs_path())
    folded: dict[str, dict[str, Any]] = {}
    for line in raw:
        tab_id = line.get("id")
        if not tab_id:
            continue
        folded[tab_id] = line

    if len(raw) - len(folded) > _COMPACT_EXCESS_LINES:
        with _TABS_LOCK:
            survivors = _compact_tabs_locked()
        return {rec["id"]: rec for rec in survivors}

    return {tid: rec for tid, rec in folded.items() if not rec.get("deleted")}


# ---------------------------------------------------------------------------
# Public API — notes
# ---------------------------------------------------------------------------


def _compute_llm_fields(
    raw_text: str, sensitive: bool
) -> tuple[str | None, list[dict[str, str]] | None]:
    """(llm_text, redactions) for a note — redact_secrets() when sensitive,
    else (None, None) since llm_view() falls through to raw_text for
    non-sensitive notes and these fields have no meaning there."""
    if not sensitive:
        return None, None
    return redact_secrets(raw_text)


def add_note(
    raw_text: str,
    tab_id: str = "",
    title: str = "",
    repo: str = "",
    sensitive: bool = False,
) -> dict[str, Any]:
    """Create a draft note. `pipeline` is null until the transform runs (Phase 1c).

    `repo`: which codebase this note is validated against (north-star:
    code is source of truth, notes are re-validated caches of it). Defaults
    to _DEFAULT_REPO ("khimaira") — must match a project name khimaira's
    discovery registry resolves to a filesystem path, or revalidate_note()
    can't find anchor files to check against.

    `sensitive`: when True, computes a redacted `llm_text` twin (see
    notebook_redaction.redact_secrets) — every downstream LLM/embedding/
    training/cross-note-context egress reads llm_view(record) instead of
    raw_text, so the note's real secret values never reach a model call.
    """
    note_id = _new_id()
    now = _now_iso()
    llm_text, redactions = _compute_llm_fields(raw_text, sensitive)
    # Auto-derived titles take their FIRST LINE verbatim (_derive_title) —
    # for a sensitive note that line could BE the secret. Derive from the
    # redacted twin instead when sensitive, so the title itself (used
    # unredacted in _index_stub/list views, ask-synthesis headers, chat
    # instructions) never carries a real secret. An EXPLICIT `title` param
    # is used as-is either way — this only affects auto-derivation.
    title_source = llm_text if sensitive else raw_text
    record: dict[str, Any] = {
        "id": note_id,
        "created_at": now,
        "updated_at": now,
        "title": title or _derive_title(title_source),
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
        # When the structuring pipeline last (re)generated the tabs — distinct
        # from updated_at (which bumps on any edit, incl. resolution/title). None
        # until the first transform completes (set in set_pipeline).
        "structured_at": None,
        "resolution": "",
        "resolved_by": "",
        "resolved_at": None,
        "kind": _DEFAULT_KIND,
        "source_path": None,
        "organized_at": None,
        "sensitive": sensitive,
        "llm_text": llm_text,
        "redactions": redactions,
        "priority": _DEFAULT_PRIORITY,
        "pinned_placement": False,
        "starred": False,
        "test_status": _DEFAULT_TEST_STATUS,
    }
    _write_note_atomic(note_id, record)
    _append_index_stub(record)
    log.info("notes: added %s in tab=%s repo=%s", note_id, record["tab_id"], record["repo"])
    return record


def add_study_guide(
    raw_text: str,
    *,
    tab_id: str = "",
    title: str = "",
    repo: str = "",
    source_path: str | None = None,
    sensitive: bool = False,
) -> dict[str, Any]:
    """Create a study guide — a distinct KIND of note: a finished,
    human-authored deliverable to be HOUSED + RENDERED, not re-expressed
    into the note pipeline's summary/technical/plain triple.

    LOAD-BEARING INVARIANT: raw_text (the guide body) is the human
    deliverable and is NEVER LLM-rewritten (except an explicit,
    human-approved research REVISE — a later phase). `pipeline` is null
    until trigger_study_guide_pipeline runs, same as a regular note's
    draft state, but the eventual shape is discriminated:
    {abstract, toc, tags, entities} — see set_study_guide_pipeline.

    `source_path`: import provenance + dedup key (notebook_import.py keys
    on this via find_by_source_path to avoid re-importing the same file) +
    the eventual export round-trip target. None for guides authored
    directly (not imported from a file).

    `sensitive`: see add_note's docstring — same redacted-twin contract.
    """
    note_id = _new_id()
    now = _now_iso()
    llm_text, redactions = _compute_llm_fields(raw_text, sensitive)
    # Auto-derived titles take their FIRST LINE verbatim (_derive_title) —
    # for a sensitive note that line could BE the secret. Derive from the
    # redacted twin instead when sensitive, so the title itself (used
    # unredacted in _index_stub/list views, ask-synthesis headers, chat
    # instructions) never carries a real secret. An EXPLICIT `title` param
    # is used as-is either way — this only affects auto-derivation.
    title_source = llm_text if sensitive else raw_text
    record: dict[str, Any] = {
        "id": note_id,
        "created_at": now,
        "updated_at": now,
        "title": title or _derive_title(title_source),
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
        "structured_at": None,
        "resolution": "",
        "resolved_by": "",
        "resolved_at": None,
        "kind": "study_guide",
        "source_path": source_path,
        "organized_at": None,
        "sensitive": sensitive,
        "llm_text": llm_text,
        "redactions": redactions,
        "priority": _DEFAULT_PRIORITY,
        "pinned_placement": False,
        "starred": False,
        "test_status": _DEFAULT_TEST_STATUS,
    }
    _write_note_atomic(note_id, record)
    _append_index_stub(record)
    log.info(
        "notes: added study guide %s in tab=%s repo=%s source_path=%s",
        note_id,
        record["tab_id"],
        record["repo"],
        source_path,
    )
    return record


def get_note(note_id: str) -> dict[str, Any]:
    record = _read_note_file(note_id)
    if record is None:
        raise ValueError(f"No note with id={note_id!r}. Use list_notes() to see available notes.")
    return record


def find_by_source_path(source_path: str) -> dict[str, Any] | None:
    """Find an existing note by its import source_path — the dedup key
    notebook_import.py uses to avoid re-importing the same file on a
    repeat run. Full-scan (mirrors list_notes' own filtering approach);
    fine at the ~130-guide scale this exists for."""
    for stub in list_notes():
        if stub.get("source_path") == source_path:
            return get_note(stub["id"])
    return None


def list_notes(
    tab_id: str | None = None,
    repo: str | None = None,
    kind: str | None = None,
    priority: str | None = None,
    starred: bool | None = None,
    test_status: str | None = None,
) -> list[dict[str, Any]]:
    """Newest-created first. Sorted by created_at (not updated_at) so a
    revalidate/heal pass — which only bumps updated_at — doesn't reshuffle
    the list out from under someone reading it.

    `repo`, when given, scopes to that repo PLUS GENERAL_REPO (the "no
    codebase" bucket for cross-cutting notes always stays visible alongside
    whichever project is in view). `repo=None` returns everything — the
    "All projects" view.

    `kind`, when given, scopes to that kind ("note" | "study_guide").
    `kind=None` returns both — callers that want notes-only (the existing
    UI) or guides-only (the grimoire Library view) filter explicitly,
    mirroring how personal-tab notes are excluded client-side today rather
    than hidden by default here.

    `priority`, when given, scopes to that priority ("low"|"normal"|"high"|
    "urgent"). `priority=None` returns all priorities.

    `starred`, when given, scopes to that starred state (FILE-MANAGER,
    2026-07-04 — the Starred rail). Filters on the folded index STUB, not a
    fresh get_note() per note — see _index_stub, which projects `starred`
    for exactly this reason.

    `test_status`, when given, scopes to that testing-workflow status
    ("untested"|"needs_testing"|"in_review"|"tested"). `test_status=None`
    returns all."""
    stubs = list(_fold_index().values())
    if tab_id is not None:
        stubs = [s for s in stubs if s["tab_id"] == tab_id]
    if repo is not None:
        stubs = [s for s in stubs if s.get("repo") in (repo, GENERAL_REPO)]
    if kind is not None:
        stubs = [s for s in stubs if s.get("kind", _DEFAULT_KIND) == kind]
    if priority is not None:
        stubs = [s for s in stubs if s.get("priority", _DEFAULT_PRIORITY) == priority]
    if starred is not None:
        stubs = [s for s in stubs if s.get("starred", False) == starred]
    if test_status is not None:
        stubs = [s for s in stubs if s.get("test_status", _DEFAULT_TEST_STATUS) == test_status]
    stubs.sort(key=lambda s: s["created_at"], reverse=True)
    return stubs


def update_note(note_id: str, **fields: Any) -> dict[str, Any]:
    """Edit a note. Accepts title/tab_id/raw_text/status/links/repo, plus a
    `pipeline` kwarg treated as a partial patch merged onto the existing
    pipeline dict (manual edits to summary/technical/plain/etc).

    Changing `repo` to a different value re-anchors future validation: the
    old validated_git_sha means nothing against a different repo's git
    history, so it's cleared along with last_validated_at, forcing a full
    re-check (not a heal-vs-stale-sha comparison) on the next revalidate.

    Grimoire Phase 4 (2026-07-04): any ACTUAL raw_text change (guide or
    note) snapshots the OUTGOING raw_text into `history` before it's
    overwritten — a REVISE Apply (or any manual edit) is otherwise a lossy,
    unrecoverable overwrite of the deliverable. Shape is `{raw_text,
    replaced_at}` — deliberately a DIFFERENT key shape than
    apply_validation's pipeline-heal entries (`{pipeline, replaced_at,
    validated_git_sha}`), so the two kinds coexist in the same list without
    a schema migration; discriminated by which key is present, not an
    explicit "kind" tag. See backfill_drop_spurious_heals below for the
    corresponding read-side fix (it used to assume every history entry has
    a "pipeline" key)."""
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
    if "priority" in fields and fields["priority"] not in _VALID_PRIORITIES:
        raise ValueError(
            f"Invalid priority {fields['priority']!r}; must be one of {sorted(_VALID_PRIORITIES)}."
        )
    if "test_status" in fields and fields["test_status"] not in _VALID_TEST_STATUSES:
        raise ValueError(
            f"Invalid test_status {fields['test_status']!r}; "
            f"must be one of {sorted(_VALID_TEST_STATUSES)}."
        )
    if "repo" in fields and fields["repo"] != record.get("repo"):
        record["validated_git_sha"] = None
        record["last_validated_at"] = None
    raw_text_changed = "raw_text" in fields and fields["raw_text"] != record.get("raw_text")
    if raw_text_changed:
        record.setdefault("history", []).append(
            {"raw_text": record["raw_text"], "replaced_at": _now_iso()}
        )
    sensitive_changed = "sensitive" in fields and fields["sensitive"] != record.get(
        "sensitive", False
    )
    record.update(fields)

    # Re-derive the redacted twin whenever raw_text actually changed OR the
    # sensitive flag flipped — whichever affects what llm_view() returns.
    # Clears llm_text/redactions when sensitive flips off (they're moot once
    # llm_view() falls through to raw_text directly).
    if record.get("sensitive") and (raw_text_changed or sensitive_changed):
        record["llm_text"], record["redactions"] = redact_secrets(record["raw_text"])
    elif not record.get("sensitive") and sensitive_changed:
        record["llm_text"] = None
        record["redactions"] = None

    if pipeline_patch is not None:
        merged = dict(record.get("pipeline") or {})
        merged.update(pipeline_patch)
        record["pipeline"] = merged
    record["updated_at"] = _now_iso()
    _write_note_atomic(note_id, record)
    _append_index_stub(record)
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
    now = _now_iso()
    record["updated_at"] = now
    # Stamp when the tabs were (re)generated — the reader shows "structured
    # <time>" from this, and a fresh reprocess visibly bumps it.
    record["structured_at"] = now
    _write_note_atomic(note_id, record)
    _append_index_stub(record)
    return record


def set_study_guide_pipeline(note_id: str, pipeline: dict[str, Any]) -> dict[str, Any]:
    """Full replace of a study guide's pipeline dict (called by
    notebook_pipeline.trigger_study_guide_pipeline on completion) — marks
    it processed. `pipeline` is the discriminated guide shape:
    {abstract, toc, tags, entities}.

    Unlike set_pipeline, NEVER touches `title` — a study guide's title is
    human-authored or import-derived (from its filename/first heading),
    never LLM-regenerated. Guides are finished deliverables, and title is
    part of that deliverable, not a note-pipeline artifact to improve.
    Delegates to set_pipeline (identical write, just never passes a title)
    rather than duplicating the atomic-write logic."""
    return set_pipeline(note_id, pipeline)


def mark_organized(note_id: str, tab_id: str | None = None) -> dict[str, Any]:
    """Stamp organized_at (the organizer just placed/checked this note) and
    optionally re-file it (set tab_id) in one write. The Phase 1 hook is
    notebook_organizer.assign_deterministic (called right after import/
    creation); the batched LLM organize_library() pass also lands here;
    notebook_import.py's own inline re-file does too.

    Pinned notes (2026-07-04, FILE-MANAGER): a note with
    `pinned_placement=True` NEVER has its tab_id changed here, regardless of
    caller. This is the single chokepoint every automated organize path
    (assign_deterministic, organize_library, notebook_import's re-file)
    already funnels through, so guarding HERE closes the whole "auto-
    organizer overrides a manual pin" class in one place instead of
    requiring every current AND future caller to remember its own check —
    same shape as llm_view() closing the sensitive-notes egress class.
    organized_at is still stamped either way (the item WAS considered/
    checked, it just kept its user-chosen placement).

    This does NOT apply to a structural re-file where the note's OWN tab was
    deleted (delete_tab) — that path writes tab_id directly via update_note,
    not through here, since a pinned note whose home vanished must still be
    relocated (a dead tab_id is a genuine data-integrity break, not an
    organizer opinion the pin should override)."""
    record = get_note(note_id)
    now = _now_iso()
    if tab_id is not None and not record.get("pinned_placement"):
        record["tab_id"] = tab_id
    record["organized_at"] = now
    record["updated_at"] = now
    _write_note_atomic(note_id, record)
    _append_index_stub(record)
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
    _append_index_stub(record)
    return record


def add_resolution(note_id: str, resolution: str, resolved_by: str = "") -> dict[str, Any]:
    """Attach a resolution to a note — the roster-loop write-back.

    A note is the shared problem/task record between Joseph and the agent
    roster; a resolution is what closes it (worked to completion, written
    back). This is the notebook's training-quality gate: `resolution != ""`
    is what promotes a note's lifecycle to "resolved" (see `derive_lifecycle`)
    and is what makes it eligible to feed the mnemosyne distiller (see
    khimaira.monitor.notebook_training). Additive only — raw_text and
    pipeline are untouched, matching apply_validation's "never overwrite the
    original paste" discipline.

    Passing resolution="" clears resolved_at/resolved_by (explicit un-resolve),
    mirroring update_note's general edit semantics.
    """
    record = get_note(note_id)
    now = _now_iso()
    record["resolution"] = resolution
    record["resolved_by"] = resolved_by if resolution else ""
    record["resolved_at"] = now if resolution else None
    record["updated_at"] = now
    _write_note_atomic(note_id, record)
    _append_index_stub(record)
    log.info("notes: resolution added to %s by=%s", note_id, resolved_by or "(unattributed)")
    return record


def promote_note(note_id: str) -> dict[str, Any]:
    """Curated promotion — mark training.promoted=True. Human gate only;
    never auto-promoted.

    Sensitive notes are hard-excluded from training (2026-07-04) — raises
    rather than silently no-op'ing, since promotion is an explicit human
    action that deserves a loud, explicit rejection, not a confusing no-op."""
    record = get_note(note_id)
    if record.get("sensitive"):
        raise ValueError(
            f"Note {note_id!r} is sensitive — sensitive notes are hard-excluded "
            "from training/promotion."
        )
    now = _now_iso()
    record["training"]["promoted"] = True
    record["training"]["promoted_at"] = now
    record["status"] = "promoted"
    record["updated_at"] = now
    _write_note_atomic(note_id, record)
    _append_index_stub(record)
    return record


def delete_note(note_id: str) -> dict[str, Any]:
    record = get_note(note_id)
    _note_path(note_id).unlink(missing_ok=True)
    _append_index_stub(record, deleted=True)
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
    return all(a.get(k) == b.get(k) for k in _SUBSTANCE_FIELDS) and a.get("organized_md") != b.get(
        "organized_md"
    )


def backfill_drop_spurious_heals(note_id: str) -> dict[str, Any]:
    """Drops any history entry whose archived pipeline differs from the
    NEXT version in the chain (or the current pipeline, for the last
    entry) ONLY in organized_md. Idempotent — a no-op on already-clean
    history.

    Grimoire Phase 4: `history` can now also carry raw_text-revision
    entries (see update_note) that have NO "pipeline" key at all — these
    are never a spurious-heal candidate (a different kind of entry
    entirely), so they're always kept. When comparing a pipeline-heal entry
    against "the next version in the chain," any interleaved raw_text
    entries are skipped over (they carry no pipeline to compare against) —
    the comparison target is the next entry that DOES carry a pipeline, or
    the record's current pipeline if none follows."""
    record = get_note(note_id)
    history = record.get("history") or []
    if not history:
        return record

    kept = []
    for i, entry in enumerate(history):
        if "pipeline" not in entry:
            kept.append(entry)  # e.g. a raw_text-revision entry — not a heal candidate
            continue
        next_pipeline = record.get("pipeline") or {}
        for later in history[i + 1 :]:
            if "pipeline" in later:
                next_pipeline = later["pipeline"]
                break
        if _same_substance_different_organized_md(entry.get("pipeline") or {}, next_pipeline):
            continue  # spurious wording-only "heal" — drop
        kept.append(entry)

    if len(kept) == len(history):
        return record

    record["history"] = kept
    _write_note_atomic(note_id, record)
    _append_index_stub(record)
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


def _assert_sibling_unique(
    title: str, kind: str, parent_id: str | None, *, exclude_tab_id: str | None = None
) -> None:
    """FILE-MANAGER (2026-07-04): `(kind, parent_id, title_norm)` must be
    unique among live tabs — the invariant that replaces the old GLOBAL
    title-uniqueness assumption once tabs nest (two "API" collections under
    DIFFERENT parents are fine; two under the SAME parent are not — the
    latter is what silently let the organizer misfile before nesting
    existed to make the ambiguity possible)."""
    title_norm = title.strip().lower()
    for tab in list_tabs():
        if tab["id"] == exclude_tab_id:
            continue
        if (
            tab.get("kind") == kind
            and tab.get("parent_id") == parent_id
            and tab["title"].strip().lower() == title_norm
        ):
            raise TabValidationError(
                f"A {kind} tab titled {title!r} already exists under this parent "
                "— sibling titles must be unique."
            )


def _tab_ancestor_ids(tab_id: str, tabs_by_id: dict[str, dict[str, Any]]) -> list[str]:
    """Ancestor tab_ids from immediate parent up to root, in order. Defends
    against a corrupt/cyclic parent chain already on disk (pre-invariant
    data) by capping at len(tabs_by_id) hops rather than looping forever —
    this walk itself must never be the infinite loop the cycle check exists
    to prevent elsewhere."""
    ancestors: list[str] = []
    seen: set[str] = set()
    current = tabs_by_id.get(tab_id)
    hops = 0
    while current is not None and current.get("parent_id") is not None and hops <= len(tabs_by_id):
        pid = current["parent_id"]
        if pid in seen:
            break
        seen.add(pid)
        ancestors.append(pid)
        current = tabs_by_id.get(pid)
        hops += 1
    return ancestors


def add_tab(title: str = "", *, kind: str = "", parent_id: str | None = None) -> dict[str, Any]:
    """`kind`: "folder" (default, regular note groups) or "collection"
    (study-guide groups, shown in the Library view) — so the two don't
    intermix in the filter bar.

    `parent_id` (FILE-MANAGER, 2026-07-04): nests this tab under an
    existing tab (adjacency list — None means root level). Enforces, at
    creation time same as reparent: the parent must exist (reuses get_tab's
    own ValueError), `parent.kind == kind` (homogeneous subtree — folders
    and collections never nest into each other), and sibling-uniqueness
    among `(kind, parent_id, title_norm)`.
    """
    _ensure_dirs()
    if kind and kind not in _VALID_TAB_KINDS:
        raise ValueError(f"Invalid tab kind {kind!r}; must be one of {sorted(_VALID_TAB_KINDS)}.")
    kind = kind or _DEFAULT_TAB_KIND
    if parent_id is not None:
        parent = get_tab(parent_id)  # raises plain ValueError if missing
        if parent.get("kind") != kind:
            raise TabValidationError(
                f"Cannot create a {kind!r} tab under a {parent.get('kind')!r} parent "
                "(folders and collections don't nest into each other)."
            )
    title = title or f"Tab {_new_id()[:6]}"
    _assert_sibling_unique(title, kind, parent_id)
    tab_id = _new_id()
    now = _now_iso()
    record = {
        "id": tab_id,
        "title": title,
        "kind": kind,
        "parent_id": parent_id,
        "created_at": now,
        "updated_at": now,
        "deleted": False,
    }
    _append_tab_record(record)
    return _with_note_ids(record)


def get_tab(tab_id: str) -> dict[str, Any]:
    folded = _fold_tabs()
    record = folded.get(tab_id)
    if record is None:
        raise ValueError(f"No tab with id={tab_id!r}. Use list_tabs() to see available tabs.")
    return _with_note_ids(record)


def update_tab(tab_id: str, **fields: Any) -> dict[str, Any]:
    """Edit title/kind/parent_id. Reparenting (`parent_id` in fields)
    enforces ALL FOUR tab invariants (FILE-MANAGER, 2026-07-04):

    1. No cycle — a tab is never its own ancestor (else infinite-loop /
       stack-overflow DoS on every tree-build / breadcrumb / descendant
       walk). Walks ancestors of the proposed parent; rejects if `tab_id`
       appears.
    2. Homogeneous subtree — `parent.kind == (new) kind`.
    3. Sibling-unique — `(kind, parent_id, title_norm)` among live tabs.
    4. Parent exists — a dangling `parent_id` is rejected (reuses get_tab's
       own ValueError).

    Raises TabValidationError (a ValueError subclass) for 1-3; a plain
    ValueError for an unknown tab_id/parent_id (mirrors get_tab)."""
    _ensure_dirs()
    existing = get_tab(tab_id)
    existing.pop("note_ids", None)
    unknown = set(fields) - {"title", "kind", "parent_id"}
    if unknown:
        raise ValueError(
            f"Unknown tab field(s): {sorted(unknown)}. "
            "Mutable fields: ['title', 'kind', 'parent_id']."
        )
    if "kind" in fields and fields["kind"] not in _VALID_TAB_KINDS:
        raise ValueError(
            f"Invalid tab kind {fields['kind']!r}; must be one of {sorted(_VALID_TAB_KINDS)}."
        )

    new_kind = fields.get("kind", existing["kind"])
    new_parent_id = fields["parent_id"] if "parent_id" in fields else existing.get("parent_id")
    new_title = fields.get("title", existing["title"])

    if "parent_id" in fields and new_parent_id is not None:
        if new_parent_id == tab_id:
            raise TabValidationError(f"Tab {tab_id!r} cannot be its own parent.")
        tabs_by_id = {t["id"]: t for t in list_tabs()}
        parent = tabs_by_id.get(new_parent_id)
        if parent is None:
            raise ValueError(
                f"No tab with id={new_parent_id!r}. Use list_tabs() to see available tabs."
            )
        if parent["kind"] != new_kind:
            raise TabValidationError(
                f"Cannot reparent a {new_kind!r} tab under a {parent['kind']!r} tab "
                "(folders and collections don't nest into each other)."
            )
        if tab_id in _tab_ancestor_ids(new_parent_id, tabs_by_id):
            raise TabValidationError(
                f"Reparenting {tab_id!r} under {new_parent_id!r} would create a cycle "
                "(the proposed parent is a descendant of this tab)."
            )

    if {"title", "parent_id", "kind"} & set(fields):
        _assert_sibling_unique(new_title, new_kind, new_parent_id, exclude_tab_id=tab_id)

    existing.update(fields)
    existing["updated_at"] = _now_iso()
    existing["deleted"] = False
    _append_tab_record(existing)
    return _with_note_ids(existing)


def delete_tab(tab_id: str) -> dict[str, Any]:
    """Delete a tab (FILE-MANAGER, 2026-07-04 — greenfield, no prior
    implementation). "Never lose a guide" requires re-filing TWO things,
    not just one — the easy thing a naive implementation omits is the
    second:

    1. Child tabs → reparent to the deleted tab's OWN parent (one level up
       — not always root, so deleting a tab deep in a tree collapses one
       level, not the whole subtree). Bypasses update_tab's own
       cycle/kind/uniqueness validation via a direct write: cycle is
       structurally impossible here (the new parent is this tab's own
       parent, which cannot be a descendant of any of this tab's children
       without an already-corrupt tree) and kind homogeneity is preserved
       automatically (a child's kind already equals this tab's kind, which
       already equals the grandparent's kind, transitively). A sibling-name
       COLLISION at the destination is possible (rare) and is deliberately
       NOT blocked here — a cosmetic, later-fixable naming clash is a far
       better failure mode than aborting a delete partway through or losing
       a child tab's contents outright.
    2. Direct member NOTES → re-file to the deleted tab's parent (or
       _DEFAULT_TAB_ID if the deleted tab was itself at root) — THIS is the
       actual "never lose a guide" enforcement. Reparenting child tabs
       alone leaves member notes carrying a now-dead tab_id: not deleted
       from disk, but tree-unreachable (invisible in every tab-scoped
       list/breadcrumb). Also un-pins the note (a pin to a tab that no
       longer exists doesn't mean anything — the organizer is free to
       reclaim it on the next sweep; the user can always re-pin).

    Raises ValueError if tab_id doesn't exist (mirrors get_tab)."""
    existing = get_tab(tab_id)
    fallback_parent_id = existing.get("parent_id")  # None -> root
    fallback_note_tab_id = fallback_parent_id or _DEFAULT_TAB_ID

    for child in list_tabs():
        if child.get("parent_id") == tab_id:
            child["parent_id"] = fallback_parent_id
            child["updated_at"] = _now_iso()
            child["deleted"] = False
            _append_tab_record(child)

    for note_stub in list_notes(tab_id=tab_id):
        update_note(note_stub["id"], tab_id=fallback_note_tab_id, pinned_placement=False)

    now = _now_iso()
    existing.pop("note_ids", None)
    existing["deleted"] = True
    existing["updated_at"] = now
    _append_tab_record(existing)
    log.info(
        "notes: deleted tab %s (children+notes reparented to %s)", tab_id, fallback_note_tab_id
    )
    return {"id": tab_id, "deleted": True}


def list_tabs() -> list[dict[str, Any]]:
    tabs = [_with_note_ids(rec) for rec in _fold_tabs().values()]
    tabs.sort(key=lambda t: t["created_at"])
    return tabs


def _with_note_ids(tab_record: dict[str, Any]) -> dict[str, Any]:
    out = dict(tab_record)
    out.setdefault("kind", _DEFAULT_TAB_KIND)
    out.setdefault("parent_id", None)  # pre-FILE-MANAGER tabs read as root
    out["note_ids"] = [n["id"] for n in list_notes(tab_id=tab_record["id"])]
    return out


def _get_or_create_tab_by_kind(
    title: str, kind: str, parent_id: str | None = None
) -> dict[str, Any]:
    """Find an existing tab of the given `kind` + `parent_id` matching
    `title` (case-insensitive), or create one. The deterministic-first
    primitive shared by notebook_import.py and notebook_organizer.py — both
    need "does a tab named X already exist AT THIS LOCATION" before
    deciding to create a new one vs re-file into an existing one.

    Parent-scoped (FILE-MANAGER, 2026-07-04): matching on `(kind, parent_id,
    title_norm)`, NOT title alone — title-alone matching returns an
    arbitrary same-named sibling once titles are non-unique across a
    nested tree, silently cross-filing guides on every organize sweep. v1
    callers (notebook_organizer) always pass `parent_id=None` — the
    organizer auto-files into ROOT-level collections/folders only; a
    human-authored nested "API" collection is deliberately invisible to
    the organizer's auto-create until a tree-aware v2 pass (documented safe
    limitation, not a silent misfile)."""
    title_norm = title.strip().lower()
    for tab in list_tabs():
        if (
            tab.get("kind") == kind
            and tab.get("parent_id") == parent_id
            and tab["title"].strip().lower() == title_norm
        ):
            return tab
    return add_tab(title=title, kind=kind, parent_id=parent_id)


def get_or_create_collection(title: str, parent_id: str | None = None) -> dict[str, Any]:
    """Get-or-create a `kind="collection"` tab — study guides' organize
    destination. `parent_id=None` (root) is what the organizer always
    passes — nesting is human-authored, see _get_or_create_tab_by_kind."""
    return _get_or_create_tab_by_kind(title, "collection", parent_id)


def get_or_create_folder(title: str, parent_id: str | None = None) -> dict[str, Any]:
    """Get-or-create a `kind="folder"` tab — regular notes' organize
    destination (the sibling to get_or_create_collection, added when the
    organizer was extended to notes — kept in its own namespace so notes
    and guides never intermix in the tab filter bar)."""
    return _get_or_create_tab_by_kind(title, "folder", parent_id)
