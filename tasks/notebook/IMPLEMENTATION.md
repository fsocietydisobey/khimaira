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

## ⭐ NORTH STAR (locked by Joseph 2026-07-03) — self-healing, code-grounded KB

The notebook is NOT a static structured scratchpad — it's a **self-healing knowledge base
where the CODE is the source of truth and notes are re-validated caches of it.**

1. **Ingest everything** — NO curation gate. Every paste is structured + stored.
2. **Validate-on-query** — when Joseph asks a question, re-ground the relevant note(s)
   against the ACTUAL code: read the anchor files the note's `entities` reference (via Séance
   + Read), compare note ⇄ code, answer from the code-current version.
3. **Auto-heal staleness** — if the note diverges from the code, UPDATE it to match (a new
   VERSION; raw_text is immutable; prior versions kept → reversible). "It just updates it."
4. **Validation = the quality gate** (replaces manual curation). Training promotion is now
   AUTOMATIC on code-validation — you train only on code-grounded truth, so ingest-everything
   is safe (the code-check catches wrong/stale notes, no human gate needed).

**Refinement (accepted): staleness-GATE the re-grounding — don't re-read the codebase on
every query.** Each note stores `last_validated_at` + the git SHA it was validated against.
On query, a cheap `git diff`-since check on the note's anchor files decides: unchanged → answer
from cache instantly; changed (or never validated) → pay the full code-read + heal. Preserves
"always aligned with the code" without the per-query cost.

## Decisions (locked by Joseph 2026-07-03)

1. **Pipeline LLM = a background Claude Code session**, NOT the Anthropic API. Headless
   `claude -p` transforms the paste → JSON. Joseph's CC auth, full Claude quality. Perception
   step; the local oracle *learns* from its output (no degenerate loop). The SAME pattern
   drives the north-star revalidation pass (code + note → aligned? → healed note).
2. ~~Curated promotion~~ → **SUPERSEDED by the north star.** Ingest everything; validation-
   against-code is the gate; training promotion is automatic on validation.
3. **Repo/project tag on every note** — validation must know WHICH codebase is truth
   (khimaira vs jeevy). Also resolves the "notes are global, not project-scoped" flag.
4. **Full vision** — capture (done) → validate-on-query (the north-star core) → training.

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

### Phase 1c — The pipeline (headless `claude -p`)  ◀── VALIDATED RECIPE (master de-risked; void-null implements after 1b)

`trigger_pipeline(note_id)` spawns a **headless Claude Code session** to transform the note.
Master validated the exact mechanism live (2026-07-03) — build to THIS recipe, don't re-derive.

**The invocation** (content via STDIN so any text/quotes/size is safe; instruction via system prompt):
```
printf '%s' "<raw_text>" | env CLAUDE_CONFIG_DIR="<ISOLATED_CFG>" \
  claude -p --append-system-prompt "<INSTRUCTION>" \
    --output-format json --strict-mcp-config --mcp-config '{"mcpServers":{}}' \
    --model claude-sonnet-5
```
Run from the daemon via `asyncio.create_subprocess_exec` (async — do NOT block the POST;
note starts `status="draft"`, pipeline fills it → `"processed"`).

**ISOLATED_CFG (critical — kills stray-session pollution + trims the context load):** a dedicated
config dir containing ONLY `~/.claude/.credentials.json` (copied, for auth) + `settings.json` = `{}`
(empty → NO hooks) + NO CLAUDE.md. Create it once (e.g. under the khimaira state dir) and reuse.
Validated effects: **zero stray session registration** (empty settings = no SessionStart hook = no
khimaira session dir) + drops the CLAUDE.md/rules from the prompt. Do NOT clobber auth — copy the
real `.credentials.json`; an empty apiKeyHelper breaks auth (see the apiKeyHelper memory).

**COST — corrected 2026-07-03 (my earlier "$0.02–0.10/note" was WRONG — that was the HAIKU
number).** Real cost with **claude-sonnet-5 is ~$0.40/call** (notional). The isolation trims the
project context but NOT the dominant driver: a **~42K base Claude-Code-AGENT system prompt billed
on every call** (no cross-process prompt-cache reuse; `--disallowedTools` only shaves it to ~42K →
$0.28, so it's the agent scaffolding itself, inherent to `claude -p`). On Joseph's SUBSCRIPTION
this is QUOTA consumption, not a dollar bill — and that was his explicit choice (Sonnet quality via
his Claude sub, NOT the API). The staleness-gate keeps asks cheap when code hasn't moved (heal only
when needed). If the notebook ever needs to be cheap-at-volume, the API + prompt-caching is ~10×
cheaper per call and off-quota — but that reverses the "use Claude Code" decision, so it's parked.

**Model = `claude-sonnet-5`** (Joseph's call): Sonnet reliably emits pure JSON; Haiku drifts
conversational ("I see you landed a fix… is there a follow-up?") and ignores "output only JSON".
Since notes are training data, reliability > the few cents. ~10–15s/note.

**INSTRUCTION** (via `--append-system-prompt`): "You structure a pasted note (often an AI
coding-assistant response). Output ONLY a JSON object, no prose, no markdown fence, with keys:
summary (1-3 sentence string), technical (markdown string), plain (plain-language string),
organized_md (markdown string), tags (array of strings), entities (array of strings — code
symbols/files/concepts referenced). The entire user message is the raw note to structure."

**Parse + tollgate (deterministic, per ai-engineering):** the CLI returns a JSON envelope; read
`.result` (a string), strip an optional ```json fence, `json.loads`, then validate against a
Pydantic `PipelineOutput{summary,technical,plain,organized_md,tags,entities}`. On parse/validate
failure → RETRY once (Sonnet is usually clean); on second failure → `status="failed"` + keep
`raw_text` (never lose it). On success → `notes.set_pipeline(id, ...)` + `status="processed"`.
Belt-and-suspenders: if a stray session dir ever appears, the envelope's `.session_id` names it
for cleanup — but with the isolated config it shouldn't.

Module: `packages/khimaira/src/khimaira/monitor/notebook_pipeline.py` (`async def transform_note`
+ `async def trigger_pipeline`); fill the `trigger_pipeline` stub in `api/notebook.py` to call it.
Tests: mock the subprocess to return a canned envelope; assert parse→set_pipeline on success,
retry-then-failed on bad JSON, raw_text preserved throughout.

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
