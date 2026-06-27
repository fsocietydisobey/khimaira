"""Tests for the generic graph proxy: GET /api/graph/<project> (graph.py) +
the attach-registry kg_adapter helpers (registry.py).

Covers:
- registry: set/get kg_adapter round-trip; record_attach preserves it; unknown → None
- route happy path: adapter reached with Bearer; generic contract returned verbatim
- route happy (no token_env): no Authorization header sent
- 404 when no kg_adapter is registered for the project
- 500 when the adapter declares a token_env but the env var is unset
- 502 when the adapter is unreachable / returns an error status / returns non-JSON
"""

from __future__ import annotations

import importlib
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# ---------------------------------------------------------------------------
# Isolated registry (XDG_STATE_HOME → tmp, module reloaded)
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.attach import registry as reg

    importlib.reload(reg)
    yield reg
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(reg)


# ---------------------------------------------------------------------------
# Registry helper tests
# ---------------------------------------------------------------------------


def test_set_and_get_kg_adapter_roundtrip(isolated_registry):
    reg = isolated_registry
    reg.record_attach(Path("/abs/jeevy_portal"), Path("/abs/jeevy_portal/.venv"))

    # By label (defaults to dir basename)
    ok = reg.set_kg_adapter(
        "jeevy_portal", url="http://127.0.0.1:8001/internal/kg/graph", token_env="JEEVY_KG_ADAPTER_TOKEN"
    )
    assert ok is True

    adapter = reg.get_kg_adapter("jeevy_portal")
    assert adapter == {
        "url": "http://127.0.0.1:8001/internal/kg/graph",
        "token_env": "JEEVY_KG_ADAPTER_TOKEN",
    }
    # Also resolvable by full path
    assert reg.get_kg_adapter("/abs/jeevy_portal") == adapter


def test_get_kg_adapter_unknown_returns_none(isolated_registry):
    reg = isolated_registry
    assert reg.get_kg_adapter("nonexistent") is None
    # Known project but no adapter set → None
    reg.record_attach(Path("/abs/proj"), Path("/abs/proj/.venv"))
    assert reg.get_kg_adapter("proj") is None


def test_set_kg_adapter_unknown_project_returns_false(isolated_registry):
    reg = isolated_registry
    assert reg.set_kg_adapter("ghost", url="http://x") is False


def test_record_attach_preserves_kg_adapter(isolated_registry):
    reg = isolated_registry
    reg.record_attach(Path("/abs/jeevy"), Path("/abs/jeevy/.venv"))
    reg.set_kg_adapter("jeevy", url="http://x/graph", token_env="TOK")

    # Re-attach (e.g. detach/attach cycle) must not drop the adapter.
    reg.record_attach(Path("/abs/jeevy"), Path("/abs/jeevy/.venv"))
    assert reg.get_kg_adapter("jeevy") == {"url": "http://x/graph", "token_env": "TOK"}


# ---------------------------------------------------------------------------
# Route tests — fake async httpx client
# ---------------------------------------------------------------------------

_CONTRACT = {"data": {"nodes": [{"id": "n1", "type": "shop", "label": "Shop 10"}], "edges": []}}


class _FakeResp:
    def __init__(self, status_code=200, json_data=None, json_raises=False):
        self.status_code = status_code
        self._json = json_data
        self._raises = json_raises

    def json(self):
        if self._raises:
            raise ValueError("no json body")
        return self._json


class _FakeClient:
    """Async-context-manager stand-in for httpx.AsyncClient."""

    last_headers: dict | None = None
    last_params: dict | None = None
    last_url: str | None = None

    def __init__(self, *, resp=None, raise_exc=None):
        self._resp = resp
        self._raise = raise_exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, params=None, headers=None):
        type(self).last_headers = headers
        type(self).last_params = params
        type(self).last_url = url
        if self._raise is not None:
            raise self._raise
        return self._resp


def _client_for(graph_api, *, resp=None, raise_exc=None):
    """Patch the graph module's httpx.AsyncClient with a fake factory."""
    _FakeClient.last_headers = None
    _FakeClient.last_params = None
    _FakeClient.last_url = None

    def _factory(*_a, **_k):
        return _FakeClient(resp=resp, raise_exc=raise_exc)

    return _factory


@pytest.fixture
def graph_mod():
    from khimaira.monitor.api import graph as graph_api

    return graph_api


def _client(graph_api) -> TestClient:
    app = FastAPI()
    app.include_router(graph_api.build_router(), prefix="/api")
    return TestClient(app)


def test_graph_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal", params={"scope": "shop:10"})
    assert r.status_code == 404
    assert "no KG adapter" in r.json()["detail"]


def test_graph_500_token_env_unset(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/graph", "token_env": "MISSING_TOK"}
    )
    monkeypatch.delenv("MISSING_TOK", raising=False)
    # Neutralize load_dotenv so it can't repopulate the env from a real .env.
    monkeypatch.setattr(graph_mod, "_resolve_token", lambda _e: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal")
    assert r.status_code == 500
    assert "MISSING_TOK" in r.json()["detail"]


def test_graph_502_adapter_unreachable(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/graph"})
    monkeypatch.setattr(
        graph_mod.httpx, "AsyncClient", _client_for(graph_mod, raise_exc=httpx.ConnectError("boom"))
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal")
    assert r.status_code == 502
    assert "unreachable" in r.json()["detail"]


def test_graph_502_adapter_error_status(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/graph"})
    monkeypatch.setattr(
        graph_mod.httpx, "AsyncClient", _client_for(graph_mod, resp=_FakeResp(status_code=503))
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal")
    assert r.status_code == 502
    assert "503" in r.json()["detail"]


def test_graph_502_non_json(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/graph"})
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_raises=True)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal")
    assert r.status_code == 502
    assert "non-JSON" in r.json()["detail"]


def test_graph_happy_with_token(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/graph", "token_env": "KG_TOK"}
    )
    monkeypatch.setattr(graph_mod, "_resolve_token", lambda _e: "secret-123")
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_CONTRACT)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal", params={"scope": "shop:10"})
    assert r.status_code == 200
    assert r.json() == _CONTRACT
    # Bearer sent + scope forwarded
    assert _FakeClient.last_headers == {"Authorization": "Bearer secret-123"}
    assert _FakeClient.last_params == {"scope": "shop:10"}
    assert _FakeClient.last_url == "http://x/graph"


def test_graph_happy_no_token_env(graph_mod, monkeypatch):
    """Adapter without a token_env → no Authorization header (no-auth adapter)."""
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/graph"})
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_CONTRACT)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal")
    assert r.status_code == 200
    assert r.json() == _CONTRACT
    assert _FakeClient.last_headers == {}
    assert _FakeClient.last_params == {}


def test_graph_custom_auth_header_sends_raw_token(graph_mod, monkeypatch):
    """auth_header override (e.g. X-Internal-Key) → raw token under that header,
    NOT Authorization: Bearer. This is how the daemon reuses jeevy's existing
    verify_internal_key service-auth."""
    monkeypatch.setattr(
        graph_mod,
        "get_kg_adapter",
        lambda _p: {
            "url": "http://x/graph",
            "token_env": "KG_TOK",
            "auth_header": "X-Internal-Key",
        },
    )
    monkeypatch.setattr(graph_mod, "_resolve_token", lambda _e: "secret-123")
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_CONTRACT)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal", params={"scope": "shop:10"})
    assert r.status_code == 200
    assert _FakeClient.last_headers == {"X-Internal-Key": "secret-123"}
    assert "Authorization" not in _FakeClient.last_headers


def test_set_kg_adapter_with_auth_header_roundtrip(isolated_registry):
    reg = isolated_registry
    reg.record_attach(Path("/abs/jeevy"), Path("/abs/jeevy/.venv"))
    reg.set_kg_adapter(
        "jeevy", url="http://x/graph", token_env="TOK", auth_header="X-Internal-Key"
    )
    assert reg.get_kg_adapter("jeevy") == {
        "url": "http://x/graph",
        "token_env": "TOK",
        "auth_header": "X-Internal-Key",
    }


# ---------------------------------------------------------------------------
# Node-detail proxy: GET /api/graph/<project>/node/<id>
# ---------------------------------------------------------------------------

_NODE_DETAIL = {
    "data": {
        "id": "uuid-1",
        "type": "task",
        "label": "Cut sheet",
        "currentFacts": [{"label": "status", "value": "open"}],
        "historyFacts": [],
        "edgesFrom": [],
        "edgesTo": [],
    }
}


def test_node_url_derivation():
    from khimaira.monitor.api.graph import _node_url

    assert _node_url("http://j/internal/kg/graph", "u1") == "http://j/internal/kg/node/u1"
    assert _node_url("http://j/internal/kg/", "u1") == "http://j/internal/kg/node/u1"
    assert _node_url("http://j/kg", "u1") == "http://j/kg/node/u1"


def test_graph_node_happy_proxies_to_node_subpath(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_NODE_DETAIL)),
    )
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/node/uuid-1", params={"scope": "shop:10"}
    )
    assert r.status_code == 200
    assert r.json() == _NODE_DETAIL
    # Proxied to the derived node sub-path, scope forwarded.
    assert _FakeClient.last_url == "http://x/internal/kg/node/uuid-1"
    assert _FakeClient.last_params == {"scope": "shop:10"}


def test_graph_node_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/node/uuid-1")
    assert r.status_code == 404


def test_graph_node_502_adapter_unreachable(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, raise_exc=httpx.ConnectError("boom")),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/node/uuid-1")
    assert r.status_code == 502
