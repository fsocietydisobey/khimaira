"""Tests for POST /api/oracle/query (oracle.py).

Covers:
- Happy path: both stores return data → full context, degraded=False
- Séance errored → degraded=True (exception, not empty result)
- Séance empty (ok, unindexed) → degraded=False
- Mnemosyne empty (None) → degraded=False (can't distinguish down-from-empty; treat as empty)
- Both stores empty → degraded=False (valid state; section text explains)
- Staleness note always present in every response
- project arg reaches both store calls
"""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


@pytest.fixture
def oracle_client() -> TestClient:
    """FastAPI TestClient wired to the oracle router only."""
    from khimaira.monitor.api import oracle as oracle_api

    app = FastAPI()
    app.include_router(oracle_api.build_router(), prefix="/api")
    return TestClient(app)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SEANCE_CHUNK = {
    "file_path": "src/foo.py",
    "symbol_name": "bar",
    "chunk_type": "function",
    "language": "python",
    "start_line": 10,
    "end_line": 20,
    "score": 0.1234,
    "text": "def bar(): pass",
}

_MNEMOSYNE_RESULT: dict[str, Any] = {
    "domain": "khimaira:backend",
    "question": "recent context for khimaira:backend",
    "answer": "Q: How does auth work?\nA: JWT via middleware.",
    "training_pairs_available": 3,
}

# _seance_search returns (results, errored: bool)
_SEANCE_OK = ([_SEANCE_CHUNK], False)       # healthy, results found
_SEANCE_EMPTY_OK = ([], False)              # healthy, no results (unindexed / no match)
_SEANCE_ERRORED = ([], True)               # exception raised (import fail / API error)


def _call_oracle(
    client: TestClient,
    question: str = "how does auth work?",
    project: str = "khimaira",
    scope: str = "all",
) -> dict:
    resp = client.post(
        "/api/oracle/query",
        json={"question": question, "project": project, "scope": scope, "mode": "context"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_oracle_happy_path_both_stores(oracle_client: TestClient) -> None:
    """Both stores return data → full context, degraded=False, both cited."""
    with (
        patch("khimaira.monitor.api.oracle._seance_search", return_value=_SEANCE_OK),
        patch(
            "khimaira.monitor.api.oracle._mnemosyne_query",
            return_value=_MNEMOSYNE_RESULT,
        ),
    ):
        data = _call_oracle(oracle_client)

    assert data["mode"] == "context"
    assert data["degraded"] is False
    assert set(data["stores_hit"]) == {"seance", "mnemosyne"}
    assert len(data["citations"]) == 2

    stores = {c["store"] for c in data["citations"]}
    assert stores == {"seance", "mnemosyne"}

    seance_cit = next(c for c in data["citations"] if c["store"] == "seance")
    assert seance_cit["ref"] == "src/foo.py:10-20 (bar)"
    assert abs(seance_cit["score"] - 0.1234) < 1e-4

    context = data["context"]
    assert "Séance" in context
    assert "src/foo.py:10-20" in context
    assert "mnemosyne" in context
    assert "JWT via middleware" in context


def test_oracle_seance_errored_sets_degraded(oracle_client: TestClient) -> None:
    """Séance raises an exception → degraded=True (real failure, not empty result)."""
    with (
        patch("khimaira.monitor.api.oracle._seance_search", return_value=_SEANCE_ERRORED),
        patch(
            "khimaira.monitor.api.oracle._mnemosyne_query",
            return_value=_MNEMOSYNE_RESULT,
        ),
    ):
        data = _call_oracle(oracle_client)

    assert data["degraded"] is True
    assert "seance" not in data["stores_hit"]
    assert "mnemosyne" in data["stores_hit"]
    assert "JWT via middleware" in data["context"]


def test_oracle_seance_empty_ok_not_degraded(oracle_client: TestClient) -> None:
    """Séance healthy but returned nothing (unindexed / no match) → degraded=False."""
    with (
        patch("khimaira.monitor.api.oracle._seance_search", return_value=_SEANCE_EMPTY_OK),
        patch(
            "khimaira.monitor.api.oracle._mnemosyne_query",
            return_value=_MNEMOSYNE_RESULT,
        ),
    ):
        data = _call_oracle(oracle_client)

    assert data["degraded"] is False
    assert "seance" not in data["stores_hit"]  # no data returned
    assert "mnemosyne" in data["stores_hit"]


def test_oracle_mnemosyne_empty_not_degraded(oracle_client: TestClient) -> None:
    """Mnemosyne returns None (empty domain / down — v1 can't distinguish) → degraded=False."""
    with (
        patch("khimaira.monitor.api.oracle._seance_search", return_value=_SEANCE_OK),
        patch("khimaira.monitor.api.oracle._mnemosyne_query", return_value=None),
    ):
        data = _call_oracle(oracle_client)

    assert data["degraded"] is False
    assert "seance" in data["stores_hit"]
    assert "mnemosyne" not in data["stores_hit"]
    assert "src/foo.py:10-20" in data["context"]
    assert len(data["citations"]) == 1
    assert data["citations"][0]["store"] == "seance"


def test_oracle_both_stores_empty_not_degraded(oracle_client: TestClient) -> None:
    """Both stores return nothing (but no exception) → degraded=False, no citations."""
    with (
        patch("khimaira.monitor.api.oracle._seance_search", return_value=_SEANCE_EMPTY_OK),
        patch("khimaira.monitor.api.oracle._mnemosyne_query", return_value=None),
    ):
        data = _call_oracle(oracle_client)

    assert data["degraded"] is False
    assert data["stores_hit"] == []
    assert data["citations"] == []
    assert data["mode"] == "context"
    assert isinstance(data["context"], str)


def test_oracle_staleness_note_always_present(oracle_client: TestClient) -> None:
    """seance_index_note is present in every response."""
    with (
        patch("khimaira.monitor.api.oracle._seance_search", return_value=_SEANCE_EMPTY_OK),
        patch("khimaira.monitor.api.oracle._mnemosyne_query", return_value=None),
    ):
        data = _call_oracle(oracle_client)

    assert "seance_index_note" in data
    assert (
        "stale" in data["seance_index_note"].lower()
        or "lag" in data["seance_index_note"].lower()
    )


def test_oracle_project_passed_to_stores(oracle_client: TestClient) -> None:
    """The project argument reaches both store calls."""
    seance_calls: list[tuple] = []
    mnemo_calls: list[str] = []

    def fake_seance(project: str, question: str) -> tuple:
        seance_calls.append((project, question))
        return [], False

    def fake_mnemo(project: str) -> None:
        mnemo_calls.append(project)
        return None

    with (
        patch("khimaira.monitor.api.oracle._seance_search", side_effect=fake_seance),
        patch("khimaira.monitor.api.oracle._mnemosyne_query", side_effect=fake_mnemo),
    ):
        _call_oracle(oracle_client, project="jeevy_portal")

    assert seance_calls[0][0] == "jeevy_portal"
    assert mnemo_calls[0] == "jeevy_portal"


def test_oracle_mnemosyne_qualified_key_fanout(oracle_client: TestClient) -> None:
    """Fan-out queries qualified `project:domain` keys; returns data from the matching domain."""
    backend_result = {
        "domain": "khimaira:backend",
        "question": "recent context for khimaira:backend",
        "answer": "Q: How does auth work?\nA: JWT via middleware.",
        "training_pairs_available": 3,
    }

    def fake_inner_query(domain: str) -> dict | None:
        # Only khimaira:backend has data; all others return None.
        return backend_result if domain == "khimaira:backend" else None

    with (
        patch("khimaira.monitor.api.oracle._seance_search", return_value=_SEANCE_EMPTY_OK),
        patch("khimaira.monitor.api.oracle.mnemosyne_client.query", side_effect=fake_inner_query),
    ):
        data = _call_oracle(oracle_client, project="khimaira")

    assert data["degraded"] is False
    assert "mnemosyne" in data["stores_hit"]
    assert "JWT via middleware" in data["context"]
    # Citation should reference the merged result, not bare project
    mnemo_citation = next(c for c in data["citations"] if c["store"] == "mnemosyne")
    assert "backend" in mnemo_citation["ref"]
