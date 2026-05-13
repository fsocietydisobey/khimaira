"""`/api/mcp-calls` — MCP tool invocation telemetry.

Endpoints:
  GET /api/mcp-calls               — recent calls (filterable)
  GET /api/mcp-calls/summary       — aggregate stats over a window
"""

from __future__ import annotations

from typing import Any

from khimaira.monitor import mcp_calls

from .._optional import require


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.get("/mcp-calls")
    async def list_calls(
        window_minutes: int | None = None,
        tool: str | None = None,
        only_failures: bool = False,
        limit: int = 200,
    ) -> dict[str, Any]:
        calls = mcp_calls.query_calls(
            window_minutes=window_minutes,
            tool_filter=tool,
            only_failures=only_failures,
            limit=limit,
        )
        return {
            "log_path": str(mcp_calls.log_file_path()),
            "count": len(calls),
            "calls": calls,
        }

    @router.get("/mcp-calls/summary")
    async def get_summary(window_minutes: int = 60 * 24) -> dict[str, Any]:
        return mcp_calls.summarize(window_minutes=window_minutes)

    return router
