"""Unit tests for khimaira.hooks.mnemosyne_client (stdlib-only HTTP client).

Tests cover distill() and query() happy paths, fail-open on network errors,
and the "No accumulated memory" sentinel filtering in query().
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import urllib.error

import pytest

from unittest.mock import patch as _patch  # noqa: F401 (alias kept for clarity)

import khimaira.hooks.mnemosyne_client as _mc
from khimaira.hooks.mnemosyne_client import ask_oracle, distill, query


def _completion(content: str) -> bytes:
    return json.dumps(
        {"choices": [{"message": {"content": content}}], "model": "khimaira", "usage": {}}
    ).encode()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_resp(body: bytes):
    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

        def read(self):
            return body

    return _Resp()


# ---------------------------------------------------------------------------
# distill()
# ---------------------------------------------------------------------------


def test_distill_posts_correct_payload():
    captured: list[dict] = []

    def _fake_urlopen(req, timeout=None):
        captured.append(json.loads(req.data))
        return _fake_resp(b'{"stored": true}')

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        result = distill("khimaira:backend", "some transcript", "backend-lead-1")

    assert result == {"stored": True}
    assert len(captured) == 1
    assert captured[0]["domain"] == "khimaira:backend"
    assert captured[0]["transcript"] == "some transcript"
    assert captured[0]["session_slug"] == "backend-lead-1"


def test_distill_returns_none_on_url_error():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        result = distill("khimaira:backend", "transcript", "slug")
    assert result is None


def test_distill_returns_none_on_timeout():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = distill("khimaira:backend", "transcript", "slug")
    assert result is None


def test_distill_returns_none_on_os_error():
    with patch("urllib.request.urlopen", side_effect=OSError("os error")):
        result = distill("khimaira:backend", "transcript", "slug")
    assert result is None


# ---------------------------------------------------------------------------
# query()
# ---------------------------------------------------------------------------


def test_query_uses_post_verb():
    captured: list[str] = []
    payload = {
        "domain": "khimaira:backend",
        "answer": "# Domain memory\nQ: something\nA: reply",
        "training_pairs_available": 1,
    }

    def _fake_urlopen(req, timeout=None):
        captured.append(req.method)
        return _fake_resp(json.dumps(payload).encode())

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        query("khimaira:backend")

    assert captured == ["POST"]


def test_query_returns_dict_with_answer():
    payload = {
        "domain": "khimaira:backend",
        "question": "recent context for khimaira:backend",
        "answer": "# Domain memory — khimaira:backend (3 of 3 pairs)\nQ: something\nA: reply",
        "training_pairs_available": 3,
    }

    with patch(
        "urllib.request.urlopen", return_value=_fake_resp(json.dumps(payload).encode())
    ):
        result = query("khimaira:backend")

    assert result is not None
    assert result["domain"] == "khimaira:backend"
    assert result["training_pairs_available"] == 3
    assert "Domain memory" in result["answer"]


def test_query_returns_none_when_no_accumulated_memory():
    payload = {
        "domain": "khimaira:backend",
        "answer": "No accumulated memory for khimaira:backend",
        "training_pairs_available": 0,
    }

    with patch(
        "urllib.request.urlopen", return_value=_fake_resp(json.dumps(payload).encode())
    ):
        result = query("khimaira:backend")

    assert result is None


def test_query_returns_none_when_answer_empty():
    payload = {
        "domain": "khimaira:backend",
        "answer": "",
        "training_pairs_available": 0,
    }

    with patch(
        "urllib.request.urlopen", return_value=_fake_resp(json.dumps(payload).encode())
    ):
        result = query("khimaira:backend")

    assert result is None


def test_query_returns_none_on_url_error():
    with patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        result = query("khimaira:backend")
    assert result is None


def test_query_returns_none_on_timeout():
    with patch("urllib.request.urlopen", side_effect=TimeoutError("timed out")):
        result = query("khimaira:backend")
    assert result is None


# ---------------------------------------------------------------------------
# ask_oracle() — proxy primary + direct-upstream fallback (#31)
# ---------------------------------------------------------------------------


def test_ask_oracle_returns_answer_from_primary():
    with patch("urllib.request.urlopen", return_value=_fake_resp(_completion("grounded answer"))):
        result = ask_oracle("a question", project="khimaira")
    assert result is not None
    assert result["answer"] == "grounded answer"


def test_ask_oracle_falls_back_to_direct_on_transport_error():
    """Proxy (primary) unreachable → retry the direct upstream before giving up."""
    calls: list[str] = []

    def _fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        if "18100" in req.full_url:  # the proxy is down
            raise urllib.error.URLError("connection refused")
        return _fake_resp(_completion("answer via direct fallback"))

    with _patch.dict(_mc._ORACLES, {"khimaira": ("http://localhost:18100", "khimaira")}), \
         _patch.dict(_mc._ORACLE_DIRECT, {"khimaira": "http://192.168.1.117:18000"}), \
         patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        result = ask_oracle("a question", project="khimaira")

    assert result is not None
    assert result["answer"] == "answer via direct fallback"
    assert len(calls) == 2  # proxy tried, then direct
    assert "18100" in calls[0] and "18000" in calls[1]


def test_ask_oracle_no_fallback_on_empty_answer():
    """A reached-but-empty response must NOT trigger a direct retry (oracle answered)."""
    calls: list[str] = []

    def _fake_urlopen(req, timeout=None):
        calls.append(req.full_url)
        return _fake_resp(_completion(""))  # reached, but no content

    with _patch.dict(_mc._ORACLES, {"khimaira": ("http://localhost:18100", "khimaira")}), \
         _patch.dict(_mc._ORACLE_DIRECT, {"khimaira": "http://192.168.1.117:18000"}), \
         patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        result = ask_oracle("a question", project="khimaira")

    assert result is None
    assert len(calls) == 1  # no fallback — the oracle was reached


def test_ask_oracle_returns_none_when_both_unreachable():
    with _patch.dict(_mc._ORACLES, {"khimaira": ("http://localhost:18100", "khimaira")}), \
         _patch.dict(_mc._ORACLE_DIRECT, {"khimaira": "http://192.168.1.117:18000"}), \
         patch("urllib.request.urlopen", side_effect=urllib.error.URLError("refused")):
        result = ask_oracle("a question", project="khimaira")
    assert result is None
