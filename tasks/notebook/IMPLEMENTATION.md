# AI-Powered Notebook — Implementation Spec

> Status: DRAFT (2026-07-03). Owner: khimaira-1 (master). Backend slice delegated to khimaira-void-null.

## Vision (what & why)

Joseph copy/pastes long Claude Code responses into a scratch text editor to review them,
but it doesn't save and it's just raw text. Build a persistent, AI-structured notebook
**inside khimaira (monitor-ui)**.

**The key reframe:** this is NOT a standalone notebook — it's the missing **capture →
structure → curate → learn** front-end for the **mnemosyne knowledge loop**. The back
half already exists (distiller → training pairs → oracle re-bake, + RAG hippocampus for
real-time retrieval). We're building the human-in-the-loop front: paste → auto-structure
→ review → promote. So "gets smarter as I add notes" is largely *free* — we feed a loop
that already exists.

Every stage maps to an existing pattern:

| Ask | What it is | Reuses |
|---|---|---|
| paste → organize/summary/technical/plain | Perception: unstructured → strict schema | headless `claude -p` (see Pipeline) |
| saves, multiple tabs | Deterministic storage | JSONL store (mirror `chats.py`) |
| "two tabs belong together → ask to consolidate" | Similarity → propose, don't dispose | qdrant :6343 + bge-small (already running) |
| "gets smarter on the codebase" | real-time RAG + periodic re-bake | mnemosyne hippocampus + distiller |
| "use as training data" | curated notes → training pairs | mnemosyne distiller |

## Decisions (locked by Joseph 2026-07-03)

1. **Pipeline LLM = a background Claude Code session**, NOT the Anthropic API. Spin up a
   headless `claude -p` (print mode) process that transforms the paste and returns JSON.
   Uses Joseph's CC auth (no API key/cost), full Claude quality. This is the *perception*
   step; the local oracle *learns* from its output (no degenerate loop).
2. **Curated promotion** — a note becomes training data only when Joseph explicitly marks
   it "good for training." Notes are RAG-retrievable immediately regardless; only *promoted*
   ones feed the distiller/bake. Propose, don't dispose.
3. **Full vision** — build all phases (capture pipeline + consolidation + training wiring),
   not MVP-only. Backend-first, then frontend, then consolidation, then training.

## Architecture (data flow)

```
paste (raw) ──▶ POST /api/notes ──▶ notes store (JSONL)
                                         │
                        (async) spawn headless `claude -p` transform
                                         │
                                         ▼
             validated {summary, technical, plain, tags[], entities[], organized_md}
                                         │
                         ┌───────────────┼───────────────────────┐
                         ▼               ▼                        ▼
                  store note      embed → qdrant :6343      (on promote) → mnemosyne
                  (structured)    (RAG, immediate)          distiller/store → next bake
                                         │
                         on new note: similarity-search existing notes
                                  → if > threshold, PROPOSE consolidate → Joseph disposes
```

The store is the source of truth; the LLM only *transforms* text into the schema. Every
load-bearing decision (what to store, when to consolidate, when to train) is deterministic
code or a human gate.

## Data model (the note record)

One JSONL record per note (mirror the sessions/chats record conventions):

```jsonc
{
  "id": "<uuid4 hex[:12]>",
  "created_at": "<iso8601>",
  "updated_at": "<iso8601>",
  "title": "<short, derived from summary or first line>",
  "tab_id": "<tab/group id>",          // multiple tabs; a tab groups related notes
  "raw_text": "<the pasted original — never lost>",
  "status": "draft | processed | promoted",
  "pipeline": {                         // filled by the headless-CC transform; null until processed
    "summary": "<1-3 sentence TL;DR>",
    "technical": "<technical section, markdown>",
    "plain": "<plain-language version>",
    "organized_md": "<the reorganized full note, markdown>",
    "tags": ["..."],
    "entities": ["<code symbols / files / concepts referenced>"]
  },
  "embedding_id": "<qdrant point id | null>",   // Phase 2
  "training": {                                 // Phase 3
    "promoted": false,
    "promoted_at": null,
    "domain": "khimaira:notes",
    "distilled_pairs": 0
  },
  "links": ["<related note id>"]                // consolidation proposals accepted
}
```

Tabs are their own lightweight records (`{id, title, created_at, note_ids[]}`) OR derived
by grouping notes on `tab_id` — implementer's call; grouping-by-field is simpler to start.

---

## BUILD PLAN

### Phase 1a — Backend store + API  ◀── DELEGATED TO khimaira-void-null

Mirror the existing `chats.py` / `sessions.py` conventions exactly.

**Create `packages/khimaira/src/khimaira/monitor/notes.py`** — the JSONL store. Follow
`chats.py` as the template (private path helpers → `_append`/`_read` → public verbs):
- `_NOTES_DIR` derived from `XDG_STATE_HOME` at module load (like `sessions.py:49`), e.g.
  `$XDG_STATE_HOME/khimaira/notes/`. One file per note or a per-tab JSONL — prefer
  `notes/<id>.json` for the note body + a `notes/index.jsonl` for listing (cheap list).
- Atomic writes via the `tmp = path.with_suffix(".tmp"); tmp.write_text(...); tmp.replace(path)`
  pattern (`chats.py:345-348`). `_now_iso()` = `datetime.now(UTC).isoformat()`.
- Public functions: `add_note(raw_text, tab_id, title="") -> dict`, `get_note(id) -> dict`,
  `list_notes(tab_id=None) -> list[dict]`, `update_note(id, **fields) -> dict`,
  `delete_note(id) -> dict`, `set_pipeline(id, pipeline: dict) -> dict`,
  `list_tabs() -> list[dict]`, plus tab CRUD if tabs are their own records.
- Every mutating primitive gets **round-trip test coverage** (write → read back → assert),
  per the repo rule "Every primitive that mutates JSONL needs round-trip coverage."

**Create `packages/khimaira/src/khimaira/monitor/api/notebook.py`** — the router. Template:
`api/projects.py` (daemon-local data, `build_router()` no-arg) or `api/oracle.py:380-497`
(smallest self-contained example). Routes (all under `/api` prefix):
- `POST /notes` `{raw_text, tab_id, title?}` → creates a draft note, returns it, and
  **kicks off the async pipeline** (Phase 1c). For now, stub the pipeline call behind a
  function `trigger_pipeline(note_id)` that Phase 1c fills in.
- `GET /notes?tab_id=` → list.
- `GET /notes/{id}` → one note.
- `PATCH /notes/{id}` → edit (title, tab, manual edits to pipeline fields).
- `DELETE /notes/{id}`.
- `GET /tabs` / `POST /tabs` / `PATCH /tabs/{id}` → tab CRUD.
- `POST /notes/{id}/promote` → sets `training.promoted=true` (Phase 3 wires the distiller).
- Wrap `notes.*` `ValueError` → `fastapi.HTTPException(404, str(e))` (repo rule: session/
  resource-resolving endpoints need unknown-id → 404 coverage, with a test each).

**Register in `server.py`** — 2 lines: `from .api import notebook as notebook_api` (~line 112)
+ `app.include_router(notebook_api.build_router(), prefix="/api")` (~line 134).

**Tests** (`packages/khimaira/tests/test_notes_unit.py` + `test_notebook_api.py`): round-trip
for every store primitive; happy-path + unknown-id-404 for every resolving endpoint; the
"unhappy path" set (empty store, corrupt line, missing note).

### Phase 1b — Frontend Notebook view  (next; can start once the API shape is fixed)

Add a `Notebook` view to `apps/monitor-ui` (research map available). Files:
- `src/components/notebook/Notebook.tsx` (template: `kg/KgMapper.tsx` for a complex view, or
  `observer/CostDashboard.tsx` for a data view). Tabs across the top (the "multiple tabs"),
  a paste box, and a note render showing summary/technical/plain sections.
- `src/components/notebook/notebookTypes.ts` (wire types; template `kg/kgTypes.ts`).
- `src/App.tsx`: add `<Route path=":name/notebook" element={<Notebook/>} />`.
- `src/components/project/ProjectNavTabs.tsx`: add a Notebook tab item.
- `src/api.ts`: add RTK Query endpoints (`getNotes`, `getNote`, `createNote`, `promoteNote`,
  …) + export hooks. (RTK Query is the cleaner path; KG's hand-rolled fetch is the fallback.)
- Styling: Tailwind + shadcn primitives in `src/components/ui/`. No build/proxy changes
  needed — `/api/notebook/*` flows through the existing `/api` proxy; `dist/` self-rebuilds.

### Phase 1c — The pipeline (headless `claude -p`)  ◀── MASTER owns the design

`trigger_pipeline(note_id)` spawns a **headless Claude Code session** to transform the note:
- `claude -p "<transform prompt>" --output-format json` (print mode returns structured JSON;
  uses Joseph's CC auth, no API key). Feed the raw_text; constrain output to the `pipeline`
  schema (summary/technical/plain/organized_md/tags/entities). Run async (don't block the
  POST); on completion validate against the schema (tollgate — retry on mismatch) and call
  `notes.set_pipeline(id, ...)`; set `status="processed"`.
- Spawn via khimaira's process infra (`spawn_process` / the observer) or a plain
  `asyncio.create_subprocess_exec`. Master to finalize the exact spawn mechanism + prompt.

### Phase 2 — Consolidation ("these two tabs belong together?")

- On note create/process, embed its text (bge-small via the mnemosyne retrieval path, or a
  direct fastembed call) → upsert to qdrant (collection e.g. `khimaira_notes`, :6343).
- Similarity-search existing notes; if top hit > threshold (~0.75, tune), surface a
  **proposal** in the UI: "This looks related to <note/tab X> — consolidate?" Joseph accepts
  → merge (append/reorganize via another `claude -p` pass) or reject. Deterministic detection,
  human disposition.

### Phase 3 — Training wiring (curated promotion)

- `POST /notes/{id}/promote` → send the structured note to mnemosyne:
  - **Immediate RAG:** `store.append(domain="khimaira:notes", instruction, response)` (or POST
    `/distill` at :8766 for Haiku pair-extraction) → live qdrant upsert → retrievable now.
  - **Periodic bake:** add the `khimaira:notes` prefix to `refresh_oracle.sh`'s
    `export_sft_pairs.py --prefixes` list (NOTE: a prefix is silently excluded unless named)
    so promoted notes join the next oracle re-bake.
- Only *promoted* notes flow here — drafts/wrong answers never pollute the oracle.

## Integration reference (from research, cite when building)

- **monitor-ui**: RTK Query single API slice (`src/api.ts`, `baseUrl:"/api"`); routes in
  `src/App.tsx`; view tabs in `src/components/project/ProjectNavTabs.tsx`; daemon serves
  built `dist/` (`server.py:536-547`); dev proxy `vite.config.ts:16-28` → `:8740`.
- **daemon routes**: `api/*.py` each `build_router()`; register in `server.py:96-134`.
- **storage**: JSONL under `$XDG_STATE_HOME/khimaira/`; `_append_jsonl`/`_read_jsonl`/atomic
  rename in `sessions.py` + `chats.py`.
- **mnemosyne** (separate repo `~/dev/ai-lab/mnemosyne/`): distiller `POST /distill` (:8766,
  client `hooks/mnemosyne_client.py`); `store.append` → live RAG upsert (`store.py:34`);
  qdrant :6343 collection `mnemosyne_<project>`; bake `scripts/refresh_oracle.sh` (prefixes at
  `export_sft_pairs.py`). A note = plain text + `<project>:<domain>` — distiller structures it.

## Guardrails

- Perception at the edge only: `claude -p` structures the paste; every other decision is
  deterministic code or a human gate. Validate the pipeline output against the schema (fail
  loud + retry) before it enters the store.
- Never auto-promote to training. Curation is a human gate.
- Follow repo test rules: round-trip for JSONL mutators, unknown-id-404 for resolving routes,
  unhappy-path set before merge. Format (ruff) before commit.
