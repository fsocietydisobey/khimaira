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
        "jeevy_portal",
        url="http://127.0.0.1:8001/internal/kg/graph",
        token_env="JEEVY_KG_ADAPTER_TOKEN",
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
        graph_mod,
        "get_kg_adapter",
        lambda _p: {"url": "http://x/graph", "token_env": "MISSING_TOK"},
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
    reg.set_kg_adapter("jeevy", url="http://x/graph", token_env="TOK", auth_header="X-Internal-Key")
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


def test_sub_url_derivation():
    from khimaira.monitor.api.graph import _sub_url

    assert _sub_url("http://j/internal/kg/graph", "node/u1") == "http://j/internal/kg/node/u1"
    assert _sub_url("http://j/internal/kg/", "node/u1") == "http://j/internal/kg/node/u1"
    assert _sub_url("http://j/kg", "node/u1") == "http://j/kg/node/u1"
    assert _sub_url("http://j/internal/kg/graph", "health") == "http://j/internal/kg/health"
    assert (
        _sub_url("http://j/internal/kg/graph", "edges-audit") == "http://j/internal/kg/edges-audit"
    )


def test_graph_node_happy_proxies_to_node_subpath(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_NODE_DETAIL)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/node/uuid-1", params={"scope": "shop:10"})
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


# ---------------------------------------------------------------------------
# Source-record proxy: GET /api/graph/<project>/node/<id>/source (DB-RECORD peek)
# ---------------------------------------------------------------------------

_NODE_SOURCE_FOUND = {
    "data": {
        "found": True,
        "node_id": "uuid-1",
        "node_type": "job",
        "canonical_key": "job:688",
        "table": "deliverables",
        "source_id": 688,
        "row": {"id": 688, "owner_kind": "project", "status": "active"},
    },
    "meta": {"scope": "shop:10", "shop_id": 10},
}

_NODE_SOURCE_NOT_FOUND = {
    "data": {
        "found": False,
        "node_id": "uuid-2",
        "node_type": "bom-line",
        "row": None,
        "reason": "name-keyed type has no single source PK",
    },
    "meta": {"scope": "shop:10"},
}


def test_source_url_derivation():
    from khimaira.monitor.api.graph import _sub_url

    assert (
        _sub_url("http://j/internal/kg/graph", "node/u1/source")
        == "http://j/internal/kg/node/u1/source"
    )


def test_graph_node_source_proxies_to_source_subpath(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_NODE_SOURCE_FOUND)),
    )
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/node/uuid-1/source", params={"scope": "shop:10"}
    )
    assert r.status_code == 200
    assert r.json() == _NODE_SOURCE_FOUND
    # Proxied to the derived node/<id>/source sub-path, scope forwarded.
    assert _FakeClient.last_url == "http://x/internal/kg/node/uuid-1/source"
    assert _FakeClient.last_params == {"scope": "shop:10"}


def test_graph_node_source_found_false_passthrough(graph_mod, monkeypatch):
    """found:false (name-keyed type / out-of-scope) is a graceful-empty case the
    adapter returns at HTTP 200 — the proxy passes it through verbatim, NOT a 4xx."""
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_NODE_SOURCE_NOT_FOUND)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/node/uuid-2/source")
    assert r.status_code == 200
    assert r.json()["data"]["found"] is False
    assert r.json()["data"]["reason"]


def test_graph_node_source_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/node/uuid-1/source")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Edge-detail proxy: GET /api/graph/<project>/edge/<id>
# ---------------------------------------------------------------------------

_EDGE_DETAIL = {
    "data": {
        "id": "edge-1",
        "type": "has-type",
        "from": "a",
        "to": "b",
        "weight": 0.62,
        "meta": {"match_method": "fuzzy", "page": 3},
    }
}


def test_edge_url_derivation():
    from khimaira.monitor.api.graph import _sub_url

    assert _sub_url("http://j/internal/kg/graph", "edge/e1") == "http://j/internal/kg/edge/e1"
    assert _sub_url("http://j/internal/kg/", "edge/e1") == "http://j/internal/kg/edge/e1"


def test_graph_edge_happy_proxies_to_edge_subpath(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_EDGE_DETAIL)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/edge/edge-1", params={"scope": "shop:10"})
    assert r.status_code == 200
    assert r.json() == _EDGE_DETAIL
    assert _FakeClient.last_url == "http://x/internal/kg/edge/edge-1"
    assert _FakeClient.last_params == {"scope": "shop:10"}


def test_graph_edge_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/edge/edge-1")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Schema (type meta-graph) proxy: GET /api/graph/<project>/schema
# ---------------------------------------------------------------------------

_SCHEMA = {
    "data": {
        "nodeTypes": ["job", "task"],
        "linkTypes": ["belongs-to"],
        "triples": [{"fromType": "task", "linkType": "belongs-to", "toType": "job", "count": 2}],
    }
}


def test_schema_url_derivation():
    from khimaira.monitor.api.graph import _sub_url

    assert _sub_url("http://j/internal/kg/graph", "schema") == "http://j/internal/kg/schema"
    assert _sub_url("http://j/internal/kg/", "schema") == "http://j/internal/kg/schema"


def test_graph_schema_happy_proxies_to_schema_subpath(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_SCHEMA)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/schema", params={"scope": "shop:10"})
    assert r.status_code == 200
    assert r.json() == _SCHEMA
    assert _FakeClient.last_url == "http://x/internal/kg/schema"


def test_graph_schema_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    assert _client(graph_mod).get("/api/graph/jeevy_portal/schema").status_code == 404


# ---------------------------------------------------------------------------
# Phase 3 aggregate routes — health / coverage / edges-audit + `since`
# ---------------------------------------------------------------------------

_HEALTH = {"data": {"totals": {"nodes": 5719, "edges": 9970, "orphanNodes": 1814}}}
_COVERAGE = {"data": {"entities": [{"entity": "user", "relationalCount": 46, "kgCount": 4}]}}
_AUDIT = {"data": {"matchMethods": [{"method": "normalized", "count": 9970}]}}


def _ok(graph_mod, monkeypatch, payload):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=payload)),
    )


def test_graph_health_proxies_to_health_subpath(graph_mod, monkeypatch):
    _ok(graph_mod, monkeypatch, _HEALTH)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/health", params={"scope": "shop:10"})
    assert r.status_code == 200 and r.json() == _HEALTH
    assert _FakeClient.last_url == "http://x/internal/kg/health"


def test_graph_health_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    assert _client(graph_mod).get("/api/graph/jeevy_portal/health").status_code == 404


def test_graph_coverage_proxies_to_coverage_subpath(graph_mod, monkeypatch):
    _ok(graph_mod, monkeypatch, _COVERAGE)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/coverage", params={"scope": "shop:10"})
    assert r.status_code == 200 and r.json() == _COVERAGE
    assert _FakeClient.last_url == "http://x/internal/kg/coverage"


def test_graph_edges_audit_proxies_to_subpath(graph_mod, monkeypatch):
    _ok(graph_mod, monkeypatch, _AUDIT)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/edges-audit", params={"scope": "shop:10"})
    assert r.status_code == 200 and r.json() == _AUDIT
    assert _FakeClient.last_url == "http://x/internal/kg/edges-audit"


def test_since_param_forwarded_to_adapter(graph_mod, monkeypatch):
    _ok(graph_mod, monkeypatch, _HEALTH)
    _client(graph_mod).get(
        "/api/graph/jeevy_portal/health",
        params={"scope": "shop:10", "since": "2026-06-01T00:00:00Z"},
    )
    assert _FakeClient.last_params == {"scope": "shop:10", "since": "2026-06-01T00:00:00Z"}


def test_since_omitted_when_absent(graph_mod, monkeypatch):
    _ok(graph_mod, monkeypatch, _HEALTH)
    _client(graph_mod).get("/api/graph/jeevy_portal/health", params={"scope": "shop:10"})
    assert _FakeClient.last_params == {"scope": "shop:10"}  # no since key


# ---------------------------------------------------------------------------
# #38 Tier-2 — live contract gate on GET /api/graph/<project> (_filter_to_contract).
# Fail-SAFE by default (drop nonconforming + annotate data._contract), hard-502
# only under ?strict=true / KHIMAIRA_KG_CONTRACT_STRICT. The loud source-of-truth
# conformance suite is tests/test_kg_contract_gate.py; this covers the LIVE route.
# ---------------------------------------------------------------------------


# one conforming node/edge + one leaking a raw jeevy term (node_type/canonical_key).
# Factory (not a module constant) so each test gets a fresh dict — the gate returns a
# new payload, but a shared mutable fixture is still a cross-test footgun.
def _drifted() -> dict:
    return {
        "data": {
            "nodes": [
                {"id": "n1", "type": "shop", "label": "Shop 10"},
                {"id": "n2", "node_type": "job", "canonical_key": "job:1", "label": "J"},
            ],
            "edges": [
                {"from": "n2", "to": "n1", "type": "owns"},
                {"from": "n2", "to": "n1", "type": "owns", "weight": "high"},  # bad weight
            ],
        }
    }


def test_contract_gate_permissive_drops_and_annotates(graph_mod, monkeypatch):
    """Default (no strict): nonconforming items are DROPPED, conforming ones served,
    and data._contract carries the dropped counts + a violation sample (no silent
    truncation). Partial data > no data for a debugging surface."""
    monkeypatch.setattr(graph_mod, "_CONTRACT_STRICT", False)
    _ok(graph_mod, monkeypatch, _drifted())
    r = _client(graph_mod).get("/api/graph/jeevy_portal", params={"scope": "shop:10"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert [n["id"] for n in body["nodes"]] == ["n1"]  # leaky node dropped
    assert len(body["edges"]) == 1  # bad-weight edge dropped
    c = body["_contract"]
    assert c["ok"] is False
    assert c["droppedNodes"] == 1 and c["droppedEdges"] == 1
    assert c["sampleViolations"]  # populated, not silently truncated


def test_contract_gate_strict_502(graph_mod, monkeypatch):
    """?strict=true → hard-fail the whole payload (CI / opt-in posture)."""
    monkeypatch.setattr(graph_mod, "_CONTRACT_STRICT", False)
    _ok(graph_mod, monkeypatch, _drifted())
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal", params={"scope": "shop:10", "strict": "true"}
    )
    assert r.status_code == 502
    assert "violates contract" in r.json()["detail"]


def test_contract_gate_conforming_passes_through_untouched(graph_mod, monkeypatch):
    """A fully-conforming payload is returned verbatim — no _contract annotation."""
    monkeypatch.setattr(graph_mod, "_CONTRACT_STRICT", False)
    _ok(graph_mod, monkeypatch, _CONTRACT)
    r = _client(graph_mod).get("/api/graph/jeevy_portal")
    assert r.status_code == 200
    assert r.json() == _CONTRACT
    assert "_contract" not in r.json()["data"]


def test_contract_gate_env_strict_default(graph_mod, monkeypatch):
    """KHIMAIRA_KG_CONTRACT_STRICT=1 (module flag) makes strict the default even
    without the query param."""
    monkeypatch.setattr(graph_mod, "_CONTRACT_STRICT", True)
    _ok(graph_mod, monkeypatch, _drifted())
    r = _client(graph_mod).get("/api/graph/jeevy_portal")
    assert r.status_code == 502


# ---------------------------------------------------------------------------
# CONTRACT-CONFIRMED routes (2026-07-13, task-f4220ba84f33) — griffin-0's
# real jeevy paths/shapes for 5 tools + kg_duplicates (new). Endpoints
# aren't live yet (still proxy to a fake adapter here, same as every other
# route test) but the params/shape asserted below are the settled contract,
# not a guess — supersedes the pre-6bed449 scaffolding-assumption tests.
# ---------------------------------------------------------------------------

_SUBGRAPH_CONTRACT = {
    "data": {
        "root": "n1",
        "nodes": [{"id": "n1", "type": "task", "depth": 0, "facts": [{"label": "status", "value": "open"}]}],
        "edges": [{"from": "n2", "to": "n1", "link_type": "belongs_to", "match_method": "exact", "confidence": 1.0, "link_source": "projector", "status": "active"}],
    }
}


def test_graph_subgraph_proxies_and_forwards_direction_and_link_types(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_SUBGRAPH_CONTRACT)),
    )
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/node/n1/subgraph",
        params={"scope": "shop:10", "hops": 3, "direction": "out", "link_types": "belongs_to,assigned_to"},
    )
    assert r.status_code == 200
    assert r.json() == _SUBGRAPH_CONTRACT
    assert _FakeClient.last_url == "http://x/internal/kg/node/n1/subgraph"
    assert _FakeClient.last_params == {
        "hops": 3,
        "direction": "out",
        "link_types": "belongs_to,assigned_to",
        "scope": "shop:10",
    }


def test_graph_subgraph_omits_direction_and_link_types_when_unset(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_SUBGRAPH_CONTRACT)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/node/n1/subgraph")
    assert r.status_code == 200
    assert _FakeClient.last_params == {"hops": 2}


def test_graph_subgraph_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/node/n1/subgraph")
    assert r.status_code == 404


_ANOMALIES_ORPHANS = {"data": {"kind": "orphans", "rows": [{"id": "n2", "type": "user", "label": "Ghost"}], "total": 1, "truncated": False}}


def test_graph_anomalies_requires_kind(graph_mod, monkeypatch):
    """`kind` is a REQUIRED daemon-side input check (422), not forwarded
    to the adapter unset — this is the one-kind-per-call contract."""
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/anomalies")
    assert r.status_code == 422


def test_graph_anomalies_rejects_invalid_kind(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/anomalies", params={"kind": "bogus"})
    assert r.status_code == 422
    assert "bogus" in r.json()["detail"]


def test_graph_anomalies_proxies_one_kind_per_call(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_ANOMALIES_ORPHANS)),
    )
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/anomalies",
        params={"kind": "orphans", "scope": "shop:10", "node_type": "user"},
    )
    assert r.status_code == 200
    assert r.json() == _ANOMALIES_ORPHANS
    assert _FakeClient.last_url == "http://x/internal/kg/anomalies"
    assert _FakeClient.last_params == {"kind": "orphans", "node_type": "user", "scope": "shop:10"}


def test_graph_anomalies_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/anomalies", params={"kind": "orphans"})
    assert r.status_code == 404


_DUPLICATES_CONTRACT = {
    "data": {"rows": [{"ids": ["n1", "n2"], "canonical_key": "user:alice", "node_type": "user"}], "total": 1}
}


def test_graph_duplicates_proxies_and_forwards_params(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_DUPLICATES_CONTRACT)),
    )
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/duplicates",
        params={"scope": "shop:10", "node_type": "user", "threshold": 0.8},
    )
    assert r.status_code == 200
    assert r.json() == _DUPLICATES_CONTRACT
    assert _FakeClient.last_url == "http://x/internal/kg/duplicates"
    assert _FakeClient.last_params == {"node_type": "user", "threshold": 0.8, "scope": "shop:10"}


def test_graph_duplicates_omits_unset_params(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_DUPLICATES_CONTRACT)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/duplicates")
    assert r.status_code == 200
    assert _FakeClient.last_params == {}


def test_graph_duplicates_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/duplicates")
    assert r.status_code == 404


_EDGES_FILTER_CONTRACT = {"data": {"edges": [{"id": "e1", "type": "assigned"}], "total": 1, "truncated": False}}


def test_graph_edges_filter_proxies_and_forwards_renamed_predicates(graph_mod, monkeypatch):
    """min_weight/max_weight (not weight_min/weight_max) + link_source +
    real limit/offset pagination — the confirmed-contract param names."""
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_EDGES_FILTER_CONTRACT)),
    )
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/edges",
        params={
            "scope": "shop:10",
            "link_type": "assigned_to",
            "match_method": "fuzzy",
            "min_weight": 0.1,
            "max_weight": 0.5,
            "status": "active",
            "link_source": "extractor",
            "limit": 25,
            "offset": 50,
        },
    )
    assert r.status_code == 200
    assert r.json() == _EDGES_FILTER_CONTRACT
    assert _FakeClient.last_url == "http://x/internal/kg/edges"
    assert _FakeClient.last_params == {
        "limit": 25,
        "offset": 50,
        "link_type": "assigned_to",
        "match_method": "fuzzy",
        "min_weight": 0.1,
        "max_weight": 0.5,
        "status": "active",
        "link_source": "extractor",
        "scope": "shop:10",
    }


def test_graph_edges_filter_defaults_limit_and_offset(graph_mod, monkeypatch):
    """limit/offset are ALWAYS sent (real pagination, not an optional
    display cap) — default limit=50, offset=0."""
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_EDGES_FILTER_CONTRACT)),
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/edges")
    assert r.status_code == 200
    assert _FakeClient.last_params == {"limit": 50, "offset": 0}


def test_graph_edges_filter_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/edges")
    assert r.status_code == 404


_PATH_REACHABLE = {"data": {"path": ["n1", "n2"], "hops": 1, "reachable": True}}
_PATH_UNREACHABLE = {"data": {"path": None, "hops": None, "reachable": False}}


def test_graph_path_proxies_and_forwards_endpoints(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_PATH_REACHABLE)),
    )
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/path",
        params={"from_node": "n1", "to_node": "n2", "max_hops": 3, "scope": "shop:10"},
    )
    assert r.status_code == 200
    assert r.json() == _PATH_REACHABLE
    assert _FakeClient.last_url == "http://x/internal/kg/path"
    assert _FakeClient.last_params == {"from": "n1", "to": "n2", "max_hops": 3, "scope": "shop:10"}


def test_graph_path_unreachable_is_still_200(graph_mod, monkeypatch):
    """reachable:false is a legitimate finding, not an error — always 200,
    never 404/500 for a no-path result."""
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx,
        "AsyncClient",
        _client_for(graph_mod, resp=_FakeResp(status_code=200, json_data=_PATH_UNREACHABLE)),
    )
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/path", params={"from_node": "n1", "to_node": "n99"}
    )
    assert r.status_code == 200
    assert r.json()["data"]["reachable"] is False


def test_graph_path_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get(
        "/api/graph/jeevy_portal/path", params={"from_node": "n1", "to_node": "n2"}
    )
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# kg_export — server-side pagination loop (griffin-0-confirmed design)
# ---------------------------------------------------------------------------


class _SequencedFakeClient:
    """Async-context-manager stand-in returning a DIFFERENT response per
    call — needed for kg_export, which calls the adapter multiple times."""

    calls: list[dict] = []

    def __init__(self, *, responses):
        self._responses = list(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def get(self, url, params=None, headers=None):
        type(self).calls.append({"url": url, "params": params, "headers": headers})
        idx = len(type(self).calls) - 1
        return self._responses[min(idx, len(self._responses) - 1)]


def _sequenced_client_for(responses):
    _SequencedFakeClient.calls = []

    def _factory(*_a, **_k):
        return _SequencedFakeClient(responses=responses)

    return _factory


def test_graph_export_single_page_no_more(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    page = _FakeResp(
        status_code=200,
        json_data={"data": {"nodes": [{"id": "n1"}], "edges": [{"id": "e1"}]}, "meta": {"next_cursor": None, "has_more": False}},
    )
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", _sequenced_client_for([page]))
    r = _client(graph_mod).get("/api/graph/jeevy_portal/export", params={"scope": "shop:10"})
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["nodes"] == [{"id": "n1"}]
    assert body["edges"] == [{"id": "e1"}]
    assert body["truncated"] is False
    assert body["nextCursor"] is None
    assert len(_SequencedFakeClient.calls) == 1


def test_graph_export_loops_across_pages_and_assembles(graph_mod, monkeypatch):
    """The daemon loops server-side — 3 pages in, ONE assembled payload out.
    Real jeevy shape (LIVE-VERIFIED 2026-07-13 via direct curl against real
    shop:10 data): pagination state is `meta.has_more`/`meta.next_cursor`
    (snake_case, under `meta`) — NOT `data.has_more`/`data.next_cursor`
    (37c08aa's bug) and NOT camelCase (the FIRST correction's guess, also
    wrong — direct curl was needed to nail the exact casing). Getting
    either detail wrong reproduces the same silent single-page-truncation
    bug: `has_more` reads as always-falsy either way."""
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    pages = [
        _FakeResp(status_code=200, json_data={"data": {"nodes": [{"id": "n1"}], "edges": []}, "meta": {"next_cursor": "c1", "has_more": True}}),
        _FakeResp(status_code=200, json_data={"data": {"nodes": [{"id": "n2"}], "edges": []}, "meta": {"next_cursor": "c2", "has_more": True}}),
        _FakeResp(status_code=200, json_data={"data": {"nodes": [{"id": "n3"}], "edges": []}, "meta": {"next_cursor": None, "has_more": False}}),
    ]
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", _sequenced_client_for(pages))
    r = _client(graph_mod).get("/api/graph/jeevy_portal/export")
    assert r.status_code == 200
    body = r.json()["data"]
    assert [n["id"] for n in body["nodes"]] == ["n1", "n2", "n3"]
    assert body["truncated"] is False
    assert len(_SequencedFakeClient.calls) == 3
    # cursor forwarded correctly across the loop
    assert _SequencedFakeClient.calls[1]["params"].get("cursor") == "c1"
    assert _SequencedFakeClient.calls[2]["params"].get("cursor") == "c2"


def test_graph_export_truncates_at_item_cap(graph_mod, monkeypatch):
    """A page that pushes the running total past _EXPORT_MAX_ITEMS stops
    the loop early with truncated=True, even though hasMore was still true."""
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(graph_mod, "_EXPORT_MAX_ITEMS", 2)
    pages = [
        _FakeResp(status_code=200, json_data={"data": {"nodes": [{"id": "n1"}, {"id": "n2"}], "edges": []}, "meta": {"next_cursor": "c1", "has_more": True}}),
        _FakeResp(status_code=200, json_data={"data": {"nodes": [{"id": "n3"}], "edges": []}, "meta": {"next_cursor": "c2", "has_more": True}}),
    ]
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", _sequenced_client_for(pages))
    r = _client(graph_mod).get("/api/graph/jeevy_portal/export")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["truncated"] is True
    assert len(_SequencedFakeClient.calls) == 1  # stopped after the FIRST page hit the cap


def test_graph_export_truncates_at_page_cap(graph_mod, monkeypatch):
    """hasMore=true forever → stops at _EXPORT_MAX_PAGES, truncated=True
    (never loops forever on the daemon's dime)."""
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(graph_mod, "_EXPORT_MAX_PAGES", 3)
    page = _FakeResp(
        status_code=200,
        json_data={"data": {"nodes": [{"id": "n"}], "edges": []}, "meta": {"next_cursor": "cN", "has_more": True}},
    )
    monkeypatch.setattr(graph_mod.httpx, "AsyncClient", _sequenced_client_for([page]))
    r = _client(graph_mod).get("/api/graph/jeevy_portal/export")
    assert r.status_code == 200
    body = r.json()["data"]
    assert body["truncated"] is True
    assert len(_SequencedFakeClient.calls) == 3  # exactly the page cap, not infinite


def test_graph_export_404_no_adapter(graph_mod, monkeypatch):
    monkeypatch.setattr(graph_mod, "get_kg_adapter", lambda _p: None)
    r = _client(graph_mod).get("/api/graph/jeevy_portal/export")
    assert r.status_code == 404


def test_graph_export_502_first_page_unreachable(graph_mod, monkeypatch):
    monkeypatch.setattr(
        graph_mod, "get_kg_adapter", lambda _p: {"url": "http://x/internal/kg/graph"}
    )
    monkeypatch.setattr(
        graph_mod.httpx, "AsyncClient", _client_for(graph_mod, raise_exc=httpx.ConnectError("boom"))
    )
    r = _client(graph_mod).get("/api/graph/jeevy_portal/export")
    assert r.status_code == 502


def test_runtime_field_constants_match_kgtypes_source(graph_mod):
    """Drift-pin: the runtime-cheap field sets in graph.py MUST equal the contract
    parsed from kgTypes.ts (the source of truth). If kgTypes.ts changes, this fails
    loud — the live gate can't silently diverge from the schema it enforces."""
    from test_kg_contract_gate import _KG_TYPES_REL, _find_repo_file, _parse_ts_interface

    src = _find_repo_file(_KG_TYPES_REL).read_text()
    node = _parse_ts_interface(src, "GraphNode")
    edge = _parse_ts_interface(src, "GraphEdge")

    node_required = {f for f, s in node.items() if not s["optional"]}
    node_optional = {f for f, s in node.items() if s["optional"]}
    edge_required = {f for f, s in edge.items() if not s["optional"]}
    edge_optional = {f for f, s in edge.items() if s["optional"]}

    assert set(graph_mod._NODE_REQUIRED) == node_required
    assert set(graph_mod._NODE_OPTIONAL) == node_optional
    assert set(graph_mod._EDGE_REQUIRED) == edge_required
    assert set(graph_mod._EDGE_OPTIONAL) == edge_optional
