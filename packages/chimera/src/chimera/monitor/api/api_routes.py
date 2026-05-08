"""`/api/api_routes/{project}` — extract FastAPI routes for a project,
plus their links to LangGraph invocations.

Used by the full-stack-trace skill to follow user actions across
layers. The chimera-monitor daemon serves the data; the chimera MCP
server exposes it as a tool (`monitor_api_routes`).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._optional import require
from ..discovery import api_routes as api_routes_extractor
from ..discovery.project import Project


def build_router(projects: list[Project]):
    fastapi = require("fastapi")
    router = fastapi.APIRouter()
    name_to_path: dict[str, Path] = {p.name: p.path for p in projects}

    @router.get("/api_routes/{name}")
    async def list_api_routes(name: str) -> dict[str, Any]:
        path = name_to_path.get(name)
        if path is None:
            raise fastapi.HTTPException(404, f"unknown project: {name}")
        routes = api_routes_extractor.extract_from_path(path)
        return {
            "project": name,
            "count": len(routes),
            "graph_linked_count": sum(1 for r in routes if r.invokes_graph),
            "routes": [r.to_dict() for r in routes],
        }

    return router
