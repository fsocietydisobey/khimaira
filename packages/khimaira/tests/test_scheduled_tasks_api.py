"""HTTP API tests for /api/scheduled-tasks.

Per khimaira CLAUDE.md rule: every endpoint gets a happy-path + an
unhappy-path test, including unknown-id 404s and the 409 cancel race.
"""

from __future__ import annotations

import importlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest


@pytest.fixture
def api_client_scheduler(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """FastAPI TestClient mounting only the scheduled-tasks router on
    isolated XDG state."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import scheduler as scheduler_mod

    importlib.reload(scheduler_mod)
    from khimaira.monitor.api import scheduled_tasks as api_mod

    importlib.reload(api_mod)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_mod.build_router(), prefix="/api")
    client = TestClient(app)
    # Plant a session for the happy-path tests.
    sd = sessions_mod._session_dir("target-api")
    (sd / "status.json").write_text(
        json.dumps({"status": "implementing", "detail": ""}), encoding="utf-8"
    )
    yield client, scheduler_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(scheduler_mod)
    importlib.reload(api_mod)


def test_post_creates_task(api_client_scheduler):
    client, scheduler_mod = api_client_scheduler
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    resp = client.post(
        "/api/scheduled-tasks",
        json={
            "target_session": "target-api",
            "fire_at_utc": fire_at,
            "prompt": "do the thing",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == scheduler_mod.SCHEDULED
    assert body["id"].startswith("task-")


def test_post_unknown_target_returns_404(api_client_scheduler):
    client, _ = api_client_scheduler
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    resp = client.post(
        "/api/scheduled-tasks",
        json={
            "target_session": "definitely-not-a-real-session",
            "fire_at_utc": fire_at,
            "prompt": "fails",
        },
    )
    assert resp.status_code == 404


def test_get_list_returns_created_tasks(api_client_scheduler):
    client, _ = api_client_scheduler
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    client.post(
        "/api/scheduled-tasks",
        json={
            "target_session": "target-api",
            "fire_at_utc": fire_at,
            "prompt": "one",
        },
    )
    resp = client.get("/api/scheduled-tasks")
    assert resp.status_code == 200
    body = resp.json()
    assert "tasks" in body
    assert len(body["tasks"]) == 1


def test_get_list_filters_by_status(api_client_scheduler):
    client, _ = api_client_scheduler
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    client.post(
        "/api/scheduled-tasks",
        json={
            "target_session": "target-api",
            "fire_at_utc": fire_at,
            "prompt": "one",
        },
    )
    resp = client.get("/api/scheduled-tasks?status=fired")
    assert resp.status_code == 200
    assert resp.json()["tasks"] == []


def test_get_one_returns_task(api_client_scheduler):
    client, _ = api_client_scheduler
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    created = client.post(
        "/api/scheduled-tasks",
        json={
            "target_session": "target-api",
            "fire_at_utc": fire_at,
            "prompt": "one",
        },
    ).json()

    resp = client.get(f"/api/scheduled-tasks/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["id"] == created["id"]


def test_get_unknown_returns_404(api_client_scheduler):
    client, _ = api_client_scheduler
    resp = client.get("/api/scheduled-tasks/task-doesnotexist")
    assert resp.status_code == 404


def test_delete_cancels_scheduled_task(api_client_scheduler):
    client, scheduler_mod = api_client_scheduler
    fire_at = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    created = client.post(
        "/api/scheduled-tasks",
        json={
            "target_session": "target-api",
            "fire_at_utc": fire_at,
            "prompt": "cancel me",
        },
    ).json()

    resp = client.delete(f"/api/scheduled-tasks/{created['id']}")
    assert resp.status_code == 200
    assert resp.json()["status"] == scheduler_mod.CANCELLED


def test_delete_unknown_returns_404(api_client_scheduler):
    client, _ = api_client_scheduler
    resp = client.delete("/api/scheduled-tasks/task-doesnotexist")
    assert resp.status_code == 404


def test_delete_firing_returns_409(api_client_scheduler):
    client, scheduler_mod = api_client_scheduler
    # Hand-craft a firing task in the JSONL directly.
    from khimaira.monitor import sessions as sessions_mod

    sd = sessions_mod._session_dir("target-api")
    sd.mkdir(parents=True, exist_ok=True)
    fresh_ts = datetime.now(UTC).isoformat()
    firing = {
        "id": "task-firingApi0001",
        "target_session_name": "target-api",
        "target_session_id": "target-api",
        "fire_at_utc": fresh_ts,
        "prompt": "in flight",
        "retry_policy": {"max_attempts": 1, "retry_after_seconds": 300},
        "status": scheduler_mod.FIRING,
        "created_at": fresh_ts,
        "expires_at": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        "attempts": [{"ts": fresh_ts, "outcome": "firing", "detail": "running"}],
    }
    scheduler_mod._append(firing)

    resp = client.delete("/api/scheduled-tasks/task-firingApi0001")
    assert resp.status_code == 409
