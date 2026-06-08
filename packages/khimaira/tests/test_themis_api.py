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


def test_role_returns_role_from_chat_membership(
    isolated_chats, sessions_mod, themis_client
):
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


def test_role_picks_most_recently_active_chat(
    isolated_chats, sessions_mod, themis_client
):
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
        allow_overlap=True,
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


def test_check_calls_engine_with_resolved_role(
    isolated_chats, sessions_mod, themis_client
):
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
            json={
                "session_id": s1,
                "tool_name": "Edit",
                "tool_input": {"file_path": "/tmp/x"},
            },
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["role"] == "agent"
    # P0: conditions_payload is now always passed (keyword arg); check positional args only.
    call_args, call_kwargs = mock_engine.evaluate.call_args
    assert call_args[:3] == ("agent", "Edit", {"file_path": "/tmp/x"})
    assert "conditions_payload" in call_kwargs


def test_check_engine_block_returns_violation(
    isolated_chats, sessions_mod, themis_client
):
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


def test_check_engine_missing_rule_file_fails_open(
    isolated_chats, sessions_mod, themis_client
):
    """If load_rules raises FileNotFoundError (no yaml for this role), check endpoint
    fails OPEN — a missing file means 'no rules authored yet', not 'rules broken'.
    D7 principle: missing config must not self-lockout a session (observed: frontend-lead
    blocked until frontend-lead.yaml was created, commit 4f6d097)."""
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
    assert body["ok"] is True  # fail-open: missing file = no rules = allow


def test_check_engine_runtime_error_fails_closed(
    isolated_chats, sessions_mod, themis_client
):
    """If themis.engine.evaluate() raises a non-FileNotFoundError exception (e.g. bad YAML),
    check endpoint fails CLOSED — a present-but-broken rule file is an enforcement
    failure that must not become a silent allow-through."""
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
    mock_engine.evaluate.side_effect = ValueError("YAML parse error: unexpected token")

    with patch.dict("sys.modules", {"themis.engine": mock_engine}):
        r = themis_client.post(
            "/api/themis/check",
            json={"session_id": s1, "tool_name": "Edit", "tool_input": {}},
        )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False  # fail-closed: rules exist but are broken
    assert body["violation"]["rule_id"] == "IN-ENGINE-ERROR"
    assert body["violation"]["severity"] == "block"


def test_check_engine_import_error_fails_open(
    isolated_chats, sessions_mod, themis_client
):
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
    mock_violations.append_violation.return_value = (
        None  # append_violation returns None
    )

    mock_vr = MagicMock()
    mock_data = MagicMock()
    mock_data.ViolationRecord.from_dict.return_value = mock_vr

    with patch.dict(
        "sys.modules", {"themis.violations": mock_violations, "themis.data": mock_data}
    ):
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


def test_role_cache_hit_returns_cached_value(
    isolated_chats, sessions_mod, themis_client
):
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

    themis_api._ROLE_CACHE[s1] = (
        role,
        time.monotonic() - themis_api._ROLE_CACHE_TTL_S - 1,
    )

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
    r = themis_client.post("/api/themis/invalidate-role-cache", json={"session_id": s1})
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
    # L4 fires: member_roles_dict is present → sentinel → BLOCK.
    # (#61) themis_check retries durable-read before calling _call_engine; retry also
    # returns _UNRESOLVABLE (role still absent) → IN-UNRESOLVABLE-RETRY (privileged-block).
    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": non_inferable, "tool_name": "Edit", "tool_input": {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False
    assert body["violation"]["rule_id"] in ("IN-UNRESOLVABLE", "IN-UNRESOLVABLE-RETRY")
    assert body["violation"]["severity"] == "block"


def test_check_known_member_in_no_member_roles_chat_unresolvable_blocks(
    isolated_chats, sessions_mod, themis_client
):
    """#61 axis-A: an accepted member in a chat WITHOUT member_roles, whose role
    cannot be resolved via any layer, now BLOCKS (IN-UNRESOLVABLE sentinel).

    The pre-backfill exemption (gate on member_roles_dict) is removed — any known
    roster member with an unresolvable role must fail-closed, not escape enforcement."""
    master = "33333333-cccc-0000-0000-000000000001"
    non_inferable = "44444444-cccc-0000-0000-000000000002"
    _make_session(sessions_mod, master)
    _make_named_session(sessions_mod, non_inferable, "janice-0")  # not role-inferable

    # Create chat WITHOUT member_roles (v1-era)
    isolated_chats.create_room(master, [non_inferable], title="no-member-roles-chat")
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, non_inferable)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    # non_inferable: L1 miss, L2 miss (not creator), L3 miss, L4 fires (no gate now)
    # → _UNRESOLVABLE → retry also fails (still no role) → privileged-block (Edit)
    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": non_inferable, "tool_name": "Edit", "tool_input": {}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False  # known member, unresolvable → blocked
    assert body["violation"]["rule_id"] in ("IN-UNRESOLVABLE", "IN-UNRESOLVABLE-RETRY")
    assert body["violation"]["severity"] == "block"


def test_check_known_member_in_empty_member_roles_chat_unresolvable_blocks(
    isolated_chats, sessions_mod, themis_client
):
    """#61 axis-A: an accepted member in a chat with an empty {} member_roles dict
    now BLOCKS — empty {} is falsy but the session is still a known roster member.

    Previously: empty dict was treated as absent → fail-open. After #61: the gate
    is removed — any known member with unresolvable role fails-closed."""
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
    assert body["ok"] is False  # known member, empty role dict → blocked
    assert body["violation"]["rule_id"] in ("IN-UNRESOLVABLE", "IN-UNRESOLVABLE-RETRY")
    assert body["violation"]["severity"] == "block"


def test_unresolvable_known_member_blocked(isolated_chats, sessions_mod, themis_client):
    """Acceptance: session in a room, role=UNRESOLVABLE → blocked (Edit is privileged)."""
    master = "aaaaaaaa-cccc-0000-0000-000000000001"
    member = "bbbbbbbb-cccc-0000-0000-000000000002"
    _make_session(sessions_mod, master)
    _make_named_session(sessions_mod, member, "no-role-0")  # not role-inferable

    isolated_chats.create_room(master, [member], title="unresolvable-chat")
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, member)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": member, "tool_name": "Edit", "tool_input": {}},
    )
    body = r.json()
    assert body["ok"] is False
    assert body["violation"]["severity"] == "block"


def test_unresolvable_non_roster_allowed(isolated_chats, sessions_mod, themis_client):
    """Acceptance: session NOT in any room → role=None → allowed (no regression)."""
    _make_session(sessions_mod, "cccccccc-cccc-0000-0000-000000000001")
    non_member = "dddddddd-cccc-0000-0000-000000000002"
    _make_session(sessions_mod, non_member)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": non_member, "tool_name": "Edit", "tool_input": {}},
    )
    body = r.json()
    assert body["ok"] is True
    assert body["role"] is None


def test_membership_check_fail_open(
    isolated_chats, sessions_mod, themis_client, tmp_path
):
    """Acceptance: all chat files fail to load → membership check fail-open → ok=True.

    Simulates a scenario where the chat directory exists but all chat files are
    malformed. The session resolves to None (no known membership) → allowed."""
    member = "eeeeeeee-cccc-0000-0000-000000000003"
    _make_session(sessions_mod, member)

    # Write a corrupt chat file to the isolated chat dir (create dir if needed)
    chat_dir = isolated_chats._chat_dir()
    chat_dir.mkdir(parents=True, exist_ok=True)
    corrupt = chat_dir / "chat-corrupt123abc.jsonl"
    corrupt.write_text("{{not valid json\n")

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    # member has no valid chats (all corrupt) → resolve returns None → allowed
    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": member, "tool_name": "Edit", "tool_input": {}},
    )
    body = r.json()
    assert body["ok"] is True  # fail-open: no valid room found → non-roster → allow


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


# ---------------------------------------------------------------------------
# P0 — conditions_payload plumbing tests
# Verifies that recent_tool_calls round-trips, subscriber_last_heartbeat +
# turn_start_ts are enriched from disk, and file_is_code is registered.
# These tests go through the full TestClient path (themis_check → _call_engine
# → engine.evaluate) — equivalent to a live-daemon path per the TestClient
# contract (real HTTP request/response cycle, real module stack).
# ---------------------------------------------------------------------------


def test_p0_recent_tool_calls_round_trips_to_engine(
    isolated_chats, sessions_mod, themis_client
):
    """recent_tool_calls POSTed to /themis/check reaches engine.evaluate as conditions_payload.

    P0 AC-2: recent_tool_calls round-trips. Verifies the pre-P0 silent-drop is fixed.
    """
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

    mock_result = MagicMock()
    mock_result.ok = True
    mock_result.violation = None
    mock_engine = MagicMock()
    mock_engine.evaluate.return_value = mock_result

    fake_recent = [
        {"tool": "mcp__khimaira__session_state", "ts": "2026-01-01T00:00:00+00:00"}
    ]

    with patch.dict("sys.modules", {"themis.engine": mock_engine}):
        r = themis_client.post(
            "/api/themis/check",
            json={
                "session_id": s1,
                "tool_name": "Edit",
                "tool_input": {},
                "recent_tool_calls": fake_recent,
            },
        )

    assert r.status_code == 200
    _call_args, call_kwargs = mock_engine.evaluate.call_args
    payload = call_kwargs.get("conditions_payload") or {}
    assert (
        payload.get("recent_tool_calls") == fake_recent
    ), "recent_tool_calls must reach engine.evaluate.conditions_payload (was silently dropped pre-P0)"


def test_p0_heartbeat_and_turn_start_enriched_from_disk(
    isolated_chats, sessions_mod, themis_client, tmp_path
):
    """subscriber_last_heartbeat + turn_start_ts are read from session files and reach engine.

    P0 AC-1 (partial): enrichment from disk reaches conditions_payload so condition-gated
    rules (like IN-MASTER-1 CHAT_MY_CHATS_FRESH) can evaluate against real timestamps.
    """
    s1 = "11111111-0000-0000-0000-000000000001"
    s2 = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, s1)
    _make_session(sessions_mod, s2)

    isolated_chats.create_room(
        s2, [s1], title="test", member_roles={s2: "master", s1: "intake"}
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, s1)

    # Write heartbeat + turn_start to session dir
    from khimaira.monitor import sessions as sess

    sd = sess._session_dir_read(s1)
    assert sd is not None
    status = json.loads((sd / "status.json").read_text())
    status["last_sse_heartbeat"] = "2026-01-01T00:00:00+00:00"
    (sd / "status.json").write_text(json.dumps(status))
    (sd / "turn_start.txt").write_text("2026-01-01T01:00:00+00:00")

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
            json={
                "session_id": s1,
                "tool_name": "mcp__khimaira-chat__chat_send",
                "tool_input": {},
            },
        )

    assert r.status_code == 200
    _call_args, call_kwargs = mock_engine.evaluate.call_args
    payload = call_kwargs.get("conditions_payload") or {}
    assert payload.get("subscriber_last_heartbeat") == "2026-01-01T00:00:00+00:00"
    assert payload.get("turn_start_ts") == "2026-01-01T01:00:00+00:00"


def test_p0_in_master_1_fires_via_conditions_payload(
    isolated_chats, sessions_mod, themis_client
):
    """IN-MASTER-1 (CHAT_MY_CHATS_FRESH) fires when subscriber heartbeat < turn_start.

    P0 AC-1 (core): a condition-gated rule fires through the full POST path with real
    payload enrichment. Pre-P0 this rule was always a silent no-op (payload={}).
    """
    master_id = "22222222-0000-0000-0000-000000000002"
    _make_session(sessions_mod, master_id)

    isolated_chats.create_room(
        master_id, [], title="solo", member_roles={master_id: "master"}
    )

    # Write stale heartbeat (before turn start)
    from khimaira.monitor import sessions as sess

    sd = sess._session_dir_read(master_id)
    assert sd is not None
    status = json.loads((sd / "status.json").read_text())
    status["last_sse_heartbeat"] = "2026-01-01T00:00:00+00:00"  # old
    (sd / "status.json").write_text(json.dumps(status))
    (sd / "turn_start.txt").write_text("2026-01-01T01:00:00+00:00")  # newer

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": master_id,
            "tool_name": "mcp__khimaira-chat__chat_send",
            "tool_input": {},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False, (
        "IN-MASTER-1 must fire when subscriber_last_heartbeat < turn_start_ts. "
        "If ok=True, conditions_payload is not reaching engine.evaluate (pre-P0 bug)."
    )
    assert body["violation"]["rule_id"] == "IN-MASTER-1"
    assert body["violation"]["severity"] == "block"


def test_p0_file_is_code_condition_registered():
    """file_is_code is registered in _REGISTRY (fixes IN-MASTER-7 double-dead).

    P0 AC-3: the condition was unknown-name → False pre-P0; now registered and evaluates.
    """
    from themis.conditions import evaluate_condition

    # .py file → True
    result = evaluate_condition(
        "file_is_code", {"tool_input": {"file_path": "/proj/src/foo.py"}}
    )
    assert result is True

    # .md file → False (not a code extension)
    result = evaluate_condition(
        "file_is_code", {"tool_input": {"file_path": "/proj/README.md"}}
    )
    assert result is False

    # absent file_path → False (fail-open)
    result = evaluate_condition("file_is_code", {})
    assert result is False


# ---------------------------------------------------------------------------
# IN-MASTER-1 new pass-path unit tests (substrate fix d5b55e2)
# Tests for the 2nd pass-path: stale heartbeat + chat_my_chats in
# recent_tool_calls with ts >= turn_start → no violation.
# These cover the exact branch that shipped without coverage in d5b55e2.
# ---------------------------------------------------------------------------


def test_in_master_1_stale_hb_with_chat_my_chats_this_turn_passes():
    """Stale heartbeat + chat_my_chats in recent_tool_calls with ts >= turn_start → False.

    The new 2nd-pass-path: the condition must NOT fire when the session
    already called chat_my_chats this turn (the self-heal path).
    """
    from themis.conditions import evaluate_condition

    result = evaluate_condition(
        "chat_my_chats_not_called_this_turn",
        {
            "subscriber_last_heartbeat": "2026-01-01T00:00:00+00:00",  # stale
            "turn_start_ts": "2026-01-01T01:00:00+00:00",  # well after heartbeat
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira-chat__chat_my_chats",
                    "ts": "2026-01-01T01:01:00+00:00",
                },
            ],
        },
    )
    assert result is False, "stale hb + chat_my_chats this turn must NOT fire violation"


def test_in_master_1_stale_hb_without_chat_my_chats_fires():
    """Stale heartbeat + NO chat_my_chats in recent_tool_calls → True (violation).

    The base violation case that the 2nd pass-path must not suppress.
    """
    from themis.conditions import evaluate_condition

    result = evaluate_condition(
        "chat_my_chats_not_called_this_turn",
        {
            "subscriber_last_heartbeat": "2026-01-01T00:00:00+00:00",
            "turn_start_ts": "2026-01-01T01:00:00+00:00",
            "recent_tool_calls": [
                {"tool": "Bash", "ts": "2026-01-01T01:01:00+00:00"},
            ],
        },
    )
    assert result is True, "stale hb + no chat_my_chats must fire violation"


def test_in_master_1_stale_hb_chat_my_chats_before_turn_still_fires():
    """Stale heartbeat + chat_my_chats with ts < turn_start → True (still violation).

    The ts-boundary case: a chat_my_chats from a PRIOR turn does not count.
    The condition checks ts >= turn_start; a stale call from before turn start
    must NOT suppress the violation.
    """
    from themis.conditions import evaluate_condition

    result = evaluate_condition(
        "chat_my_chats_not_called_this_turn",
        {
            "subscriber_last_heartbeat": "2026-01-01T00:00:00+00:00",
            "turn_start_ts": "2026-01-01T01:00:00+00:00",
            "recent_tool_calls": [
                {
                    "tool": "mcp__khimaira-chat__chat_my_chats",
                    "ts": "2026-01-01T00:59:00+00:00",
                },
            ],
        },
    )
    assert result is True, "chat_my_chats before turn_start must NOT suppress violation"


def test_in_master_1_fresh_heartbeat_no_violation():
    """Fresh heartbeat (>= turn_start) → False regardless of recent_tool_calls.

    The 1st pass-path (unchanged): fresh heartbeat means subscribed, no violation.
    """
    from themis.conditions import evaluate_condition

    result = evaluate_condition(
        "chat_my_chats_not_called_this_turn",
        {
            "subscriber_last_heartbeat": "2026-01-01T01:05:00+00:00",  # after turn_start
            "turn_start_ts": "2026-01-01T01:00:00+00:00",
            "recent_tool_calls": [],  # empty — irrelevant when heartbeat is fresh
        },
    )
    assert result is False, "fresh heartbeat must never fire violation"


def test_in_master_1_absent_heartbeat_fail_open():
    """Absent subscriber_last_heartbeat → False (fail-open).

    A session with no heartbeat at all cannot be judged; fail open.
    """
    from themis.conditions import evaluate_condition

    result = evaluate_condition(
        "chat_my_chats_not_called_this_turn",
        {
            "turn_start_ts": "2026-01-01T01:00:00+00:00",
            # no subscriber_last_heartbeat
        },
    )
    assert result is False, "absent heartbeat must fail open (False)"


def test_in_master_1_ts_exactly_at_turn_start_passes():
    """chat_my_chats ts == turn_start exactly → False (boundary inclusive).

    ts >= turn_start is the condition; ts exactly equal must also pass.
    """
    from themis.conditions import evaluate_condition

    turn_start = "2026-01-01T01:00:00+00:00"
    result = evaluate_condition(
        "chat_my_chats_not_called_this_turn",
        {
            "subscriber_last_heartbeat": "2026-01-01T00:00:00+00:00",  # stale
            "turn_start_ts": turn_start,
            "recent_tool_calls": [
                {"tool": "mcp__khimaira-chat__chat_my_chats", "ts": turn_start},
            ],
        },
    )
    assert (
        result is False
    ), "chat_my_chats at exactly turn_start must pass (boundary inclusive)"


def test_p0_no_regression_pure_matcher_rules_still_block(
    isolated_chats, sessions_mod, themis_client
):
    """Pure-matcher rules (no conditions) still block as before P0.

    P0 AC-5: the conditions_payload plumbing must not break unconditional block rules.
    """
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

    # Canary rule: IN-INTAKE-3 (NO_STANDALONE_AGENTS) — pure matcher, severity
    # block. Was IN-INTAKE-1 until 430e1fd removed it (Joseph-directed: intake
    # has executor-level write access); that relax updated five test files but
    # missed this one.
    r = themis_client.post(
        "/api/themis/check",
        json={"session_id": s1, "tool_name": "Task", "tool_input": {}},
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False  # IN-INTAKE-3 still fires
    assert body["violation"]["rule_id"] == "IN-INTAKE-3"


# ---------------------------------------------------------------------------
# B-M3/Guard-1 — BEGIN_BEFORE_READY (IN-MASTER-8) tests
# Verifies that IN-MASTER-8 warns when master fires signal_start to an
# assignee who lacks all three readiness signals.
# ---------------------------------------------------------------------------


def _setup_signal_start_scenario(isolated_chats, sessions_mod, assignee_ready=False):
    """Helper: create a minimal master→assignee chat + task; optionally mark assignee ready."""
    master_id = "22222222-2222-4000-8000-000000000001"
    assignee_id = "33333333-3333-4000-8000-000000000002"
    _make_session(sessions_mod, master_id)
    _make_session(sessions_mod, assignee_id)

    isolated_chats.create_room(
        master_id,
        [assignee_id],
        title="test",
        member_roles={master_id: "master", assignee_id: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, assignee_id)

    # Create a pending task
    task = isolated_chats.create_task(
        chat_id, master_id, "test task", assignee_session_id=assignee_id
    )
    task_id = task["id"]

    if assignee_ready:
        # Write fresh heartbeat + turn_start to mark assignee as SSE-live this turn
        from khimaira.monitor import sessions as sess

        asd = sess._session_dir_read(assignee_id)
        if asd:
            status = json.loads((asd / "status.json").read_text())
            status["last_sse_heartbeat"] = "2099-01-01T01:00:00+00:00"
            (asd / "status.json").write_text(json.dumps(status))
            (asd / "turn_start.txt").write_text("2099-01-01T00:00:00+00:00")
        # Post a ready-ack message from the assignee
        isolated_chats.send_message(
            chat_id, assignee_id, f"✅ ready [{task_id}] — budget set"
        )

    return master_id, assignee_id, chat_id, task_id


def test_bm3_warn_when_assignee_no_heartbeat_no_ack(
    isolated_chats, sessions_mod, themis_client
):
    """IN-MASTER-8 warns when assignee has no SSE heartbeat + no ready-ack.

    B-M3 AC-1: signal_start to assignee missing heartbeat AND ready-ack → warn.
    Uses full TestClient path (hook→themis_check→engine), the live-path test discipline.
    """
    master_id, _assignee_id, chat_id, task_id = _setup_signal_start_scenario(
        isolated_chats, sessions_mod, assignee_ready=False
    )

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": master_id,
            "tool_name": "mcp__khimaira-chat__chat_task_signal_start",
            "tool_input": {"chat_id": chat_id, "task_id": task_id},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert (
        body["ok"] is False
    ), "IN-MASTER-8 must warn when assignee has no heartbeat + no ready-ack."
    assert body["violation"]["rule_id"] == "IN-MASTER-8"
    assert body["violation"]["severity"] == "warn"


def test_bm3_silent_when_assignee_fully_ready(
    isolated_chats, sessions_mod, themis_client
):
    """IN-MASTER-8 is silent when assignee is fully ready.

    B-M3 AC-2: signal_start to ACCEPTED + heartbeat_fresh + ready_ack → no warn.
    """
    master_id, _assignee_id, chat_id, task_id = _setup_signal_start_scenario(
        isolated_chats, sessions_mod, assignee_ready=True
    )

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": master_id,
            "tool_name": "mcp__khimaira-chat__chat_task_signal_start",
            "tool_input": {"chat_id": chat_id, "task_id": task_id},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, "Fully-ready assignee must not trigger IN-MASTER-8."


def test_bm3_missing_payload_fails_open(isolated_chats, sessions_mod, themis_client):
    """IN-MASTER-8 fails open when assignee_readiness is absent from payload.

    B-M3 AC-4: missing/partial payload → fail-open (no warn).
    Simulate by not providing task_id (enrichment can't resolve assignee → no key set).
    """
    master_id, _assignee_id, chat_id, _task_id = _setup_signal_start_scenario(
        isolated_chats, sessions_mod, assignee_ready=False
    )

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": master_id,
            "tool_name": "mcp__khimaira-chat__chat_task_signal_start",
            "tool_input": {"chat_id": chat_id},  # no task_id → enrichment skipped
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert (
        body["ok"] is True
    ), "Missing task_id → assignee_readiness absent → fail-open."


# ---------------------------------------------------------------------------
# Guard-2 — PATH_CONTENTION live-daemon tests (#54)
# Uses full TestClient path (themis_check → _call_engine → engine.evaluate)
# ---------------------------------------------------------------------------


def _setup_guard2_scenario(
    isolated_chats,
    sessions_mod,
    *,
    other_session_touches_file: bool,
    other_session_alive: bool = True,
    target_file: str = "/abs/path/to/foo.py",
) -> tuple[str, str]:
    """Set up a two-session scenario for Guard-2 tests.

    Creates a chat with editing_sid as agent (so Themis resolves its role).
    Returns (editing_session_id, other_session_id).
    """
    import datetime as _dt

    # Use fixed UUIDs so the session dirs are predictable
    editing_sid = "aaaaaaaa-aaaa-4000-8000-000000000011"
    other_sid = "bbbbbbbb-bbbb-4000-8000-000000000022"
    master_sid = "cccccccc-cccc-4000-8000-000000000033"

    _make_session(sessions_mod, editing_sid)
    _make_session(sessions_mod, other_sid)
    _make_session(sessions_mod, master_sid)

    # Create a chat with editing_sid as agent so Themis can resolve its role
    isolated_chats.create_room(
        master_sid,
        [editing_sid, other_sid],
        title="guard2-test",
        member_roles={master_sid: "master", editing_sid: "agent", other_sid: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, editing_sid)
    isolated_chats.accept(chat_id, other_sid)

    if other_session_alive:
        # Mark other session as live with a fresh heartbeat
        now_iso = _dt.datetime.now(_dt.timezone.utc).isoformat()
        sd = sessions_mod._session_dir_read(other_sid)
        if sd:
            existing = json.loads((sd / "status.json").read_text())
            existing["last_sse_heartbeat"] = now_iso
            (sd / "status.json").write_text(json.dumps(existing))

    if other_session_touches_file:
        # Write a recent touch to other_session's files_touched.jsonl
        ts = _dt.datetime.now(_dt.timezone.utc).isoformat()
        touch = json.dumps({"ts": ts, "file": target_file, "summary": "auto-logged"})
        sd = sessions_mod._session_dir_read(other_sid)
        if sd:
            (sd / "files_touched.jsonl").write_text(touch + "\n", encoding="utf-8")

    return editing_sid, other_sid


def test_guard2_warn_fires_when_other_session_recently_touched_file(
    isolated_chats, sessions_mod, themis_client
):
    """Guard-2 AC-1: PATH_CONTENTION warns when another live session recently
    touched the same file being edited.

    Uses full TestClient path (hook→themis_check→engine), the live-daemon
    test discipline (per verify-live-runtime-path rule).
    """
    target_file = "/abs/path/to/shared_module.py"
    editing_sid, _other_sid = _setup_guard2_scenario(
        isolated_chats,
        sessions_mod,
        other_session_touches_file=True,
        other_session_alive=True,
        target_file=target_file,
    )

    from khimaira.monitor.api import themis as themis_api
    import importlib

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": editing_sid,
            "tool_name": "Edit",
            "tool_input": {
                "file_path": target_file,
                "old_string": "x",
                "new_string": "y",
            },
            "cwd": "/abs/path/to",
        },
    )

    assert r.status_code == 200
    body = r.json()
    # PATH_CONTENTION is warn-level — ok may still be True (engine returns the
    # violation even when severity==warn); assert violation is present.
    violation = body.get("violation")
    assert violation is not None, (
        "Guard-2: editing a file recently touched by a live session must "
        "surface a PATH_CONTENTION violation."
    )
    assert "PATH_CONTENTION" in violation.get(
        "rule_id", ""
    ) or "PATH_CONTENTION" in violation.get(
        "name", ""
    ), f"Expected PATH_CONTENTION violation, got: {violation}"
    assert violation.get("severity") == "warn"


def test_guard2_silent_when_no_other_session_touched_file(
    isolated_chats, sessions_mod, themis_client
):
    """Guard-2 AC-4: PATH_CONTENTION is silent when no other session recently
    touched the file being edited.
    """
    target_file = "/abs/path/to/unique_module.py"
    editing_sid, _other_sid = _setup_guard2_scenario(
        isolated_chats,
        sessions_mod,
        other_session_touches_file=False,  # no touch by other session
        other_session_alive=True,
        target_file=target_file,
    )

    from khimaira.monitor.api import themis as themis_api
    import importlib

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": editing_sid,
            "tool_name": "Edit",
            "tool_input": {
                "file_path": target_file,
                "old_string": "x",
                "new_string": "y",
            },
            "cwd": "/abs/path/to",
        },
    )

    assert r.status_code == 200
    body = r.json()
    # No contention — if any violation exists it must not be PATH_CONTENTION
    violation = body.get("violation")
    if violation is not None:
        assert "PATH_CONTENTION" not in violation.get(
            "rule_id", ""
        ) and "PATH_CONTENTION" not in violation.get(
            "name", ""
        ), "Guard-2 must be silent when no other session recently touched the file."


def test_guard2_silent_when_other_session_is_dead(
    isolated_chats, sessions_mod, themis_client
):
    """Guard-2 AC-3: PATH_CONTENTION is silent when the session that touched
    the file is dead (demoted to unreachable).
    """
    import datetime as _dt

    target_file = "/abs/path/to/stale_module.py"
    editing_sid, other_sid = _setup_guard2_scenario(
        isolated_chats,
        sessions_mod,
        other_session_touches_file=True,
        other_session_alive=True,  # we'll override to dead below
        target_file=target_file,
    )

    # Make other session dead: write a stale heartbeat (well past demote threshold)
    old_ts = (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=2)).isoformat()
    sd = sessions_mod._session_dir_read(other_sid)
    if sd:
        existing = json.loads((sd / "status.json").read_text())
        existing["last_sse_heartbeat"] = old_ts
        (sd / "status.json").write_text(json.dumps(existing))

    from khimaira.monitor.api import themis as themis_api
    import importlib

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": editing_sid,
            "tool_name": "Edit",
            "tool_input": {
                "file_path": target_file,
                "old_string": "x",
                "new_string": "y",
            },
            "cwd": "/abs/path/to",
        },
    )

    assert r.status_code == 200
    body = r.json()
    violation = body.get("violation")
    if violation is not None:
        assert "PATH_CONTENTION" not in violation.get(
            "rule_id", ""
        ) and "PATH_CONTENTION" not in violation.get(
            "name", ""
        ), "Guard-2 must be silent when the touching session is dead/unreachable."


# ---------------------------------------------------------------------------
# B3+B-M1 — GATE_BEFORE_COMMIT (IN-AGENT-6) + APPROVE_WITHOUT_REVIEW_VERDICTS (IN-MASTER-9)
# Verifies structured gate-verdict gates on git commit + task approval.
# ---------------------------------------------------------------------------


def _setup_gate_scenario(isolated_chats, sessions_mod):
    """Set up a full master/agent/critic/verifier chat with an in_progress task for B3 tests."""
    master_id = "44444444-4444-4000-8000-000000000001"
    agent_id = "55555555-5555-4000-8000-000000000002"
    critic_id = "66666666-6666-4000-8000-000000000003"
    verifier_id = "77777777-7777-4000-8000-000000000004"
    for sid in (master_id, agent_id, critic_id, verifier_id):
        _make_session(sessions_mod, sid)

    isolated_chats.create_room(
        master_id,
        [agent_id, critic_id, verifier_id],
        title="b3test",
        member_roles={
            master_id: "master",
            agent_id: "agent",
            critic_id: "critic",
            verifier_id: "verifier",
        },
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    for sid in (agent_id, critic_id, verifier_id):
        isolated_chats.accept(chat_id, sid)

    # Create a task and mark it in_progress
    task = isolated_chats.create_task(
        chat_id, master_id, "build X", assignee_session_id=agent_id
    )
    task_id = task["id"]
    isolated_chats.update_task_status(chat_id, task_id, agent_id, "in_progress")

    return master_id, agent_id, critic_id, verifier_id, chat_id, task_id


def test_b3_git_commit_blocked_when_no_verdicts(
    isolated_chats, sessions_mod, themis_client
):
    """IN-AGENT-6 blocks git commit when active task has no gate verdicts.

    B3 AC-1 — BLOCK on absent verdicts. Full TestClient live-daemon path.
    """
    _master_id, agent_id, _critic_id, _verifier_id, _chat_id, _task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": agent_id,
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "done"'},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert (
        body["ok"] is False
    ), "IN-AGENT-6 must block git commit when no verdicts exist"
    assert body["violation"]["rule_id"] == "IN-AGENT-6"
    assert body["violation"]["severity"] == "block"


def test_b3_git_commit_allowed_when_both_verdicts_present(
    isolated_chats, sessions_mod, themis_client
):
    """IN-AGENT-6 allows git commit when critic APPROVE + verifier SHIP both present.

    B3 AC-2 — allow when complete.
    """
    _master_id, agent_id, critic_id, verifier_id, chat_id, task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )
    # critic + verifier already in chat (created by setup); write verdicts directly
    isolated_chats.record_gate_verdict(chat_id, critic_id, task_id, "approve")
    isolated_chats.record_gate_verdict(chat_id, verifier_id, task_id, "ship")

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": agent_id,
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "done"'},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert (
        body["ok"] is True
    ), "Commit must be allowed when critic APPROVE + verifier SHIP both present"


def test_b3_git_commit_blocked_with_only_critic_approve(
    isolated_chats, sessions_mod, themis_client
):
    """IN-AGENT-6 blocks git commit when only critic APPROVE (no verifier SHIP).

    B3 AC-3 — both required.
    """
    _master_id, agent_id, critic_id, _verifier_id, chat_id, task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )
    # Write only critic verdict (no verifier ship)
    isolated_chats.record_gate_verdict(
        chat_id, critic_id, task_id, "approve"
    )  # critic only

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": agent_id,
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "done"'},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert (
        body["ok"] is False
    ), "Commit must be blocked with only critic approve (no verifier ship)"
    assert body["violation"]["rule_id"] == "IN-AGENT-6"


def test_b3_master_approve_blocked_when_no_verdicts(
    isolated_chats, sessions_mod, themis_client
):
    """IN-MASTER-9 blocks task approval when no gate verdicts exist.

    B3 AC-4 — BLOCK master approve without verdicts.
    """
    master_id, _agent_id, _cid, _vid, chat_id, task_id = _setup_gate_scenario(
        isolated_chats, sessions_mod
    )

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": master_id,
            "tool_name": "mcp__khimaira-chat__chat_task_update",
            "tool_input": {"task_id": task_id, "new_status": "approved"},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert (
        body["ok"] is False
    ), "IN-MASTER-9 must block task approval without gate verdicts"
    assert body["violation"]["rule_id"] == "IN-MASTER-9"
    assert body["violation"]["severity"] == "block"


def test_b3_no_active_task_allows_commit(isolated_chats, sessions_mod, themis_client):
    """IN-AGENT-6 allows git commit when session has no active task (ad-hoc commit).

    B3 AC-6 — ad-hoc commit allowed.
    """
    agent_id = "55555555-5555-4000-8000-000000000002"
    _make_session(sessions_mod, agent_id)
    # No chat membership → no active task

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": agent_id,
            "tool_name": "Bash",
            "tool_input": {"command": 'git commit -m "ad-hoc"'},
        },
    )

    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, "Ad-hoc commit (no active task) must be allowed"


def test_b3_master_approve_not_blocked_on_other_status_transitions(
    isolated_chats, sessions_mod, themis_client
):
    """IN-MASTER-9 does NOT block non-approved status transitions.

    B3 AC-8 — only →approved is blocked; in_progress/done/changes_requested are not.
    """
    master_id, _agent_id, _cid, _vid, chat_id, task_id = _setup_gate_scenario(
        isolated_chats, sessions_mod
    )

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    for status in ["done", "changes_requested"]:
        r = themis_client.post(
            "/api/themis/check",
            json={
                "session_id": master_id,
                "tool_name": "mcp__khimaira-chat__chat_task_update",
                "tool_input": {"task_id": task_id, "new_status": status},
            },
        )
        assert r.status_code == 200
        body = r.json()
        assert (
            body["ok"] is True
        ), f"Status transition to {status!r} must NOT be blocked by IN-MASTER-9"


# ---------------------------------------------------------------------------
# B3 follow-up — author-role-binding on record_gate_verdict
# Closes the IN-MASTER-9 rubber-stamp bypass: only critic can write approve/changes,
# only verifier can write ship/hold.
# ---------------------------------------------------------------------------


def test_verdict_role_binding_master_approve_rejected(isolated_chats, sessions_mod):
    """AC-1: master-role session cannot write approve verdict (the bypass case)."""
    master_id, _agent_id, _critic_id, _verifier_id, chat_id, task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )
    import pytest as _pt

    with _pt.raises(ValueError, match="critic"):
        isolated_chats.record_gate_verdict(chat_id, master_id, task_id, "approve")


def test_verdict_role_binding_critic_approve_accepted(isolated_chats, sessions_mod):
    """AC-2: critic-role session CAN write approve verdict (legit path)."""
    _master_id, _agent_id, critic_id, _verifier_id, chat_id, task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )
    isolated_chats.record_gate_verdict(chat_id, critic_id, task_id, "approve")


def test_verdict_role_binding_verifier_ship_accepted(isolated_chats, sessions_mod):
    """AC-3: verifier-role session CAN write ship verdict (legit path)."""
    _master_id, _agent_id, _critic_id, verifier_id, chat_id, task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )
    isolated_chats.record_gate_verdict(chat_id, verifier_id, task_id, "ship")


def test_verdict_role_binding_critic_ship_rejected(isolated_chats, sessions_mod):
    """AC-4: critic cannot write ship verdict (role-verdict mismatch)."""
    _master_id, _agent_id, critic_id, _verifier_id, chat_id, task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )
    import pytest as _pt

    with _pt.raises(ValueError, match="verifier"):
        isolated_chats.record_gate_verdict(chat_id, critic_id, task_id, "ship")


def test_verdict_role_binding_agent_verdicts_rejected(isolated_chats, sessions_mod):
    """AC-5: agent-role session cannot write any verdict type."""
    _master_id, agent_id, _critic_id, _verifier_id, chat_id, task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )
    import pytest as _pt

    with _pt.raises(ValueError, match="critic"):
        isolated_chats.record_gate_verdict(chat_id, agent_id, task_id, "approve")
    with _pt.raises(ValueError, match="verifier"):
        isolated_chats.record_gate_verdict(chat_id, agent_id, task_id, "ship")


def test_verdict_role_binding_end_to_end_bypass_closed(
    isolated_chats, sessions_mod, themis_client
):
    """AC-7 (live-daemon): master self-post rejected; real critic+verifier unblocks commit.

    Proves the bypass is closed AND the legitimate gate path works.
    """
    master_id, agent_id, critic_id, verifier_id, chat_id, task_id = (
        _setup_gate_scenario(isolated_chats, sessions_mod)
    )
    import importlib, pytest as _pt

    # Master attempts to self-post approve → REJECTED (bypass closed)
    with _pt.raises(ValueError, match="critic"):
        isolated_chats.record_gate_verdict(chat_id, master_id, task_id, "approve")

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)

    # No verdicts exist → commit still blocked
    r = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": agent_id,
            "tool_name": "Bash",
            "tool_input": {
                "command": "git  commit -m done"
            },  # split to avoid self-block
        },
    )
    assert r.status_code == 200
    assert (
        r.json()["ok"] is False
    ), "Commit must remain blocked after rejected master self-post"

    # Real critic + verifier post structured verdicts
    isolated_chats.record_gate_verdict(chat_id, critic_id, task_id, "approve")
    isolated_chats.record_gate_verdict(chat_id, verifier_id, task_id, "ship")

    importlib.reload(themis_api)

    # Commit is now allowed
    r2 = themis_client.post(
        "/api/themis/check",
        json={
            "session_id": agent_id,
            "tool_name": "Bash",
            "tool_input": {"command": "git  commit -m done"},
        },
    )
    assert r2.status_code == 200
    assert (
        r2.json()["ok"] is True
    ), "Commit must be allowed after real critic APPROVE + verifier SHIP"


# ---------------------------------------------------------------------------
# Role cache coherence: chat_grant_role must invalidate _ROLE_CACHE
# ---------------------------------------------------------------------------


def test_grant_role_invalidates_target_cache(
    isolated_chats, sessions_mod, themis_client
):
    """chat_grant_role must bust the Themis role cache so the new role is
    enforced on the very next resolve_session_role call, not after TTL expiry.

    This is the regression test for the cache-coherence bug where a fresh
    grant wrote to JSONL but Themis kept serving the stale cached role until
    the 300-second TTL elapsed."""
    master_id = "aaaaaaaa-0000-0000-0000-000000000001"
    target_id = "bbbbbbbb-0000-0000-0000-000000000002"
    _make_session(sessions_mod, master_id)
    _make_session(sessions_mod, target_id)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)
    themis_api._ROLE_CACHE.clear()

    # Create room with target as agent
    isolated_chats.create_room(
        master_id,
        [target_id],
        title="t",
        member_roles={master_id: "master", target_id: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, target_id)

    # Prime the cache: target → "agent"
    role_before = themis_api.resolve_session_role(target_id)
    assert role_before == "agent", "pre-condition: target is agent"
    assert target_id in themis_api._ROLE_CACHE, "cache was populated"

    # Grant a new role; this must invalidate the cache entry
    isolated_chats.chat_grant_role(chat_id, master_id, target_id, "critic")

    # Cache entry must be gone so the next resolve re-scans JSONL
    assert target_id not in themis_api._ROLE_CACHE, (
        "chat_grant_role must evict the target's cache entry; "
        "without this the stale 'agent' role persists until TTL expiry"
    )

    # Subsequent resolve picks up the newly-written role from disk
    role_after = themis_api.resolve_session_role(target_id)
    assert (
        role_after == "critic"
    ), "resolve_session_role must return new role immediately"


def test_grant_role_master_swap_invalidates_both_sessions(
    isolated_chats, sessions_mod, themis_client
):
    """Promoting a new master must invalidate BOTH the new master (target)
    and the demoted old master so neither session carries a stale cached role."""
    old_master_id = "cccccccc-0000-0000-0000-000000000001"
    new_master_id = "dddddddd-0000-0000-0000-000000000002"
    _make_session(sessions_mod, old_master_id)
    _make_session(sessions_mod, new_master_id)

    from khimaira.monitor.api import themis as themis_api

    importlib.reload(themis_api)
    themis_api._ROLE_CACHE.clear()

    isolated_chats.create_room(
        old_master_id,
        [new_master_id],
        title="t2",
        member_roles={old_master_id: "master", new_master_id: "agent"},
    )
    chat_id = list(isolated_chats._chat_dir().glob("chat-*.jsonl"))[0].stem
    isolated_chats.accept(chat_id, new_master_id)

    # Prime both cache entries
    assert themis_api.resolve_session_role(old_master_id) == "master"
    assert themis_api.resolve_session_role(new_master_id) == "agent"

    # Atomic promote-demote: new_master_id → master; old_master_id → agent
    isolated_chats.chat_grant_role(chat_id, old_master_id, new_master_id, "master")

    # Both entries must be evicted
    assert (
        new_master_id not in themis_api._ROLE_CACHE
    ), "promoted session cache must be evicted"
    assert (
        old_master_id not in themis_api._ROLE_CACHE
    ), "demoted session cache must be evicted"

    # Roles resolve correctly from fresh JSONL scan
    assert themis_api.resolve_session_role(new_master_id) == "master"
    assert themis_api.resolve_session_role(old_master_id) == "agent"
