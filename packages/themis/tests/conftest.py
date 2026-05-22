"""Shared pytest fixtures for themis package tests."""

from __future__ import annotations

import importlib
import os
from pathlib import Path

import pytest


@pytest.fixture
def isolated_violations(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root themis violations log at tmp_path for the test's lifetime."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    import themis.violations as violations_mod
    importlib.reload(violations_mod)
    yield violations_mod

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(violations_mod)


@pytest.fixture
def violations_path(tmp_path: Path) -> Path:
    """Return a fresh violations JSONL path inside tmp_path."""
    p = tmp_path / "themis_violations.jsonl"
    return p
