"""Tests for khimaira.server.monitor_tools._get / _post error mapping.

Regression for the 2026-05-10 "daemon unreachable" mystery: _get was
catching urllib.error.URLError (which includes HTTPError as a subclass)
before HTTPError, so ANY 4xx/5xx response got mapped to the
"daemon down" hint. That made an unknown-session-id 404 look exactly
like a real daemon outage.

The fix differentiates:
  HTTPError                       → surface real status + detail
  URLError(ConnectionRefusedError) → truly down (the original case)
  URLError(other)                 → transient; suggest retry
"""

from __future__ import annotations

import io
import urllib.error
from unittest.mock import MagicMock, patch

import pytest


def test_get_http_404_surfaces_real_status():
    """A 404 from the daemon should NOT be mapped to 'daemon down'."""
    from khimaira.server.monitor_tools import _get

    fake_response = MagicMock()
    fake_response.read.return_value = b'{"detail":"No session named or id\'d \'foo\'."}'

    http_error = urllib.error.HTTPError(
        url="http://127.0.0.1:8740/api/sessions/foo/pending",
        code=404, msg="Not Found", hdrs={}, fp=io.BytesIO(b'{"detail":"No session named or id\'d \'foo\'."}'),
    )

    with patch("urllib.request.urlopen", side_effect=http_error):
        result = _get("/api/sessions/foo/pending")

    assert isinstance(result, str)
    assert "HTTP 404" in result
    assert "No session" in result
    # CRITICAL: the misleading "daemon is not running" hint must NOT appear
    assert "daemon is not running" not in result


def test_get_http_500_surfaces_real_status():
    """500s also don't get masked."""
    from khimaira.server.monitor_tools import _get

    http_error = urllib.error.HTTPError(
        url="http://127.0.0.1:8740/api/whatever",
        code=500, msg="Internal Server Error", hdrs={},
        fp=io.BytesIO(b'{"detail":"boom"}'),
    )
    with patch("urllib.request.urlopen", side_effect=http_error):
        result = _get("/api/whatever")
    assert "HTTP 500" in result
    assert "daemon is not running" not in result


def test_get_connection_refused_says_daemon_down():
    """Actual connection refused → the original 'daemon down' hint IS right."""
    from khimaira.server.monitor_tools import _get

    url_error = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    with patch("urllib.request.urlopen", side_effect=url_error):
        result = _get("/api/whatever")
    assert "daemon is not running" in result.lower()


def test_get_transient_timeout_suggests_retry():
    """Timeouts / DNS / other transient URLErrors should suggest retry,
    not falsely say daemon is down."""
    from khimaira.server.monitor_tools import _get

    url_error = urllib.error.URLError(TimeoutError("timeout"))
    with patch("urllib.request.urlopen", side_effect=url_error):
        result = _get("/api/whatever")
    assert "transient" in result.lower()
    assert "retry" in result.lower()
    # Not the daemon-down case
    assert "daemon is not running" not in result


def test_get_success_returns_parsed_json():
    """Happy path — JSON response parses correctly."""
    from khimaira.server.monitor_tools import _get

    fake_resp = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=None)
    fake_resp.read.return_value = b'{"key": "value"}'

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = _get("/api/whatever")
    assert result == {"key": "value"}


def test_get_malformed_json_surfaces_parse_error():
    from khimaira.server.monitor_tools import _get

    fake_resp = MagicMock()
    fake_resp.__enter__ = MagicMock(return_value=fake_resp)
    fake_resp.__exit__ = MagicMock(return_value=None)
    fake_resp.read.return_value = b'<html>not json</html>'

    with patch("urllib.request.urlopen", return_value=fake_resp):
        result = _get("/api/whatever")
    assert "non-JSON" in result
