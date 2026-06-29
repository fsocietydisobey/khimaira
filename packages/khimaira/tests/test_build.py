"""Tests for the monitor UI auto-build helper — the FAIL-OPEN contract.

Regression guard for the 2026-06-29 outage: `ensure_built` used to `sys.exit(1)`
when `npm` wasn't in PATH (systemd's minimal PATH), which crash-looped the daemon.
The contract is now: if the build can't run / fails but a `dist/` exists, serve the
stale bundle (loud WARNING, no exit); hard-exit ONLY when there is no dist at all.
"""

from __future__ import annotations

import pytest
from khimaira.monitor import build


def _ui(tmp_path, *, with_dist: bool, with_node_modules: bool = False):
    """Build a fake monitor-ui dir; point the module at it. Returns the dir."""
    ui = tmp_path / "monitor-ui"
    (ui / "src").mkdir(parents=True)
    if with_dist:
        (ui / "dist").mkdir(parents=True)
        (ui / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")
    if with_node_modules:
        (ui / "node_modules").mkdir()
    return ui


class _FakeResult:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode


def test_npm_missing_dist_exists_serves_does_not_raise(tmp_path, monkeypatch, capsys):
    """THE outage path: npm not in PATH but a built dist exists → serve it, no exit."""
    ui = _ui(tmp_path, with_dist=True)
    monkeypatch.setattr(build, "_MONITOR_UI", ui)
    monkeypatch.setattr(build, "needs_build", lambda: True)
    monkeypatch.setattr(build.shutil, "which", lambda _: None)  # npm missing

    build.ensure_built()  # must NOT raise / SystemExit

    err = capsys.readouterr().err.lower()
    assert "existing" in err or "stale" in err  # loud warning emitted


def test_npm_missing_no_dist_hard_exits(tmp_path, monkeypatch):
    """No dist at all → nothing to serve → hard-exit is correct."""
    ui = _ui(tmp_path, with_dist=False)
    monkeypatch.setattr(build, "_MONITOR_UI", ui)
    monkeypatch.setattr(build, "needs_build", lambda: True)
    monkeypatch.setattr(build.shutil, "which", lambda _: None)

    with pytest.raises(SystemExit):
        build.ensure_built()


def test_build_fails_dist_exists_serves(tmp_path, monkeypatch, capsys):
    """`npm run build` returns non-zero but a dist exists → serve stale, no exit."""
    ui = _ui(tmp_path, with_dist=True, with_node_modules=True)
    monkeypatch.setattr(build, "_MONITOR_UI", ui)
    monkeypatch.setattr(build, "needs_build", lambda: True)
    monkeypatch.setattr(build.shutil, "which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: _FakeResult(1))

    build.ensure_built()  # must NOT raise

    assert "existing" in capsys.readouterr().err.lower()


def test_build_fails_no_dist_hard_exits(tmp_path, monkeypatch):
    """`npm run build` fails and there's no dist → hard-exit."""
    ui = _ui(tmp_path, with_dist=False, with_node_modules=True)
    monkeypatch.setattr(build, "_MONITOR_UI", ui)
    monkeypatch.setattr(build, "needs_build", lambda: True)
    monkeypatch.setattr(build.shutil, "which", lambda _: "/usr/bin/npm")
    monkeypatch.setattr(build.subprocess, "run", lambda *a, **k: _FakeResult(1))

    with pytest.raises(SystemExit):
        build.ensure_built()


def test_subprocess_oserror_dist_exists_serves(tmp_path, monkeypatch):
    """A subprocess that can't even spawn (OSError) must fail-open when dist exists —
    an exception must never crash-loop the daemon."""
    ui = _ui(tmp_path, with_dist=True, with_node_modules=True)
    monkeypatch.setattr(build, "_MONITOR_UI", ui)
    monkeypatch.setattr(build, "needs_build", lambda: True)
    monkeypatch.setattr(build.shutil, "which", lambda _: "/usr/bin/npm")

    def _boom(*a, **k):
        raise OSError("npm vanished mid-call")

    monkeypatch.setattr(build.subprocess, "run", _boom)

    build.ensure_built()  # must NOT raise


def test_no_build_when_fresh(tmp_path, monkeypatch):
    """needs_build False → no npm invocation at all (fast path unaffected)."""
    ui = _ui(tmp_path, with_dist=True)
    monkeypatch.setattr(build, "_MONITOR_UI", ui)
    monkeypatch.setattr(build, "needs_build", lambda: False)

    def _should_not_run(*a, **k):
        raise AssertionError("subprocess.run must not be called when build is fresh")

    monkeypatch.setattr(build.subprocess, "run", _should_not_run)
    build.ensure_built()  # returns immediately, no raise
