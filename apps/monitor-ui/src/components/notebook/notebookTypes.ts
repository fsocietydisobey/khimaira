/**
 * Wire shapes for the AI-notebook — mirrors the note/tab record schema in
 * packages/khimaira/src/khimaira/monitor/notes.py (Phase 1a backend).
 *
 * Served by the khimaira daemon at /api/notes + /api/tabs (same-origin via
 * the vite /api proxy → daemon 8740, no project scoping — notes are global
 * daemon state, not per-project).
 */

export type NoteStatus = "draft" | "processed" | "promoted" | "failed";

/** User-set importance, independent of `NoteStatus` (lifecycle). Mirrors the
 *  backend's `_VALID_PRIORITIES`; default "normal". */
export type NotePriority = "low" | "normal" | "high" | "urgent";

/** What got masked in a sensitive note's `llm_text` twin — never the secret
 *  value itself, only its placeholder + kind (for the reader's "what's
 *  hidden" panel). */
export interface NoteRedaction {
  placeholder: string;
  kind: string;
}

/** Grimoire (Phase 1f): a note is either a regular structured note, or a
 *  study guide — a finished deliverable to be housed + rendered, never
 *  re-expressed into the summary/technical/plain triple. Discriminated by
 *  `Note.kind`; narrow on that field before reading `pipeline`. */
export type NoteKind = "note" | "study_guide";

export interface NotePipeline {
  summary: string;
  technical: string;
  plain: string;
  organized_md: string;
  tags: string[];
  entities: string[];
}

/** One heading in a study guide's table of contents — a deterministic parse
 *  of `raw_text`'s markdown headings, not LLM-derived. `anchor` must match
 *  the `id` the guide reader assigns to the corresponding rendered heading. */
export interface StudyGuideTocEntry {
  title: string;
  anchor: string;
  level: number;
}

export interface StudyGuidePipeline {
  /** LLM-derived library-card blurb — the only LLM-derived field on a guide. */
  abstract: string;
  toc: StudyGuideTocEntry[];
  tags: string[];
  entities: string[];
}

export interface NoteTraining {
  promoted: boolean;
  promoted_at: string | null;
  domain: string;
  distilled_pairs: number;
}

export interface Note {
  id: string;
  created_at: string;
  updated_at: string;
  title: string;
  tab_id: string;
  raw_text: string;
  status: NoteStatus;
  /** Default "note" for every pre-grimoire record. Narrow on this before
   *  reading `pipeline` — it discriminates NotePipeline vs StudyGuidePipeline. */
  kind: NoteKind;
  /** Null until the Phase 1c transform (note) or study-guide pipeline completes. */
  pipeline: NotePipeline | StudyGuidePipeline | null;
  /** Import provenance for a study guide (dedup key + export round-trip target).
   *  Null for notes and for guides authored directly (not imported). */
  source_path: string | null;
  embedding_id: string | null;
  training: NoteTraining;
  links: string[];
  /** North-star (Phase 2a): which codebase this note is validated against. */
  repo: string;
  /** Null if never revalidated. */
  last_validated_at: string | null;
  /** Git SHA the note was last checked against; null if never revalidated. */
  validated_git_sha: string | null;
  /** When the structuring pipeline last (re)generated the tabs — distinct from
   *  updated_at (which bumps on any edit). null until first structured. A
   *  raw_text edit re-runs the pipeline and bumps this. */
  structured_at: string | null;
  /** Count of prior pipeline versions superseded by a heal (list view carries
   *  only the count — full version bodies are on the note's `history` array,
   *  fetched via GET /notes/{id} if ever needed). */
  history_count: number;
  /** v2 roster-loop write-back: the outcome written when a note is resolved
   *  (`notebook_add_resolution`). Empty string until resolved — its presence
   *  is what flips the note's lifecycle to "resolved" in the UI. */
  resolution: string;
  /** Session name/id that attached the resolution; "" if unattributed. */
  resolved_by: string;
  /** When the resolution was attached; null until resolved. */
  resolved_at: string | null;
  /** When the organizer last placed/checked this note's tab_id. Null if the
   *  organizer has never touched it (e.g. every pre-grimoire note). */
  organized_at: string | null;
  /** User-set importance; independent of `status`. Default "normal". */
  priority: NotePriority;
  /** True if this note contains credentials — its LLM egress (structuring,
   *  embedding, chat, training export) is redacted; `raw_text` (this field)
   *  stays the real, human-readable text. Default false on new records —
   *  but pre-grimoire records were never backfilled, so this is live-
   *  verified `null` on those (not `false`); always check truthiness,
   *  never `=== false`. */
  sensitive: boolean | null;
  /** What was masked in the redacted twin — null for non-sensitive notes.
   *  NEVER contains the actual secret value. */
  redactions: NoteRedaction[] | null;
  /** File-manager (2026-07-04): a manual drag-move sets `tab_id` AND this
   *  together — `organize_library` skips pinned notes, so a manual placement
   *  survives every future auto-organize sweep (and re-import). Default
   *  false. */
  pinned_placement: boolean;
  /** File-manager: the Starred rail. Default false. */
  starred: boolean;
}

/** Discriminates a plain note-folder from a study-guide collection so the
 *  two don't intermix in the tab filter bar. Absent/undefined on pre-grimoire
 *  tab records — treat as "folder". */
export type NotebookTabKind = "folder" | "collection";

export interface NotebookTab {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  kind?: NotebookTabKind;
  /** File-manager (2026-07-04): adjacency-list nesting — null is root level.
   *  Pre-migration tabs read as null via the backend's setdefault. */
  parent_id: string | null;
  /** Note ids grouped by tab_id — derived at read time, not stored redundantly. */
  note_ids: string[];
}

/** True when `note.kind === "study_guide"` — narrows `note.pipeline` to
 *  `StudyGuidePipeline` for callers that already branched on `Note.kind`. */
export function isStudyGuidePipeline(
  pipeline: NotePipeline | StudyGuidePipeline | null,
): pipeline is StudyGuidePipeline {
  return pipeline !== null && "toc" in pipeline;
}

/** A code chunk (Séance semantic search, or a grep fallback) cited in an ask answer. */
export interface CodeSource {
  repo: string;
  file_path: string;
  start_line: number;
  end_line: number;
}

/** Grimoire (Phase 3 chat redesign) — grounding metadata on a chat message.
 *  `code_citations`/`web_citations` are plain strings, NOT objects — the
 *  same shape already byte-verified on the (now-retired) research toolbar
 *  (`"file_path:line"` for code, bare URLs for web). Carrying that forward
 *  rather than re-guessing an object shape. */
export interface ChatGrounding {
  web_grounded: boolean;
  web_grounding_unverified: boolean;
  code_citations: string[];
  web_citations: string[];
}

/** An edit a chat turn auto-applied to the guide's raw_text. `diff` is a
 *  single pre-formatted string from the backend (NOT a {before, after} pair
 *  — unlike the retired toolbar's REVISE proposal, there is no separate
 *  Accept step to diff against, since chat edits auto-apply). UNVERIFIED:
 *  the exact text format of `diff` (assumed unified-diff, +/- line prefixes)
 *  hasn't been confirmed against a live call yet — confirm before trusting
 *  the color-coding in ChatEditDiff renders it meaningfully. */
export interface ChatEdit {
  section_anchor?: string;
  diff: string;
  applied_at?: string;
}

/** One turn in the unified chat sidebar (Grimoire, tasks/grimoire/CHAT-
 *  UNIFY.md) — renders TWO distinct backend shapes through one type:
 *
 *  - **Per-record** (guide or note, `POST/GET /notes/{id}/chat`, persistent):
 *    `edit`/`cost`/`grounding` — agentic, may auto-apply an edit. `sources`
 *    (2026-07-06) is now ALSO populated here — the per-record chat's
 *    subprocess has no live tool to browse other notes (deliberately
 *    MCP-free), so `notebook_chat.run_chat_turn` retrieval-injects the
 *    top semantically-related OTHER notes into the prompt and reports
 *    which ones via this same field, matching answer_question's shape so
 *    `ChatBubble` doesn't need a mode check to render either kind's
 *    citations.
 *  - **Notebook-wide** (`POST /notes/chat`, one-shot, client-accumulated
 *    only — no server history): `sources`/`healed`/`codeSources`/
 *    `codeUnavailable` — `answer_question`'s retrieval shape, converted at
 *    the integration boundary (`useNotebookChat`) so `ChatBubble` doesn't
 *    need to know which backend produced a message. No `edit`, no
 *    `grounding` (that's an agentic-only concept) on this path.
 *
 *  `system` is compact's summary message (per-record only — notebook-wide
 *  has no compact). */
export interface ChatMessage {
  role: "user" | "assistant" | "system";
  content: string;
  ts?: string;
  /** Per-record only. */
  edit?: ChatEdit;
  /** Per-record only. */
  cost?: number | null;
  /** Per-record only. */
  grounding?: ChatGrounding;
  /** Note ids consulted for this turn — populated by BOTH per-record chat
   *  (related-notes retrieval-injection) and notebook-wide ask. */
  sources?: string[];
  /** Notebook-wide only — subset of `sources` that were healed (revalidated
   *  and found stale) during this ask. Per-record chat does NOT revalidate
   *  its injected related notes (cost — chat turns are far more frequent
   *  than a one-off ask), so this stays empty/absent there. */
  healed?: string[];
  /** Notebook-wide only — code chunks, pre-formatted as "file_path:start-end"
   *  (converted from `CodeSource[]` at the integration boundary, same
   *  plain-string convention as `ChatGrounding.code_citations`). */
  codeSources?: string[];
  /** Notebook-wide only — repos where no code-grounding was available. */
  codeUnavailable?: string[];
}

