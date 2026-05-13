"""Shared pytest fixtures for khimaira package tests.

Most fixtures here isolate state on disk so tests don't pollute the
user's real ~/.local/state/khimaira/ tree. They also let multiple test
runs proceed in parallel without colliding.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _suppress_desktop_notifications(monkeypatch: pytest.MonkeyPatch):
    """Block real desktop popups from firing during the test suite.

    Several tests exercise code paths (post_handoff, invite_handoff,
    post_notice, post_answer) that now fire desktop notifications. If
    we don't disable them globally, every test run blasts the developer
    with dozens of system popups. KHIMAIRA_DESKTOP_NOTIFY=0 is the same
    opt-out users have in production.

    Tests that need to verify notification behavior explicitly opt
    back in via their own monkeypatch.setenv.
    """
    monkeypatch.setenv("KHIMAIRA_DESKTOP_NOTIFY", "0")
    yield


@pytest.fixture
def isolated_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root khimaira's state dir at a tmp_path for the test's lifetime.

    Sets XDG_STATE_HOME so khimaira.monitor.sessions._BASE_DIR resolves
    to tmp_path/<state_subpath>/sessions. Reloads the sessions module
    after the env var is set so the module-level _BASE_DIR is computed
    against the new XDG_STATE_HOME.
    """
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    # Reload so module-level constants pick up the new env var
    from khimaira.monitor import sessions as sessions_mod
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

    from khimaira.monitor.api import sessions as api_sessions
    importlib.reload(api_sessions)

    app = FastAPI()
    app.include_router(api_sessions.build_router(), prefix="/api")
    return TestClient(app)
