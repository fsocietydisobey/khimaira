"""Tests for khimaira.notebook_readonly.server.

All daemon calls are mocked via httpx.MockTransport — never hits a real
khimaira-monitor daemon. Two things this module MUST get right (both a
security boundary, not just a feature):

  1. Bearer-token auth on every notebook route (never on /health).
  2. Sensitive notes never leak `raw_text` to a remote caller — the
     redacted `llm_text` twin goes out instead, and `history` is dropped.

Repo scoping (KHIMAIRA_NOTEBOOK_RO_REPO) gets its own coverage: forced on
search/ask regardless of client-supplied repo, and a hard 404 from
get_note for any note outside scope.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from khimaira.notebook_readonly import server

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setattr(server, "_AUTH_TOKEN", "test-token")
    monkeypatch.setattr(server, "_REPO_SCOPE", None)


def _client_for(handler) -> TestClient:
    """TestClient wired to a mock daemon via the given httpx handler."""
    client = TestClient(server.app)
    client.__enter__()  # run lifespan (startup) so _client exists
    server._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=server._DAEMON_BASE
    )
    return client


def _json_handler(status: int, body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_missing_bearer_401(self):
        client = _client_for(_json_handler(200, {"hits": []}))
        resp = client.get("/notes/search", params={"q": "x"})
        assert resp.status_code == 401

    def test_wrong_bearer_401(self):
        client = _client_for(_json_handler(200, {"hits": []}))
        resp = client.get("/notes/search", params={"q": "x"}, headers={"Authorization": "Bearer nope"})
        assert resp.status_code == 401

    def test_correct_bearer_200(self):
        client = _client_for(_json_handler(200, {"hits": []}))
        resp = client.get("/notes/search", params={"q": "x"}, headers=AUTH)
        assert resp.status_code == 200

    def test_health_requires_no_auth(self):
        client = _client_for(_json_handler(200, {}))
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_require_token_raises_when_unset(self, monkeypatch):
        monkeypatch.setattr(server, "_AUTH_TOKEN", "")
        with pytest.raises(SystemExit):
            server._require_token()


# ---------------------------------------------------------------------------
# Sensitive-note redaction
# ---------------------------------------------------------------------------


class TestSensitiveRedaction:
    def test_sensitive_note_raw_text_replaced_by_llm_text(self):
        daemon_note = {
            "id": "abc123",
            "raw_text": "the real secret sk-ant-api03-REALVALUE",
            "llm_text": "the real secret [REDACTED-api-key]",
            "sensitive": True,
            "redactions": [{"placeholder": "[REDACTED-api-key]", "kind": "api_key"}],
            "history": [{"pipeline": {"summary": "old"}}],
            "repo": "jeevy_portal",
        }
        client = _client_for(_json_handler(200, daemon_note))
        resp = client.get("/notes/abc123", headers=AUTH)
        assert resp.status_code == 200
        body = resp.json()
        assert body["raw_text"] == "the real secret [REDACTED-api-key]"
        assert "sk-ant-api03-REALVALUE" not in str(body)
        assert "history" not in body
        assert body["redactions"] == [{"placeholder": "[REDACTED-api-key]", "kind": "api_key"}]

    def test_sensitive_note_missing_llm_text_falls_back_to_placeholder_not_raw(self):
        daemon_note = {
            "id": "abc123",
            "raw_text": "the real secret",
            "llm_text": None,
            "sensitive": True,
            "repo": "jeevy_portal",
        }
        client = _client_for(_json_handler(200, daemon_note))
        resp = client.get("/notes/abc123", headers=AUTH)
        body = resp.json()
        assert body["raw_text"] != "the real secret"
        assert "real secret" not in body["raw_text"]

    def test_non_sensitive_note_passes_through_unchanged(self):
        daemon_note = {
            "id": "abc123",
            "raw_text": "totally public content",
            "sensitive": False,
            "history": [{"pipeline": {"summary": "old"}}],
            "repo": "jeevy_portal",
        }
        client = _client_for(_json_handler(200, daemon_note))
        resp = client.get("/notes/abc123", headers=AUTH)
        body = resp.json()
        assert body["raw_text"] == "totally public content"
        assert body["history"] == [{"pipeline": {"summary": "old"}}]

    def test_sensitive_none_treated_as_non_sensitive(self):
        """Pre-grimoire records carry `sensitive: null`, not `false` — must
        not be treated as sensitive (that would needlessly strip history),
        but importantly must ALSO not be treated as a reason to skip
        redaction if it were ever true — covered by the truthy check."""
        daemon_note = {"id": "abc123", "raw_text": "public", "sensitive": None, "repo": "jeevy_portal"}
        client = _client_for(_json_handler(200, daemon_note))
        resp = client.get("/notes/abc123", headers=AUTH)
        assert resp.json()["raw_text"] == "public"

    def test_daemon_404_passthrough(self):
        client = _client_for(_json_handler(404, {"detail": "not found"}))
        resp = client.get("/notes/missing", headers=AUTH)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Repo scoping
# ---------------------------------------------------------------------------


class TestRepoScope:
    def test_get_note_outside_scope_404s(self, monkeypatch):
        monkeypatch.setattr(server, "_REPO_SCOPE", "jeevy_portal")
        daemon_note = {"id": "abc123", "raw_text": "x", "sensitive": False, "repo": "some_other_repo"}
        client = _client_for(_json_handler(200, daemon_note))
        resp = client.get("/notes/abc123", headers=AUTH)
        assert resp.status_code == 404

    def test_get_note_in_general_bucket_allowed(self, monkeypatch):
        monkeypatch.setattr(server, "_REPO_SCOPE", "jeevy_portal")
        daemon_note = {"id": "abc123", "raw_text": "x", "sensitive": False, "repo": server._GENERAL_REPO}
        client = _client_for(_json_handler(200, daemon_note))
        resp = client.get("/notes/abc123", headers=AUTH)
        assert resp.status_code == 200

    def test_search_forces_scoped_repo_ignoring_client_value(self, monkeypatch):
        monkeypatch.setattr(server, "_REPO_SCOPE", "jeevy_portal")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["repo"] = dict(httpx.QueryParams(request.url.query))["repo"]
            return httpx.Response(200, json={"hits": []})

        client = _client_for(handler)
        client.get("/notes/search", params={"q": "x", "repo": "attacker_supplied_repo"}, headers=AUTH)
        assert seen["repo"] == "jeevy_portal"

    def test_ask_forces_scoped_repo_ignoring_client_value(self, monkeypatch):
        monkeypatch.setattr(server, "_REPO_SCOPE", "jeevy_portal")
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            import json

            seen["repo"] = json.loads(request.content)["repo"]
            return httpx.Response(200, json={"answer": "ok", "sources": []})

        client = _client_for(handler)
        client.post(
            "/notes/ask",
            json={"question": "how does X work", "repo": "attacker_supplied_repo"},
            headers=AUTH,
        )
        assert seen["repo"] == "jeevy_portal"

    def test_no_scope_configured_honors_client_repo(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["repo"] = dict(httpx.QueryParams(request.url.query)).get("repo")
            return httpx.Response(200, json={"hits": []})

        client = _client_for(handler)
        client.get("/notes/search", params={"q": "x", "repo": "whatever_repo"}, headers=AUTH)
        assert seen["repo"] == "whatever_repo"
