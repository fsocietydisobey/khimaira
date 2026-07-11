"""HTTP-layer tests for /api/notes + /api/tabs (Phase 1a)."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def notebook_client(isolated_state, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from khimaira.monitor import notebook_pipeline as pipeline_mod
    from khimaira.monitor import notebook_training as training_mod
    from khimaira.monitor import notes as notes_mod
    from khimaira.monitor.api import notebook as notebook_api

    importlib.reload(notes_mod)
    importlib.reload(pipeline_mod)
    importlib.reload(training_mod)
    importlib.reload(notebook_api)

    # Never let a real mnemosyne network call fire during API tests — the
    # resolution route schedules notebook_training.schedule_promote as a
    # background task; without this, tests would race a real (or
    # never-connecting) urllib call.
    monkeypatch.setattr(notebook_api.notebook_training, "schedule_promote", lambda record: None)

    app = FastAPI()
    app.include_router(notebook_api.build_router(), prefix="/api")
    return TestClient(app)


def test_create_note_returns_draft(notebook_client):
    r = notebook_client.post("/api/notes", json={"raw_text": "hello world", "tab_id": "t1"})
    assert r.status_code == 200
    body = r.json()
    assert body["raw_text"] == "hello world"
    assert body["tab_id"] == "t1"
    assert body["status"] == "draft"
    assert body["pipeline"] is None


def test_create_note_in_personal_folder_skips_structuring(notebook_client):
    """Personal/Behavior notes are behavioral context, not content to
    structure — created directly as processed, no draft->structuring wait."""
    r = notebook_client.post(
        "/api/notes", json={"raw_text": "Always be terse.", "tab_id": "personal"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "processed"
    assert body["pipeline"] is None


def test_create_note_defaults_kind_to_note(notebook_client):
    r = notebook_client.post("/api/notes", json={"raw_text": "hello"})
    assert r.json()["kind"] == "note"


def test_create_note_study_guide_kind(notebook_client):
    r = notebook_client.post(
        "/api/notes",
        json={
            "raw_text": "# Guide\n\nBody.",
            "kind": "study_guide",
            "source_path": "/tmp/guide.md",
            "repo": "jeevy_portal",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "study_guide"
    assert body["source_path"] == "/tmp/guide.md"
    assert body["status"] == "draft"
    assert body["pipeline"] is None
    assert body["repo"] == "jeevy_portal"


def test_create_note_study_guide_collection_resolves_to_tab(notebook_client):
    """Grimoire Phase 2: `collection` is a friendly get-or-create name (the
    MCP client has no in-process access to notes.get_or_create_collection),
    resolved to a tab_id server-side."""
    r = notebook_client.post(
        "/api/notes",
        json={"raw_text": "# Guide\n\nBody.", "kind": "study_guide", "collection": "Onboarding"},
    )
    assert r.status_code == 200
    body = r.json()

    tabs = notebook_client.get("/api/tabs").json()["tabs"]
    onboarding = next(t for t in tabs if t["title"] == "Onboarding")
    assert onboarding["kind"] == "collection"
    assert body["tab_id"] == onboarding["id"]


def test_create_note_study_guide_collection_reuses_existing(notebook_client):
    first = notebook_client.post(
        "/api/notes",
        json={
            "raw_text": "# Guide One\n\nBody.",
            "kind": "study_guide",
            "collection": "Onboarding",
        },
    ).json()
    second = notebook_client.post(
        "/api/notes",
        json={
            "raw_text": "# Guide Two\n\nBody.",
            "kind": "study_guide",
            "collection": "onboarding",
        },
    ).json()

    assert first["tab_id"] == second["tab_id"]
    collections = [
        t for t in notebook_client.get("/api/tabs").json()["tabs"] if t["kind"] == "collection"
    ]
    assert len(collections) == 1


def test_list_notes_kind_filter_route(notebook_client):
    notebook_client.post("/api/notes", json={"raw_text": "a note"})
    notebook_client.post(
        "/api/notes", json={"raw_text": "# A Guide\n\nbody", "kind": "study_guide"}
    )

    guides = notebook_client.get("/api/notes", params={"kind": "study_guide"}).json()["notes"]
    assert len(guides) == 1
    assert guides[0]["kind"] == "study_guide"

    both = notebook_client.get("/api/notes").json()["notes"]
    assert len(both) == 2


def test_list_notes_empty_store(notebook_client):
    r = notebook_client.get("/api/notes")
    assert r.status_code == 200
    assert r.json() == {"notes": []}


def test_list_notes_filters_by_tab(notebook_client):
    notebook_client.post("/api/notes", json={"raw_text": "a", "tab_id": "t1"})
    notebook_client.post("/api/notes", json={"raw_text": "b", "tab_id": "t2"})
    r = notebook_client.get("/api/notes", params={"tab_id": "t1"})
    assert r.status_code == 200
    notes = r.json()["notes"]
    assert len(notes) == 1
    assert notes[0]["tab_id"] == "t1"


def test_list_notes_repo_filter_includes_general(notebook_client):
    notebook_client.post("/api/notes", json={"raw_text": "a", "repo": "khimaira"})
    notebook_client.post("/api/notes", json={"raw_text": "b", "repo": "jeevy_portal"})
    notebook_client.post("/api/notes", json={"raw_text": "c", "repo": "general"})

    r = notebook_client.get("/api/notes", params={"repo": "khimaira"})
    assert r.status_code == 200
    repos = {n["repo"] for n in r.json()["notes"]}
    assert repos == {"khimaira", "general"}

    r_all = notebook_client.get("/api/notes")
    assert {n["repo"] for n in r_all.json()["notes"]} == {"khimaira", "jeevy_portal", "general"}


def test_get_note_happy_path(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.get(f"/api/notes/{created['id']}")
    assert r.status_code == 200
    assert r.json()["raw_text"] == "hi"


def test_get_note_unknown_id_returns_404(notebook_client):
    r = notebook_client.get("/api/notes/no-such-id")
    assert r.status_code == 404
    assert "no note with id" in r.json()["detail"].lower()


def test_patch_note_happy_path(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"title": "renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "renamed"
    # Unset fields on the request are left untouched.
    assert r.json()["raw_text"] == "hi"


def test_patch_note_unknown_id_returns_404(notebook_client):
    r = notebook_client.patch("/api/notes/no-such-id", json={"title": "x"})
    assert r.status_code == 404


def test_patch_note_invalid_status_returns_422(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"status": "bogus"})
    assert r.status_code == 422


def test_patch_raw_text_reprocesses_pipeline(notebook_client, monkeypatch):
    """A raw_text edit invalidates the derived summary/technical/plain tabs, so
    PATCH must re-trigger the structuring pipeline (they'd go silently stale
    otherwise). Regression for the notebook_update-doesn't-reprocess footgun.

    Asserts against notebook_pipeline.schedule_pipeline (what
    reprocess_after_raw_text_change actually calls) rather than the
    api-module's own `trigger_pipeline` wrapper — the PATCH route now
    shares that helper with the chat auto-apply path (Grimoire chat-model,
    2026-07-04), so `trigger_pipeline` itself is no longer on this call path."""
    from khimaira.monitor.api import notebook as notebook_api

    calls: list[str] = []
    monkeypatch.setattr(
        notebook_api.notebook_pipeline, "schedule_pipeline", lambda nid, **k: calls.append(nid)
    )
    created = notebook_client.post("/api/notes", json={"raw_text": "original"}).json()
    calls.clear()  # ignore the create-time trigger; we're testing the PATCH path

    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"raw_text": "edited body"})
    assert r.status_code == 200
    assert calls == [created["id"]]


def test_patch_metadata_only_does_not_reprocess(notebook_client, monkeypatch):
    """Title/tab/repo/status edits don't touch the structured content, so they
    must NOT fire an (expensive) reprocess."""
    from khimaira.monitor.api import notebook as notebook_api

    calls: list[str] = []
    monkeypatch.setattr(
        notebook_api.notebook_pipeline, "schedule_pipeline", lambda nid, **k: calls.append(nid)
    )
    created = notebook_client.post("/api/notes", json={"raw_text": "original"}).json()
    calls.clear()

    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"title": "renamed"})
    assert r.status_code == 200
    assert calls == []


def test_patch_raw_text_on_personal_note_does_not_reprocess(notebook_client, monkeypatch):
    """Personal-tab notes are read raw (never structured), mirroring create —
    a raw_text edit on one must not schedule a pipeline run. The exemption
    now lives inside reprocess_after_raw_text_change itself (shared with the
    chat auto-apply path), not the route."""
    from khimaira.monitor.api import notebook as notebook_api

    calls: list[str] = []
    monkeypatch.setattr(
        notebook_api.notebook_pipeline, "schedule_pipeline", lambda nid, **k: calls.append(nid)
    )
    created = notebook_client.post(
        "/api/notes", json={"raw_text": "x", "tab_id": "personal"}
    ).json()
    calls.clear()

    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"raw_text": "edited"})
    assert r.status_code == 200
    assert calls == []


def test_patch_raw_text_flips_status_to_draft_for_reprocessing(notebook_client):
    """A raw_text edit flips the note back to 'draft' — the UI's reprocessing
    signal — while the async pipeline re-runs (set_pipeline flips it back)."""
    created = notebook_client.post("/api/notes", json={"raw_text": "original"}).json()
    notebook_client.patch(f"/api/notes/{created['id']}", json={"status": "processed"})
    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"raw_text": "edited"})
    assert r.status_code == 200
    assert r.json()["status"] == "draft"


def test_set_pipeline_stamps_structured_at(isolated_state):
    """structured_at is None until the pipeline completes, then stamped — the
    reader's 'structured <time>' signal, distinct from updated_at."""
    import importlib

    from khimaira.monitor import notes as notes_mod

    importlib.reload(notes_mod)
    note = notes_mod.add_note("hi")
    assert note["structured_at"] is None
    updated = notes_mod.set_pipeline(
        note["id"],
        {
            "summary": "s",
            "technical": "t",
            "plain": "p",
            "organized_md": "m",
            "tags": [],
            "entities": [],
        },
    )
    assert updated["structured_at"] is not None
    assert updated["status"] == "processed"


def test_delete_note_happy_path(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.delete(f"/api/notes/{created['id']}")
    assert r.status_code == 200
    assert r.json() == {"id": created["id"], "deleted": True}
    assert notebook_client.get(f"/api/notes/{created['id']}").status_code == 404


def test_delete_note_unknown_id_returns_404(notebook_client):
    r = notebook_client.delete("/api/notes/no-such-id")
    assert r.status_code == 404


def test_promote_note_happy_path(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.post(f"/api/notes/{created['id']}/promote")
    assert r.status_code == 200
    body = r.json()
    assert body["training"]["promoted"] is True
    assert body["status"] == "promoted"


def test_promote_note_unknown_id_returns_404(notebook_client):
    r = notebook_client.post("/api/notes/no-such-id/promote")
    assert r.status_code == 404


def test_tabs_crud_happy_path(notebook_client):
    r = notebook_client.post("/api/tabs", json={"title": "My Tab"})
    assert r.status_code == 200
    tab = r.json()
    assert tab["title"] == "My Tab"
    assert tab["note_ids"] == []

    r = notebook_client.get("/api/tabs")
    assert r.status_code == 200
    assert len(r.json()["tabs"]) == 1

    r = notebook_client.patch(f"/api/tabs/{tab['id']}", json={"title": "renamed"})
    assert r.status_code == 200
    assert r.json()["title"] == "renamed"


def test_tabs_group_notes_by_tab_id(notebook_client):
    tab = notebook_client.post("/api/tabs", json={"title": "grouped"}).json()
    note = notebook_client.post("/api/notes", json={"raw_text": "a", "tab_id": tab["id"]}).json()
    r = notebook_client.get("/api/tabs")
    tabs = r.json()["tabs"]
    matched = next(t for t in tabs if t["id"] == tab["id"])
    assert matched["note_ids"] == [note["id"]]


def test_patch_tab_unknown_id_returns_404(notebook_client):
    r = notebook_client.patch("/api/tabs/no-such-tab", json={"title": "x"})
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# FILE-MANAGER (2026-07-04) — tab hierarchy (parent_id) + note pinned_placement/starred
# ---------------------------------------------------------------------------


def test_create_tab_with_kind_and_parent_id(notebook_client):
    root = notebook_client.post("/api/tabs", json={"title": "Root", "kind": "collection"}).json()
    child = notebook_client.post(
        "/api/tabs", json={"title": "Child", "kind": "collection", "parent_id": root["id"]}
    )
    assert child.status_code == 200
    assert child.json()["parent_id"] == root["id"]


def test_create_tab_dangling_parent_returns_404(notebook_client):
    r = notebook_client.post("/api/tabs", json={"title": "x", "parent_id": "no-such-tab"})
    assert r.status_code == 404


def test_create_tab_cross_kind_parent_returns_422(notebook_client):
    collection = notebook_client.post("/api/tabs", json={"title": "C", "kind": "collection"}).json()
    r = notebook_client.post(
        "/api/tabs", json={"title": "F", "kind": "folder", "parent_id": collection["id"]}
    )
    assert r.status_code == 422


def test_create_tab_sibling_collision_returns_422(notebook_client):
    parent = notebook_client.post("/api/tabs", json={"title": "P", "kind": "collection"}).json()
    notebook_client.post(
        "/api/tabs", json={"title": "API", "kind": "collection", "parent_id": parent["id"]}
    )
    r = notebook_client.post(
        "/api/tabs", json={"title": "api", "kind": "collection", "parent_id": parent["id"]}
    )
    assert r.status_code == 422


def test_patch_tab_reparent(notebook_client):
    root = notebook_client.post("/api/tabs", json={"title": "Root", "kind": "collection"}).json()
    child = notebook_client.post("/api/tabs", json={"title": "Child", "kind": "collection"}).json()
    r = notebook_client.patch(f"/api/tabs/{child['id']}", json={"parent_id": root["id"]})
    assert r.status_code == 200
    assert r.json()["parent_id"] == root["id"]


def test_patch_tab_reparent_cycle_returns_422(notebook_client):
    a = notebook_client.post("/api/tabs", json={"title": "A", "kind": "collection"}).json()
    b = notebook_client.post(
        "/api/tabs", json={"title": "B", "kind": "collection", "parent_id": a["id"]}
    ).json()
    r = notebook_client.patch(f"/api/tabs/{a['id']}", json={"parent_id": b["id"]})
    assert r.status_code == 422


def test_delete_tab_route_happy_path(notebook_client):
    parent = notebook_client.post("/api/tabs", json={"title": "Parent"}).json()
    note = notebook_client.post("/api/notes", json={"raw_text": "a", "tab_id": parent["id"]}).json()

    r = notebook_client.delete(f"/api/tabs/{parent['id']}")
    assert r.status_code == 200
    assert r.json() == {"id": parent["id"], "deleted": True}

    # The note must resolve to a live tab_id (re-filed to "default", parent had no parent).
    refetched = notebook_client.get(f"/api/notes/{note['id']}").json()
    assert refetched["tab_id"] == "default"

    r = notebook_client.get("/api/tabs")
    assert parent["id"] not in [t["id"] for t in r.json()["tabs"]]


def test_delete_tab_route_unknown_id_returns_404(notebook_client):
    r = notebook_client.delete("/api/tabs/no-such-tab")
    assert r.status_code == 404


def test_patch_note_pinned_placement_and_starred(notebook_client):
    note = notebook_client.post("/api/notes", json={"raw_text": "a"}).json()
    r = notebook_client.patch(
        f"/api/notes/{note['id']}", json={"pinned_placement": True, "starred": True}
    )
    assert r.status_code == 200
    assert r.json()["pinned_placement"] is True
    assert r.json()["starred"] is True


def test_list_notes_starred_filter_route(notebook_client):
    starred = notebook_client.post("/api/notes", json={"raw_text": "a"}).json()
    notebook_client.patch(f"/api/notes/{starred['id']}", json={"starred": True})
    notebook_client.post("/api/notes", json={"raw_text": "b"})

    r = notebook_client.get("/api/notes", params={"starred": "true"})
    assert r.status_code == 200
    ids = [n["id"] for n in r.json()["notes"]]
    assert ids == [starred["id"]]


def test_list_tabs_empty_store(notebook_client):
    r = notebook_client.get("/api/tabs")
    assert r.status_code == 200
    assert r.json() == {"tabs": []}


def test_create_note_defaults_repo(notebook_client):
    r = notebook_client.post("/api/notes", json={"raw_text": "hi"})
    assert r.status_code == 200
    assert r.json()["repo"] == "khimaira"


def test_create_note_repo_override(notebook_client):
    r = notebook_client.post("/api/notes", json={"raw_text": "hi", "repo": "jeevy_portal"})
    assert r.status_code == 200
    assert r.json()["repo"] == "jeevy_portal"


def test_revalidate_note_happy_path(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()

    async def fake_revalidate(note_id):
        assert note_id == created["id"]
        return {**created, "validated_git_sha": "deadbeef"}

    monkeypatch.setattr(notebook_api.notebook_pipeline, "revalidate_note", fake_revalidate)

    r = notebook_client.post(f"/api/notes/{created['id']}/revalidate")
    assert r.status_code == 200
    assert r.json()["validated_git_sha"] == "deadbeef"


def test_revalidate_note_unknown_id_returns_404(notebook_client):
    r = notebook_client.post("/api/notes/no-such-id/revalidate")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# POST /notes/research + POST /notes/{id}/research-revise (Grimoire Phase 3)
# ---------------------------------------------------------------------------


def test_research_route_schedules_job_and_returns_job_id(notebook_client, monkeypatch):
    """Grimoire Phase 4 addendum: /notes/research is ASYNC — it schedules a
    background job and returns {job_id} immediately rather than awaiting
    the (1-2 minute) agentic call inline. See notebook_pipeline's module
    comment for why (systemd KillMode + client-disconnect resilience)."""
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post(
        "/api/notes", json={"raw_text": "# G\n\nbody", "kind": "study_guide"}
    ).json()
    seen = {}

    def fake_schedule(note_id, question, *, max_budget_usd):
        seen["note_id"] = note_id
        seen["question"] = question
        return "job-abc123"

    monkeypatch.setattr(notebook_api.notebook_pipeline, "schedule_research_answer", fake_schedule)
    r = notebook_client.post(
        "/api/notes/research", json={"note_id": note["id"], "question": "what is this?"}
    )
    assert r.status_code == 200
    assert r.json() == {"job_id": "job-abc123", "status": "pending"}
    assert seen == {"note_id": note["id"], "question": "what is this?"}


def test_research_route_unknown_note_returns_404(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    def fake_schedule(note_id, question, *, max_budget_usd):
        raise ValueError(f"No note with id={note_id!r}. Use list_notes() to see available notes.")

    monkeypatch.setattr(notebook_api.notebook_pipeline, "schedule_research_answer", fake_schedule)
    r = notebook_client.post("/api/notes/research", json={"note_id": "nope", "question": "q"})
    assert r.status_code == 404


def test_research_revise_route_schedules_job_and_returns_job_id(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post(
        "/api/notes", json={"raw_text": "# G\n\nbody", "kind": "study_guide"}
    ).json()
    seen = {}

    def fake_schedule(note_id, directive, *, section_anchor, max_budget_usd):
        seen["note_id"] = note_id
        seen["directive"] = directive
        seen["section_anchor"] = section_anchor
        return "job-xyz789"

    monkeypatch.setattr(notebook_api.notebook_pipeline, "schedule_research_revise", fake_schedule)
    r = notebook_client.post(
        f"/api/notes/{note['id']}/research-revise", json={"directive": "improve it"}
    )
    assert r.status_code == 200
    assert r.json() == {"job_id": "job-xyz789", "status": "pending"}
    assert seen == {"note_id": note["id"], "directive": "improve it", "section_anchor": None}


def test_research_revise_route_passes_section_anchor(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post(
        "/api/notes", json={"raw_text": "# G\n\n## A\n\nbody", "kind": "study_guide"}
    ).json()
    seen = {}

    def fake_schedule(note_id, directive, *, section_anchor, max_budget_usd):
        seen["section_anchor"] = section_anchor
        return "job-1"

    monkeypatch.setattr(notebook_api.notebook_pipeline, "schedule_research_revise", fake_schedule)
    notebook_client.post(
        f"/api/notes/{note['id']}/research-revise",
        json={"directive": "improve A", "section_anchor": "a"},
    )
    assert seen["section_anchor"] == "a"


def test_get_research_job_pending(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    monkeypatch.setattr(
        notebook_api.notebook_pipeline, "get_research_job", lambda job_id: {"status": "pending"}
    )
    r = notebook_client.get("/api/notes/research/job-1")
    assert r.status_code == 200
    assert r.json() == {"status": "pending"}


def test_get_research_job_done(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    done = {
        "status": "done",
        "kind": "answer",
        "answer": "the answer",
        "code_citations": [],
        "web_citations": [],
        "proposed_patch": None,
        "web_grounded": True,
        "web_grounding_unverified": False,
        "total_cost_usd": 0.4,
    }
    monkeypatch.setattr(notebook_api.notebook_pipeline, "get_research_job", lambda job_id: done)
    r = notebook_client.get("/api/notes/research/job-1")
    assert r.status_code == 200
    assert r.json() == done


def test_get_research_job_error(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    errored = {"status": "error", "kind": "revise", "error": "boom"}
    monkeypatch.setattr(notebook_api.notebook_pipeline, "get_research_job", lambda job_id: errored)
    r = notebook_client.get("/api/notes/research/job-1")
    assert r.status_code == 200
    assert r.json() == errored


def test_get_research_job_unknown_id_returns_404(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    def fake_get(job_id):
        raise ValueError(
            f"No research job with id={job_id!r} — it may have completed and been cleared."
        )

    monkeypatch.setattr(notebook_api.notebook_pipeline, "get_research_job", fake_get)
    r = notebook_client.get("/api/notes/research/no-such-job")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# CHAT-UNIFY §2 MVP — POST /notes/chat (notebook-wide chat)
# ---------------------------------------------------------------------------


def test_notebook_chat_route_schedules_job_and_returns_job_id(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    seen = {}

    def fake_schedule(message, mentioned_note_ids=None):
        seen["message"] = message
        seen["refs"] = mentioned_note_ids
        return "nb-job-1"

    monkeypatch.setattr(notebook_api.notebook_pipeline, "schedule_notebook_chat", fake_schedule)
    r = notebook_client.post(
        "/api/notes/chat", json={"message": "what changed?", "refs": ["n1", "n2"]}
    )

    assert r.status_code == 200
    assert r.json() == {"job_id": "nb-job-1", "status": "pending"}
    assert seen == {"message": "what changed?", "refs": ["n1", "n2"]}


def test_notebook_chat_route_defaults_refs_to_empty_list(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    seen = {}

    def fake_schedule(message, mentioned_note_ids=None):
        seen["refs"] = mentioned_note_ids
        return "nb-job-2"

    monkeypatch.setattr(notebook_api.notebook_pipeline, "schedule_notebook_chat", fake_schedule)
    r = notebook_client.post("/api/notes/chat", json={"message": "anything"})

    assert r.status_code == 200
    assert seen["refs"] == []


def test_notebook_chat_route_is_pollable_via_shared_job_route(notebook_client, monkeypatch):
    """The whole point of reusing the shared job store: a job scheduled by
    POST /notes/chat is pollable at the SAME GET /notes/research/{job_id}
    route research/per-record chat already use."""
    from khimaira.monitor.api import notebook as notebook_api

    monkeypatch.setattr(
        notebook_api.notebook_pipeline, "schedule_notebook_chat", lambda message, refs=None: "nb-1"
    )
    r = notebook_client.post("/api/notes/chat", json={"message": "hi"})
    job_id = r.json()["job_id"]

    done = {"status": "done", "kind": "notebook_chat", "answer": "the answer", "sources": []}
    monkeypatch.setattr(notebook_api.notebook_pipeline, "get_research_job", lambda jid: done)
    poll = notebook_client.get(f"/api/notes/research/{job_id}")

    assert poll.status_code == 200
    assert poll.json() == done


def test_export_note_route_happy_path(notebook_client, tmp_path):
    src = tmp_path / "guide.md"
    src.write_text("original")
    note = notebook_client.post(
        "/api/notes",
        json={"raw_text": "original", "kind": "study_guide", "source_path": str(src)},
    ).json()

    r = notebook_client.post(f"/api/notes/{note['id']}/export", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["path"] == str(src)
    assert src.read_text() == "original"


def test_export_note_route_explicit_path(notebook_client, tmp_path):
    note = notebook_client.post(
        "/api/notes", json={"raw_text": "body", "kind": "study_guide"}
    ).json()
    target = tmp_path / "exported.md"

    r = notebook_client.post(f"/api/notes/{note['id']}/export", json={"path": str(target)})
    assert r.status_code == 200
    assert target.read_text() == "body"


def test_export_note_route_non_guide_returns_404(notebook_client):
    note = notebook_client.post("/api/notes", json={"raw_text": "just a note"}).json()
    r = notebook_client.post(f"/api/notes/{note['id']}/export", json={})
    assert r.status_code == 404


def test_export_note_route_unknown_id_returns_404(notebook_client):
    r = notebook_client.post("/api/notes/no-such-id/export", json={})
    assert r.status_code == 404


def test_research_revise_route_unknown_section_anchor_returns_404(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post(
        "/api/notes", json={"raw_text": "# G\n\nbody", "kind": "study_guide"}
    ).json()

    def fake_schedule(note_id, directive, *, section_anchor, max_budget_usd):
        raise ValueError(
            f"No section anchored at {section_anchor!r} in this guide's current raw_text."
        )

    monkeypatch.setattr(notebook_api.notebook_pipeline, "schedule_research_revise", fake_schedule)
    r = notebook_client.post(
        f"/api/notes/{note['id']}/research-revise",
        json={"directive": "x", "section_anchor": "nope"},
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# GET /notes/search (repo filter — v2 addition, needed by notebook_search
# MCP tool; search_notes_async already supported repo=, the route just
# never forwarded it)
# ---------------------------------------------------------------------------


def test_search_notes_forwards_repo_filter(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    captured_kwargs: dict = {}

    async def fake_search(query, **kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(notebook_api.notebook_retrieval, "search_notes_async", fake_search)

    r = notebook_client.get("/api/notes/search", params={"q": "race", "repo": "jeevy_portal"})
    assert r.status_code == 200
    assert captured_kwargs.get("repo") == "jeevy_portal"


def test_search_notes_repo_omitted_defaults_to_none(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    captured_kwargs: dict = {}

    async def fake_search(query, **kwargs):
        captured_kwargs.update(kwargs)
        return []

    monkeypatch.setattr(notebook_api.notebook_retrieval, "search_notes_async", fake_search)

    r = notebook_client.get("/api/notes/search", params={"q": "race"})
    assert r.status_code == 200
    assert captured_kwargs.get("repo") is None


# ---------------------------------------------------------------------------
# POST /notes/{id}/resolution (v2 roster loop)
# ---------------------------------------------------------------------------


def test_add_resolution_happy_path(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.post(
        f"/api/notes/{created['id']}/resolution",
        json={"resolution": "fixed it", "resolved_by": "agent-1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["resolution"] == "fixed it"
    assert body["resolved_by"] == "agent-1"
    assert body["resolved_at"] is not None

    refetched = notebook_client.get(f"/api/notes/{created['id']}").json()
    assert refetched["resolution"] == "fixed it"


def test_add_resolution_unknown_id_returns_404(notebook_client):
    r = notebook_client.post("/api/notes/no-such-id/resolution", json={"resolution": "fixed it"})
    assert r.status_code == 404
    assert "no note with id" in r.json()["detail"].lower()


def test_add_resolution_schedules_training_promote(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    scheduled: list[dict] = []
    monkeypatch.setattr(notebook_api.notebook_training, "schedule_promote", scheduled.append)

    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.post(
        f"/api/notes/{created['id']}/resolution", json={"resolution": "fixed it"}
    )
    assert r.status_code == 200
    assert len(scheduled) == 1
    assert scheduled[0]["id"] == created["id"]


def test_add_resolution_empty_string_does_not_schedule_training_promote(
    notebook_client, monkeypatch
):
    from khimaira.monitor.api import notebook as notebook_api

    scheduled: list[dict] = []
    monkeypatch.setattr(notebook_api.notebook_training, "schedule_promote", scheduled.append)

    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.post(f"/api/notes/{created['id']}/resolution", json={"resolution": ""})
    assert r.status_code == 200
    assert scheduled == []


def test_add_resolution_on_ticket_does_not_schedule_training_promote(notebook_client, monkeypatch):
    """Tickets (2026-07-11) are excluded from training promotion even when
    resolved — a ticket resolution is a work-tracking closure, not a
    curated {problem, solution} pair, and linear-pulled tickets can carry
    team/project-internal text never meant for the oracle corpus. See
    chat-102d8b5fd82f task-8191e0a1672b."""
    from khimaira.monitor.api import notebook as notebook_api

    scheduled: list[dict] = []
    monkeypatch.setattr(notebook_api.notebook_training, "schedule_promote", scheduled.append)

    ticket = notebook_client.post("/api/tickets", json={"title": "Fix the reaper race"}).json()
    r = notebook_client.post(f"/api/notes/{ticket['id']}/resolution", json={"resolution": "fixed in PR #123"})
    assert r.status_code == 200
    assert r.json()["resolution"] == "fixed in PR #123"
    assert scheduled == [], "a resolved ticket must never enter the training-promotion path"


def test_import_guides_route_dry_run_default_writes_nothing(notebook_client, tmp_path):
    (tmp_path / "onboarding").mkdir()
    (tmp_path / "onboarding" / "start.md").write_text("# Start\n\nwelcome")

    r = notebook_client.post("/api/notes/import", json={"root": str(tmp_path)})
    assert r.status_code == 200
    body = r.json()
    assert body["imported"] == []
    assert len(body["manifest"]) == 1
    assert body["manifest"][0]["status"] == "would_import"
    assert body["manifest"][0]["collection"] == "Onboarding"

    listed = notebook_client.get("/api/notes", params={"kind": "study_guide"}).json()["notes"]
    assert listed == []


def test_import_guides_route_real_import(notebook_client, tmp_path, monkeypatch):
    from khimaira.monitor import notebook_pipeline as pipeline_mod

    monkeypatch.setattr(pipeline_mod, "schedule_pipeline", lambda note_id: None)

    (tmp_path / "onboarding").mkdir()
    (tmp_path / "onboarding" / "start.md").write_text("# Start\n\nwelcome")

    r = notebook_client.post(
        "/api/notes/import", json={"root": str(tmp_path), "repo": "jeevy_portal", "dry_run": False}
    )
    assert r.status_code == 200
    body = r.json()
    assert len(body["imported"]) == 1

    listed = notebook_client.get("/api/notes", params={"kind": "study_guide"}).json()["notes"]
    assert len(listed) == 1
    assert listed[0]["repo"] == "jeevy_portal"
    assert listed[0]["organized_at"] is not None


# ---------------------------------------------------------------------------
# Chat model (Grimoire chat-model addendum, 2026-07-04)
# ---------------------------------------------------------------------------


def test_chat_route_schedules_job_and_returns_job_id(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post(
        "/api/notes", json={"raw_text": "# G\n\nbody", "kind": "study_guide"}
    ).json()
    seen = {}

    def fake_schedule(note_id, message, *, max_budget_usd):
        seen["note_id"] = note_id
        seen["message"] = message
        return "chat-job-1"

    monkeypatch.setattr(notebook_api.notebook_chat, "schedule_chat_turn", fake_schedule)
    r = notebook_client.post(f"/api/notes/{note['id']}/chat", json={"message": "what is this?"})

    assert r.status_code == 200
    assert r.json() == {"job_id": "chat-job-1", "status": "pending"}
    assert seen == {"note_id": note["id"], "message": "what is this?"}


def test_chat_route_works_for_regular_notes(notebook_client, monkeypatch):
    """CHAT-UNIFY (2026-07-04): chat is no longer guide-only — a regular
    note schedules a chat job exactly like a guide does."""
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post("/api/notes", json={"raw_text": "just a note"}).json()
    seen = {}

    def fake_schedule(note_id, message, *, max_budget_usd):
        seen["note_id"] = note_id
        return "chat-job-2"

    monkeypatch.setattr(notebook_api.notebook_chat, "schedule_chat_turn", fake_schedule)
    r = notebook_client.post(f"/api/notes/{note['id']}/chat", json={"message": "hi"})

    assert r.status_code == 200
    assert r.json() == {"job_id": "chat-job-2", "status": "pending"}
    assert seen == {"note_id": note["id"]}


def test_chat_route_unknown_note_returns_404(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    def fake_schedule(note_id, message, *, max_budget_usd):
        raise ValueError(f"No note with id={note_id!r}.")

    monkeypatch.setattr(notebook_api.notebook_chat, "schedule_chat_turn", fake_schedule)
    r = notebook_client.post("/api/notes/no-such-id/chat", json={"message": "hi"})
    assert r.status_code == 404


def test_get_chat_route_returns_history(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post(
        "/api/notes", json={"raw_text": "# G\n\nbody", "kind": "study_guide"}
    ).json()
    history = [{"role": "user", "content": "hi", "ts": "t1"}]
    monkeypatch.setattr(notebook_api.notebook_chat, "get_chat_history", lambda nid: history)

    r = notebook_client.get(f"/api/notes/{note['id']}/chat")
    assert r.status_code == 200
    assert r.json() == {"history": history}


def test_get_chat_route_unknown_note_returns_404(notebook_client):
    r = notebook_client.get("/api/notes/no-such-id/chat")
    assert r.status_code == 404


def test_chat_clear_route(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post(
        "/api/notes", json={"raw_text": "# G\n\nbody", "kind": "study_guide"}
    ).json()
    seen = []
    monkeypatch.setattr(
        notebook_api.notebook_chat,
        "clear_chat",
        lambda nid: seen.append(nid) or {"cleared": True},
    )

    r = notebook_client.post(f"/api/notes/{note['id']}/chat/clear")
    assert r.status_code == 200
    assert r.json() == {"cleared": True}
    assert seen == [note["id"]]


def test_chat_clear_route_unknown_note_returns_404(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    def fake_clear(note_id):
        raise ValueError(f"No note with id={note_id!r}.")

    monkeypatch.setattr(notebook_api.notebook_chat, "clear_chat", fake_clear)
    r = notebook_client.post("/api/notes/no-such-id/chat/clear")
    assert r.status_code == 404


def test_chat_compact_route(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    note = notebook_client.post(
        "/api/notes", json={"raw_text": "# G\n\nbody", "kind": "study_guide"}
    ).json()

    async def fake_compact(note_id):
        return {"compacted": True, "message_count": 5}

    monkeypatch.setattr(notebook_api.notebook_chat, "compact_chat_history", fake_compact)
    r = notebook_client.post(f"/api/notes/{note['id']}/chat/compact")
    assert r.status_code == 200
    assert r.json() == {"compacted": True, "message_count": 5}


def test_chat_compact_route_unknown_note_returns_404(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    async def failing_compact(note_id):
        raise ValueError(f"No note with id={note_id!r}.")

    monkeypatch.setattr(notebook_api.notebook_chat, "compact_chat_history", failing_compact)
    r = notebook_client.post("/api/notes/no-such-id/chat/compact")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Sensitive notes + priority flags (2026-07-04)
# ---------------------------------------------------------------------------


def test_create_note_sensitive_computes_redaction(notebook_client):
    secret = "sk-ant-" + "a" * 30
    r = notebook_client.post("/api/notes", json={"raw_text": f"key: {secret}", "sensitive": True})
    assert r.status_code == 200
    body = r.json()
    assert body["sensitive"] is True
    assert secret not in body["llm_text"]
    assert body["raw_text"] == f"key: {secret}"  # single-note fetch: real content


def test_create_note_defaults_not_sensitive(notebook_client):
    r = notebook_client.post("/api/notes", json={"raw_text": "hello"})
    assert r.json()["sensitive"] is False


def test_create_study_guide_sensitive(notebook_client):
    secret = "sk-ant-" + "b" * 30
    r = notebook_client.post(
        "/api/notes",
        json={"raw_text": f"# G\n\nkey: {secret}", "kind": "study_guide", "sensitive": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["sensitive"] is True
    assert secret not in body["llm_text"]


def test_list_notes_masks_raw_text_for_sensitive_notes(notebook_client):
    secret = "sk-ant-" + "c" * 30
    notebook_client.post("/api/notes", json={"raw_text": f"key: {secret}", "sensitive": True})

    listed = notebook_client.get("/api/notes").json()["notes"]
    assert len(listed) == 1
    assert secret not in listed[0]["raw_text"]
    assert secret not in listed[0]["title"]  # auto-derived title also safe


def test_get_note_returns_real_raw_text_for_sensitive_note(notebook_client):
    secret = "sk-ant-" + "d" * 30
    created = notebook_client.post(
        "/api/notes", json={"raw_text": f"key: {secret}", "sensitive": True}
    ).json()

    fetched = notebook_client.get(f"/api/notes/{created['id']}").json()
    assert fetched["raw_text"] == f"key: {secret}"


def test_patch_note_can_flip_sensitive_on(notebook_client):
    secret = "sk-ant-" + "e" * 30
    created = notebook_client.post("/api/notes", json={"raw_text": f"key: {secret}"}).json()
    assert created["sensitive"] is False

    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"sensitive": True})
    assert r.status_code == 200
    body = r.json()
    assert body["sensitive"] is True
    assert secret not in body["llm_text"]


def test_patch_note_priority_round_trips(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    assert created["priority"] == "normal"

    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"priority": "urgent"})
    assert r.status_code == 200
    assert r.json()["priority"] == "urgent"


def test_patch_note_invalid_priority_returns_422(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"priority": "critical"})
    assert r.status_code == 422


def test_list_notes_priority_filter_route(notebook_client):
    notebook_client.post("/api/notes", json={"raw_text": "a"})
    urgent = notebook_client.post("/api/notes", json={"raw_text": "b"}).json()
    notebook_client.patch(f"/api/notes/{urgent['id']}", json={"priority": "urgent"})

    filtered = notebook_client.get("/api/notes", params={"priority": "urgent"}).json()["notes"]
    assert len(filtered) == 1
    assert filtered[0]["id"] == urgent["id"]


def test_list_notes_sort_by_priority_descending(notebook_client):
    low = notebook_client.post("/api/notes", json={"raw_text": "low one"}).json()
    notebook_client.patch(f"/api/notes/{low['id']}", json={"priority": "low"})
    urgent = notebook_client.post("/api/notes", json={"raw_text": "urgent one"}).json()
    notebook_client.patch(f"/api/notes/{urgent['id']}", json={"priority": "urgent"})
    notebook_client.post("/api/notes", json={"raw_text": "normal one"})

    sorted_notes = notebook_client.get("/api/notes", params={"sort": "-priority"}).json()["notes"]
    priorities = [n["priority"] for n in sorted_notes]
    assert priorities == ["urgent", "normal", "low"]

    ascending = notebook_client.get("/api/notes", params={"sort": "priority"}).json()["notes"]
    assert [n["priority"] for n in ascending] == ["low", "normal", "urgent"]


# ---------------------------------------------------------------------------
# Testing-workflow status (2026-07-07)
# ---------------------------------------------------------------------------


def test_patch_note_test_status_round_trips(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    assert created["test_status"] == "untested"

    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"test_status": "tested"})
    assert r.status_code == 200
    assert r.json()["test_status"] == "tested"


def test_patch_note_invalid_test_status_returns_422(notebook_client):
    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"test_status": "done"})
    assert r.status_code == 422


def test_list_notes_test_status_filter_route(notebook_client):
    notebook_client.post("/api/notes", json={"raw_text": "a"})
    tested = notebook_client.post("/api/notes", json={"raw_text": "b"}).json()
    notebook_client.patch(f"/api/notes/{tested['id']}", json={"test_status": "tested"})

    filtered = notebook_client.get("/api/notes", params={"test_status": "tested"}).json()["notes"]
    assert len(filtered) == 1
    assert filtered[0]["id"] == tested["id"]


def test_promote_note_route_refuses_sensitive_notes(notebook_client):
    created = notebook_client.post(
        "/api/notes", json={"raw_text": "secret stuff", "sensitive": True}
    ).json()
    r = notebook_client.post(f"/api/notes/{created['id']}/promote")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Event-loop-blocking regression (2026-07-07) — every notes.py-touching route
# below must run its synchronous file I/O via asyncio.to_thread. Reported
# symptom: notebook_add_resolution/notebook_update calls intermittently
# returned "timed out" even though the write had already landed — a classic
# blocked-event-loop signature (see performance.md: "No blocking I/O in
# async handlers"). These tests assert a SLOW synchronous notes.py call does
# NOT stall a concurrent, otherwise-instant request on the same app —
# proving the blocking work actually left the event loop, not just that the
# route eventually returns the right value (every other test here already
# covers correctness).
# ---------------------------------------------------------------------------


@pytest.fixture
async def async_notebook_client(isolated_state, monkeypatch):
    import importlib

    import httpx
    from fastapi import FastAPI
    from khimaira.monitor import notebook_pipeline as pipeline_mod
    from khimaira.monitor import notebook_training as training_mod
    from khimaira.monitor import notes as notes_mod
    from khimaira.monitor.api import notebook as notebook_api

    importlib.reload(notes_mod)
    importlib.reload(pipeline_mod)
    importlib.reload(training_mod)
    importlib.reload(notebook_api)
    monkeypatch.setattr(notebook_api.notebook_training, "schedule_promote", lambda record: None)

    app = FastAPI()
    app.include_router(notebook_api.build_router(), prefix="/api")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client, notebook_api


class TestEventLoopNotBlocked:
    """Methodology note: the timer MUST start before either task is created,
    and both tasks MUST be created back-to-back with no intervening `await`.
    An earlier draft of these tests used `asyncio.create_task(slow)` then
    `await asyncio.sleep(0.05)` as a "head start" before timing the fast
    request — that's wrong and produces a false pass: `asyncio.sleep`'s own
    resumption ALSO needs the event loop, so if the slow task's raw
    `time.sleep` grabs the thread first, the sleep(0.05) call doesn't
    return until the block already cleared — meaning the timer starts
    AFTER the blocking already happened, measuring nothing. Verified this
    exact failure mode with a minimal repro before rewriting these tests
    (a bare `time.sleep()` route "passed" the old-style test at 0.001s
    elapsed — clearly wrong for something with an 0.5s blocking sleep in
    its path)."""

    async def test_slow_update_note_does_not_stall_concurrent_get(self, async_notebook_client):
        import asyncio
        import time

        client, notebook_api = async_notebook_client
        created = (await client.post("/api/notes", json={"raw_text": "hi"})).json()
        other = (await client.post("/api/notes", json={"raw_text": "unrelated"})).json()

        real_update_note = notebook_api.notes.update_note

        def slow_update_note(note_id, **fields):
            time.sleep(0.5)  # a REAL blocking sleep — simulates slow disk I/O
            return real_update_note(note_id, **fields)

        original = notebook_api.notes.update_note
        notebook_api.notes.update_note = slow_update_note
        try:
            fast_start = time.monotonic()  # captured BEFORE either task exists
            slow_task = asyncio.create_task(
                client.patch(f"/api/notes/{created['id']}", json={"title": "renamed"})
            )
            fast_task = asyncio.create_task(client.get(f"/api/notes/{other['id']}"))

            fast_resp = await fast_task
            fast_elapsed = time.monotonic() - fast_start

            assert fast_resp.status_code == 200
            # The whole point: a concurrent GET must return in well under the
            # slow PATCH's 0.5s sleep — if the event loop were blocked (the
            # pre-fix bug), this GET would queue behind the sleep instead.
            assert fast_elapsed < 0.3

            slow_resp = await slow_task
            assert slow_resp.status_code == 200
        finally:
            notebook_api.notes.update_note = original

    async def test_slow_add_resolution_does_not_stall_concurrent_get(self, async_notebook_client):
        import asyncio
        import time

        client, notebook_api = async_notebook_client
        created = (await client.post("/api/notes", json={"raw_text": "hi"})).json()
        other = (await client.post("/api/notes", json={"raw_text": "unrelated"})).json()

        real_add_resolution = notebook_api.notes.add_resolution

        def slow_add_resolution(note_id, resolution, resolved_by=""):
            time.sleep(0.5)
            return real_add_resolution(note_id, resolution, resolved_by=resolved_by)

        original = notebook_api.notes.add_resolution
        notebook_api.notes.add_resolution = slow_add_resolution
        try:
            fast_start = time.monotonic()  # captured BEFORE either task exists
            slow_task = asyncio.create_task(
                client.post(f"/api/notes/{created['id']}/resolution", json={"resolution": "done"})
            )
            fast_task = asyncio.create_task(client.get(f"/api/notes/{other['id']}"))

            fast_resp = await fast_task
            fast_elapsed = time.monotonic() - fast_start

            assert fast_resp.status_code == 200
            assert fast_elapsed < 0.3

            slow_resp = await slow_task
            assert slow_resp.status_code == 200
        finally:
            notebook_api.notes.add_resolution = original


# ---------------------------------------------------------------------------
# Tickets (2026-07-11) — /api/tickets*
# ---------------------------------------------------------------------------


def test_create_ticket_is_local_created(notebook_client):
    r = notebook_client.post("/api/tickets", json={"title": "Fix the reaper race"})
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "ticket"
    assert body["origin"] == "local-created"
    assert body["state"] == "Backlog"


def test_list_tickets_project_filter(notebook_client):
    notebook_client.post("/api/tickets", json={"title": "A", "project": "Langgraph"})
    notebook_client.post("/api/tickets", json={"title": "B", "project": "Other"})

    r = notebook_client.get("/api/tickets", params={"project": "Langgraph"})
    assert r.status_code == 200
    tickets = r.json()["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["title"] == "A"


def test_get_ticket_404_for_non_ticket_note(notebook_client):
    note = notebook_client.post("/api/notes", json={"raw_text": "a plain note"}).json()
    r = notebook_client.get(f"/api/tickets/{note['id']}")
    assert r.status_code == 404


def test_get_ticket_404_for_unknown_id(notebook_client):
    r = notebook_client.get("/api/tickets/does-not-exist")
    assert r.status_code == 404


def test_update_ticket_local_created_succeeds(notebook_client):
    created = notebook_client.post("/api/tickets", json={"title": "Local"}).json()
    r = notebook_client.patch(f"/api/tickets/{created['id']}", json={"state": "In Progress"})
    assert r.status_code == 200
    assert r.json()["state"] == "In Progress"


def test_update_ticket_rejects_synced_field_on_linear_pulled(notebook_client):
    upsert = notebook_client.post(
        "/api/tickets/bulk-upsert",
        json={"project": "Langgraph", "tickets": [{"linear_ref": "LIN-1", "title": "Mirrored"}]},
    )
    assert upsert.status_code == 200
    tickets = notebook_client.get("/api/tickets", params={"project": "Langgraph"}).json()["tickets"]
    ticket_id = tickets[0]["id"]

    r = notebook_client.patch(f"/api/tickets/{ticket_id}", json={"title": "local rename attempt"})
    assert r.status_code == 422


def test_add_ticket_comment(notebook_client):
    created = notebook_client.post("/api/tickets", json={"title": "X"}).json()
    r = notebook_client.post(
        f"/api/tickets/{created['id']}/comments", json={"text": "looks good", "author": "reviewer"}
    )
    assert r.status_code == 200
    assert len(r.json()["comments"]) == 1


def test_bulk_upsert_tickets_is_idempotent(notebook_client):
    """The resync idempotency invariant at the HTTP layer: running the same
    linear_ref twice must never create a duplicate ticket, and the summary
    counts must reflect create-then-update, not create-then-create."""
    payload = {
        "project": "Langgraph",
        "tickets": [{"linear_ref": "LIN-100", "title": "Original", "state": "Backlog"}],
    }
    first = notebook_client.post("/api/tickets/bulk-upsert", json=payload)
    assert first.status_code == 200
    assert first.json() == {"pulled": 1, "created": 1, "updated": 0}

    payload["tickets"][0]["title"] = "Renamed upstream"
    payload["tickets"][0]["state"] = "In Progress"
    second = notebook_client.post("/api/tickets/bulk-upsert", json=payload)
    assert second.status_code == 200
    assert second.json() == {"pulled": 1, "created": 0, "updated": 1}

    tickets = notebook_client.get("/api/tickets", params={"project": "Langgraph"}).json()["tickets"]
    assert len(tickets) == 1
    assert tickets[0]["title"] == "Renamed upstream"
    assert tickets[0]["state"] == "In Progress"


def test_bulk_upsert_tickets_rejects_missing_linear_ref(notebook_client):
    r = notebook_client.post(
        "/api/tickets/bulk-upsert",
        json={"project": "Langgraph", "tickets": [{"title": "no ref"}]},
    )
    assert r.status_code == 422


# ---------------------------------------------------------------------------
# Idempotency-key chokepoint (2026-07-11, task-6f8d92534e3d) — the class fix
# for chimera-0's duplicate-study-guide report: a client-side timeout can
# land the write server-side anyway; the retry must not create a second
# record. These tests prove the CLASS (same key -> same record, across all
# four vulnerable routes), not just a config-shape check.
# ---------------------------------------------------------------------------


def test_create_note_idempotency_key_dedupes(notebook_client):
    key = "attempt-abc-123"
    first = notebook_client.post("/api/notes", json={"raw_text": "hello", "idempotency_key": key})
    second = notebook_client.post("/api/notes", json={"raw_text": "hello", "idempotency_key": key})
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"], "same idempotency_key must return the same note, not a duplicate"

    all_notes = notebook_client.get("/api/notes").json()["notes"]
    assert len(all_notes) == 1, "a retried create with the same key must never produce a second record"


def test_create_note_without_idempotency_key_is_unchanged(notebook_client):
    """Omitting the key preserves today's behavior — every call creates a
    genuinely new note. Guards against the chokepoint accidentally becoming
    the new default (heuristic dedup was explicitly rejected)."""
    first = notebook_client.post("/api/notes", json={"raw_text": "hello"})
    second = notebook_client.post("/api/notes", json={"raw_text": "hello"})
    assert first.json()["id"] != second.json()["id"]

    all_notes = notebook_client.get("/api/notes").json()["notes"]
    assert len(all_notes) == 2


def test_create_note_idempotency_key_does_not_double_schedule_pipeline(notebook_client, monkeypatch):
    """The cache-hit path must skip trigger_pipeline/schedule_upsert too —
    not just avoid a duplicate record — since those call asyncio.create_task
    and re-firing them on every retry would waste an LLM structuring pass
    per retry."""
    from khimaira.monitor.api import notebook as notebook_api

    triggered: list[str] = []
    monkeypatch.setattr(notebook_api, "trigger_pipeline", lambda note_id: triggered.append(note_id))

    key = "attempt-pipeline-1"
    notebook_client.post("/api/notes", json={"raw_text": "hello", "idempotency_key": key})
    notebook_client.post("/api/notes", json={"raw_text": "hello", "idempotency_key": key})
    assert len(triggered) == 1, "a deduped retry must not re-trigger the structuring pipeline"


def test_create_study_guide_idempotency_key_dedupes(notebook_client):
    key = "attempt-guide-1"
    body = {"raw_text": "# Guide\n\nbody", "kind": "study_guide", "idempotency_key": key}
    first = notebook_client.post("/api/notes", json=body)
    second = notebook_client.post("/api/notes", json=body)
    assert first.json()["id"] == second.json()["id"]

    guides = notebook_client.get("/api/notes", params={"kind": "study_guide"}).json()["notes"]
    assert len(guides) == 1


def test_add_resolution_idempotency_key_dedupes_and_skips_second_promote(notebook_client, monkeypatch):
    from khimaira.monitor.api import notebook as notebook_api

    scheduled: list[dict] = []
    monkeypatch.setattr(notebook_api.notebook_training, "schedule_promote", scheduled.append)

    created = notebook_client.post("/api/notes", json={"raw_text": "hi"}).json()
    key = "attempt-resolve-1"
    body = {"resolution": "fixed it", "idempotency_key": key}
    first = notebook_client.post(f"/api/notes/{created['id']}/resolution", json=body)
    second = notebook_client.post(f"/api/notes/{created['id']}/resolution", json=body)

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json() == second.json()
    assert len(scheduled) == 1, "a deduped retry must not re-schedule a redundant training distill"


def test_create_ticket_idempotency_key_dedupes(notebook_client):
    key = "attempt-ticket-1"
    body = {"title": "Fix the reaper race", "idempotency_key": key}
    first = notebook_client.post("/api/tickets", json=body)
    second = notebook_client.post("/api/tickets", json=body)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["id"] == second.json()["id"]

    tickets = notebook_client.get("/api/tickets").json()["tickets"]
    assert len(tickets) == 1, "a retried ticket create with the same key must never produce a duplicate"


def test_different_idempotency_keys_create_separate_records(notebook_client):
    """Distinct keys are genuinely distinct creation attempts — the guard
    must not over-dedup two legitimately different creates."""
    first = notebook_client.post("/api/tickets", json={"title": "A", "idempotency_key": "key-a"})
    second = notebook_client.post("/api/tickets", json={"title": "B", "idempotency_key": "key-b"})
    assert first.json()["id"] != second.json()["id"]

    tickets = notebook_client.get("/api/tickets").json()["tickets"]
    assert len(tickets) == 2
