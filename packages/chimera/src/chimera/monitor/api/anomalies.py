"""`/api/anomalies` — recent self-watch anomaly results.

Surfaces what the in-daemon invariant checker has flagged. Used by
the `monitor_anomalies` MCP tool and by anyone curious about whether
the dashboard's claims match reality.
"""

from __future__ import annotations

from typing import Any

from .._optional import require
from .. import anomalies as anomalies_module


def build_router():
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.get("/anomalies")
    async def list_anomalies(limit: int = 50, only_failures: bool = False) -> dict[str, Any]:
        items = anomalies_module.recent_anomalies(limit=limit)
        if only_failures:
            items = [it for it in items if not it.get("passed", True)]
        return {
            "count": len(items),
            "items": items,
        }

    @router.get("/heartbeat")
    async def get_heartbeat() -> dict[str, Any]:
        """Latest self-watch completion timestamp. Use for liveness checks:
        if `last_self_watch_at` is older than expected (e.g. >10min when
        cadence is 5min), the daemon's self-watch loop is stuck or dead.

        Returns {} when self-watch has never run (first ~90s of daemon life).
        Otherwise: { last_self_watch_at, checks_total, checks_failed }.
        """
        from datetime import datetime, timezone

        hb = anomalies_module.heartbeat()
        if not hb:
            return {"healthy": False, "reason": "self-watch has never run"}
        try:
            last = datetime.fromisoformat(hb["last_self_watch_at"])
            age_s = (datetime.now(timezone.utc) - last).total_seconds()
        except Exception:
            return {"healthy": False, "reason": "malformed heartbeat", **hb}
        # Cadence is 5min; allow 2× margin before flagging unhealthy
        healthy = age_s < 600
        return {
            "healthy": healthy,
            "age_seconds": age_s,
            **hb,
        }

    return router
