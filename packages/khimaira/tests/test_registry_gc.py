"""Registry auto-GC — reap windowless session records (2026-06-08 boot-tax fix).

2026-06-12 (muther symptom 2): liveness now matches the drift-proof launch `-n`
name in addition to the mutable window title, and a transient empty kitty result
is treated as can't-tell (skip) rather than reap-everything.
"""

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
    monkeypatch.setattr(gc, "_live_window_identities", lambda: None)  # kitty down
    deleted = []
    monkeypatch.setattr(sess, "list_sessions", lambda **k: _rows(("uuid-1", "agent-1", 99999)))
    monkeypatch.setattr(sess, "delete_session", lambda *a, **k: deleted.append(a) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert res["skipped"] == "kitty-unavailable"
    assert deleted == [], "MUST NOT reap when kitty is unavailable"


def test_reaps_windowless_idle_session(gc_mod, monkeypatch):
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_identities", lambda: {"agent-1", "master"})  # agent-2 gone
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
    monkeypatch.setattr(gc, "_live_window_identities", lambda: {"agent-1"})
    deleted = []
    monkeypatch.setattr(sess, "list_sessions", lambda **k: _rows(("uuid-1", "agent-1", 99999)))
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert deleted == []


def test_keeps_fresh_session_even_if_windowless(gc_mod, monkeypatch):
    """A just-launched session may not have bound its window yet — the idle
    threshold protects it from being reaped mid-boot."""
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_identities", lambda: {"master"})
    deleted = []
    monkeypatch.setattr(sess, "list_sessions",
                        lambda **k: _rows(("uuid-new", "agent-9", 5)))  # 5s old
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert deleted == [], "fresh session must not be reaped (window not bound yet)"


def test_empty_set_is_noop_not_mass_reap(gc_mod, monkeypatch):
    """muther symptom 2 fix: an empty kitty result is a transient hiccup, not a
    real empty desktop. Reaping the whole registry on it was a mass false
    positive — now treated as can't-tell (skip)."""
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_identities", lambda: set())
    deleted = []
    monkeypatch.setattr(sess, "list_sessions", lambda **k: _rows(("uuid-1", "agent-1", 99999)))
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert res["skipped"] == "kitty-empty-suspicious"
    assert deleted == [], "empty set must NOT reap (transient kitty hiccup)"


def test_title_drift_not_reaped_when_cmdline_name_matches(gc_mod, monkeypatch):
    """THE muther symptom 2 case: a live agent whose window TITLE drifted from its
    session name must NOT be reaped — its launch `-n` name still proves it live."""
    gc, sess = gc_mod
    # window title is something unrelated, but cmdline carries -n muther-agent-3
    monkeypatch.setattr(gc, "_live_window_identities",
                        lambda: {"some-drifted-title", "muther-agent-3"})
    deleted = []
    monkeypatch.setattr(sess, "list_sessions",
                        lambda **k: _rows(("uuid-3", "muther-agent-3", 99999)))
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert deleted == [], "launch -n name proves liveness despite title drift"


# --- _name_from_cmdline parsing + identity collection -----------------------

def test_name_from_cmdline_variants(gc_mod):
    gc, _ = gc_mod
    assert gc._name_from_cmdline(["claude", "-n", "muther-agent-3"]) == "muther-agent-3"
    assert gc._name_from_cmdline(["claude", "-nmuther-agent-3"]) == "muther-agent-3"
    assert gc._name_from_cmdline(["claude", "--session-name=master"]) == "master"
    assert gc._name_from_cmdline(["claude", "--other"]) is None
    assert gc._name_from_cmdline([]) is None


def test_live_identities_includes_title_and_cmdline_name(gc_mod, monkeypatch):
    gc, _ = gc_mod
    import json as _json
    ls = [{"tabs": [{"windows": [{
        "title": "drifted-title",
        "foreground_processes": [{"cmdline": ["claude", "-n", "muther-agent-3"]}],
    }]}]}]
    from khimaira.monitor import roster_recovery as rr
    monkeypatch.setattr(rr, "_kitty", lambda *a: _json.dumps(ls))
    ids = gc._live_window_identities()
    assert "drifted-title" in ids and "muther-agent-3" in ids
