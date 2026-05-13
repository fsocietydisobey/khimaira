"""RuntimeStatus — `khimaira dev`'s view of the running stack.

Polled by the dashboard's runtime panel ('is the dev server up? is Chrome
attached? is the DB connected?'). Also returned by `khimaira doctor`.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ComponentStatus = Literal["starting", "ready", "degraded", "stopped", "failed"]


class ComponentHealth(BaseModel):
    """One subsystem khimaira dev manages."""

    name: str = Field(description="dev_server | browser | postgres | langgraph_monitor | etc.")
    status: ComponentStatus
    detail: str = Field(default="", description="Short human-readable status.")
    pid: int | None = None
    port: int | None = None
    url: str | None = None
    started_at: str | None = Field(default=None, description="ISO 8601 UTC.")
    last_check_at: str | None = None


class RuntimeStatus(BaseModel):
    """Composite status of the whole `khimaira dev` stack."""

    project_name: str
    project_path: str

    components: list[ComponentHealth]

    overall_status: ComponentStatus = Field(
        description=(
            "Worst-of-many: failed > degraded > starting > ready > stopped. "
            "Drives the single status badge in the dashboard header."
        ),
    )
