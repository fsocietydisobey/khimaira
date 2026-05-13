"""`/api/projects` — list discovered projects + their detected connections."""

from __future__ import annotations

from pathlib import Path

from .._optional import require
from ..discovery.connections import Connections
from ..discovery.project import Project


def build_router(projects: list[Project], connections_by_project: dict[Path, Connections]):
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.get("/projects")
    async def list_projects():
        return [_serialize(p, connections_by_project.get(p.path)) for p in projects]

    @router.get("/projects/{name}")
    async def get_project(name: str):
        for p in projects:
            if p.name == name:
                return _serialize(p, connections_by_project.get(p.path))
        raise fastapi.HTTPException(status_code=404, detail=f"project not found: {name}")

    return router


def _serialize(project: Project, conns: Connections | None) -> dict:
    pg = conns.postgres if conns else []
    sqlite = conns.sqlite if conns else []
    return {
        "name": project.name,
        "path": str(project.path),
        "detected_via": project.detected_via,
        "has_pyproject": project.has_pyproject,
        "connections": [
            {"kind": "postgres", "var": c.var, "host": c.host, "database": c.database}
            for c in pg
        ] + [
            {"kind": "sqlite", "label": c.label, "path": c.path}
            for c in sqlite
        ],
    }
