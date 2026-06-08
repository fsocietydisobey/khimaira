"""Registry auto-GC — reap windowless session records (2026-06-08 boot-tax fix)."""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def gc_mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    from khimaira.monitor import sessions as sess
    importlib.reload(sess)
    from khimaira.monitor import registry_gc as gc
    importlib.reload(gc)
    yield gc, sess
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sess)


def _rows(*specs):
    # specs: (session_id, name, age_s)
    return [{"session_id": s, "name": n, "last_active_age_s": a} for s, n, a in specs]


def test_noop_when_kitty_unavailable(gc_mod, monkeypatch):
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_titles", lambda: None)  # kitty down
    deleted = []
    monkeypatch.setattr(sess, "list_sessions", lambda **k: _rows(("uuid-1", "agent-1", 99999)))
    monkeypatch.setattr(sess, "delete_session", lambda *a, **k: deleted.append(a) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert res["skipped"] == "kitty-unavailable"
    assert deleted == [], "MUST NOT reap when kitty is unavailable"


def test_reaps_windowless_idle_session(gc_mod, monkeypatch):
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_titles", lambda: {"agent-1", "master"})  # agent-2 gone
    deleted = []
    monkeypatch.setattr(
        sess, "list_sessions",
        lambda **k: _rows(("uuid-1", "agent-1", 99999), ("uuid-2", "agent-2", 99999)),
    )
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 1
    assert deleted == ["uuid-2"], "only the windowless session is reaped"


def test_keeps_live_window_session(gc_mod, monkeypatch):
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_titles", lambda: {"agent-1"})
    deleted = []
    monkeypatch.setattr(sess, "list_sessions", lambda **k: _rows(("uuid-1", "agent-1", 99999)))
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert deleted == []


def test_keeps_fresh_session_even_if_windowless(gc_mod, monkeypatch):
    """A just-launched session may not have bound its window title yet — the
    idle threshold protects it from being reaped mid-boot."""
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_titles", lambda: {"master"})
    deleted = []
    monkeypatch.setattr(sess, "list_sessions",
                        lambda **k: _rows(("uuid-new", "agent-9", 5)))  # 5s old
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert deleted == [], "fresh session must not be reaped (window not bound yet)"


def test_empty_titles_reaps_idle_windowless(gc_mod, monkeypatch):
    """Empty set (kitty answered, zero windows) is distinct from None — idle
    windowless sessions ARE reapable."""
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_titles", lambda: set())
    deleted = []
    monkeypatch.setattr(sess, "list_sessions", lambda **k: _rows(("uuid-1", "agent-1", 99999)))
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 1
