"""Shared pytest fixtures for Specter tests.

Provides:
  mock_cdp        — async-mock CDPConnection with configurable send() responses
  fixture_page    — temp HTTP server serving fixture.html; teardown closes it
  chrome_or_skip  — skip integration tests when Chrome/Firefox not reachable
"""

from __future__ import annotations

import asyncio
import http.server
import os
import socket
import socketserver
import threading
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from specter.browser.connection import CDPConnection, Target
from specter.config import load_config

FIXTURE_HTML = Path(__file__).parent / "fixture.html"


# ---------------------------------------------------------------------------
# mock_cdp — mocked CDPConnection for unit tests
# ---------------------------------------------------------------------------


class MockCDPConnection:
    """Minimal CDPConnection substitute that records calls + returns canned data.

    Usage:
        conn = MockCDPConnection()
        conn.set_response("Runtime.evaluate", {"result": {"type": "string", "value": '{"url":"http://localhost"}'}})
        result = await conn.send("Runtime.evaluate", ...)
    """

    def __init__(self) -> None:
        self._responses: dict[str, Any] = {}
        self._default_response: dict = {}
        self.calls: list[dict] = []
        self.is_connected = True
        self.current_target = Target(
            id="mock-target-1",
            title="Mock Page",
            url="http://localhost/fixture.html",
            ws_url="ws://localhost/mock",
            type="page",
        )
        self._event_handlers: dict[str, list] = {}

    def set_response(self, method: str, response: dict) -> None:
        """Configure return value for a specific CDP method."""
        self._responses[method] = response

    def set_default_response(self, response: dict) -> None:
        """Fallback for any method not explicitly configured."""
        self._default_response = response

    async def send(self, method: str, params: dict | None = None) -> dict:
        self.calls.append({"method": method, "params": params or {}})
        return self._responses.get(method, self._default_response)

    def on(self, event: str, handler) -> None:
        self._event_handlers.setdefault(event, []).append(handler)

    def fire_event(self, event: str, params: dict) -> None:
        """Test helper: simulate an incoming CDP event."""
        for handler in self._event_handlers.get(event, []):
            handler(params)

    async def disconnect(self) -> None:
        self.is_connected = False


@pytest.fixture
def mock_cdp() -> MockCDPConnection:
    """Return a MockCDPConnection pre-configured for unit tests."""
    return MockCDPConnection()


# ---------------------------------------------------------------------------
# fixture_page — HTTP server serving fixture.html
# ---------------------------------------------------------------------------


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    """Serve fixture.html for any GET request; 404 for anything else."""

    def do_GET(self):
        if not FIXTURE_HTML.exists():
            self.send_response(404)
            self.end_headers()
            return
        body = FIXTURE_HTML.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # suppress access logs during tests


@pytest.fixture
def fixture_page():
    """Spin up a temp HTTP server serving fixture.html.

    Yields the URL (e.g., ``http://127.0.0.1:PORT``).
    Closes the server on teardown.
    """
    server = socketserver.TCPServer(("127.0.0.1", 0), _FixtureHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.shutdown()    # stop serve_forever loop
        server.server_close()  # release the socket


# ---------------------------------------------------------------------------
# chrome_or_skip — gate for integration tests
# ---------------------------------------------------------------------------

def _chrome_reachable() -> bool:
    """Return True if a CDP debug port is reachable."""
    port = int(os.environ.get("SPECTER_DEBUG_PORT", os.environ.get("CHROME_DEBUGGING_PORT", "9222")))
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


@pytest.fixture
def chrome_or_skip():
    """Skip the test if Chrome/Firefox debug port is not reachable.

    Attach to a test that needs a live browser:

        def test_something(chrome_or_skip):
            ...

    Set SPECTER_DEBUG_PORT env var to override the default port (9222).
    Integration tests are excluded from default CI runs; they require a
    browser launched with --remote-debugging-port=9222 (or equivalent).
    """
    if not _chrome_reachable():
        pytest.skip("Chrome/Firefox not running with remote debugging enabled")
