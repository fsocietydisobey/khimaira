"""Class-invariant test for ppid restart-wipe durability.

Bug class: _session_ppid in-memory dict wiped on daemon restart →
get_session_ppid returns None even though the session is alive.

Fix: set_session_ppid persists ppid to status.json; get_session_ppid
falls back to status.json on cache miss.
"""

from __future__ import annotations


def test_ppid_survives_registry_wipe(isolated_state):
    """Class invariant: ppid readable after in-memory registry cleared (simulates daemon restart)."""
    sid = "test-session-restart-ppid"
    isolated_state.set_session_ppid(sid, 12345)

    # Simulate daemon restart: wipe in-memory dict
    isolated_state._session_ppid.clear()

    # Must still read back from persistent store
    assert isolated_state.get_session_ppid(sid) == 12345


def test_ppid_in_memory_cache_hit(isolated_state):
    """Cache hit: second call returns from _session_ppid without file I/O."""
    sid = "test-session-cache-hit"
    isolated_state.set_session_ppid(sid, 99999)
    # Without clearing, returns from cache
    assert isolated_state.get_session_ppid(sid) == 99999


def test_ppid_unknown_session_returns_none(isolated_state):
    """Unknown session_id → None, no exception."""
    assert isolated_state.get_session_ppid("nonexistent-session-id") is None


def test_ppid_written_to_status_json(isolated_state, tmp_path):
    """set_session_ppid persists ppid field into status.json."""
    import json, os
    from pathlib import Path

    sid = "test-session-status-json"
    isolated_state.set_session_ppid(sid, 55555)

    state_root = Path(os.environ["XDG_STATE_HOME"])
    status_path = state_root / "khimaira" / "sessions" / sid / "status.json"
    assert status_path.is_file(), "status.json must be written by set_session_ppid"
    data = json.loads(status_path.read_text())
    assert data["ppid"] == 55555


def test_ppid_set_preserves_existing_status_fields(isolated_state, tmp_path):
    """set_session_ppid does not clobber existing status.json fields."""
    import json, os
    from pathlib import Path

    sid = "test-session-preserve-fields"
    # First write a status
    isolated_state.set_status(sid, "researching", "investigating bug")
    isolated_state.set_session_ppid(sid, 77777)

    state_root = Path(os.environ["XDG_STATE_HOME"])
    data = json.loads(
        (state_root / "khimaira" / "sessions" / sid / "status.json").read_text()
    )
    assert data["ppid"] == 77777
    assert data["status"] == "researching"
    assert data["detail"] == "investigating bug"


def test_ppid_warms_cache_on_read_through(isolated_state):
    """get_session_ppid warms _session_ppid on a status.json read-through."""
    sid = "test-session-warm-cache"
    isolated_state.set_session_ppid(sid, 42)
    isolated_state._session_ppid.clear()

    # Read-through: populates cache
    assert isolated_state.get_session_ppid(sid) == 42
    # Now in cache
    assert isolated_state._session_ppid.get(sid) == 42
