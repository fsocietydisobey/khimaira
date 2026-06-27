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

import os
from typing import Any

import httpx

from ...attach.registry import get_kg_adapter
from .._optional import require

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
    async def get_graph(project: str, scope: str = "", since: str = "") -> dict[str, Any]:
        adapter = _adapter_or_404(project)
        return await _proxy_get(project, adapter, adapter["url"], scope, since)

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

    return router
