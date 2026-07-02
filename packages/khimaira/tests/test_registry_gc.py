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


def test_decorated_title_yields_de_decorated_identity(gc_mod, monkeypatch):
    """THE 2026-06-21 muther drop: kitty decorates the live window title with an
    activity marker (✳ idle / ⠂ thinking / * bell). Plain .strip() leaves the
    marker, so "✳ muther" != "muther" and the reaper false-deletes the LIVE
    session — cascading a chat-membership leave. The reaper must add the
    de-decorated identity so the marked window still proves "muther" alive."""
    gc, _ = gc_mod
    import json as _json
    ls = [{"tabs": [{"windows": [{
        "title": "✳ muther",
        "foreground_processes": [{"cmdline": ["claude-chat", "-r", "muther"]}],
    }]}]}]
    from khimaira.monitor import roster_recovery as rr
    monkeypatch.setattr(rr, "_kitty", lambda *a: _json.dumps(ls))
    ids = gc._live_window_identities()
    assert "muther" in ids, "de-decorated title must mark the session live"


def test_decorated_window_session_not_reaped(gc_mod, monkeypatch):
    """End-to-end: a live session whose only liveness proof is a decorated
    window title ("⠂ muther") must NOT be reaped. Regression guard for the
    cascade that dropped muther from her jeevy roster chat twice."""
    gc, sess = gc_mod
    monkeypatch.setattr(gc, "_live_window_identities", lambda: {"muther"})  # de-decorated
    deleted = []
    monkeypatch.setattr(sess, "list_sessions", lambda **k: _rows(("uuid-m", "muther", 99999)))
    monkeypatch.setattr(sess, "delete_session",
                        lambda sid, **k: deleted.append(sid) or {"deleted": True})
    res = gc.reap_windowless_sessions()
    assert res["reaped"] == 0
    assert deleted == [], "decorated-title live session must survive the reaper"


# --- /clear-orphan same-window dedup (2026-07-01) -------------------------------
# `/clear` mints a fresh session in the same kitty window; the operator renames it
# back, and the old record lingers with the same name+window_id. The name-based
# windowless sweep keeps it (name is a live title). reap_stale_window_duplicates
# catches it by window_id + turn-marker freshness. False-reap here cascades
# chat-leaves (BUG3), so the guards below are the contract.

def _dup_env(monkeypatch, sess, rows, windows, fresh, mid=(), deleted=None):
    """Wire the monkeypatches reap_stale_window_duplicates reads."""
    from pathlib import Path
    monkeypatch.setattr(sess, "list_sessions", lambda **k: rows)
    monkeypatch.setattr(sess, "get_session_window", lambda sid: windows.get(sid))
    monkeypatch.setattr(sess, "_session_dir", lambda sid: Path(sid))
    monkeypatch.setattr(sess, "_read_marker_ts", lambda p: fresh.get(Path(p).parent.name))
    monkeypatch.setattr(sess, "is_mid_turn", lambda sid: sid in mid)
    if deleted is not None:
        monkeypatch.setattr(sess, "delete_session",
                            lambda sid, **k: deleted.append(sid) or {"deleted": True})


def test_reaps_clear_orphan_same_window(gc_mod, monkeypatch):
    gc, sess = gc_mod
    # window 62 held by BOTH: live (fresh turn marker, recently active) + orphan
    # (turn-frozen at /clear, idle). Same name — the windowless sweep would keep it.
    deleted = []
    _dup_env(
        monkeypatch, sess,
        rows=_rows(("live", "agent-1", 90), ("orphan", "agent-1", 99999)),
        windows={"live": 62, "orphan": 62},
        fresh={"live": 2000.0, "orphan": 1000.0},  # live turns are newer
        deleted=deleted,
    )
    res = gc.reap_stale_window_duplicates()
    assert res["reaped"] == 1
    assert deleted == ["orphan"], "reap the turn-frozen orphan, keep the live occupant"


def test_never_reaps_freshest_even_if_idle(gc_mod, monkeypatch):
    gc, sess = gc_mod
    # The freshest-turn session is the live one and must survive even if it too is
    # idle past the threshold — reaping it would drop the live agent from its chats.
    deleted = []
    _dup_env(
        monkeypatch, sess,
        rows=_rows(("live", "agent-1", 99999), ("orphan", "agent-1", 99999)),
        windows={"live": 62, "orphan": 62},
        fresh={"live": 2000.0, "orphan": 1000.0},
        deleted=deleted,
    )
    gc.reap_stale_window_duplicates()
    assert deleted == ["orphan"]
    assert "live" not in deleted, "the freshest-turn session is NEVER reaped"


def test_skips_when_no_turn_markers(gc_mod, monkeypatch):
    gc, sess = gc_mod
    # Neither has a turn marker → we can't tell which is live → reap NOTHING.
    deleted = []
    _dup_env(
        monkeypatch, sess,
        rows=_rows(("a", "agent-1", 99999), ("b", "agent-1", 99999)),
        windows={"a": 62, "b": 62},
        fresh={},  # no markers for either
        deleted=deleted,
    )
    res = gc.reap_stale_window_duplicates()
    assert res["reaped"] == 0 and deleted == [], "ambiguous → never reap"


def test_skips_mid_turn_duplicate(gc_mod, monkeypatch):
    gc, sess = gc_mod
    # An older-but-mid-turn duplicate is actively working → never reap it.
    deleted = []
    _dup_env(
        monkeypatch, sess,
        rows=_rows(("live", "agent-1", 90), ("busy", "agent-1", 99999)),
        windows={"live": 62, "busy": 62},
        fresh={"live": 2000.0, "busy": 1000.0},
        mid=("busy",),
        deleted=deleted,
    )
    res = gc.reap_stale_window_duplicates()
    assert res["reaped"] == 0 and deleted == [], "never reap a mid-turn session"


def test_skips_fresh_duplicate_under_idle_threshold(gc_mod, monkeypatch):
    gc, sess = gc_mod
    # The staler session hasn't been idle long enough to be a settled orphan.
    deleted = []
    _dup_env(
        monkeypatch, sess,
        rows=_rows(("live", "agent-1", 30), ("recent", "agent-1", 60)),  # both < 300s
        windows={"live": 62, "recent": 62},
        fresh={"live": 2000.0, "recent": 1000.0},
        deleted=deleted,
    )
    res = gc.reap_stale_window_duplicates()
    assert res["reaped"] == 0, "too fresh to be a settled orphan → skip"


def test_single_session_per_window_noop(gc_mod, monkeypatch):
    gc, sess = gc_mod
    deleted = []
    _dup_env(
        monkeypatch, sess,
        rows=_rows(("a", "agent-1", 99999), ("b", "agent-2", 99999)),
        windows={"a": 62, "b": 63},  # distinct windows
        fresh={"a": 2000.0, "b": 2000.0},
        deleted=deleted,
    )
    res = gc.reap_stale_window_duplicates()
    assert res["reaped"] == 0 and deleted == [], "no shared window → nothing to dedup"
