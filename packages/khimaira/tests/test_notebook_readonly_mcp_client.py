"""Tests for khimaira.notebook_readonly.mcp_client — the standalone MCP
server remote engineers point their own .mcp.json at (HTTP transport,
StaticTokenVerifier auth).

REST-proxy calls are mocked via httpx.MockTransport — never hits a real
khimaira-notebook-readonly proxy. Four things this module MUST get right:

  1. Every request to the REST proxy attaches `Authorization: Bearer
     <token>` (the exact gap the wire-compat verdict flagged in the daemon
     client — monitor_tools.py never attaches one at all).
  2. Routes match the proxy's real shape: `/notes/search`, `/notes/{id}`,
     `/notes/ask` — NOT `/api/notes/...` (the daemon client's prefix).
  3. Missing config (URL/token unset) fails with a clear string, before
     any HTTP call is attempted — never a crash, never a silent no-op.
  4. This server's OWN HTTP endpoint (the one now reachable from the
     tailnet) rejects requests without the correct
     KHIMAIRA_NOTEBOOK_MCP_TOKEN bearer — a separate trust boundary from
     (1), tested via a real request through `mcp.http_app()`.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest
from fastmcp.server.auth import StaticTokenVerifier
from khimaira.notebook_readonly import mcp_client
from starlette.testclient import TestClient


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _configure(monkeypatch):
    monkeypatch.setattr(mcp_client, "_BASE_URL", "http://proxy.example")
    monkeypatch.setattr(mcp_client, "_TOKEN", "test-token")
    monkeypatch.setattr(mcp_client, "_client", None)
    yield
    mcp_client._client = None


def _wire(handler) -> None:
    mcp_client._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url=mcp_client._BASE_URL
    )


def _json_handler(status: int, body: dict):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


# ---------------------------------------------------------------------------
# Config guards — fail clearly, before any HTTP call
# ---------------------------------------------------------------------------


class TestConfigGuards:
    def test_missing_base_url_returns_clear_error_no_call(self, monkeypatch):
        monkeypatch.setattr(mcp_client, "_BASE_URL", "")
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={})

        _wire(handler)
        out = _run(mcp_client.notebook_search("x"))
        assert "KHIMAIRA_NOTEBOOK_RO_URL" in out
        assert not called

    def test_missing_token_returns_clear_error_no_call(self, monkeypatch):
        monkeypatch.setattr(mcp_client, "_TOKEN", "")
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={})

        _wire(handler)
        out = _run(mcp_client.notebook_search("x"))
        assert "KHIMAIRA_NOTEBOOK_RO_TOKEN" in out
        assert not called


# ---------------------------------------------------------------------------
# Auth header + route shape — the exact wire-compat gaps this module fixes
# ---------------------------------------------------------------------------


class TestWireFormat:
    def test_search_attaches_bearer_header(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"hits": []})

        _wire(handler)
        _run(mcp_client.notebook_search("x"))
        assert seen["auth"] == "Bearer test-token"

    def test_search_hits_correct_path_and_params(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["params"] = dict(httpx.QueryParams(request.url.query))
            return httpx.Response(200, json={"hits": []})

        _wire(handler)
        _run(mcp_client.notebook_search("how does auth work", top_k=7))
        assert seen["path"] == "/notes/search"
        assert seen["params"] == {"q": "how does auth work", "top_k": "7"}

    def test_get_hits_correct_path(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("authorization")
            return httpx.Response(200, json={"id": "abc123", "raw_text": "x"})

        _wire(handler)
        _run(mcp_client.notebook_get("abc123"))
        assert seen["path"] == "/notes/abc123"
        assert seen["auth"] == "Bearer test-token"

    def test_ask_posts_to_correct_path_with_question_body(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"answer": "ok", "sources": []})

        _wire(handler)
        _run(mcp_client.notebook_ask("how does X work"))
        assert seen["path"] == "/notes/ask"
        assert seen["auth"] == "Bearer test-token"
        assert seen["body"] == {"question": "how does X work"}
        # No general write routes (create/update/delete of arbitrary notes)
        # exist on the proxy or this client — the only write capability is
        # the narrow, fixed-note `ask-joseph` append (see TestSurfaceArea).


# ---------------------------------------------------------------------------
# notebook_search
# ---------------------------------------------------------------------------


class TestNotebookSearch:
    def test_happy_path_renders_hits(self):
        _wire(_json_handler(200, {"hits": [{"note_id": "n1", "score": 0.9}]}))
        out = _run(mcp_client.notebook_search("query"))
        assert "1 match(es)" in out
        assert "n1" in out
        assert "0.9" in out

    def test_no_hits_returns_clear_message(self):
        _wire(_json_handler(200, {"hits": []}))
        out = _run(mcp_client.notebook_search("nothing matches this"))
        assert "no notes match" in out

    def test_empty_query_rejected_before_call(self):
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={"hits": []})

        _wire(handler)
        out = _run(mcp_client.notebook_search("   "))
        assert "non-empty query" in out
        assert not called

    def test_daemon_error_passes_through_verbatim(self):
        _wire(_json_handler(401, {"detail": "invalid or missing bearer token"}))
        out = _run(mcp_client.notebook_search("x"))
        assert "HTTP 401" in out
        assert "invalid or missing bearer token" in out


# ---------------------------------------------------------------------------
# notebook_get
# ---------------------------------------------------------------------------


class TestNotebookGet:
    def test_happy_path_renders_note(self):
        note = {
            "id": "n1",
            "title": "Fix the reaper race",
            "lifecycle": "reviewed",
            "repo": "jeevy_portal",
            "tab_id": "default",
            "raw_text": "the actual note body",
            "pipeline": {"summary": "short summary", "organized_md": "## organized"},
        }
        _wire(_json_handler(200, note))
        out = _run(mcp_client.notebook_get("n1"))
        assert "Fix the reaper race" in out
        assert "the actual note body" in out
        assert "short summary" in out
        assert "[reviewed]" in out

    def test_sensitive_note_redaction_passthrough(self):
        # Redaction happens server-side (in the proxy); this client just
        # renders whatever raw_text it's handed — verify it doesn't
        # second-guess or re-derive it.
        note = {"id": "n1", "title": "t", "raw_text": "[redacted — sensitive note]", "repo": "jeevy_portal"}
        _wire(_json_handler(200, note))
        out = _run(mcp_client.notebook_get("n1"))
        assert "[redacted — sensitive note]" in out

    def test_empty_note_id_rejected_before_call(self):
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={})

        _wire(handler)
        out = _run(mcp_client.notebook_get(""))
        assert "requires a note_id" in out
        assert not called

    def test_404_passes_through_verbatim(self):
        _wire(_json_handler(404, {"detail": "No note with id='missing'."}))
        out = _run(mcp_client.notebook_get("missing"))
        assert "HTTP 404" in out
        assert "No note with id" in out


# ---------------------------------------------------------------------------
# notebook_ask
# ---------------------------------------------------------------------------


class TestNotebookAsk:
    def test_happy_path_renders_answer_and_sources(self):
        _wire(_json_handler(200, {"answer": "it works like this", "sources": ["n1", "n2"]}))
        out = _run(mcp_client.notebook_ask("how does it work"))
        assert "it works like this" in out
        assert "n1" in out and "n2" in out

    def test_empty_question_rejected_before_call(self):
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={"answer": "", "sources": []})

        _wire(handler)
        out = _run(mcp_client.notebook_ask("   "))
        assert "non-empty question" in out
        assert not called

    def test_unreachable_proxy_returns_clear_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused", request=request)

        _wire(handler)
        out = _run(mcp_client.notebook_ask("anything"))
        assert "unreachable" in out


# ---------------------------------------------------------------------------
# Surface area — exactly 4 tools, no general write tools (create/update/
# delete of arbitrary notes), only the narrow ask-joseph append
# ---------------------------------------------------------------------------


class TestSurfaceArea:
    def test_only_four_tools_registered(self):
        names = {t.name for t in _run(mcp_client.mcp.list_tools())}
        assert names == {"notebook_search", "notebook_get", "notebook_ask", "notebook_ask_joseph"}


# ---------------------------------------------------------------------------
# notebook_ask_joseph
# ---------------------------------------------------------------------------


class TestNotebookAskJoseph:
    def test_happy_path_posts_and_renders_note_id(self):
        _wire(_json_handler(200, {"posted": True, "note_id": "fed9d370fb94"}))
        out = _run(mcp_client.notebook_ask_joseph("how do I run the migration?", "priya"))
        assert "fed9d370fb94" in out
        assert "notebook_get" in out

    def test_attaches_bearer_header_and_correct_path_and_body(self):
        seen = {}

        def handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["auth"] = request.headers.get("authorization")
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"posted": True, "note_id": "fed9d370fb94"})

        _wire(handler)
        _run(mcp_client.notebook_ask_joseph("how do I run the migration?", "priya"))
        assert seen["path"] == "/notes/ask-joseph"
        assert seen["auth"] == "Bearer test-token"
        assert seen["body"] == {"asker": "priya", "question": "how do I run the migration?"}

    def test_empty_question_rejected_before_call(self):
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={"posted": True, "note_id": "x"})

        _wire(handler)
        out = _run(mcp_client.notebook_ask_joseph("   ", "priya"))
        assert "non-empty question" in out
        assert not called

    def test_empty_asker_rejected_before_call(self):
        called = []

        def handler(request: httpx.Request) -> httpx.Response:
            called.append(request)
            return httpx.Response(200, json={"posted": True, "note_id": "x"})

        _wire(handler)
        out = _run(mcp_client.notebook_ask_joseph("a real question", "   "))
        assert "non-empty asker" in out
        assert not called

    def test_daemon_error_passes_through_verbatim(self):
        _wire(_json_handler(500, {"detail": "KHIMAIRA_ENGINEER_QUESTIONS_NOTE_ID is unset on the server."}))
        out = _run(mcp_client.notebook_ask_joseph("q", "priya"))
        assert "HTTP 500" in out
        assert "KHIMAIRA_ENGINEER_QUESTIONS_NOTE_ID" in out


# ---------------------------------------------------------------------------
# MCP-layer auth gate — the boundary now reachable from the tailnet
# ---------------------------------------------------------------------------


_INITIALIZE_BODY = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
_MCP_HEADERS = {"Accept": "application/json, text/event-stream"}


class TestMcpAuthGate:
    """Auth is baked into the FastMCP instance at construction (unlike the
    REST proxy's per-request `_check_auth` dependency), so these tests swap
    `mcp.auth` directly on the module's singleton, then drive it through
    `mcp.http_app()` with Starlette's TestClient (handles the app's
    lifespan, which the streamable-HTTP session manager requires)."""

    def _client_with_token(self, monkeypatch, token: str) -> TestClient:
        monkeypatch.setattr(
            mcp_client.mcp,
            "auth",
            StaticTokenVerifier(tokens={token: {"client_id": "test", "scopes": []}}),
        )
        return TestClient(mcp_client.mcp.http_app())

    def test_missing_bearer_401(self, monkeypatch):
        client = self._client_with_token(monkeypatch, "correct-token")
        with client:
            resp = client.post("/mcp", json=_INITIALIZE_BODY, headers=_MCP_HEADERS)
        assert resp.status_code == 401

    def test_wrong_bearer_401(self, monkeypatch):
        client = self._client_with_token(monkeypatch, "correct-token")
        headers = {**_MCP_HEADERS, "Authorization": "Bearer nope"}
        with client:
            resp = client.post("/mcp", json=_INITIALIZE_BODY, headers=headers)
        assert resp.status_code == 401

    def test_correct_bearer_200(self, monkeypatch):
        client = self._client_with_token(monkeypatch, "correct-token")
        headers = {**_MCP_HEADERS, "Authorization": "Bearer correct-token"}
        with client:
            resp = client.post("/mcp", json=_INITIALIZE_BODY, headers=headers)
        assert resp.status_code == 200

    def test_require_mcp_token_raises_when_unset(self, monkeypatch):
        monkeypatch.setattr(mcp_client, "_MCP_TOKEN", "")
        with pytest.raises(SystemExit):
            mcp_client._require_mcp_token()
