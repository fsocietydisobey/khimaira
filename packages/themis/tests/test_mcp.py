"""Integration tests for themis MCP tool surface (server.py).

Tests mock the daemon HTTP layer (urllib.request.urlopen) to avoid
requiring a live daemon. The data layer (load_rules, YAML files) is
real — agent-1's actual rule files are loaded.

Coverage:
  - Each tool: happy path
  - check: end-to-end verdict from rule fixture
  - my_rules with no role: returns []
  - violations_for: read-auth passthrough shape
  - sibling_tools: themis_ prefix applied correctly
"""

from __future__ import annotations

import io
import json
import urllib.error
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _urlopen_response(payload: Any, status: int = 200) -> MagicMock:
    """Return a context-manager mock that yields `payload` as JSON bytes."""
    body = json.dumps(payload).encode("utf-8")
    resp = MagicMock()
    resp.read.return_value = body
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _urlopen_error(code: int, detail: str) -> urllib.error.HTTPError:
    body = json.dumps({"detail": detail}).encode("utf-8")
    return urllib.error.HTTPError(
        url="http://127.0.0.1:8740/x",
        code=code,
        msg=detail,
        hdrs={},  # type: ignore[arg-type]
        fp=io.BytesIO(body),
    )


# ---------------------------------------------------------------------------
# my_rules (registered as mcp__khimaira__themis_my_rules)
# ---------------------------------------------------------------------------


class TestMyRules:
    def test_happy_path_returns_invariants(self):
        """Session with known role returns non-empty list of invariants."""
        from themis.server import my_rules

        role_resp = _urlopen_response({"role": "intake"})
        with patch("urllib.request.urlopen", return_value=role_resp):
            result = my_rules("session-abc")

        assert isinstance(result, list)
        assert len(result) > 0
        inv = result[0]
        assert "id" in inv
        assert "name" in inv
        assert "severity" in inv
        assert "message_template" in inv
        assert "matcher_summary" in inv

    def test_no_role_returns_empty_list(self):
        """Session with no role assigned returns []."""
        from themis.server import my_rules

        role_resp = _urlopen_response({"role": None})
        with patch("urllib.request.urlopen", return_value=role_resp):
            result = my_rules("session-no-role")

        assert result == []

    def test_daemon_down_returns_empty_list(self):
        """Role resolution failure (daemon down) returns [] gracefully."""
        from themis.server import my_rules

        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError):
            result = my_rules("session-xyz")

        assert result == []

    def test_master_role_has_invariants(self):
        """Master role has at least one invariant loaded from YAML."""
        from themis.server import my_rules

        role_resp = _urlopen_response({"role": "master"})
        with patch("urllib.request.urlopen", return_value=role_resp):
            result = my_rules("session-master")

        assert len(result) > 0
        ids = [inv["id"] for inv in result]
        assert any("MASTER" in id_ for id_ in ids)

    def test_invariant_severity_is_string(self):
        """Severity field is a plain string, not an enum object."""
        from themis.server import my_rules

        role_resp = _urlopen_response({"role": "observer"})
        with patch("urllib.request.urlopen", return_value=role_resp):
            result = my_rules("session-obs")

        for inv in result:
            assert isinstance(inv["severity"], str)
            assert inv["severity"] in ("block", "warn", "audit")


# ---------------------------------------------------------------------------
# list_rules (registered as mcp__khimaira__themis_list_rules)
# ---------------------------------------------------------------------------


class TestListRules:
    def test_no_role_returns_all_8(self):
        """Omitting role returns data for all 8 known roles."""
        from themis.server import list_rules

        result = list_rules()

        assert isinstance(result, list)
        assert len(result) == 8
        returned_roles = {entry["role"] for entry in result}
        expected = {"intake", "master", "agent", "observer", "architect", "analyst", "verifier", "critic"}
        assert returned_roles == expected

    def test_specific_role_returns_one_entry(self):
        """Passing role='intake' returns exactly one entry."""
        from themis.server import list_rules

        result = list_rules(role="intake")

        assert len(result) == 1
        assert result[0]["role"] == "intake"
        assert isinstance(result[0]["invariants"], list)
        assert len(result[0]["invariants"]) > 0

    def test_intake_has_no_file_edit_rule(self):
        """intake.yaml must contain the IN-INTAKE-1 NO_FILE_EDIT invariant."""
        from themis.server import list_rules

        result = list_rules(role="intake")
        ids = [inv["id"] for inv in result[0]["invariants"]]
        assert "IN-INTAKE-1" in ids

    def test_verifier_phase_1_omits_no_file_edit(self):
        """verifier.yaml has no NO_FILE_EDIT in Phase 1 per spec."""
        from themis.server import list_rules

        result = list_rules(role="verifier")
        assert len(result) == 1
        assert result[0]["role"] == "verifier"
        ids = [inv["id"] for inv in result[0]["invariants"]]
        assert "IN-VERIFIER-NO_FILE_EDIT" not in ids

    def test_matcher_summary_populated(self):
        """Each invariant has a non-empty matcher_summary."""
        from themis.server import list_rules

        for entry in list_rules():
            for inv in entry["invariants"]:
                assert inv["matcher_summary"], f"empty matcher_summary on {inv['id']}"


# ---------------------------------------------------------------------------
# check (registered as mcp__khimaira__themis_check)
# ---------------------------------------------------------------------------


class TestCheck:
    def test_happy_path_no_violation(self):
        """Clean tool call returns {ok: true}."""
        from themis.server import check

        daemon_resp = _urlopen_response({"ok": True, "role": "agent"})
        with patch("urllib.request.urlopen", return_value=daemon_resp):
            result = check(
                session_id="session-agent",
                tool_name="Read",
                tool_input={"file_path": "/tmp/foo.py"},
            )

        assert result["ok"] is True

    def test_violation_returned(self):
        """Blocked tool call returns {ok: false, violation: {...}}."""
        from themis.server import check

        daemon_resp = _urlopen_response(
            {
                "ok": False,
                "role": "intake",
                "violation": {
                    "rule_id": "IN-INTAKE-1",
                    "name": "NO_FILE_EDIT",
                    "severity": "block",
                    "message": "🛑 Themis IN-INTAKE-1: intake cannot call Edit.",
                },
            }
        )
        with patch("urllib.request.urlopen", return_value=daemon_resp):
            result = check(
                session_id="session-intake",
                tool_name="Edit",
                tool_input={"file_path": "/tmp/x.py", "old_string": "a", "new_string": "b"},
            )

        assert result["ok"] is False
        assert result["violation"]["rule_id"] == "IN-INTAKE-1"
        assert result["violation"]["severity"] == "block"

    def test_cwd_forwarded_in_request(self):
        """cwd parameter is included in the POST body when provided."""
        from themis.server import check

        captured_body: list[bytes] = []
        daemon_resp = _urlopen_response({"ok": True})

        def capturing_urlopen(req, **kwargs):
            if hasattr(req, "data") and req.data:
                captured_body.append(req.data)
            return daemon_resp

        with patch("urllib.request.urlopen", side_effect=capturing_urlopen):
            check(
                session_id="s",
                tool_name="Bash",
                tool_input={"command": "ls"},
                cwd="/home/user/project",
            )

        assert captured_body, "no request was made"
        body = json.loads(captured_body[0])
        assert body["cwd"] == "/home/user/project"

    def test_daemon_error_returns_error_key(self):
        """HTTP error from daemon surfaces in the response rather than crashing."""
        from themis.server import check

        with patch("urllib.request.urlopen", side_effect=_urlopen_error(500, "internal error")):
            result = check("s", "Edit", {})

        assert result["ok"] is False
        assert "error" in result

    def test_daemon_down_fail_open(self):
        """URLError (daemon unreachable) → fail-open: {ok: true, error: '...'}.

        The real daemon-down path: urlopen raises urllib.error.URLError wrapping
        ConnectionRefusedError. This must NOT block the agent (fail-open per D7).
        """
        from themis.server import check

        err = urllib.error.URLError(reason=ConnectionRefusedError())
        with patch("urllib.request.urlopen", side_effect=err):
            result = check("s", "Edit", {})

        assert result["ok"] is True
        assert "error" in result

    def test_end_to_end_fixture_intake_no_file_edit(self):
        """End-to-end: simulate daemon calling engine for intake + Edit.

        Validates that the engine returns block on IN-INTAKE-1, then that
        the MCP wrapper correctly passes through the daemon verdict.
        """
        from themis.engine import evaluate
        from themis.server import check

        eval_result = evaluate("intake", "Edit", {})
        assert not eval_result.ok
        assert eval_result.violation is not None
        assert eval_result.violation.rule_id == "IN-INTAKE-1"
        assert eval_result.violation.severity.value == "block"

        # Simulate the daemon wrapping the engine result in the response shape
        daemon_payload = {
            "ok": eval_result.ok,
            "role": "intake",
            "violation": {
                "rule_id": eval_result.violation.rule_id,
                "name": eval_result.violation.name,
                "severity": eval_result.violation.severity.value,
                "message": eval_result.violation.message,
            },
        }
        with patch("urllib.request.urlopen", return_value=_urlopen_response(daemon_payload)):
            result = check("session-intake", "Edit", {})

        assert result["ok"] is False
        assert result["violation"]["rule_id"] == "IN-INTAKE-1"


# ---------------------------------------------------------------------------
# record_violation (registered as mcp__khimaira__themis_record_violation)
# ---------------------------------------------------------------------------


class TestRecordViolation:
    def test_happy_path_returns_logged_true(self):
        """Successful POST to daemon returns {logged: true, id: ...}."""
        from themis.server import record_violation

        daemon_resp = _urlopen_response({"logged": True, "id": "viol-abc123"})
        with patch("urllib.request.urlopen", return_value=daemon_resp):
            result = record_violation(
                session_id="session-intake",
                rule_id="IN-INTAKE-1",
                tool_name="Edit",
                tool_input={"file_path": "/x.py"},
                tool_use_id="toolu_abc",
                cwd="/home/user/project",
            )

        assert result["logged"] is True
        assert "id" in result

    def test_daemon_down_returns_logged_false(self):
        """Daemon unreachable → {logged: false, error: ...}."""
        from themis.server import record_violation

        with patch("urllib.request.urlopen", side_effect=ConnectionRefusedError()):
            result = record_violation(
                session_id="s", rule_id="IN-INTAKE-1", tool_name="Edit",
                tool_input={}, tool_use_id="t", cwd="/",
            )

        assert result["logged"] is False
        assert "error" in result

    def test_all_fields_forwarded(self):
        """All required fields are present in the POST body."""
        from themis.server import record_violation

        captured: list[dict] = []
        daemon_resp = _urlopen_response({"logged": True, "id": "x"})

        def capturing(req, **kwargs):
            if hasattr(req, "data") and req.data:
                captured.append(json.loads(req.data))
            return daemon_resp

        with patch("urllib.request.urlopen", side_effect=capturing):
            record_violation(
                session_id="s-123",
                rule_id="IN-MASTER-1",
                tool_name="Bash",
                tool_input={"command": "git commit --no-verify"},
                tool_use_id="toolu_xyz",
                cwd="/repo",
            )

        assert captured, "no request captured"
        body = captured[0]
        assert body["session_id"] == "s-123"
        assert body["rule_id"] == "IN-MASTER-1"
        assert body["tool_name"] == "Bash"
        assert body["tool_use_id"] == "toolu_xyz"
        assert body["cwd"] == "/repo"


# ---------------------------------------------------------------------------
# violations_for (registered as mcp__khimaira__themis_violations_for)
# ---------------------------------------------------------------------------


def _capture_request(daemon_payload: Any):
    """Return (capturing_fn, captured_reqs_list) for urllib.request.urlopen mock.

    Captures the full urllib.request.Request object so tests can inspect
    both the URL and headers.
    """
    captured: list[Any] = []
    resp = _urlopen_response(daemon_payload)

    def capturing(url_or_req, **kwargs):
        captured.append(url_or_req)
        return resp

    return capturing, captured


class TestViolationsFor:
    def test_happy_path_own_session(self):
        """Session reading its own violations returns unwrapped list."""
        from themis.server import violations_for

        records = [
            {
                "ts": "2026-05-21T10:00:00Z",
                "session_id": "s-123",
                "role": "intake",
                "rule_id": "IN-INTAKE-1",
                "tool_name": "Edit",
                "decision": "blocked",
            }
        ]
        # Daemon returns {violations: [...]}, not a bare list
        daemon_resp = _urlopen_response({"violations": records})
        with patch("urllib.request.urlopen", return_value=daemon_resp):
            result = violations_for(session_id="s-123")

        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["rule_id"] == "IN-INTAKE-1"

    def test_cross_session_sends_x_session_id_header(self):
        """violations_for passes X-Session-ID header so daemon can enforce D12.

        The MCP layer carries the caller's identity in the header (not just as
        a query param) so the daemon can distinguish caller from filter.
        """
        import themis.server as server_mod

        from themis.server import violations_for

        fn, captured = _capture_request({"violations": []})
        with patch("urllib.request.urlopen", side_effect=fn):
            with patch.object(server_mod, "_CALLER_SESSION_ID", "caller-abc"):
                violations_for(session_id="master-session")

        assert captured, "no request made"
        req = captured[0]
        # urllib.request.Request stores headers capitalized (first letter only)
        assert req.get_header("X-session-id") == "caller-abc", (
            f"X-Session-ID header missing or wrong; got headers: {dict(req.header_items())}"
        )
        assert "session_id=master-session" in req.full_url

    def test_role_filter_included_in_request(self):
        """role= param is passed to daemon as a query param."""
        from themis.server import violations_for

        fn, captured = _capture_request({"violations": []})
        with patch("urllib.request.urlopen", side_effect=fn):
            violations_for(role="intake", limit=10)

        assert captured, "no request made"
        url = captured[0].full_url
        assert "role=intake" in url
        assert "limit=10" in url

    def test_daemon_down_returns_empty_list(self):
        """Daemon unreachable (URLError) returns [] without crashing."""
        from themis.server import violations_for

        err = urllib.error.URLError(reason=ConnectionRefusedError())
        with patch("urllib.request.urlopen", side_effect=err):
            result = violations_for(session_id="s")

        assert result == []

    def test_since_param_forwarded(self):
        """since= ISO 8601 param is forwarded to daemon."""
        from themis.server import violations_for

        fn, captured = _capture_request({"violations": []})
        ts = "2026-05-21T00:00:00Z"
        with patch("urllib.request.urlopen", side_effect=fn):
            violations_for(since=ts)

        assert captured
        assert "since=" in captured[0].full_url

    def test_none_params_not_in_url(self):
        """None-valued optional params are not appended as 'None' strings."""
        from themis.server import violations_for

        fn, captured = _capture_request({"violations": []})
        with patch("urllib.request.urlopen", side_effect=fn):
            violations_for()  # all None except limit=50

        assert captured
        url = captured[0].full_url
        assert "None" not in url
        assert "limit=50" in url

    def test_response_envelope_unwrapped(self):
        """Daemon's {violations: [...]} envelope is unwrapped to a bare list."""
        from themis.server import violations_for

        records = [{"rule_id": "IN-INTAKE-1"}, {"rule_id": "IN-AGENT-2"}]
        daemon_resp = _urlopen_response({"violations": records})
        with patch("urllib.request.urlopen", return_value=daemon_resp):
            result = violations_for()

        assert result == records


# ---------------------------------------------------------------------------
# sibling_tools wire-up: themis_ prefix applied by register_sibling_tools
# ---------------------------------------------------------------------------


class TestSiblingRegistration:
    def test_themis_in_sibling_packages(self):
        """'themis' is in khimaira's SIBLING_PACKAGES tuple."""
        from khimaira.server.sibling_tools import SIBLING_PACKAGES

        assert "themis" in SIBLING_PACKAGES

    def test_register_sibling_tools_includes_themis(self):
        """register_sibling_tools adds themis_ prefix and registers 5 tools."""
        from mcp.server.fastmcp import FastMCP

        from khimaira.server.sibling_tools import register_sibling_tools

        dummy_mcp = FastMCP("test-mcp")
        register_sibling_tools(dummy_mcp)

        registered = [t.name for t in dummy_mcp._tool_manager.list_tools()]
        themis_tools = [n for n in registered if n.startswith("themis_")]
        assert len(themis_tools) == 5, f"expected 5 themis tools, got {themis_tools}"
        assert "themis_my_rules" in themis_tools
        assert "themis_list_rules" in themis_tools
        assert "themis_check" in themis_tools
        assert "themis_record_violation" in themis_tools
        assert "themis_violations_for" in themis_tools
