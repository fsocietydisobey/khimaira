"""`/api/notes`, `/api/tabs` — AI-notebook backend (Phase 1a-2c).

Endpoints:
  POST   /notes                 — create a draft note (or, kind="study_guide", a
                                   grimoire guide); kicks off the structuring pipeline
  GET    /notes?tab_id=&repo=&kind=&priority=&starred= — list notes, optionally
                                   filtered by tab/repo/kind/priority/starred
  POST   /notes/import          — grimoire Phase 1c: bulk-import study guides from a
                                   directory (dry_run=True by default — manifest only)
  GET    /notes/search?q=       — Phase 2b: semantic search over embedded notes
  POST   /notes/ask             — Phase 2c capstone: ask -> retrieve -> heal -> answer
  POST   /notes/research        — grimoire Phase 3 ANSWER: schedules a research-grounded
                                   Q&A job (code + web), read-only; returns {job_id}
  GET    /notes/research/{job_id} — poll an ANSWER or REVISE job's status/result
  POST   /notes/chat             — CHAT-UNIFY §2 MVP: notebook-wide chat for the
                                   "nothing open" case; thin wrapper around
                                   answer_question, schedules a job, returns
                                   {job_id} — poll GET /notes/research/{job_id}
                                   (kind:"notebook_chat"). NOT true multi-turn.
  GET    /notes/{id}            — one note
  PATCH  /notes/{id}            — edit title/tab/raw_text/status/links/repo/pipeline-patch/
                                   pinned_placement/starred (FILE-MANAGER, 2026-07-04); a
                                   "move" = tab_id + pinned_placement=true together
  DELETE /notes/{id}            — delete a note
  POST   /notes/{id}/promote    — curated promotion (training.promoted=True)
  POST   /notes/{id}/resolution — v2: attach a resolution (roster-loop write-back);
                                   schedules a fire-and-forget mnemosyne distill
  POST   /notes/{id}/revalidate — Phase 2a north-star: re-ground vs current code
  POST   /notes/{id}/research-revise — grimoire Phase 3 REVISE: schedules a job
                                   proposing a patch (whole guide or one section);
                                   returns {job_id} — never applies; poll GET
                                   /notes/research/{job_id}, apply via PATCH
                                   /notes/{id} after human review
  POST   /notes/{id}/export     — grimoire Phase 4: write a guide's raw_text back
                                   to its source_path (or a given path)
  POST   /notes/{id}/chat       — grimoire chat model: one turn of a record's
                                   (guide or note — CHAT-UNIFY, 2026-07-04) persistent
                                   conversation; schedules a job, returns {job_id} —
                                   poll GET /notes/research/{job_id} (kind:"chat").
                                   Answer-vs-edit is the agent's own routing; edits
                                   AUTO-APPLY (undo via version history)
  GET    /notes/{id}/chat       — load a record's persistent chat history
  POST   /notes/{id}/chat/clear — wipe a record's chat history
  POST   /notes/{id}/chat/compact — summarize older chat turns into one message,
                                   keeping the tail verbatim (cost control — every
                                   turn passes the full history into the agentic call)
  GET    /tabs                  — list tabs (note_ids derived from live notes; FLAT —
                                   client builds the tree from parent_id)
  POST   /tabs                  — create a tab (kind/parent_id — FILE-MANAGER, 2026-07-04)
  PATCH  /tabs/{id}             — rename/re-kind/reparent a tab (parent_id — cycle,
                                   cross-kind-nesting, sibling-name checks enforced)
  DELETE /tabs/{id}             — delete a tab; reparents child tabs + re-files direct
                                   member notes so nothing becomes tree-unreachable

`trigger_pipeline` schedules the Phase 1c headless `claude -p` transform as
a background task — POST /notes returns immediately with a draft note; the
note flips to processed/failed once notebook_pipeline.trigger_pipeline
completes. `revalidate_note` is awaited directly (not backgrounded) — it's a
manual on-demand user action ("re-check vs code" button), not a write path
that needs to return instantly.

Grimoire Phase 4 addendum (2026-07-04): the research AND chat routes are
async job+poll, NOT awaited-in-request, unlike revalidate_note — a 1-2
minute agentic call held the HTTP connection open long enough that any
client disconnect (or an in-flight `systemctl restart` under the daemon's
default KillMode) would kill the call outright. POST schedules a background
task and returns a job_id immediately; the frontend polls GET
/notes/research/{job_id} until status is "done"/"error".

Note-content embedding (notebook_retrieval) is fire-and-forget on create/
delete (never blocks the response) and re-runs on structuring completion /
heal from inside notebook_pipeline itself.
"""

from __future__ import annotations

import asyncio

from pydantic import BaseModel

from khimaira.monitor import (
    notebook_chat,
    notebook_import,
    notebook_pipeline,
    notebook_retrieval,
    notebook_training,
    notes,
)

from .._optional import require

# urgent > high > normal > low — the numeric ordering ?sort=-priority uses.
_PRIORITY_SORT_ORDER = {"urgent": 3, "high": 2, "normal": 1, "low": 0}


class CreateNoteReq(BaseModel):
    raw_text: str
    tab_id: str = ""
    title: str = ""
    repo: str = ""
    kind: str = "note"
    source_path: str | None = None
    collection: str = ""
    sensitive: bool = False


class ImportGuidesReq(BaseModel):
    root: str
    repo: str = ""
    dry_run: bool = True


class UpdateNoteReq(BaseModel):
    title: str | None = None
    tab_id: str | None = None
    raw_text: str | None = None
    status: str | None = None
    links: list[str] | None = None
    repo: str | None = None
    pipeline: dict | None = None
    sensitive: bool | None = None
    priority: str | None = None
    pinned_placement: bool | None = None
    starred: bool | None = None
    test_status: str | None = None


class AddResolutionReq(BaseModel):
    resolution: str
    resolved_by: str = ""


class AskReq(BaseModel):
    question: str
    repo: str | None = None
    note_ids: list[str] = []
    exclusive: bool = False


class ResearchReq(BaseModel):
    note_id: str
    question: str
    max_budget_usd: float = notebook_pipeline._AGENTIC_DEFAULT_BUDGET_USD


class ResearchReviseReq(BaseModel):
    directive: str
    section_anchor: str | None = None
    max_budget_usd: float = notebook_pipeline._AGENTIC_DEFAULT_BUDGET_USD


class ExportNoteReq(BaseModel):
    path: str | None = None


class ChatMessageReq(BaseModel):
    message: str
    max_budget_usd: float = notebook_pipeline._AGENTIC_DEFAULT_BUDGET_USD


class NotebookChatReq(BaseModel):
    message: str
    refs: list[str] = []


class CreateTicketReq(BaseModel):
    title: str
    description: str = ""
    state: str = ""
    linear_priority: int = 0
    assignee: dict | None = None
    labels: list[str] | None = None
    project: str = ""
    parent_id: str | None = None
    links: list[str] | None = None
    tab_id: str = ""


class UpdateTicketReq(BaseModel):
    title: str | None = None
    raw_text: str | None = None
    state: str | None = None
    linear_priority: int | None = None
    assignee: dict | None = None
    labels: list[str] | None = None
    project: str | None = None
    parent_id: str | None = None
    links: list[str] | None = None
    tab_id: str | None = None


class AddTicketCommentReq(BaseModel):
    text: str
    author: str = ""


class MappedLinearIssue(BaseModel):
    """One already-fetched-and-mapped Linear issue — see bulk_upsert_tickets'
    docstring for the fetch-then-push contract this shape belongs to."""

    linear_ref: str
    title: str | None = None
    description: str | None = None
    state: str | None = None
    linear_priority: int | None = None
    assignee: dict | None = None
    labels: list[str] | None = None
    parent_id: str | None = None
    links: list[str] | None = None


class BulkUpsertTicketsReq(BaseModel):
    project: str
    tickets: list[MappedLinearIssue]


class CreateTabReq(BaseModel):
    title: str = ""
    kind: str = ""
    parent_id: str | None = None


class UpdateTabReq(BaseModel):
    title: str | None = None
    kind: str | None = None
    parent_id: str | None = None


def trigger_pipeline(note_id: str) -> None:
    """Fire the headless `claude -p` transform for note_id in the background."""
    notebook_pipeline.schedule_pipeline(note_id)


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.post("/notes")
    async def create_note(req: CreateNoteReq) -> dict:
        if req.kind == "study_guide":
            # Grimoire: study guides are housed + rendered, never
            # re-expressed — structuring only ever generates
            # abstract/tags/entities (schedule_pipeline's kind branch picks
            # trigger_study_guide_pipeline), raw_text is never touched.
            #
            # `collection`, when given, is a friendly collection NAME
            # (get-or-create semantics) — the caller (e.g. the
            # notebook_create_study_guide MCP tool, a thin HTTP client) has
            # no in-process access to notes.get_or_create_collection, so the
            # daemon resolves it here rather than requiring callers to know
            # a tab_id up front. Takes precedence over a raw tab_id if both
            # are somehow given.
            tab_id = req.tab_id
            if req.collection:
                collection = await asyncio.to_thread(notes.get_or_create_collection, req.collection)
                tab_id = collection["id"]
            record = await asyncio.to_thread(
                notes.add_study_guide,
                req.raw_text,
                tab_id=tab_id,
                title=req.title,
                repo=req.repo,
                source_path=req.source_path,
                sensitive=req.sensitive,
            )
            trigger_pipeline(record["id"])
            notebook_retrieval.schedule_upsert(record)
            return record

        record = await asyncio.to_thread(
            notes.add_note,
            req.raw_text,
            tab_id=req.tab_id,
            title=req.title,
            repo=req.repo,
            sensitive=req.sensitive,
        )
        if record["tab_id"] == notes.PERSONAL_TAB_ID:
            # Personal/Behavior folder notes are read as raw_text directly
            # (notebook_pipeline._personal_context) — no structuring, no
            # embed (notebook_retrieval.upsert_note already refuses to
            # embed them too; this just avoids the wasted structuring call).
            record = await asyncio.to_thread(notes.update_note, record["id"], status="processed")
        else:
            trigger_pipeline(record["id"])
            notebook_retrieval.schedule_upsert(record)
        return record

    @router.get("/notes")
    async def list_notes_endpoint(
        tab_id: str | None = None,
        repo: str | None = None,
        kind: str | None = None,
        priority: str | None = None,
        starred: bool | None = None,
        test_status: str | None = None,
        sort: str | None = None,
    ) -> dict:
        """`repo`, when given, scopes to that repo plus the "General" bucket
        (repo=None returns every project's notes — the "All projects" view).
        `kind`, when given, scopes to "note" or "study_guide" (kind=None
        returns both — the existing UI filters personal/guide notes out
        client-side rather than relying on server-side exclusion).

        `priority`, when given, scopes to one priority value. `starred`
        (FILE-MANAGER, 2026-07-04), when given, scopes to that starred
        state — the Starred rail. `test_status` (2026-07-07), when given,
        scopes to one testing-workflow status value. `sort`
        (`"priority"` or `"-priority"`) orders the result by urgent > high >
        normal > low (or the reverse) — a presentation concern, so it's
        applied here rather than inside notes.list_notes."""
        result = await asyncio.to_thread(
            notes.list_notes,
            tab_id=tab_id,
            repo=repo,
            kind=kind,
            priority=priority,
            starred=starred,
            test_status=test_status,
        )
        if sort in ("priority", "-priority"):
            result = sorted(
                result,
                key=lambda n: _PRIORITY_SORT_ORDER.get(n.get("priority", "normal"), 1),
                reverse=(sort == "-priority"),
            )
        return {"notes": result}

    @router.post("/notes/import")
    async def import_guides(req: ImportGuidesReq) -> dict:
        """Grimoire Phase 1c: bulk-import study guides from a flat directory.

        `dry_run` (default True) produces a manifest ONLY — writes nothing.
        Review the manifest before ever calling with dry_run=False; a real
        import creates a note per file (idempotent via source_path — safe
        to re-run) and schedules each one's structuring pipeline async."""
        return await asyncio.to_thread(notebook_import.import_dir, req.root, repo=req.repo, dry_run=req.dry_run)

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
        """Phase 2c capstone (v2): retrieve candidate notes (+ any @-mentioned
        note_ids), staleness-gated revalidate (heals stale ones) each hit,
        ground the answer in a live Séance/grep search of each note's repo,
        synthesize an answer citing both notes and code. Awaited directly —
        an on-demand ask, not a write path."""
        return await notebook_pipeline.answer_question(
            req.question, repo=req.repo, mentioned_note_ids=req.note_ids, exclusive=req.exclusive
        )

    @router.post("/notes/research")
    async def research(req: ResearchReq) -> dict:
        """Grimoire Phase 3 ANSWER path: research-grounds a question against
        a guide + the live codebase + the web, read-only (never edits the
        guide). ASYNC (Phase 4 addendum): schedules a background job and
        returns {job_id} immediately — poll GET /notes/research/{job_id}.
        Registered BEFORE /notes/{note_id} so "research" doesn't get
        swallowed as a note_id path param."""
        try:
            job_id = notebook_pipeline.schedule_research_answer(
                req.note_id, req.question, max_budget_usd=req.max_budget_usd
            )
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e
        return {"job_id": job_id, "status": "pending"}

    @router.get("/notes/research/{job_id}")
    async def get_research_job(job_id: str) -> dict:
        """Poll an ANSWER or REVISE job scheduled by POST /notes/research or
        POST /notes/{id}/research-revise. Registered BEFORE /notes/{note_id}
        (and after /notes/research, matching the literal-path-before-
        path-param convention) so "research" isn't swallowed as a note_id."""
        try:
            return notebook_pipeline.get_research_job(job_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.post("/notes/chat")
    async def notebook_chat_route(req: NotebookChatReq) -> dict:
        """CHAT-UNIFY §2 MVP (2026-07-04): notebook-wide chat for the
        "nothing open" case — a thin wrapper around answer_question,
        scheduled as a background job on the SAME shared job store as
        per-record chat/research, polled at GET /notes/research/{job_id}
        (kind="notebook_chat"). NOT true multi-turn: each call is a
        stateless ask (no persisted server-side history) — `refs` map 1:1
        onto answer_question's mentioned_note_ids; the frontend accumulates
        the conversational feel client-side. Registered BEFORE
        /notes/{note_id} so "chat" doesn't get swallowed as a note_id."""
        job_id = notebook_pipeline.schedule_notebook_chat(req.message, req.refs)
        return {"job_id": job_id, "status": "pending"}

    @router.get("/notes/{note_id}")
    async def get_note(note_id: str) -> dict:
        try:
            return await asyncio.to_thread(notes.get_note, note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.patch("/notes/{note_id}")
    async def update_note(note_id: str, req: UpdateNoteReq) -> dict:
        fields = req.model_dump(exclude_unset=True)
        try:
            record = await asyncio.to_thread(notes.update_note, note_id, **fields)
        except ValueError as e:
            msg = str(e)
            status = 404 if "No note with id" in msg else 422
            raise fastapi.HTTPException(status, msg) from e
        # A raw_text edit invalidates the DERIVED artifacts — the structuring
        # pipeline (summary/technical/plain tabs) and the search embedding are
        # both computed from raw_text, so an edit that doesn't reprocess leaves
        # the tabs silently stale vs the source. Regenerate both, exactly like
        # a fresh capture (create_note). Title/tab/repo/status-only edits don't
        # touch the structured content, so they skip reprocessing. Personal-tab
        # notes are read raw (no pipeline, no embed), mirroring create_note.
        # reprocess_after_raw_text_change flips to draft (so the UI shows a
        # "reprocessing" state while the async pipeline runs — set_pipeline
        # flips it back to processed + stamps structured_at on completion;
        # the old tabs stay visible meanwhile, just badged as reprocessing)
        # and fires the same sequence the chat auto-apply path uses.
        if "raw_text" in fields:
            record = notebook_pipeline.reprocess_after_raw_text_change(record["id"])
        return record

    @router.delete("/notes/{note_id}")
    async def delete_note(note_id: str) -> dict:
        try:
            result = await asyncio.to_thread(notes.delete_note, note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e
        notebook_retrieval.schedule_delete(note_id)
        return result

    @router.post("/notes/{note_id}/promote")
    async def promote_note(note_id: str) -> dict:
        try:
            return await asyncio.to_thread(notes.promote_note, note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.post("/notes/{note_id}/resolution")
    async def add_resolution(note_id: str, req: AddResolutionReq) -> dict:
        """v2 roster-loop write-back: attach a resolution to a note.

        Schedules a fire-and-forget mnemosyne distill of the {problem,
        resolution} pair when a non-empty resolution lands — never blocks
        the response, and mnemosyne being unreachable never fails this
        request (see notebook_training.promote_resolved's fail-open contract).

        Tickets (kind="ticket", 2026-07-11) are excluded from training
        promotion even when resolved — a ticket resolution is a work-
        tracking closure ("duplicate of X", "fixed in PR #123"), not a
        curated {problem, technical solution} pair, and linear-pulled
        tickets can carry team/project-internal text never meant for the
        shared oracle corpus. Same category of exclusion as the existing
        sensitive=True hard-exclusion, different trigger. Reversible (the
        guard can be dropped later); baking ticket text into an already-
        trained oracle is not — see chat-102d8b5fd82f task-8191e0a1672b.
        """
        try:
            updated = await asyncio.to_thread(
                notes.add_resolution, note_id, req.resolution, resolved_by=req.resolved_by
            )
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e
        if updated.get("resolution") and updated.get("kind") != "ticket":
            notebook_training.schedule_promote(updated)
        return updated

    @router.post("/notes/{note_id}/revalidate")
    async def revalidate_note(note_id: str) -> dict:
        try:
            return await notebook_pipeline.revalidate_note(note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.post("/notes/{note_id}/research-revise")
    async def research_revise(note_id: str, req: ResearchReviseReq) -> dict:
        """Grimoire Phase 3 REVISE path: proposes a patch to a guide (whole
        or one section, via `section_anchor`), grounded against the
        codebase + web. NEVER applies — the eventual proposal (including
        `proposed_raw_text`, ready to diff) is for a human to review;
        applying is the EXISTING PATCH /notes/{note_id}
        (raw_text=proposed_raw_text). ASYNC (Phase 4 addendum): schedules a
        background job and returns {job_id} immediately — poll GET
        /notes/research/{job_id}."""
        try:
            job_id = notebook_pipeline.schedule_research_revise(
                note_id,
                req.directive,
                section_anchor=req.section_anchor,
                max_budget_usd=req.max_budget_usd,
            )
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e
        return {"job_id": job_id, "status": "pending"}

    @router.post("/notes/{note_id}/export")
    async def export_note(note_id: str, req: ExportNoteReq) -> dict:
        """Grimoire Phase 4: write a study guide's raw_text back to disk —
        the reversibility valve for "notebook is canonical" (keeps the
        devs' @-ref-a-file habit alive). Guide-only; 404 on an unknown
        note_id, a non-guide note, or a guide with no source_path and no
        explicit `path` given."""
        try:
            return await asyncio.to_thread(notebook_import.export_note, note_id, req.path)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.post("/notes/{note_id}/chat")
    async def chat(note_id: str, req: ChatMessageReq) -> dict:
        """Grimoire chat model: one turn of a record's (guide or note —
        CHAT-UNIFY, 2026-07-04) persistent conversation. ASYNC (reuses the
        research job+poll infra) — schedules a background job and returns
        {job_id} immediately; poll GET /notes/research/{job_id} (kind:"chat").
        Answer-vs-edit is the agent's own structured-output routing, not a
        separate endpoint — an edit AUTO-APPLIES (version history is the
        undo mechanism)."""
        try:
            job_id = notebook_chat.schedule_chat_turn(
                note_id, req.message, max_budget_usd=req.max_budget_usd
            )
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e
        return {"job_id": job_id, "status": "pending"}

    @router.get("/notes/{note_id}/chat")
    async def get_chat(note_id: str) -> dict:
        """Load a record's persistent chat history — the frontend calls this
        on open so the conversation survives reopen."""
        try:
            await asyncio.to_thread(notes.get_note, note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e
        return {"history": notebook_chat.get_chat_history(note_id)}

    @router.post("/notes/{note_id}/chat/clear")
    async def chat_clear(note_id: str) -> dict:
        try:
            return notebook_chat.clear_chat(note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.post("/notes/{note_id}/chat/compact")
    async def chat_compact(note_id: str) -> dict:
        """Summarize older chat turns into one message, keeping the tail
        verbatim — cost control, not cosmetic (every turn passes the full
        history into the agentic call)."""
        try:
            return await notebook_chat.compact_chat_history(note_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.get("/tabs")
    async def list_tabs() -> dict:
        return {"tabs": await asyncio.to_thread(notes.list_tabs)}

    @router.post("/tabs")
    async def create_tab(req: CreateTabReq) -> dict:
        """FILE-MANAGER (2026-07-04): `kind` + `parent_id` accepted for
        human-authored nested collections/folders. Raises 422 for a
        structural validation failure (cross-kind nesting, sibling-name
        collision — notes.TabValidationError), 404 for a dangling
        parent_id (plain ValueError, mirrors get_tab)."""
        try:
            return await asyncio.to_thread(
                notes.add_tab, title=req.title, kind=req.kind, parent_id=req.parent_id
            )
        except notes.TabValidationError as e:
            raise fastapi.HTTPException(422, str(e)) from e
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.patch("/tabs/{tab_id}")
    async def update_tab(tab_id: str, req: UpdateTabReq) -> dict:
        """FILE-MANAGER (2026-07-04): `parent_id` reparents — enforces
        no-cycle/homogeneous-kind/sibling-uniqueness (422,
        notes.TabValidationError) and parent-exists (404, plain
        ValueError, mirrors get_tab)."""
        fields = req.model_dump(exclude_unset=True)
        try:
            return await asyncio.to_thread(notes.update_tab, tab_id, **fields)
        except notes.TabValidationError as e:
            raise fastapi.HTTPException(422, str(e)) from e
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    @router.delete("/tabs/{tab_id}")
    async def delete_tab_route(tab_id: str) -> dict:
        """FILE-MANAGER (2026-07-04): re-files child tabs (to this tab's own
        parent) AND direct member notes (to this tab's parent, or the
        default tab at root) — "never lose a guide." See notes.delete_tab."""
        try:
            return await asyncio.to_thread(notes.delete_tab, tab_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e

    # -------------------------------------------------------------------
    # Tickets (2026-07-11) — local mirror of Linear issues. Same store as
    # notes/tabs (kind="ticket"), a separate resource surface (not folded
    # into /notes) since its filters (project/state/assignee/label) and
    # read-only-synced-field semantics don't apply to notes/guides.
    # -------------------------------------------------------------------

    @router.post("/tickets")
    async def create_ticket(req: CreateTicketReq) -> dict:
        """Create a LOCAL ticket — always `origin="local-created"`.
        `origin`/`linear_ref` are never caller-settable here; a
        linear-pulled ticket can only be created via POST
        /tickets/bulk-upsert (the resync path)."""
        try:
            return await asyncio.to_thread(
                notes.add_ticket,
                req.title,
                req.description,
                state=req.state,
                linear_priority=req.linear_priority,
                assignee=req.assignee,
                labels=req.labels,
                project=req.project,
                parent_id=req.parent_id,
                links=req.links,
                tab_id=req.tab_id,
            )
        except ValueError as e:
            raise fastapi.HTTPException(422, str(e)) from e

    @router.get("/tickets")
    async def list_tickets_endpoint(
        project: str | None = None,
        state: str | None = None,
        assignee: str | None = None,
        label: str | None = None,
    ) -> dict:
        return {
            "tickets": await asyncio.to_thread(
                notes.list_tickets, project=project, state=state, assignee=assignee, label=label
            )
        }

    @router.post("/tickets/bulk-upsert")
    async def bulk_upsert_tickets(req: BulkUpsertTicketsReq) -> dict:
        """The resync write path — idempotent create-or-update of already-
        fetched-and-mapped Linear issues, keyed on each entry's
        `linear_ref`. Registered BEFORE /tickets/{ticket_id} (literal path
        before path param, matching this file's existing /notes/search
        convention) so "bulk-upsert" is never swallowed as a ticket_id.

        This does NOT talk to Linear. The caller (an agent) fetches the
        target project's issues via `mcp__linear__list_issues`/`get_issue`
        itself, maps each onto the shape below, and posts the mapped list
        here — the perceive (LLM fetch+map) / dispose (deterministic
        upsert) split from khimaira's own ai-engineering conventions. See
        the `ticket_bulk_upsert` MCP tool, which documents this 2-step
        contract for whoever calls it.

        Re-running with the same `linear_ref`s never creates duplicates —
        only updates the synced fields (title/state/priority/assignee/
        labels/parent_id/links/description) plus `project`/`sync_state`;
        resolution/comments/tab_id are untouched regardless of how many
        times this runs."""
        created = 0
        updated = 0
        for issue in req.tickets:
            try:
                _, was_created = await asyncio.to_thread(
                    notes.upsert_ticket_from_linear, issue.model_dump(exclude_unset=True), project=req.project
                )
            except ValueError as e:
                raise fastapi.HTTPException(422, str(e)) from e
            if was_created:
                created += 1
            else:
                updated += 1
        return {"pulled": len(req.tickets), "created": created, "updated": updated}

    @router.get("/tickets/{ticket_id}")
    async def get_ticket(ticket_id: str) -> dict:
        try:
            record = await asyncio.to_thread(notes.get_note, ticket_id)
        except ValueError as e:
            raise fastapi.HTTPException(404, str(e)) from e
        if record.get("kind") != "ticket":
            raise fastapi.HTTPException(404, f"No ticket with id={ticket_id!r}.")
        return record

    @router.patch("/tickets/{ticket_id}")
    async def update_ticket_route(ticket_id: str, req: UpdateTicketReq) -> dict:
        """422 on an unknown field, an invalid state/linear_priority, or an
        attempt to mutate a linear-pulled ticket's synced fields — see
        notes.update_ticket's docstring. 404 on an unknown ticket_id or an
        id that isn't a ticket."""
        fields = req.model_dump(exclude_unset=True)
        try:
            return await asyncio.to_thread(notes.update_ticket, ticket_id, **fields)
        except ValueError as e:
            msg = str(e)
            status = 404 if "is not a ticket" in msg or "No note with id" in msg else 422
            raise fastapi.HTTPException(status, msg) from e

    @router.post("/tickets/{ticket_id}/comments")
    async def add_ticket_comment_route(ticket_id: str, req: AddTicketCommentReq) -> dict:
        try:
            return await asyncio.to_thread(notes.add_ticket_comment, ticket_id, req.text, req.author)
        except ValueError as e:
            msg = str(e)
            status = 404 if "is not a ticket" in msg or "No note with id" in msg else 422
            raise fastapi.HTTPException(status, msg) from e

    return router
