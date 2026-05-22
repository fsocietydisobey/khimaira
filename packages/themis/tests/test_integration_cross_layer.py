"""Cross-layer integration tests for Themis D12 read-auth.

These tests verify the full chain:
  violations_for (THEMIS-C MCP tool)
    → X-Session-ID header
    → /api/themis/violations (THEMIS-B daemon endpoint)
    → D12 read-auth enforcement

Tests use FastAPI TestClient for the daemon layer so no real daemon
restart is needed, and patch _CALLER_SESSION_ID on the MCP layer to
simulate different calling sessions.

D12 contract (the invariant this suite guards):
  - Own-session read: caller session_id == filter session_id → allowed
  - Master/observer/critic cross-session read → allowed
  - Agent/intake cross-session read → empty list + auth log
  - No X-Session-ID header → caller treated as "" → blocked
"""

from __future__ import annotations

import json
import urllib.error
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers shared across tests
# ---------------------------------------------------------------------------


def _urlopen_response(payload: Any) -> MagicMock:
    body = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _make_violations_fixture(session_id: str) -> list[dict]:
    return [
        {
            "ts": "2026-05-21T10:00:00Z",
            "session_id": session_id,
            "session_name": "test-session",
            "role": "intake",
            "rule_id": "IN-INTAKE-1",
            "tool_name": "Edit",
            "tool_use_id": "toolu_abc",
            "tool_input_summary": "{}",
            "decision": "blocked",
            "cwd": "/repo",
        }
    ]


# ---------------------------------------------------------------------------
# Layer 1: MCP wrapper sends X-Session-ID header
# ---------------------------------------------------------------------------


class TestMCPLayerSendsCallerHeader:
    """Verify that violations_for passes X-Session-ID in every daemon request."""

    def test_header_sent_when_caller_id_known(self):
        """X-Session-ID header is present when CLAUDE_CODE_SESSION_ID is set."""
        import themis.server as server_mod

        from themis.server import violations_for

        captured: list[Any] = []
        daemon_resp = _urlopen_response({"violations": []})

        def capturing(req, **kwargs):
            captured.append(req)
            return daemon_resp

        with patch("urllib.request.urlopen", side_effect=capturing):
            with patch.object(server_mod, "_CALLER_SESSION_ID", "agent-1-session"):
                violations_for(session_id="agent-1-session")

        assert captured, "no request made"
        req = captured[0]
        assert req.get_header("X-session-id") == "agent-1-session"

    def test_no_header_when_caller_id_empty(self):
        """No X-Session-ID header when CLAUDE_CODE_SESSION_ID is unset."""
        import themis.server as server_mod

        from themis.server import violations_for

        captured: list[Any] = []
        daemon_resp = _urlopen_response({"violations": []})

        def capturing(req, **kwargs):
            captured.append(req)
            return daemon_resp

        with patch("urllib.request.urlopen", side_effect=capturing):
            with patch.object(server_mod, "_CALLER_SESSION_ID", ""):
                violations_for(session_id="s")

        assert captured, "no request made"
        req = captured[0]
        # No X-Session-ID — daemon will see caller as "" and may block
        assert req.get_header("X-session-id") is None

    def test_check_sends_caller_header(self):
        """check() also sends X-Session-ID for potential future auth on /check."""
        import themis.server as server_mod

        from themis.server import check

        captured: list[Any] = []
        daemon_resp = _urlopen_response({"ok": True})

        def capturing(req, **kwargs):
            captured.append(req)
            return daemon_resp

        with patch("urllib.request.urlopen", side_effect=capturing):
            with patch.object(server_mod, "_CALLER_SESSION_ID", "agent-2-session"):
                check("agent-2-session", "Read", {})

        assert captured
        req = captured[0]
        assert req.get_header("X-session-id") == "agent-2-session"

    def test_record_violation_sends_caller_header(self):
        """record_violation() sends X-Session-ID for symmetry."""
        import themis.server as server_mod

        from themis.server import record_violation

        captured: list[Any] = []
        daemon_resp = _urlopen_response({"logged": True, "id": "x"})

        def capturing(req, **kwargs):
            captured.append(req)
            return daemon_resp

        with patch("urllib.request.urlopen", side_effect=capturing):
            with patch.object(server_mod, "_CALLER_SESSION_ID", "intake-1-session"):
                record_violation(
                    session_id="intake-1-session",
                    rule_id="IN-INTAKE-1",
                    tool_name="Edit",
                    tool_input={},
                    tool_use_id="t",
                    cwd="/",
                )

        assert captured
        req = captured[0]
        assert req.get_header("X-session-id") == "intake-1-session"


# ---------------------------------------------------------------------------
# Layer 2: THEMIS-B daemon enforces D12 given the header
# ---------------------------------------------------------------------------


class TestDaemonD12Auth:
    """Verify THEMIS-B endpoint enforces D12 read-auth when X-Session-ID is set."""

    @pytest.fixture
    def test_app(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """FastAPI TestClient with the THEMIS-B router mounted on isolated state."""
        try:
            import fastapi
            from fastapi.testclient import TestClient
        except ImportError:
            pytest.skip("fastapi not available")

        from khimaira.monitor.api.themis import build_router, resolve_session_role

        app = fastapi.FastAPI()
        router = build_router()
        app.include_router(router, prefix="/api")

        # Patch resolve_session_role so tests don't need real JSONL chat state
        role_map: dict[str, str | None] = {}

        def mock_resolve_role(sid: str) -> str | None:
            return role_map.get(sid)

        monkeypatch.setattr(
            "khimaira.monitor.api.themis.resolve_session_role", mock_resolve_role
        )
        # Suppress auth-violation log writes
        monkeypatch.setattr(
            "khimaira.monitor.api.themis._log_auth_violation",
            lambda *a, **k: None,
        )

        client = TestClient(app)
        return client, role_map

    def test_own_session_read_allowed(self, test_app, violations_path: Path):
        """Caller reading own violations: X-Session-ID == session_id → allowed."""
        client, role_map = test_app
        role_map["agent-1"] = "agent"

        with patch("khimaira.monitor.api.themis._violations_query", return_value=_make_violations_fixture("agent-1")):
            resp = client.get(
                "/api/themis/violations",
                params={"session_id": "agent-1"},
                headers={"X-Session-ID": "agent-1"},
            )

        assert resp.status_code == 200
        body = resp.json()
        assert len(body["violations"]) == 1
        assert body["violations"][0]["session_id"] == "agent-1"

    def test_cross_session_agent_blocked(self, test_app):
        """Agent trying to read another agent's violations → empty list."""
        client, role_map = test_app
        role_map["agent-1"] = "agent"
        role_map["agent-2"] = "agent"

        with patch("khimaira.monitor.api.themis._violations_query", return_value=_make_violations_fixture("agent-2")):
            resp = client.get(
                "/api/themis/violations",
                params={"session_id": "agent-2"},
                headers={"X-Session-ID": "agent-1"},
            )

        assert resp.status_code == 200
        assert resp.json()["violations"] == []

    def test_master_cross_session_allowed(self, test_app):
        """Master reading any agent's violations → full results."""
        client, role_map = test_app
        role_map["master-session"] = "master"
        role_map["agent-1"] = "agent"

        fixture = _make_violations_fixture("agent-1")
        with patch("khimaira.monitor.api.themis._violations_query", return_value=fixture):
            resp = client.get(
                "/api/themis/violations",
                params={"session_id": "agent-1"},
                headers={"X-Session-ID": "master-session"},
            )

        assert resp.status_code == 200
        assert len(resp.json()["violations"]) == 1

    def test_no_header_cross_session_blocked(self, test_app):
        """No X-Session-ID header → caller="" → cross-session read blocked."""
        client, role_map = test_app
        # No role_map entry for "" — resolves to None

        with patch("khimaira.monitor.api.themis._violations_query", return_value=_make_violations_fixture("agent-1")):
            resp = client.get(
                "/api/themis/violations",
                params={"session_id": "agent-1"},
                # No X-Session-ID header
            )

        assert resp.status_code == 200
        # caller="" != "agent-1" and caller_role=None → blocked
        assert resp.json()["violations"] == []


# ---------------------------------------------------------------------------
# Layer 3: End-to-end MCP→daemon simulation
# ---------------------------------------------------------------------------


class TestEndToEndCrossLayer:
    """Simulate the full MCP→daemon path using urllib mock + manual D12 logic."""

    def test_own_session_mcp_to_daemon(self):
        """MCP violations_for(session_id=X) with caller=X → daemon returns data.

        Simulates the daemon correctly granting access when caller == filter.
        """
        import themis.server as server_mod
        from themis.server import violations_for

        caller_id = "agent-1-uuid"
        fixture = _make_violations_fixture(caller_id)

        def daemon_handler(req, **kwargs):
            # Simulate daemon: check X-Session-ID header
            caller = req.get_header("X-session-id") or ""
            filter_sid = None
            if "session_id=" in req.full_url:
                for part in req.full_url.split("?")[1].split("&"):
                    if part.startswith("session_id="):
                        filter_sid = part.split("=", 1)[1]
            # D12: allow if caller == filter
            if filter_sid and filter_sid != caller:
                return _urlopen_response({"violations": []})
            return _urlopen_response({"violations": fixture})

        with patch("urllib.request.urlopen", side_effect=daemon_handler):
            with patch.object(server_mod, "_CALLER_SESSION_ID", caller_id):
                result = violations_for(session_id=caller_id)

        assert len(result) == 1
        assert result[0]["session_id"] == caller_id

    def test_cross_session_mcp_to_daemon_blocked(self):
        """MCP violations_for(session_id=Y) with caller=X → daemon blocks → []."""
        import themis.server as server_mod
        from themis.server import violations_for

        caller_id = "agent-1-uuid"
        target_id = "agent-2-uuid"
        fixture = _make_violations_fixture(target_id)

        def daemon_handler(req, **kwargs):
            caller = req.get_header("X-session-id") or ""
            filter_sid = None
            if "?" in req.full_url:
                for part in req.full_url.split("?")[1].split("&"):
                    if part.startswith("session_id="):
                        filter_sid = part.split("=", 1)[1]
            # D12: block cross-session for agent role
            if filter_sid and filter_sid != caller:
                return _urlopen_response({"violations": []})
            return _urlopen_response({"violations": fixture})

        with patch("urllib.request.urlopen", side_effect=daemon_handler):
            with patch.object(server_mod, "_CALLER_SESSION_ID", caller_id):
                result = violations_for(session_id=target_id)

        assert result == []
