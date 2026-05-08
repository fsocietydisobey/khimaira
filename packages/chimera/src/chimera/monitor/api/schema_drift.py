"""`/api/schema_drift/{project}` — Pydantic models vs Postgres schema."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .._optional import require
from ..discovery import schema_drift as drift
from ..discovery.connections import discover_all
from ..discovery.project import Project


def build_router(projects: list[Project]):
    fastapi = require("fastapi")
    router = fastapi.APIRouter()
    name_to_path: dict[str, Path] = {p.name: p.path for p in projects}

    @router.get("/schema_drift/{name}")
    async def get_drift(name: str) -> dict[str, Any]:
        path = name_to_path.get(name)
        if path is None:
            raise fastapi.HTTPException(404, f"unknown project: {name}")

        models = drift.extract_models(path)
        if not models:
            return {
                "project": name,
                "model_count": 0,
                "with_drift": 0,
                "reports": [],
                "note": "no Pydantic / SQLAlchemy models found in source",
            }

        # Need a Postgres URL to compare against
        try:
            conns = discover_all(path)
        except Exception as exc:
            raise fastapi.HTTPException(500, f"connection discovery failed: {exc}")
        if not conns.postgres:
            return {
                "project": name,
                "model_count": len(models),
                "with_drift": 0,
                "reports": [],
                "note": (
                    "no Postgres connection discovered — schema drift "
                    "comparison requires a postgres:// URL in the project's .env"
                ),
            }

        # Use the first Postgres connection (most projects have one)
        try:
            reports = drift.diff_against_postgres(models, conns.postgres[0].url)
        except Exception as exc:
            raise fastapi.HTTPException(500, f"DB introspection failed: {exc}")

        return {
            "project": name,
            "model_count": len(models),
            "with_drift": sum(1 for r in reports if r.has_drift),
            "reports": [r.to_dict() for r in reports],
        }

    return router
