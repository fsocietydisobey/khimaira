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


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.get("/graph/{project}")
    async def get_graph(project: str, scope: str = "") -> dict[str, Any]:
        adapter = get_kg_adapter(project)
        if not adapter or not adapter.get("url"):
            raise fastapi.HTTPException(
                404,
                f"no KG adapter registered for project {project!r} "
                f"(set a kg_adapter block in attached.json via "
                f"registry.set_kg_adapter)",
            )

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

        params = {"scope": scope} if scope else {}
        try:
            async with httpx.AsyncClient(timeout=_ADAPTER_TIMEOUT) as client:
                resp = await client.get(adapter["url"], params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise fastapi.HTTPException(
                502,
                f"KG adapter unreachable for project {project!r}: "
                f"{type(exc).__name__}: {exc}",
            ) from exc

        if resp.status_code >= 400:
            raise fastapi.HTTPException(
                502,
                f"KG adapter for project {project!r} returned "
                f"HTTP {resp.status_code}",
            )

        try:
            return resp.json()
        except ValueError as exc:
            raise fastapi.HTTPException(
                502,
                f"KG adapter for project {project!r} returned non-JSON: {exc}",
            ) from exc

    return router
