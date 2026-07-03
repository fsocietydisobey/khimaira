"""HTTP-layer tests for /api/notes + /api/tabs (Phase 1a)."""

from __future__ import annotations

import importlib

import pytest


@pytest.fixture
def notebook_client(isolated_state, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from khimaira.monitor import notes as notes_mod
    from khimaira.monitor.api import notebook as notebook_api

    importlib.reload(notes_mod)
    importlib.reload(notebook_api)

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
