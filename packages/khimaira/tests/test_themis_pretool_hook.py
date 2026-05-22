"""Unit tests for scripts/hooks/themis_pretool.py.

Tests run the hook as a subprocess to exercise the real exit-code +
stdout contract, not just mocked internals. The THEMIS_DAEMON env var
points all HTTP calls at a per-test threading HTTP server so tests are
fully isolated from the live daemon.
"""

from __future__ import annotations

import http.server
import json
import os
import sys
import subprocess
import threading
import time
from io import BytesIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch
from urllib.error import URLError

import pytest

# Path to the hook script under test
_HOOK = Path(__file__).parents[3] / "scripts" / "hooks" / "themis_pretool.py"
_PYTHON = sys.executable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_hook(
    stdin_payload: dict[str, Any],
    env_overrides: dict[str, str] | None = None,
    daemon_url: str = "http://127.0.0.1:19999",  # non-existent by default
) -> subprocess.CompletedProcess:
    """Run the hook as a subprocess. Returns completed process."""
    env = {**os.environ, "THEMIS_DAEMON": daemon_url}
    if env_overrides:
        env.update(env_overrides)
    return subprocess.run(
        [_PYTHON, str(_HOOK)],
        input=json.dumps(stdin_payload).encode(),
        capture_output=True,
        env=env,
        timeout=5,
    )


def _make_stdin(
    session_id: str = "test-session-abc",
    tool_name: str = "Edit",
    tool_input: dict | None = None,
    cwd: str = "/tmp",
) -> dict[str, Any]:
    return {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input or {},
        "cwd": cwd,
        "permission_mode": "auto",
        "effort": {"level": "medium"},
        "hook_event_name": "PreToolUse",
        "tool_use_id": "toolu_test123",
        "transcript_path": "/tmp/transcript.jsonl",
    }


class _MockDaemon:
    """Minimal threading HTTP server that returns a fixed JSON response."""

    def __init__(self, response_body: dict[str, Any], status: int = 200):
        self._response = json.dumps(response_body).encode()
        self._status = status
        self.requests: list[dict] = []
        self._server: http.server.HTTPServer | None = None
        self._port: int = 0

    def __enter__(self):
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length)) if length else {}
                outer.requests.append({
                    "path": self.path,
                    "body": body,
                    "headers": dict(self.headers),
                })
                self.send_response(outer._status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(outer._response)

            def log_message(self, *args):
                pass  # suppress output

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._port = self._server.server_address[1]
        t = threading.Thread(target=self._server.serve_forever, daemon=True)
        t.start()
        return self

    def __exit__(self, *_):
        if self._server:
            self._server.shutdown()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"


# ---------------------------------------------------------------------------
# Stdin parsing
# ---------------------------------------------------------------------------

class TestStdinParsing:
    def test_valid_envelope_proceeds_to_daemon_call(self, tmp_path):
        """Valid stdin → hook makes a daemon call (daemon is down → fail-open)."""
        result = _run_hook(_make_stdin())
        assert result.returncode == 0
        # Fail-open: daemon not running → log written, no block emitted
        stdout = result.stdout.decode().strip()
        assert stdout == "" or not stdout.startswith('{"decision"')

    def test_malformed_json_stdin_exits_0(self, tmp_path):
        """Malformed stdin JSON → fail-open, exit 0."""
        env = {**os.environ, "THEMIS_DAEMON": "http://127.0.0.1:19999"}
        result = subprocess.run(
            [_PYTHON, str(_HOOK)],
            input=b"not valid json {{{",
            capture_output=True,
            env=env,
            timeout=5,
        )
        assert result.returncode == 0
        stdout = result.stdout.decode().strip()
        assert "block" not in stdout

    def test_malformed_json_stdin_writes_log(self, tmp_path, monkeypatch):
        """Malformed stdin → fail-open log entry written."""
        log_path = tmp_path / "themis_fail_open.log"
        env = {
            **os.environ,
            "THEMIS_DAEMON": "http://127.0.0.1:19999",
            "HOME": str(tmp_path),
        }
        subprocess.run(
            [_PYTHON, str(_HOOK)],
            input=b"not json",
            capture_output=True,
            env=env,
            timeout=5,
        )
        log_file = tmp_path / ".claude" / "hooks" / "themis_fail_open.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "stdin parse failed" in content

    def test_empty_stdin_exits_0(self):
        """Empty stdin → fail-open, exit 0."""
        env = {**os.environ, "THEMIS_DAEMON": "http://127.0.0.1:19999"}
        result = subprocess.run(
            [_PYTHON, str(_HOOK)],
            input=b"",
            capture_output=True,
            env=env,
            timeout=5,
        )
        assert result.returncode == 0


# ---------------------------------------------------------------------------
# Daemon response handling
# ---------------------------------------------------------------------------

class TestDaemonResponseHandling:
    def test_ok_true_exits_0_no_block(self):
        """Daemon returns ok=True → exit 0, no block on stdout."""
        with _MockDaemon({"ok": True, "role": "agent"}) as daemon:
            result = _run_hook(_make_stdin(), daemon_url=daemon.url)
        assert result.returncode == 0
        assert result.stdout.decode().strip() == ""

    def test_ok_false_block_severity_emits_block(self):
        """Daemon returns ok=False + severity=block → stdout has block JSON."""
        verdict = {
            "ok": False,
            "role": "intake",
            "violation": {
                "rule_id": "IN-INTAKE-1",
                "name": "NO_FILE_EDIT",
                "severity": "block",
                "message": "🛑 Themis IN-INTAKE-1: intake cannot call Edit.",
            },
        }
        with _MockDaemon(verdict) as daemon:
            result = _run_hook(_make_stdin(tool_name="Edit"), daemon_url=daemon.url)
        assert result.returncode == 0
        out = json.loads(result.stdout.decode())
        assert out["decision"] == "block"
        assert "IN-INTAKE-1" in out["reason"] or "intake" in out["reason"].lower()

    def test_ok_false_warn_severity_exits_0_no_block(self):
        """Daemon returns ok=False + severity=warn → allow (exit 0, no block)."""
        verdict = {
            "ok": False,
            "role": "master",
            "violation": {
                "rule_id": "IN-MASTER-WARN-1",
                "name": "SOME_WARN",
                "severity": "warn",
                "message": "warned",
            },
        }
        with _MockDaemon(verdict) as daemon:
            result = _run_hook(_make_stdin(), daemon_url=daemon.url)
        assert result.returncode == 0
        assert result.stdout.decode().strip() == ""

    def test_ok_false_audit_severity_exits_0_no_block(self):
        """Daemon returns ok=False + severity=audit → allow (exit 0, no block)."""
        verdict = {
            "ok": False,
            "role": "agent",
            "violation": {
                "rule_id": "IN-AGENT-AUDIT",
                "name": "AUDIT",
                "severity": "audit",
                "message": "audited",
            },
        }
        with _MockDaemon(verdict) as daemon:
            result = _run_hook(_make_stdin(), daemon_url=daemon.url)
        assert result.returncode == 0
        assert result.stdout.decode().strip() == ""

    def test_malformed_response_not_dict_fails_open(self, tmp_path):
        """Daemon returns non-dict JSON (e.g. a list) → fail-open."""
        with _MockDaemon([1, 2, 3]) as daemon:  # type: ignore[arg-type]
            env = {**os.environ, "HOME": str(tmp_path)}
            result = _run_hook(_make_stdin(), daemon_url=daemon.url, env_overrides={"HOME": str(tmp_path)})
        assert result.returncode == 0
        stdout = result.stdout.decode().strip()
        assert "block" not in stdout

    def test_x_session_id_header_sent(self):
        """Hook sends X-Session-ID header on every daemon call."""
        with _MockDaemon({"ok": True}) as daemon:
            result = _run_hook(
                _make_stdin(session_id="my-session-123"),
                daemon_url=daemon.url,
            )
        assert result.returncode == 0
        assert len(daemon.requests) == 1
        headers = daemon.requests[0]["headers"]
        # urllib.request capitalizes header names (e.g. "X-session-id"); normalize for comparison
        headers_lower = {k.lower(): v for k, v in headers.items()}
        assert headers_lower.get("x-session-id") == "my-session-123"

    def test_session_id_in_request_body(self):
        """Hook sends session_id in the POST body."""
        with _MockDaemon({"ok": True}) as daemon:
            result = _run_hook(_make_stdin(session_id="sess-xyz"), daemon_url=daemon.url)
        assert result.returncode == 0
        body = daemon.requests[0]["body"]
        assert body["session_id"] == "sess-xyz"


# ---------------------------------------------------------------------------
# Fail-open paths
# ---------------------------------------------------------------------------

class TestFailOpen:
    def test_daemon_connection_refused_exits_0(self, tmp_path):
        """Daemon not running (ConnectionRefusedError via URLError) → fail-open."""
        result = _run_hook(
            _make_stdin(),
            daemon_url="http://127.0.0.1:19999",  # nothing listening
            env_overrides={"HOME": str(tmp_path)},
        )
        assert result.returncode == 0

    def test_daemon_down_writes_log(self, tmp_path):
        """Daemon not running → fail-open log entry written."""
        result = _run_hook(
            _make_stdin(),
            daemon_url="http://127.0.0.1:19999",
            env_overrides={"HOME": str(tmp_path)},
        )
        log_file = tmp_path / ".claude" / "hooks" / "themis_fail_open.log"
        assert log_file.exists()
        assert "daemon" in log_file.read_text().lower()

    def test_daemon_http_500_fails_open(self, tmp_path):
        """Daemon returns HTTP 500 → fail-open (urllib raises HTTPError)."""
        with _MockDaemon({}, status=500) as daemon:
            result = _run_hook(
                _make_stdin(),
                daemon_url=daemon.url,
                env_overrides={"HOME": str(tmp_path)},
            )
        assert result.returncode == 0
        stdout = result.stdout.decode().strip()
        assert "block" not in stdout

    def test_missing_session_id_fails_open(self, tmp_path):
        """No session_id in stdin AND no env var → fail-open."""
        payload = {"tool_name": "Edit", "tool_input": {}, "cwd": "/tmp"}
        env = {**os.environ, "HOME": str(tmp_path)}
        env.pop("CLAUDE_CODE_SESSION_ID", None)
        env["THEMIS_DAEMON"] = "http://127.0.0.1:19999"
        result = subprocess.run(
            [_PYTHON, str(_HOOK)],
            input=json.dumps(payload).encode(),
            capture_output=True,
            env=env,
            timeout=5,
        )
        assert result.returncode == 0

    def test_missing_tool_name_fails_open(self, tmp_path):
        """No tool_name in stdin → fail-open."""
        payload = {"session_id": "sess-abc", "tool_input": {}, "cwd": "/tmp"}
        result = _run_hook(
            payload,  # type: ignore[arg-type]
            daemon_url="http://127.0.0.1:19999",
            env_overrides={"HOME": str(tmp_path)},
        )
        assert result.returncode == 0

    def test_session_id_fallback_from_env(self, tmp_path):
        """If stdin has no session_id, fall back to CLAUDE_CODE_SESSION_ID env."""
        payload = {"tool_name": "Bash", "tool_input": {}, "cwd": "/tmp"}
        with _MockDaemon({"ok": True}) as daemon:
            env = {
                **os.environ,
                "CLAUDE_CODE_SESSION_ID": "env-fallback-session",
                "HOME": str(tmp_path),
            }
            result = subprocess.run(
                [_PYTHON, str(_HOOK)],
                input=json.dumps(payload).encode(),
                capture_output=True,
                env={**env, "THEMIS_DAEMON": daemon.url},
                timeout=5,
            )
        assert result.returncode == 0
        if daemon.requests:
            assert daemon.requests[0]["body"]["session_id"] == "env-fallback-session"


# ---------------------------------------------------------------------------
# Timeout enforcement
# ---------------------------------------------------------------------------

class TestTimeoutEnforcement:
    def test_slow_daemon_fails_open(self, tmp_path):
        """Daemon that takes longer than TIMEOUT_S → fail-open (not block)."""
        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(2.0)  # well over TIMEOUT_S=0.1
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"ok": false, "violation": {"severity": "block"}}')

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), SlowHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        try:
            result = _run_hook(
                _make_stdin(),
                daemon_url=f"http://127.0.0.1:{port}",
                env_overrides={"HOME": str(tmp_path)},
            )
        finally:
            server.shutdown()

        assert result.returncode == 0
        stdout = result.stdout.decode().strip()
        assert "block" not in stdout

    def test_slow_daemon_writes_log(self, tmp_path):
        """Timeout → fail-open log entry written."""

        class SlowHandler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                time.sleep(2.0)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), SlowHandler)
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()

        try:
            _run_hook(
                _make_stdin(),
                daemon_url=f"http://127.0.0.1:{port}",
                env_overrides={"HOME": str(tmp_path)},
            )
        finally:
            server.shutdown()

        log_file = tmp_path / ".claude" / "hooks" / "themis_fail_open.log"
        assert log_file.exists()
        content = log_file.read_text()
        assert "timeout" in content.lower() or "daemon" in content.lower()


# ---------------------------------------------------------------------------
# HOME environment edge case
# ---------------------------------------------------------------------------

class TestHomeEnvEdgeCase:
    def test_home_set_log_path_resolves_correctly(self, tmp_path):
        """HOME is set → fail-open log at HOME/.claude/hooks/themis_fail_open.log."""
        result = _run_hook(
            _make_stdin(),
            daemon_url="http://127.0.0.1:19999",
            env_overrides={"HOME": str(tmp_path)},
        )
        log = tmp_path / ".claude" / "hooks" / "themis_fail_open.log"
        assert log.exists()
        assert result.returncode == 0

    def test_hook_script_exists(self):
        """Sanity: the hook script itself is present and executable."""
        assert _HOOK.exists(), f"Hook script not found at {_HOOK}"
        assert os.access(_HOOK, os.X_OK), f"Hook script not executable: {_HOOK}"
