# Grimoire: house the jeevy study guides + rubric self-healing + research-scientist ask

## Context

The jeevy roster produces **study guides** — long-form, code-grounded, Joseph-voiced
markdown (topic explainers + task `IMPLEMENTATION.md`s). Today they dump into
`~/work/jeevy_portal/shared-docs/` (`joseph/notes/`, `sources/**`), ~130–140 files, flat,
organized only by naming convention → **the pain is flatness: no card, no navigation, no
search, no maintenance.** They rot and are hard to find.

Joseph's four decisions frame the build:
1. **The notebook is canonical** — the roster authors guides *into* the grimoire; existing
   files are imported.
2. **A study guide is a distinct KIND of object** — a finished deliverable to be *housed +
   rendered*, not re-expressed into today's summary/technical/plain triple.
3. **Study mode gives the ask bar a research-scientist role** — an agentic, Claude-Code-like
   turn that researches the codebase *and the web*, clarifies vague sections, fills gaps, and
   applies findings back into the guide.
4. **Continuous, rubric-driven self-organization** — the notebook re-files, links,
   quality-checks, and re-grounds on every create/edit and on a periodic sweep, so it never
   drifts back into a mess.

**Outcome:** the grimoire becomes a self-maintaining, searchable, researchable knowledge
base that is the single home for study guides.

## The load-bearing invariant

**`raw_text` (the guide body) is the human deliverable and is NEVER LLM-rewritten** — except
by an *explicit, human-approved* research REVISE. Every derived artifact (abstract, TOC,
tags, collection assignment, cross-links, rubric scores, currency drift) is stored
*alongside* `raw_text`, never in place of it. This makes write-safety structural, not
prompt-enforced, and it's the one place the current note model does NOT transfer (today's
`revalidate_note` regenerates the whole pipeline; for a guide it must produce a *drift
report*, not an overwrite).

## Architecture — reuse the existing spine, branch on `kind`

The notebook already has: a JSONL store with atomic writes + append-only-fold index
(`notes.py`), an instruction/schema-parameterized structuring engine (`transform_note`), a
git-diff staleness gate (`revalidate_note`'s `_current_git_sha`/`_resolve_anchor_files`/
`_anchor_files_changed`), fastembed+qdrant retrieval with cosine (`notebook_retrieval.py`),
the ask loop (`answer_question`), tabs-as-folders, and the reader (`Notebook.tsx`,
`MarkdownView`). **Study guides reuse all of it**; the work is branches + three genuinely-new
surfaces (importer, rubric, research invocation) + Library/guide-reader UI.

## Data-model diff (`notes.py` record)

```
  id, created_at, updated_at, title, tab_id, raw_text, status,
+ kind: "note" | "study_guide"              # default "note"
  pipeline: null | <discriminated>          # note: {summary,technical,plain,organized_md,tags,entities}
                                            # guide: {abstract, toc:[{title,anchor,level}], tags, entities}
+ source_path: str | None                   # import provenance + dedup key + export round-trip
  embedding_id, training, links, repo, history,
  last_validated_at, validated_git_sha, structured_at,
  resolution, resolved_by, resolved_at,
+ organized_at: str | None                  # when the organizer last placed/checked this file
```
- `tags`/`entities` stay **shared keys** so retrieval + the entity-anchored staleness gate
  work unchanged. `toc` is a **deterministic heading parse**, not LLM. Guide `abstract` = the
  library-card blurb (LLM).
- Tab record `+ kind: "folder" | "collection"` so note-folders and guide-collections don't
  intermix in the filter bar.
- Collection membership is just `tab_id` (a collection is a tab). No separate rubric/proposals
  store.

## The organizer (pillar 4) — SIMPLE, organization-only

**Not a rubric.** The only job of the self-healing is to keep the library ORGANIZED: the LLM
sees all the files + collections and, where a file is uncollected, misfiled, or mislabeled,
it re-files / re-labels it **based on the file's own content**. No quality scoring, no
duplication/structure/currency axes, no proposals queue.

`notebook_organizer.py`:
1. **Deterministic first** (cheap, on create/import): `derive_collection(path/repo/naming)` →
   an initial `tab_id`. Handles the obvious cases at ~zero cost.
2. **LLM organize pass** (`organize_library()`): give the model the full list of files
   (title + abstract/tags + current collection) and the set of collections; it returns, per
   file, the collection it *should* be in (existing or a proposed new one) + a better title/
   collection label where the current one is wrong. Re-file = just set `tab_id`; it's
   reversible, so **auto-apply** (this is what Joseph asked for — "just fix it", not ask).
   Only genuinely large/lossy moves (e.g. deleting a collection) would surface for confirmation.

Hooks: on create/edit → deterministic assign + (if ambiguous) queue for the next organize
pass; a throttled daemon sweep (`_organize_sweep`, spawned via `server.py`'s `_spawn`,
cadence `KHIMAIRA_NOTEBOOK_ORGANIZE_SWEEP_S`) runs `organize_library()` periodically so drift
self-corrects. Cap the LLM call (one batched pass over the library, not per-file fan-out) so
the sweep can't become a money printer.

Note: the existing code-currency self-heal (`revalidate_note`) is SEPARATE and unchanged —
it is not part of this organizer. For study guides its currency check becomes a *drift report*
(never rewrites `raw_text`), but that's an independent concern from organization.

## The research-scientist ask (pillar 3)

A new **agentic** invocation `_invoke_claude_agentic` (parallel to the tool-less
`_invoke_claude`), reusing `_isolated_config_dir` + retry scaffolding but granting
**read-only** tools:
```
claude -p --tools Read,Grep,Glob,WebSearch,WebFetch   # NO Edit/Write/Bash
  --add-dir <repo_root>  --strict-mcp-config --mcp-config '{"mcpServers":{}}'
  --model claude-sonnet-5  --max-budget-usd <cap>  --json-schema <ResearchOutput>
```
Chosen over dispatching to the roster (which would couple a UI action to the most bug-dense
subsystem in the repo). The guide is a *note in the store*, so "edit the guide" =
`update_note(raw_text=)` — the child **proposes** a patch; the backend applies it after a
human gate. Intent = **Disposition** (ANSWER vs REVISE) × **Scope** (whole-guide vs section),
set explicitly by the UI (no intent classifier). ANSWER reuses today's ask center-panel
(extended with `web_sources`); REVISE returns a diff + citations → Apply/Discard →
`update_note(raw_text=)`.

## Phases

**Phase 0 — de-risk WebSearch (do first, ½ day).** Verify headless `claude -p` can invoke
`WebSearch`/`WebFetch` under an isolated config dir with copied creds. It's often
subscription/org-gated; this is the tent-pole of the web-research half. If it fails, the web
axis degrades to WebFetch-only or a mnemosyne/Séance fallback — decide before building UI.

**Phase 1 — Housing + deterministic organization (fixes the mess).**
- `notes.py`: `add_study_guide`, `set_study_guide_pipeline`, `_index_stub` (+kind/source_path/
  open_proposals), `derive_lifecycle` kind branch, `add_tab`/`update_tab` (+kind),
  `get_or_create_collection`.
- `notebook_pipeline.py`: `_STUDY_GUIDE_INSTRUCTION` + `StudyGuideOutput` schema, deterministic
  `_parse_toc`, `trigger_study_guide_pipeline` (reuses `transform_note`), `schedule_pipeline`
  kind branch.
- `notebook_retrieval.py`: `_passage_text` kind branch (embed `abstract + raw_text[:N]`).
- **New** `notebook_import.py`: `qualify`, `derive_collection` (the deterministic
  collection_fit rule — one code path shared with the rubric), `import_dir(root, dry_run=True)`
  idempotent via `source_path`. **Dry-run manifest for Joseph first** (path→collection→title).
- **New** `notebook_organizer.py`: `derive_collection` (deterministic path/repo/naming →
  collection, shared with the importer) + `get_or_create_collection`, hooked on create/import.
  (The LLM `organize_library()` pass lands in Phase 2.)
- `api/notebook.py`: `CreateNoteReq` (+kind/+source_path), `create_note` kind branch,
  `list_notes` kind filter, `POST /notes/import`.
- Frontend: `notebookTypes.ts` `pipeline` union; new **LibraryView** (grouped by collection);
  guide **Reader** (clickable TOC + full `raw_text` via `MarkdownView`); note⇄guide switch;
  guides kind-filtered out of the note list (like `personal` is today).
- **Ships: guides IN, organized, searchable, rendered.**

**Phase 2 — LLM organizer + roster authoring.**
- `notebook_organizer.py` `organize_library()`: ONE batched `_invoke_claude` pass over the
  library (title + abstract + tags + current collection per file) → per-file correct
  collection + a better label where wrong; auto-apply re-files (set `tab_id`, reversible); can
  propose a new collection when a cluster emerges.
- `_organize_sweep` daemon loop (throttled; one batched LLM call per sweep, capped).
- MCP `notebook_create_study_guide(project, raw_text, title, collection)` — roster authors in.
- **Guide currency self-heal = drift report** (SEPARATE from organization; new
  `revalidate_note` kind branch: re-check anchors, regen only `abstract`, never touch
  `raw_text`). Raise anchor caps for guides.

**Phase 3 — Research-scientist toolbar.**
- `_invoke_claude_agentic` + `research_answer` (ANSWER) + `POST /notes/research`; frontend
  "deep research" toggle; `AskAnswer` +`web_sources`; `--max-budget-usd` + visible cost footer.
- `research_revise` (REVISE) + `ResearchOutput` schema with `proposed_patch`; deterministic
  section-split apply helper in `notes.py`; `POST /notes/{id}/research-revise` (returns a
  proposal, never applies); frontend diff view + Apply/Discard; per-section "clarify" + whole-
  guide "find gaps" affordances.
- **Organize-on-edit seam:** after a research REVISE applies + reprocesses, re-run the
  organizer for that note (its content changed, so its collection might too). Reuse an
  `on_structured` callback on `trigger_pipeline` so it runs *after* structuring. **Re-entrancy
  guard required** — an organize re-file that writes the note must not re-fire the pipeline →
  organizer (a `suppress_organize` write path or per-note latch; the roster reaper-cascade class).

**Phase 4 — Polish + reversibility.**
- `POST /notes/{id}/export` round-trip to `source_path` (the reversibility valve for "notebook
  canonical" — keeps devs' `@`-ref habit alive; recommended).
- Snapshot prior `raw_text` into `history` on research-apply (REVISE is currently lossy).
- stream-json progress in the research UI; opus deep-mode; nested collections; consolidation-
  merge UX; import watcher / `khimaira notebook import-guides` cron.

## Key risks

1. **WebSearch headless availability** — UNKNOWN, Phase 0 de-risks (inspection-grade only).
2. **Organize-sweep cost** — keep it to ONE batched LLM call per sweep over the whole library
   (never per-file fan-out); throttle the cadence; deterministic collection assignment handles
   the common cases for free.
3. **Guide currency must not rewrite `raw_text`** — drift report + abstract-only regen; the
   one place the note model doesn't transfer.
4. **REVISE is lossy** (`raw_text` overwrite is unversioned today) — Phase 4 snapshots to
   `history`; until then the approval-diff is the only guard. Flag before shipping Phase 3.
5. **Organize loop** — an organize re-file that writes the note must not re-fire the pipeline →
   organizer. A `suppress_organize` write path (or per-note latch) is required.
6. **`repo="jeevy_portal"` resolvability** — verify `_repo_root` resolves the guide repo tag to
   a real checkout (MEMORY: jeevy KG is attached as label `backend`) or currency fails-open
   silently on every guide.

## Verification

- **Phase 0:** run the throwaway `claude -p --tools WebSearch,WebFetch --max-budget-usd 0.5`
  under an isolated `CLAUDE_CONFIG_DIR`; confirm it returns cited URLs.
- **Phase 1:** `import_dir(dry_run=True)` manifest reviewed → real import → `GET /api/notes?
  kind=study_guide` returns the corpus with collections + TOC; Library UI groups them;
  `notebook_search`/`notebook_ask` retrieve a guide; Specter DOM-check the guide reader renders
  markdown + TOC (screenshots time out on this page — verify via `evaluate_js`).
- **Phase 2:** create a mislabeled/uncollected guide → the organizer re-files it into the right
  collection (auto); `_organize_sweep` runs ONE batched LLM call and corrects drift across the
  library; confirm no per-file fan-out.
- **Phase 3:** ANSWER a code question → cited answer with `file:line` + web sources; REVISE a
  vague section → diff proposal → Apply → guide `raw_text` updated + reprocess badge + heal
  fires once (no loop). Check `/api/version` `stale=false` before trusting any live-daemon test.
- Every backend change adds tests mirroring `test_notebook_api.py` / `test_notebook_mcp_tools.py`
  (happy + unhappy + guard-rail); run `ruff format`/`ruff check`; restart the daemon for
  `monitor/**` changes; `npm run build` for frontend.

## Reuse ledger (NOT rebuilt)

`transform_note` (→ guide pipeline is an instruction+schema branch) · `_invoke_claude` (abstract
+ LLM axes) · `revalidate_note` + git-diff staleness gate (→ currency axis) · qdrant embed/
search/cosine (available if ever needed) · `answer_question`/ask loop/`@`-mentions (guides are
just more retrievable notes) · tabs (→ collections + one field) · `_index_stub`/`_fold_*`/
atomic-write pattern · `_spawn` background-loop (→ organize sweep) · `MarkdownView` (guide
render) · `dispatch/runners/claude.py` (tool-enabled spawn + stream-json parsing reference) ·
client/daemon split + `_get/_post/_patch/_delete` (MCP additions).
