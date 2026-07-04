/**
 * Wire shapes for the AI-notebook — mirrors the note/tab record schema in
 * packages/khimaira/src/khimaira/monitor/notes.py (Phase 1a backend).
 *
 * Served by the khimaira daemon at /api/notes + /api/tabs (same-origin via
 * the vite /api proxy → daemon 8740, no project scoping — notes are global
 * daemon state, not per-project).
 */

export type NoteStatus = "draft" | "processed" | "promoted" | "failed";

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

/** Phase 2c capstone — POST /notes/ask response. */
export interface AskAnswer {
  answer: string;
  /** Every note the answer drew on, post-revalidation. */
  sources: string[];
  /** Subset of `sources` that actually changed (healed) during this ask. */
  healed: string[];
  /** Ask-layer v2: code chunks (from each source note's repo) fed into the synthesis. */
  code_sources: CodeSource[];
  /** Repos where neither Séance nor the grep fallback could ground anything —
   *  a degrade, not a failure; surfaced so the answer doesn't look like it
   *  silently skipped checking the code. */
  code_unavailable: string[];
}
