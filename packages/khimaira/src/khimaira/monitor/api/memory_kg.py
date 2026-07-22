"""`/internal/memory-kg/*` — the memory knowledge-graph's own adapter endpoints.

The monitor daemon serves these itself (no new service/process) and registers
`http://127.0.0.1:<port>/internal/memory-kg/graph` as the `khimaira` project's
own KG adapter (see monitor/memory_kg.register_adapter — memory is a khimaira
feature, so it rides the khimaira sidebar entry's kg tab).
The existing generic proxy (`GET /api/graph/khimaira` in api/graph.py)
then reaches these routes exactly like it reaches jeevy's adapter — same
`{data: {nodes, edges}}` contract, same `_sub_url` sibling convention for
node/<id>, schema, and health.

Route logic lives in monitor/memory_kg.py (fastapi-free so the MCP server
process can import it); every handler offloads via asyncio.to_thread because
the payload builders do file + SQLite I/O.
"""

from __future__ import annotations

import asyncio
from typing import Any

from .. import memory_kg
from .._optional import require


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.get("/graph")
    async def graph(scope: str = "", since: str = "") -> dict[str, Any]:
        """Full memory graph. `scope` filters to one corpus (khimaira/jeevy);
        `since` is accepted for proxy-signature compatibility but unused —
        memory entries carry no first-appearance timestamp."""
        return await asyncio.to_thread(memory_kg.graph_payload, scope)

    @router.get("/node/{node_id}")
    async def node(node_id: str, scope: str = "") -> dict[str, Any]:
        """Single-node detail + touching edges. `found: false` when the id
        doesn't resolve to a live/archived entry (graceful-empty, not 404)."""
        return await asyncio.to_thread(memory_kg.node_payload, node_id)

    @router.get("/schema")
    async def schema(scope: str = "", since: str = "") -> dict[str, Any]:
        """Entry-type meta-graph (type nodes with counts + type-level edges)."""
        return await asyncio.to_thread(memory_kg.schema_payload)

    @router.get("/health")
    async def health(scope: str = "", since: str = "") -> dict[str, Any]:
        """Aggregate counts: nodes/edges by type, archived nodes, dangling edges."""
        return await asyncio.to_thread(memory_kg.health_payload)

    return router
