"""`GET /api/graph/<project>` — generic graph proxy to a per-project KG adapter.

Layer 2 of the code-agnostic KG mapper (mirrors how the LangGraph monitor's
`/api/topology/<project>` serves any attached project). The monitor-ui calls
this same-origin (vite `/api` proxy → daemon 8740); the daemon proxies to the
project's configured KG-adapter URL with a Bearer token, and returns the
adapter's generic `{nodes, edges}` contract verbatim.

This is what makes the UI agnostic AND solves the cross-origin/auth wall: the
UI never talks to the project's API directly, and the secret stays in the
daemon's environment (resolved from the env-var NAME in attached.json — never
stored at rest; security rule: load_dotenv(override=True) + env-var name only).

Fail loud:
  - 404 — no `kg_adapter` registered for the project
  - 500 — adapter declares a `token_env` but that env var is unset
  - 502 — adapter unreachable, returned an error status, or returned non-JSON

Auth header is per-adapter (`auth_header` in the kg_adapter config; default
`Authorization: Bearer <token>`). A project may set e.g. `X-Internal-Key` to
match its existing service-auth — the raw token is then sent under that header.

⚠️ DEPLOY GOTCHA (jeevy adapter, confirmed by muther 2026-06-27): jeevy's
`verify_internal_key` FAILS OPEN when `INTERNAL_API_KEY` is unset (a dev-mode
bypass, internal.py:26-28). So the shared secret MUST be provisioned in BOTH
environments — the daemon's `token_env` and jeevy's `INTERNAL_API_KEY` must hold
the SAME value — or the endpoint is effectively unauthenticated. Set both at
deploy; never leave the jeevy-side key empty in any environment the daemon can
reach.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

from ...attach.registry import get_kg_adapter
from .._optional import require

log = logging.getLogger(__name__)

# Outbound timeout: generous read for a large shop graph (shop 10 ≈ 4.7k nodes),
# short connect so a down adapter fails fast → 502.
_ADAPTER_TIMEOUT = httpx.Timeout(connect=5.0, read=30.0, write=10.0, pool=5.0)


def _resolve_token(token_env: str) -> str | None:
    """Resolve the bearer token from the daemon's environment.

    Mirrors the in-daemon precedent (monitor/api/oracle.py): load_dotenv with
    override=True so a project `.env` wins over inherited shell env, then read
    the named var. Returns None when unset so the caller can fail loud.
    """
    try:
        from dotenv import load_dotenv

        load_dotenv(override=True)
    except Exception:
        pass
    return os.environ.get(token_env) or None


def _sub_url(graph_url: str, suffix: str) -> str:
    """Derive an adapter sub-path from its configured graph URL.

    The kg_adapter `url` is the graph endpoint (e.g. `.../internal/kg/graph`);
    every other endpoint is its sibling (`.../internal/kg/<suffix>`). Strip a
    trailing `/graph` to get the base, then append `/<suffix>`. Keeps the daemon
    code-agnostic — it doesn't hard-code the adapter's route layout beyond the
    `/graph` ↔ `/<suffix>` sibling convention.

      `.../internal/kg/graph` + "node/<id>"   → `.../internal/kg/node/<id>`
      `.../internal/kg/graph` + "schema"      → `.../internal/kg/schema`
      `.../internal/kg/graph` + "health"      → `.../internal/kg/health`
    """
    base = graph_url[:-6] if graph_url.endswith("/graph") else graph_url.rstrip("/")
    return f"{base}/{suffix.lstrip('/')}"


# ---------------------------------------------------------------------------
# Contract gate (#38 Tier-2) — fail-SAFE boundary enforcement of the khimaira-
# owned generic graph contract (kgTypes.ts GraphNode/GraphEdge). The /graph
# payload feeds a code-agnostic renderer that ASSUMES this shape; an adapter that
# drifts (or leaks a raw jeevy column like node_type/canonical_key) would corrupt
# the viewer. This DROPS nonconforming nodes/edges + annotates a structured
# `data._contract` warning (counts + a bounded violation sample, no silent
# truncation) rather than 502-ing the whole payload — this is a DEBUGGING surface,
# so partial data > no data (default-toward-recoverable). Hard-fail is OPT-IN via
# ?strict=true / KHIMAIRA_KG_CONTRACT_STRICT=1. The loud source-of-truth guard
# lives in tests/test_kg_contract_gate.py (field rules parsed from kgTypes.ts);
# the inline field sets below are a runtime-cheap copy that tests/test_graph_api.py
# pins to that source so they can't drift.
# ---------------------------------------------------------------------------

_NODE_REQUIRED = ("id", "type", "label")  # all string
_NODE_OPTIONAL = ("badge",)  # string | number
_EDGE_REQUIRED = ("from", "to", "type")  # all string
_EDGE_OPTIONAL = ("id", "weight")  # id: string, weight: number

_CONTRACT_STRICT = os.environ.get("KHIMAIRA_KG_CONTRACT_STRICT", "0") == "1"


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_number(v: Any) -> bool:
    # bool is an int subclass — exclude it (a bool badge/weight is nonsensical).
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _node_violations(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return ["not an object"]
    errs = [f"missing '{k}'" for k in _NODE_REQUIRED if k not in item]
    errs += [f"'{k}' not a string" for k in _NODE_REQUIRED if k in item and not _is_str(item[k])]
    if "badge" in item and not (_is_str(item["badge"]) or _is_number(item["badge"])):
        errs.append("'badge' not string|number")
    extra = sorted(set(item) - set(_NODE_REQUIRED) - set(_NODE_OPTIONAL))
    if extra:
        errs.append(f"non-contract field(s) {extra}")
    return errs


def _edge_violations(item: Any) -> list[str]:
    if not isinstance(item, dict):
        return ["not an object"]
    errs = [f"missing '{k}'" for k in _EDGE_REQUIRED if k not in item]
    errs += [f"'{k}' not a string" for k in _EDGE_REQUIRED if k in item and not _is_str(item[k])]
    if "id" in item and not _is_str(item["id"]):
        errs.append("'id' not a string")
    if "weight" in item and not _is_number(item["weight"]):
        errs.append("'weight' not a number")
    extra = sorted(set(item) - set(_EDGE_REQUIRED) - set(_EDGE_OPTIONAL))
    if extra:
        errs.append(f"non-contract field(s) {extra}")
    return errs


def _filter_to_contract(payload: Any, *, strict: bool) -> Any:
    """Fail-safe contract gate for the `{data:{nodes,edges}}` graph payload.

    Drops nonconforming nodes/edges and annotates `data._contract` with the dropped
    counts + a bounded violation sample. `strict=True` raises 502 on any violation
    instead (CI / opt-in). Non-graph shapes (node/edge/schema/aggregate routes, or
    an unrecognized body) pass through untouched — this gate only owns the graph
    contract.
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        return payload
    data = payload["data"]
    nodes, edges = data.get("nodes"), data.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        return payload  # not the {nodes,edges} graph shape — not ours to gate

    good_nodes: list[Any] = []
    good_edges: list[Any] = []
    bad: list[tuple[str, Any, list[str]]] = []  # (kind, item, violations)
    for n in nodes:
        v = _node_violations(n)
        if v:
            bad.append(("node", n, v))
        else:
            good_nodes.append(n)
    for e in edges:
        v = _edge_violations(e)
        if v:
            bad.append(("edge", e, v))
        else:
            good_edges.append(e)

    if not bad:
        return payload
    if strict:
        fastapi = require("fastapi")
        raise fastapi.HTTPException(
            502,
            f"KG adapter response violates contract: {len(bad)} nonconforming item(s)",
        )

    dropped_nodes = sum(1 for k, _, _ in bad if k == "node")
    dropped_edges = len(bad) - dropped_nodes
    samples = [
        {
            "kind": k,
            "id": (it.get("id") if isinstance(it, dict) else None),
            "violations": vs,
        }
        for (k, it, vs) in bad[:5]
    ]
    log.warning(
        "kg contract-gate: dropped %d nonconforming item(s) (%d nodes, %d edges)",
        len(bad),
        dropped_nodes,
        dropped_edges,
    )
    # Build a NEW payload — never mutate the adapter's response in place (defensive:
    # the caller may hold a reference; in-place mutation is an aliasing footgun).
    new_data = {
        **data,
        "nodes": good_nodes,
        "edges": good_edges,
        "_contract": {
            "ok": False,
            "droppedNodes": dropped_nodes,
            "droppedEdges": dropped_edges,
            "sampleViolations": samples,
        },
    }
    return {**payload, "data": new_data}


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    def _adapter_or_404(project: str) -> dict[str, Any]:
        adapter = get_kg_adapter(project)
        if not adapter or not adapter.get("url"):
            raise fastapi.HTTPException(
                404,
                f"no KG adapter registered for project {project!r} "
                f"(set a kg_adapter block in attached.json via "
                f"registry.set_kg_adapter)",
            )
        return adapter

    def _auth_headers(adapter: dict[str, Any]) -> dict[str, str]:
        headers: dict[str, str] = {}
        token_env = adapter.get("token_env")
        if token_env:
            token = _resolve_token(token_env)
            if not token:
                raise fastapi.HTTPException(
                    500,
                    f"KG adapter token env {token_env!r} is not set in the "
                    f"daemon environment (provision it via systemd/.env)",
                )
            # Auth-header convention is PER-ADAPTER so the daemon stays
            # code-agnostic — not every project uses `Authorization: Bearer`.
            # Default is Bearer; a project may set e.g. auth_header="X-Internal-Key"
            # (jeevy's existing internal-auth) → the raw token is sent under that
            # header with no scheme prefix.
            auth_header = adapter.get("auth_header") or "Authorization"
            if auth_header.lower() == "authorization":
                headers["Authorization"] = f"Bearer {token}"
            else:
                headers[auth_header] = token
        return headers

    async def _proxy_get(
        project: str,
        adapter: dict[str, Any],
        url: str,
        scope: str,
        since: str = "",
    ) -> dict[str, Any]:
        """Proxy a GET to the adapter URL with auth + scope; return its JSON.

        Shared by every graph/aggregate route so they fail loud identically
        (502 unreachable / error-status / non-JSON). `since` (ISO timestamp) is
        forwarded verbatim when set — the adapter decides which created_at
        column it filters (first-appearance); the daemon stays code-agnostic.
        """
        headers = _auth_headers(adapter)
        params = {}
        if scope:
            params["scope"] = scope
        if since:
            params["since"] = since
        try:
            async with httpx.AsyncClient(timeout=_ADAPTER_TIMEOUT) as client:
                resp = await client.get(url, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise fastapi.HTTPException(
                502,
                f"KG adapter unreachable for project {project!r}: {type(exc).__name__}: {exc}",
            ) from exc

        if resp.status_code >= 400:
            raise fastapi.HTTPException(
                502,
                f"KG adapter for project {project!r} returned HTTP {resp.status_code}",
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise fastapi.HTTPException(
                502,
                f"KG adapter for project {project!r} returned non-JSON: {exc}",
            ) from exc

    @router.get("/graph/{project}")
    async def get_graph(
        project: str, scope: str = "", since: str = "", strict: bool = False
    ) -> dict[str, Any]:
        adapter = _adapter_or_404(project)
        payload = await _proxy_get(project, adapter, adapter["url"], scope, since)
        # #38 contract gate: fail-safe by default (drop+annotate nonconforming),
        # hard-502 only when ?strict=true or KHIMAIRA_KG_CONTRACT_STRICT=1.
        return _filter_to_contract(payload, strict=strict or _CONTRACT_STRICT)

    @router.get("/graph/{project}/node/{node_id}")
    async def get_graph_node(project: str, node_id: str, scope: str = "") -> dict[str, Any]:
        """Proxy a single node's detail (facts + edges) to the project's adapter.

        The opaque `node_id` (graph-contract id) is passed through verbatim — the
        daemon never interprets it; only the adapter resolves it to a node.
        """
        adapter = _adapter_or_404(project)
        return await _proxy_get(
            project, adapter, _sub_url(adapter["url"], f"node/{node_id}"), scope
        )

    @router.get("/graph/{project}/node/{node_id}/source")
    async def get_graph_node_source(
        project: str, node_id: str, scope: str = ""
    ) -> dict[str, Any]:
        """Proxy a node's underlying SOURCE DB record — the "DB RECORD" peek.

        Returns the real source row behind the projected KG node, so the viewer
        can show ground-truth fields the projection drops (owner_kind, status,
        timestamps). Adapter envelope passed through verbatim:
        `{data:{found, node_type, canonical_key, table, source_id, row}, meta}`.
        `found:false` (node out-of-scope, OR a name/composite-keyed type with no
        single source PK) is a graceful-empty case with `data.reason`, NOT an
        error. The opaque node_id passes through verbatim — only the adapter
        resolves it (via its canonical_key → table+PK registry).
        """
        adapter = _adapter_or_404(project)
        return await _proxy_get(
            project, adapter, _sub_url(adapter["url"], f"node/{node_id}/source"), scope
        )

    @router.get("/graph/{project}/edge/{edge_id}")
    async def get_graph_edge(project: str, edge_id: str, scope: str = "") -> dict[str, Any]:
        """Proxy a single edge's provenance (the edge-debug surface) to the
        project's adapter. The opaque edge_id passes through verbatim."""
        adapter = _adapter_or_404(project)
        return await _proxy_get(
            project, adapter, _sub_url(adapter["url"], f"edge/{edge_id}"), scope
        )

    @router.get("/graph/{project}/schema")
    async def get_graph_schema(project: str, scope: str = "", since: str = "") -> dict[str, Any]:
        """Proxy the project's KG type meta-graph (structural-gap finder)."""
        adapter = _adapter_or_404(project)
        return await _proxy_get(project, adapter, _sub_url(adapter["url"], "schema"), scope, since)

    # --- Phase 3: aggregate / monitoring routes ----------------------------
    # Each proxies to the adapter's sibling endpoint and returns a generic,
    # opaque-keyed shape (the adapter is the only schema-aware layer). These
    # turn "audit the whole graph" — previously SQL-only — into one MCP call
    # for roster agents that have no DB access.

    @router.get("/graph/{project}/health")
    async def get_graph_health(project: str, scope: str = "", since: str = "") -> dict[str, Any]:
        """Aggregate KG health: per-type node counts + degree-0 orphans +
        dangling edges + parent-containment coverage (the "172/276 jobs
        disconnected" headline). `since` scopes to first-appearance ≥ ts."""
        adapter = _adapter_or_404(project)
        return await _proxy_get(project, adapter, _sub_url(adapter["url"], "health"), scope, since)

    @router.get("/graph/{project}/coverage")
    async def get_graph_coverage(project: str, scope: str = "") -> dict[str, Any]:
        """Relational-vs-KG coverage per entity (the under-projection detector,
        e.g. "46 users / 4 nodes"). The adapter owns the entity→node-kind map."""
        adapter = _adapter_or_404(project)
        return await _proxy_get(project, adapter, _sub_url(adapter["url"], "coverage"), scope)

    @router.get("/graph/{project}/edges-audit")
    async def get_graph_edges_audit(
        project: str, scope: str = "", since: str = ""
    ) -> dict[str, Any]:
        """Aggregate edge provenance: match-method + confidence histograms and
        the low-confidence/fuzzy/llm suspect tail (with a no-silent-truncation
        total). The population view that complements per-edge kg_edge."""
        adapter = _adapter_or_404(project)
        return await _proxy_get(
            project, adapter, _sub_url(adapter["url"], "edges-audit"), scope, since
        )

    @router.get("/graph/{project}/scopes")
    async def get_graph_scopes(project: str) -> dict[str, Any]:
        """Scope discovery: lists available scopes (shops/tenants) for this
        adapter.

        No scope/since args — this IS the call that tells you which scopes
        exist. Returns the adapter's JSON verbatim; the tool (kg_scopes) renders
        it. Expected adapter shape: `{"data": {"scopes": [{"scope", "nodes",
        "edges", "label"}, ...]}}` sorted by nodes DESC (richest first).
        """
        adapter = _adapter_or_404(project)
        return await _proxy_get(project, adapter, _sub_url(adapter["url"], "scopes"), "")

    return router
