"""`/api/frontend_components/{project}` — extract React/Next components."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._optional import require
from ..discovery import frontend_components as fc_extractor
from ..discovery.project import Project


def build_router(projects: list[Project]):
    fastapi = require("fastapi")
    router = fastapi.APIRouter()
    name_to_path: dict[str, Path] = {p.name: p.path for p in projects}

    @router.get("/frontend_components/{name}")
    async def list_components(name: str) -> dict[str, Any]:
        path = name_to_path.get(name)
        if path is None:
            raise fastapi.HTTPException(404, f"unknown project: {name}")
        comps = fc_extractor.extract_from_path(path)
        return {
            "project": name,
            "count": len(comps),
            "with_api_calls": sum(1 for c in comps if c.api_calls),
            "components": [c.to_dict() for c in comps],
        }

    return router
