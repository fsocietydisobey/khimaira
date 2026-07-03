/**
 * Wire shapes for the AI-notebook — mirrors the note/tab record schema in
 * packages/khimaira/src/khimaira/monitor/notes.py (Phase 1a backend).
 *
 * Served by the khimaira daemon at /api/notes + /api/tabs (same-origin via
 * the vite /api proxy → daemon 8740, no project scoping — notes are global
 * daemon state, not per-project).
 */

export type NoteStatus = "draft" | "processed" | "promoted" | "failed";

export interface NotePipeline {
  summary: string;
  technical: string;
  plain: string;
  organized_md: string;
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
  /** Null until the Phase 1c transform completes. */
  pipeline: NotePipeline | null;
  embedding_id: string | null;
  training: NoteTraining;
  links: string[];
  /** North-star (Phase 2a): which codebase this note is validated against. */
  repo: string;
  /** Null if never revalidated. */
  last_validated_at: string | null;
  /** Git SHA the note was last checked against; null if never revalidated. */
  validated_git_sha: string | null;
  /** Count of prior pipeline versions superseded by a heal (list view carries
   *  only the count — full version bodies are on the note's `history` array,
   *  fetched via GET /notes/{id} if ever needed). */
  history_count: number;
}

export interface NotebookTab {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  /** Note ids grouped by tab_id — derived at read time, not stored redundantly. */
  note_ids: string[];
}

/** Phase 2c capstone — POST /notes/ask response. */
export interface AskAnswer {
  answer: string;
  /** Every note the answer drew on, post-revalidation. */
  sources: string[];
  /** Subset of `sources` that actually changed (healed) during this ask. */
  healed: string[];
}
