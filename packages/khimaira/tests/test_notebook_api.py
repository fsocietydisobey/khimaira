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
    otherwise). Regression for the notebook_update-doesn't-reprocess footgun."""
    from khimaira.monitor.api import notebook as notebook_api

    calls: list[str] = []
    monkeypatch.setattr(notebook_api, "trigger_pipeline", lambda nid: calls.append(nid))
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
    monkeypatch.setattr(notebook_api, "trigger_pipeline", lambda nid: calls.append(nid))
    created = notebook_client.post("/api/notes", json={"raw_text": "original"}).json()
    calls.clear()

    r = notebook_client.patch(f"/api/notes/{created['id']}", json={"title": "renamed"})
    assert r.status_code == 200
    assert calls == []


def test_patch_raw_text_on_personal_note_does_not_reprocess(notebook_client, monkeypatch):
    """Personal-tab notes are read raw (never structured), mirroring create —
    a raw_text edit on one must not schedule a pipeline run."""
    from khimaira.monitor.api import notebook as notebook_api

    calls: list[str] = []
    monkeypatch.setattr(notebook_api, "trigger_pipeline", lambda nid: calls.append(nid))
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
