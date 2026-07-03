"""`/api/notes`, `/api/tabs` — AI-notebook backend (Phase 1a-2c).

Endpoints:
  POST   /notes                 — create a draft note; kicks off the (stub) pipeline
  GET    /notes?tab_id=         — list notes, optionally filtered by tab
  GET    /notes/search?q=       — Phase 2b: semantic search over embedded notes
  POST   /notes/ask             — Phase 2c capstone: ask -> retrieve -> heal -> answer
  GET    /notes/{id}            — one note
  PATCH  /notes/{id}            — edit title/tab/raw_text/status/links/repo/pipeline-patch
  DELETE /notes/{id}            — delete a note
  POST   /notes/{id}/promote    — curated promotion (training.promoted=True)
  POST   /notes/{id}/resolution — v2: attach a resolution (roster-loop write-back);
                                   schedules a fire-and-forget mnemosyne distill
  POST   /notes/{id}/revalidate — Phase 2a north-star: re-ground vs current code
  GET    /tabs                  — list tabs (note_ids derived from live notes)
  POST   /tabs                  — create a tab
  PATCH  /tabs/{id}             — rename a tab

`trigger_pipeline` schedules the Phase 1c headless `claude -p` transform as
a background task — POST /notes returns immediately with a draft note; the
note flips to processed/failed once notebook_pipeline.trigger_pipeline
completes. `revalidate_note` is awaited directly (not backgrounded) — it's a
manual on-demand user action ("re-check vs code" button), not a write path
that needs to return instantly.

Note-content embedding (notebook_retrieval) is fire-and-forget on create/
delete (never blocks the response) and re-runs on structuring completion /
heal from inside notebook_pipeline itself.
"""

from __future__ import annotations

from pydantic import BaseModel

from khimaira.monitor import notebook_pipeline, notebook_retrieval, notebook_training, notes

from .._optional import require


class CreateNoteReq(BaseModel):
    raw_text: str
    tab_id: str = ""
    title: str = ""
    repo: str = ""


class UpdateNoteReq(BaseModel):
    title: str | None = None
    tab_id: str | None = None
    raw_text: str | None = None
    status: str | None = None
    links: list[str] | None = None
    repo: str | None = None
    pipeline: dict | None = None


class AddResolutionReq(BaseModel):
    resolution: str
    resolved_by: str = ""


class AskReq(BaseModel):
    question: str
    repo: str | None = None


class CreateTabReq(BaseModel):
    title: str = ""


class UpdateTabReq(BaseModel):
    title: str | None = None


def trigger_pipeline(note_id: str) -> None:
    """Fire the headless `claude -p` transform for note_id in the background."""
    notebook_pipeline.schedule_pipeline(note_id)


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.post("/notes")
    async def create_note(req: CreateNoteReq) -> dict:
        record = notes.add_note(req.raw_text, tab_id=req.tab_id, title=req.title, repo=req.repo)
        trigger_pipeline(record["id"])
        notebook_retrieval.schedule_upsert(record)
        return record

    @router.get("/notes")
    async def list_notes_endpoint(tab_id: str | None = None) -> dict:
        return {"notes": notes.list_notes(tab_id=tab_id)}

    @router.get("/notes/search")
    async def search_notes(
        q: str, top_k: int = notebook_retrieval.DEFAULT_TOP_K, repo: str | None = None
    ) -> dict:
        """Phase 2b: semantic search over embedded notes. [] on no hits / qdrant
        down / RAG disabled — never errors. Registered BEFORE /notes/{note_id}
        so "search" doesn't get swallowed as a note_id path param.

        `repo`: optional scope to one repo's notes (mirrors /notes/ask's repo
        filter) — notebook_retrieval.search_notes_async already supports this;
        v1 just never exposed it as a query param here."""
        hits = await notebook_retrieval.search_notes_async(q, top_k=top_k, repo=repo)
        return {"hits": hits}

    @router.post("/notes/ask")
    async def ask(req: AskReq) -> dict:
        """Phase 2c capstone: retrieve candidate notes, staleness-gated
        revalidate (heals stale ones) each hit, synthesize an answer from
        the now-code-current bodies. Awaited directly — an on-demand ask,
        not a write path."""
        return await notebook_pipeline.answer_question(req.question, repo=req.repo)

    @router.get("/notes/{note_id}")
    async def get_note(note_id: str) -> dict:
        try:
            return notes.get_note(note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))

    @router.patch("/notes/{note_id}")
    async def update_note(note_id: str, req: UpdateNoteReq) -> dict:
        fields = req.model_dump(exclude_unset=True)
        try:
            return notes.update_note(note_id, **fields)
        except ValueError as e:
            msg = str(e)
            status = 404 if "No note with id" in msg else 422
            raise fastapi.HTTPException(status, msg)

    @router.delete("/notes/{note_id}")
    async def delete_note(note_id: str) -> dict:
        try:
            result = notes.delete_note(note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))
        notebook_retrieval.schedule_delete(note_id)
        return result

    @router.post("/notes/{note_id}/promote")
    async def promote_note(note_id: str) -> dict:
        try:
            return notes.promote_note(note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))

    @router.post("/notes/{note_id}/resolution")
    async def add_resolution(note_id: str, req: AddResolutionReq) -> dict:
        """v2 roster-loop write-back: attach a resolution to a note.

        Schedules a fire-and-forget mnemosyne distill of the {problem,
        resolution} pair when a non-empty resolution lands — never blocks
        the response, and mnemosyne being unreachable never fails this
        request (see notebook_training.promote_resolved's fail-open contract).
        """
        try:
            updated = notes.add_resolution(note_id, req.resolution, resolved_by=req.resolved_by)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))
        if updated.get("resolution"):
            notebook_training.schedule_promote(updated)
        return updated

    @router.post("/notes/{note_id}/revalidate")
    async def revalidate_note(note_id: str) -> dict:
        try:
            return await notebook_pipeline.revalidate_note(note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))

    @router.get("/tabs")
    async def list_tabs() -> dict:
        return {"tabs": notes.list_tabs()}

    @router.post("/tabs")
    async def create_tab(req: CreateTabReq) -> dict:
        return notes.add_tab(title=req.title)

    @router.patch("/tabs/{tab_id}")
    async def update_tab(tab_id: str, req: UpdateTabReq) -> dict:
        fields = req.model_dump(exclude_unset=True)
        try:
            return notes.update_tab(tab_id, **fields)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))

    return router
