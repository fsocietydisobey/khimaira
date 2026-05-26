"""Unit tests for khimaira.hooks.mnemosyne_client (stdlib-only HTTP client).

Tests cover distill() and query() happy paths, fail-open on network errors,
and the "No accumulated memory" sentinel filtering in query().
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import urllib.error

import pytest

from khimaira.hooks.mnemosyne_client import distill, query

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
