"""Tests for the Themis daemon endpoints.

Covers per CLAUDE.md conventions:
- Happy path per endpoint
- 404 on unknown session name (CLAUDE.md: every session-resolving endpoint)
- Read-auth enforcement for violations (D12)
- Role resolution from chat membership

Uses isolated_chats + FastAPI TestClient so no live daemon is required.
The themis engine and violations module are mocked to avoid requiring the
packages/themis package to be installed.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_chats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Re-root state in tmp_path; reload sessions + chats modules."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod

    importlib.reload(chats_mod)
    yield chats_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)


@pytest.fixture
def themis_client(isolated_chats, monkeypatch: pytest.MonkeyPatch):
    """FastAPI TestClient for /api/themis + /api/sessions/.../role, on isolated state.

    Reloads the themis API module so it picks up the reloaded chats module's
    _chat_dir() path.
    """
    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    app = FastAPI()
    app.include_router(themis_api.build_router(), prefix="/api")
    return TestClient(app)


@pytest.fixture
def sessions_mod(isolated_chats):
    """Convenience: expose sessions module via the isolated_chats fixture chain."""
    from khimaira.monitor import sessions as s

    return s


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(sessions_mod, session_id: str) -> None:
    """Write minimal session state so the session is resolvable by name."""
    sd = sessions_mod._session_dir(session_id)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "status.json").write_text(
        json.dumps({"status": "idle", "detail": ""}), encoding="utf-8"
    )


def _make_named_session(sessions_mod, session_id: str, name: str) -> None:
    sd = sessions_mod._session_dir(session_id)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "status.json").write_text(
        json.dumps({"status": "idle", "detail": "", "name": name}), encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# GET /api/sessions/{session_id}/role
# ---------------------------------------------------------------------------


def test_role_unknown_name_returns_404(themis_client):
    """CLAUDE.md: every session-resolving endpoint needs unknown-name 404 coverage."""
    r = themis_client.get("/api/sessions/no-such-session/role")
    assert r.status_code == 404
    assert "no session" in r.text.lower() or "session" in r.text.lower()


def test_role_uuid_with_no_chat_returns_null(themis_client):
    """A valid-format UUID that has no chat memberships returns role=null."""
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    r = themis_client.get(f"/api/sessions/{session_id}/role")
    assert r.status_code == 200
    assert r.json()["role"] is None


def test_role_returns_role_from_chat_membership(isolated_chats, sessions_mod, themis_client):
    """Session with master role in a chat gets role=master."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s1,
        [s2],
        title="test-room",
        member_roles={s1: "master", s2: "agent"},
    )
    isolated_chats.accept("chat-" + _derive_chat_id_suffix([s1, s2]), s2)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.get(f"/api/sessions/{s1}/role")
    assert r.status_code == 200
    assert r.json()["role"] == "master"


def _derive_chat_id_suffix(members: list[str]) -> str:
    """Reproduce the chat_id derivation for fixture setup."""
    import hashlib

    sorted_members = sorted(members)
    payload = "|".join(sorted_members) + "|"
    return hashlib.sha256(payload.encode()).hexdigest()[:12]


def test_role_picks_most_recently_active_chat(isolated_chats, sessions_mod, themis_client):
    """When session is in multiple chats with different roles, the role from the
    most-recently-active chat (by last_message_ts) wins."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    s3 = "33333333-0000-0000-0000-000000000003"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)
    _make_session(sessions_mod, s3)

    # Chat 1: s1 is agent; chat 2: s1 is master
    isolated_chats.create_room(
        s2,
        [s1],
        title="older-chat",
        member_roles={s2: "master", s1: "agent"},
        fresh=True,
    )
    chat1_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat1_id, s1)
    isolated_chats.send_message(chat1_id, s2, "older message")

    isolated_chats.create_room(
        s3,
        [s1],
        title="newer-chat",
        member_roles={s3: "agent", s1: "master"},
        fresh=True,
    )
    all_chats = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))
    chat2_id = next(c.stem for c in all_chats if c.stem != chat1_id)
    isolated_chats.accept(chat2_id, s1)
    isolated_chats.send_message(chat2_id, s3, "newer message")

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.get(f"/api/sessions/{s1}/role")
    assert r.status_code == 200
    # Newer chat has master role for s1
    assert r.json()["role"] == "master"


# ---------------------------------------------------------------------------
# POST /api/themis/check
# ---------------------------------------------------------------------------


def test_check_no_role_returns_ok(themis_client):
    """Session with no role assignment → ok=true, role=null (D4 passthrough)."""
    session_id = "aaaaaaaa-0000-0000-0000-000000000000"
    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": session_id, "tool_name": "Edit", "tool_input": {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["role"] is None


def test_check_calls_engine_with_resolved_role(isolated_chats, sessions_mod, themis_client):
    """When session has a role, engine.evaluate() is called with that role."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s2,
        [s1],
        title="test",
        member_roles={s2: "master", s1: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    mock_result = MagicMock()
    mock_result.ok = True
    mock_result.violation = None

    mock_engine = MagicMock()
    mock_engine.evaluate.return_value = mock_result

    with patch.dict("sys.modules", {"themis.engine": mock_engine}):
        r = themis_client.post(
            "/api/themis/check",
            json={"session_id": s1, "tool_name": "Edit", "tool_input": {"file_path": "/tmp/x"}},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["role"] == "agent"
    mock_engine.evaluate.assert_called_once_with(
        "agent", "Edit", {"file_path": "/tmp/x"}
    )


def test_check_engine_block_returns_violation(isolated_chats, sessions_mod, themis_client):
    """When engine returns a block violation, response includes violation details."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s2,
        [s1],
        title="test",
        member_roles={s2: "master", s1: "intake"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    mock_violation = MagicMock()
    mock_violation.rule_id = "IN-INTAKE-1"
    mock_violation.name = "NO_FILE_EDIT"
    mock_violation.message = "intake cannot call Edit"
    mock_violation.severity = "block"

    mock_result = MagicMock()
    mock_result.ok = False
    mock_result.violation = mock_violation

    mock_engine = MagicMock()
    mock_engine.evaluate.return_value = mock_result

    with patch.dict("sys.modules", {"themis.engine": mock_engine}):
        r = themis_client.post(
            "/api/themis/check",
            json={"session_id": s1, "tool_name": "Edit", "tool_input": {}},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["role"] == "intake"
    assert body["violation"]["rule_id"] == "IN-INTAKE-1"
    assert body["violation"]["severity"] == "block"


def test_check_engine_runtime_error_fails_closed(isolated_chats, sessions_mod, themis_client):
    """If themis.engine.evaluate() raises any non-ImportError exception (bad YAML, etc.),
    check endpoint fails CLOSED — a resolved-role-with-broken-rules is an enforcement
    failure that must not become a silent allow-through (unconditional, not gated on
    member_roles being present)."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s2, [s1], title="test", member_roles={s2: "master", s1: "intake"}
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    mock_engine = MagicMock()
    mock_engine.evaluate.side_effect = FileNotFoundError("rules/intake.yaml not found")

    with patch.dict("sys.modules", {"themis.engine": mock_engine}):
        r = themis_client.post(
            "/api/themis/check",
            json={"session_id": s1, "tool_name": "Edit", "tool_input": {}},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False  # fail-closed: resolved role but rules broken
    assert body["violation"]["rule_id"] == "IN-ENGINE-ERROR"
    assert body["violation"]["severity"] == "block"


def test_check_engine_import_error_fails_open(isolated_chats, sessions_mod, themis_client):
    """If themis.engine is not installed, check endpoint fails open (D7).

    Setting sys.modules["themis.engine"] = None causes importlib.import_module
    to raise ImportError, simulating the package not being installed.
    """
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s2,
        [s1],
        title="test",
        member_roles={s2: "master", s1: "intake"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    # None in sys.modules → importlib.import_module raises ImportError (simulates not-installed)
    with patch.dict("sys.modules", {"themis.engine": None}):
        r = themis_client.post(
            "/api/themis/check",
            json={"session_id": s1, "tool_name": "Edit", "tool_input": {}},
        )

    assert r.status_code == 200
    assert r.json()["ok"] is True  # fail-open


# ---------------------------------------------------------------------------
# POST /api/themis/violations
# ---------------------------------------------------------------------------


def test_record_violation_happy_path(themis_client):
    """Appending a violation returns {logged: true, id: ...}.

    Mocks both themis.violations and themis.data to isolate endpoint behavior.
    """
    mock_violations = MagicMock()
    mock_violations.append_violation.return_value = None  # append_violation returns None

    mock_vr = MagicMock()
    mock_data = MagicMock()
    mock_data.ViolationRecord.from_dict.return_value = mock_vr

    with patch.dict("sys.modules", {"themis.violations": mock_violations, "themis.data": mock_data}):
        r = themis_client.post(
            "/api/themis/violations",
            json={
                "record": {
                    "session_id": "aaaaaaaa-0000-0000-0000-000000000001",
                    "role": "intake",
                    "rule_id": "IN-INTAKE-1",
                    "tool_name": "Edit",
                    "tool_use_id": "toolu_abc12345",
                    "decision": "blocked",
                }
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["logged"] is True
    assert body["id"] == "toolu_abc12345"
    mock_violations.append_violation.assert_called_once_with(mock_vr)


def test_record_violation_fallback_when_themis_not_installed(
    themis_client, tmp_path, monkeypatch
):
    """When themis.violations not installed, fallback writes directly to JSONL.

    Setting sys.modules entries to None simulates ImportError on import.
    """
    from khimaira.monitor.api import themis as themis_api

    violations_path = tmp_path / "themis_violations.jsonl"
    monkeypatch.setattr(themis_api, "_VIOLATIONS_PATH", violations_path)

    # None in sys.modules → importlib.import_module raises ImportError
    with patch.dict("sys.modules", {"themis.violations": None, "themis.data": None}):
        r = themis_client.post(
            "/api/themis/violations",
            json={
                "record": {
                    "session_id": "aaaaaaaa-0000-0000-0000-000000000001",
                    "rule_id": "IN-INTAKE-1",
                    "tool_name": "Edit",
                }
            },
        )

    assert r.status_code == 200
    assert r.json()["logged"] is True

    # Verify it wrote to disk
    lines = violations_path.read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["rule_id"] == "IN-INTAKE-1"
    assert "id" in entry
    assert "ts" in entry


# ---------------------------------------------------------------------------
# GET /api/themis/violations — read-auth (D12)
# ---------------------------------------------------------------------------


def test_violations_own_session_read_allowed(themis_client):
    """Caller reading its own session_id's violations → returns results."""
    session_id = "aaaaaaaa-0000-0000-0000-000000000001"

    mock_violations = MagicMock()
    mock_violations.read_violations.return_value = [
        {"session_id": session_id, "rule_id": "IN-INTAKE-1"}
    ]

    with patch.dict("sys.modules", {"themis.violations": mock_violations}):
        r = themis_client.get(
            f"/api/themis/violations?session_id={session_id}",
            headers={"X-Session-ID": session_id},
        )

    assert r.status_code == 200
    assert len(r.json()["violations"]) == 1


def test_violations_cross_session_read_blocked_for_agent(
    isolated_chats, sessions_mod, themis_client, tmp_path, monkeypatch
):
    """Agent reading another agent's violations → empty list + auth warning logged."""
    s1 = "11111111-0000-0000-0000-000000000001"  # caller (agent role)
    s2 = "22222222-0000-0000-0000-000000000002"  # target
    s3 = "33333333-0000-0000-0000-000000000003"  # master

    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)
    _make_session(sessions_mod, s3)

    isolated_chats.create_room(
        s3,
        [s1, s2],
        title="test",
        member_roles={s3: "master", s1: "agent", s2: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)
    isolated_chats.accept(chat_id, s2)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    auth_log = tmp_path / "themis_authviolations.log"
    monkeypatch.setattr(themis_api, "_AUTH_VIOLATIONS_LOG", auth_log)

    mock_violations = MagicMock()
    mock_violations.read_violations.return_value = [{"session_id": s2}]

    with patch.dict("sys.modules", {"themis.violations": mock_violations}):
        r = themis_client.get(
            f"/api/themis/violations?session_id={s2}",
            headers={"X-Session-ID": s1},
        )

    assert r.status_code == 200
    assert r.json()["violations"] == []  # blocked
    assert auth_log.exists()
    log_text = auth_log.read_text()
    assert "AUTH_VIOLATION" in log_text
    assert s1 in log_text
    assert s2 in log_text


def test_violations_cross_session_read_allowed_for_master(
    isolated_chats, sessions_mod, themis_client
):
    """Master may read any session's violations."""
    s1 = "11111111-0000-0000-0000-000000000001"  # master
    s2 = "22222222-0000-0000-0000-000000000002"  # agent
    s3 = "33333333-0000-0000-0000-000000000003"  # another member

    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)
    _make_session(sessions_mod, s3)

    isolated_chats.create_room(
        s1,
        [s2],
        title="test",
        member_roles={s1: "master", s2: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s2)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    mock_violations = MagicMock()
    mock_violations.read_violations.return_value = [
        {"session_id": s2, "rule_id": "IN-AGENT-2"}
    ]

    with patch.dict("sys.modules", {"themis.violations": mock_violations}):
        r = themis_client.get(
            f"/api/themis/violations?session_id={s2}",
            headers={"X-Session-ID": s1},
        )

    assert r.status_code == 200
    assert len(r.json()["violations"]) == 1
    assert r.json()["violations"][0]["rule_id"] == "IN-AGENT-2"


def test_violations_cross_session_read_allowed_for_observer(
    isolated_chats, sessions_mod, themis_client
):
    """Observer may read any session's violations."""
    s1 = "11111111-0000-0000-0000-000000000001"  # observer
    s2 = "22222222-0000-0000-0000-000000000002"  # master
    s3 = "33333333-0000-0000-0000-000000000003"  # target agent

    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)
    _make_session(sessions_mod, s3)

    isolated_chats.create_room(
        s2,
        [s1, s3],
        title="test",
        member_roles={s2: "master", s1: "observer", s3: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)
    isolated_chats.accept(chat_id, s3)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    mock_violations = MagicMock()
    mock_violations.read_violations.return_value = [{"session_id": s3}]

    with patch.dict("sys.modules", {"themis.violations": mock_violations}):
        r = themis_client.get(
            f"/api/themis/violations?session_id={s3}",
            headers={"X-Session-ID": s1},
        )

    assert r.status_code == 200
    assert len(r.json()["violations"]) == 1


def test_violations_no_session_filter_returns_all_for_master(
    isolated_chats, sessions_mod, themis_client
):
    """Master calling /violations with no session_id filter → full results."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s1,
        [s2],
        title="test",
        member_roles={s1: "master", s2: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s2)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    all_records = [{"session_id": s2}, {"session_id": "other"}]
    mock_violations = MagicMock()
    mock_violations.read_violations.return_value = all_records

    with patch.dict("sys.modules", {"themis.violations": mock_violations}):
        r = themis_client.get(
            "/api/themis/violations",
            headers={"X-Session-ID": s1},
        )

    assert r.status_code == 200
    assert len(r.json()["violations"]) == 2


# ---------------------------------------------------------------------------
# Role cache tests
# ---------------------------------------------------------------------------


def test_role_cache_hit_returns_cached_value(isolated_chats, sessions_mod, themis_client):
    """Cache hit: after priming the cache, mutating the underlying JSONL
    doesn't affect the returned role — cache wins until invalidated."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s2, [s1], title="test", member_roles={s2: "master", s1: "agent"}
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)
    themis_api._ROLE_CACHE.clear()  # ensure clean slate

    # Prime the cache by calling resolve_session_role
    role_before = themis_api.resolve_session_role(s1)
    assert role_before == "agent"
    assert s1 in themis_api._ROLE_CACHE

    # Delete the chat JSONL to simulate "role no longer on disk"
    for path in isolated_chats._chat_dir().glob("chat-*.jsonl"):
        path.unlink()

    # Cache should still return "agent" (cache hit)
    role_cached = themis_api.resolve_session_role(s1)
    assert role_cached == "agent"


def test_role_cache_expiry(isolated_chats, sessions_mod, themis_client):
    """Expired cache entry triggers re-scan."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s2, [s1], title="test", member_roles={s2: "master", s1: "agent"}
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)
    themis_api._ROLE_CACHE.clear()

    # Prime the cache
    assert themis_api.resolve_session_role(s1) == "agent"

    # Expire the entry by backdating cached_at past TTL
    role, _ = themis_api._ROLE_CACHE[s1]
    import time

    themis_api._ROLE_CACHE[s1] = (role, time.monotonic() - themis_api._ROLE_CACHE_TTL_S - 1)

    # Delete the JSONL so re-scan returns None (cache miss → no role on disk)
    for path in isolated_chats._chat_dir().glob("chat-*.jsonl"):
        path.unlink()

    # Expired cache + no disk data → None (re-scan was triggered)
    assert themis_api.resolve_session_role(s1) is None


def test_role_cache_invalidation_endpoint(isolated_chats, sessions_mod, themis_client):
    """POST /api/themis/invalidate-role-cache removes entry; next lookup re-scans."""
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s2, [s1], title="test", member_roles={s2: "master", s1: "agent"}
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)
    themis_api._ROLE_CACHE.clear()

    # Prime the cache
    assert themis_api.resolve_session_role(s1) == "agent"
    assert s1 in themis_api._ROLE_CACHE

    # Call the invalidation endpoint
    r = themis_client.post(
        "/api/themis/invalidate-role-cache", json={"session_id": s1}
    )
    assert r.status_code == 200
    assert r.json()["invalidated"] is True

    # Cache entry should be gone
    assert s1 not in themis_api._ROLE_CACHE

    # Next lookup re-scans disk (role still in JSONL → returns "agent")
    assert themis_api.resolve_session_role(s1) == "agent"
    assert s1 in themis_api._ROLE_CACHE  # re-populated after re-scan


def test_role_cache_accept_invalidates_via_chat_api(isolated_chats, sessions_mod):
    """Accepting a chat invite via the chat API endpoint invalidates the role cache."""
    import importlib

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    # Create a room (s1 is invited but not yet accepted)
    isolated_chats.create_room(
        s2, [s1], title="test", member_roles={s2: "master", s1: "agent"}
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem

    from khimaira.monitor.api import chats as chats_api
    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)
    importlib.reload(chats_api)
    themis_api._ROLE_CACHE.clear()

    # Prime cache with role=None (s1 not yet accepted, so no ACCEPTED membership)
    initial_role = themis_api.resolve_session_role(s1)
    assert initial_role is None  # pending member → no role yet
    assert s1 in themis_api._ROLE_CACHE  # None is cached

    # Build test client for chats API
    app = FastAPI()
    app.include_router(chats_api.build_router(), prefix="/api")
    client = TestClient(app)

    # Accept the invite via the chat API endpoint
    r = client.post(f"/api/chats/{chat_id}/accept", json={"session_id": s1})
    assert r.status_code == 200

    # Cache entry for s1 should now be gone (invalidated by accept handler)
    assert s1 not in themis_api._ROLE_CACHE

    # Re-scan now returns "agent" (s1 is ACCEPTED with agent role)
    assert themis_api.resolve_session_role(s1) == "agent"


# ---------------------------------------------------------------------------
# 4-layer resolution table (S1+S2+S4 class tests)
# ---------------------------------------------------------------------------


def test_role_resolves_via_created_by_when_no_member_roles(
    isolated_chats, sessions_mod, themis_client
):
    """L2 created_by fallback: session that created a chat with no member_roles
    resolves as 'master' via the created_by field (v1-era chat)."""
    creator = "cccccccc-0000-0000-0000-000000000001"
    member = "dddddddd-0000-0000-0000-000000000002"
    _make_session(sessions_mod, creator)
    _make_session(sessions_mod, member)

    # Create chat WITHOUT member_roles (v1-era — no explicit role map)
    isolated_chats.create_room(creator, [member], title="v1-era-chat")
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, member)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    # Creator resolves as master via created_by (L2)
    r = themis_client.get(f"/api/sessions/{creator}/role")
    assert r.status_code == 200
    assert r.json()["role"] == "master"


def test_role_resolves_via_name_inference_no_member_roles(
    isolated_chats, sessions_mod, themis_client
):
    """L3 inference: a session named 'jp-frontend-lead-1' in a no-member_roles
    chat resolves to 'jp-frontend-lead' via registry-validated rsplit inference."""
    master = "eeeeeeee-0000-0000-0000-000000000001"
    lead = "ffffffff-0000-0000-0000-000000000002"
    _make_session(sessions_mod, master)
    _make_named_session(sessions_mod, lead, "jp-frontend-lead-1")

    # Create chat WITHOUT member_roles
    isolated_chats.create_room(master, [lead], title="jp-roster-legacy")
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, lead)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    # Lead resolves to jp-frontend-lead via inference (L3)
    r = themis_client.get(f"/api/sessions/{lead}/role")
    assert r.status_code == 200
    assert r.json()["role"] == "jp-frontend-lead"


def test_check_known_member_in_member_roles_chat_unresolvable_blocks(
    isolated_chats, sessions_mod, themis_client
):
    """L4 gated fail-closed: a known member in a chat WITH member_roles, whose
    role cannot be resolved via any layer, gets BLOCK (sentinel → fail-closed).
    Proves S4 backstop fires when member_roles is present."""
    master = "11111111-cccc-0000-0000-000000000001"
    non_inferable = "22222222-cccc-0000-0000-000000000002"
    _make_session(sessions_mod, master)
    _make_named_session(sessions_mod, non_inferable, "janice-0")  # not role-inferable

    # Create chat WITH member_roles (only master has a role)
    room = isolated_chats.create_room(
        master,
        [non_inferable],
        title="member-roles-chat",
        member_roles={master: "master"},  # non_inferable NOT in member_roles
    )
    chat_id = room["meta"]["chat_id"]
    isolated_chats.accept(chat_id, non_inferable)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    # non_inferable: L1 miss, L2 miss (not creator), L3 miss (janice-0 → not in registry)
    # L4 fires: member_roles_dict is present → sentinel → BLOCK
    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": non_inferable, "tool_name": "Edit", "tool_input": {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["violation"]["rule_id"] == "IN-UNRESOLVABLE"
    assert body["violation"]["severity"] == "block"


def test_check_known_member_in_no_member_roles_chat_unresolvable_is_open(
    isolated_chats, sessions_mod, themis_client
):
    """L4 per-chat gate: a known member in a chat WITHOUT member_roles, whose
    role cannot be resolved via any layer, gets fail-OPEN (gate blocks sentinel).
    Proves the per-chat gate prevents bricking non-inferable sessions in legacy chats."""
    master = "33333333-cccc-0000-0000-000000000001"
    non_inferable = "44444444-cccc-0000-0000-000000000002"
    _make_session(sessions_mod, master)
    _make_named_session(sessions_mod, non_inferable, "janice-0")  # not role-inferable

    # Create chat WITHOUT member_roles (legacy)
    isolated_chats.create_room(master, [non_inferable], title="no-member-roles-chat")
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, non_inferable)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    # non_inferable: L1 miss, L2 miss (not creator), L3 miss, L4 gate fails (no member_roles)
    # → None → fail-open
    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": non_inferable, "tool_name": "Edit", "tool_input": {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True  # fail-open, gate prevented the sentinel
    assert body["role"] is None


def test_check_known_member_in_empty_member_roles_chat_unresolvable_is_open(
    isolated_chats, sessions_mod, themis_client
):
    """L4 gate: empty dict {} member_roles is semantically absent — treated as fail-OPEN.

    Spec (msg-f0249): gate is bool(chat.meta.member_roles). An empty dict means no roles
    recorded for anyone — identical to absent — and should NOT brick non-inferable members.
    Validates that `if member_roles_dict:` (truthy) was used, not `is not None`.
    """
    master = "77777777-cccc-0000-0000-000000000001"
    non_inferable = "88888888-cccc-0000-0000-000000000002"
    _make_session(sessions_mod, master)
    _make_named_session(sessions_mod, non_inferable, "janice-empty")

    isolated_chats.create_room(master, [non_inferable], title="empty-member-roles-chat")
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, non_inferable)

    # Manually write an empty member_roles dict into the chat meta
    import json as _json
    path = isolated_chats._chat_dir() / f"{chat_id}.jsonl"
    lines = path.read_text().splitlines()
    new_lines = []
    for ln in lines:
        entry = _json.loads(ln)
        if entry.get("kind") == "meta":
            entry["member_roles"] = {}  # meta content is top-level, not nested
        new_lines.append(_json.dumps(entry))
    path.write_text("\n".join(new_lines) + "\n")

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": non_inferable, "tool_name": "Edit", "tool_input": {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True  # empty {} → gate OFF → fail-open (not bricked)
    assert body["role"] is None


def test_check_engine_runtime_error_fails_closed_without_member_roles(
    isolated_chats, sessions_mod, themis_client
):
    """S4 split-gate: a session whose role resolves (via L2 created_by) but whose
    rules fail to load → BLOCK UNCONDITIONALLY, even in a NO-member_roles chat.

    This proves the #1/#2 backstop is NOT gated on member_roles being present —
    the per-chat gate applies ONLY to the unresolvable-role case, not to
    resolved-role-broken-rules."""
    creator = "55555555-cccc-0000-0000-000000000001"
    member = "66666666-cccc-0000-0000-000000000002"
    _make_session(sessions_mod, creator)
    _make_session(sessions_mod, member)

    # Create chat WITHOUT member_roles — creator resolves as master via L2
    isolated_chats.create_room(creator, [member], title="no-member-roles-broken-rules")
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, member)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    mock_engine = MagicMock()
    mock_engine.evaluate.side_effect = ValueError("broken YAML in master.yaml")

    with patch.dict("sys.modules", {"themis.engine": mock_engine}):
        r = themis_client.post(
            "/api/themis/check",
            json={"session_id": creator, "tool_name": "Edit", "tool_input": {}},
        )

    assert r.status_code == 200
    body = r.json()
    # Role resolved as "master" via L2, but engine fails → fail-closed (no gate)
    assert body["ok"] is False
    assert body["violation"]["rule_id"] == "IN-ENGINE-ERROR"
    assert body["violation"]["severity"] == "block"
