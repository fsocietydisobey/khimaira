"""`/api/notes`, `/api/tabs` — AI-notebook backend (Phase 1a).

Endpoints:
  POST   /notes              — create a draft note; kicks off the (stub) pipeline
  GET    /notes?tab_id=      — list notes, optionally filtered by tab
  GET    /notes/{id}         — one note
  PATCH  /notes/{id}         — edit title/tab/raw_text/status/links/pipeline-patch
  DELETE /notes/{id}         — delete a note
  POST   /notes/{id}/promote — curated promotion (training.promoted=True)
  GET    /tabs               — list tabs (note_ids derived from live notes)
  POST   /tabs               — create a tab
  PATCH  /tabs/{id}          — rename a tab

`trigger_pipeline` is a Phase 1c stub — the headless `claude -p` transform
spawn lands there; for now it's a no-op so POST /notes returns immediately
with a draft note.
"""

from __future__ import annotations

from pydantic import BaseModel

from khimaira.monitor import notes

from .._optional import require


class CreateNoteReq(BaseModel):
    raw_text: str
    tab_id: str = ""
    title: str = ""


class UpdateNoteReq(BaseModel):
    title: str | None = None
    tab_id: str | None = None
    raw_text: str | None = None
    status: str | None = None
    links: list[str] | None = None
    pipeline: dict | None = None


class CreateTabReq(BaseModel):
    title: str = ""


class UpdateTabReq(BaseModel):
    title: str | None = None


def trigger_pipeline(note_id: str) -> None:
    """Phase 1c stub — spawns a headless `claude -p` transform for note_id.

    No-op for now; Phase 1c fills this in to call notes.set_pipeline(note_id, ...)
    asynchronously once the transform completes.
    """
    return None


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.post("/notes")
    async def create_note(req: CreateNoteReq) -> dict:
        record = notes.add_note(req.raw_text, tab_id=req.tab_id, title=req.title)
        trigger_pipeline(record["id"])
        return record

    @router.get("/notes")
    async def list_notes_endpoint(tab_id: str | None = None) -> dict:
        return {"notes": notes.list_notes(tab_id=tab_id)}

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
            return notes.delete_note(note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e))

    @router.post("/notes/{note_id}/promote")
    async def promote_note(note_id: str) -> dict:
        try:
            return notes.promote_note(note_id)
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
