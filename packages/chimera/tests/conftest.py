"""Shared pytest fixtures for chimera package tests.

Most fixtures here isolate state on disk so tests don't pollute the
user's real ~/.local/state/chimera/ tree. They also let multiple test
runs proceed in parallel without colliding.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root chimera's state dir at a tmp_path for the test's lifetime.

    Sets XDG_STATE_HOME so chimera.monitor.sessions._BASE_DIR resolves
    to tmp_path/<state_subpath>/sessions. Reloads the sessions module
    after the env var is set so the module-level _BASE_DIR is computed
    against the new XDG_STATE_HOME.
    """
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    # Reload so module-level constants pick up the new env var
    from chimera.monitor import sessions as sessions_mod
    importlib.reload(sessions_mod)
    yield sessions_mod
    # Reload again after test so subsequent tests / non-test code
    # see the original path
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)


@pytest.fixture
def api_client(isolated_state, monkeypatch: pytest.MonkeyPatch):
    """FastAPI TestClient for /api/sessions endpoints, on isolated state.

    Reloads the API router module too so it picks up the reloaded
    sessions module's _BASE_DIR.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from chimera.monitor.api import sessions as api_sessions
    importlib.reload(api_sessions)

    app = FastAPI()
    app.include_router(api_sessions.build_router(), prefix="/api")
    return TestClient(app)
