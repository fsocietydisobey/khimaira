"""CDP WebSocket connection manager.

Maintains a persistent connection to a Firefox instance running with
--remote-debugging-port. Handles:
  - Target (tab) discovery via HTTP /json endpoint
  - WebSocket connection to a chosen target
  - Command/response protocol (id-based request matching)
  - Event subscription and dispatch
  - Reconnection on disconnect

Firefox must be launched with:
  firefox --remote-debugging-port 9222
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx
import websockets
from websockets.asyncio.client import ClientConnection

from specter.config import SpecterConfig

logger = logging.getLogger(__name__)


@dataclass
class Target:
    """A browser tab/target."""

    id: str
    title: str
    url: str
    ws_url: str
    type: str

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "url": self.url,
            "type": self.type,
        }


class CDPConnection:
    """Persistent CDP connection to a Firefox browser tab.

    Usage:
        conn = CDPConnection(config)
        await conn.connect()           # connects to first page target
        result = await conn.send("Runtime.evaluate", {"expression": "1+1"})
        await conn.disconnect()
    """

    def __init__(self, config: SpecterConfig) -> None:
        self._config = config
        self._ws: ClientConnection | None = None
        self._request_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._event_handlers: dict[str, list[Callable]] = {}
        self._listener_task: asyncio.Task | None = None
        self._connected_target: Target | None = None

    @property
    def is_connected(self) -> bool:
        if self._ws is None:
            return False
        try:
            return self._ws.close_code is None
        except Exception:
            return False

    @property
    def current_target(self) -> Target | None:
        return self._connected_target

    async def list_targets(self) -> list[Target]:
        """List all available browser targets (tabs)."""
        async with httpx.AsyncClient() as client:
            try:
                resp = await client.get(self._config.json_endpoint, timeout=5.0)
                resp.raise_for_status()
            except (httpx.ConnectError, httpx.TimeoutException) as e:
                raise ConnectionError(
                    f"Cannot reach Firefox at {self._config.http_endpoint}. "
                    f"Is Firefox running with --remote-debugging-port {self._config.debug_port}?"
                ) from e

        targets: list[Target] = []
        for entry in resp.json():
            if entry.get("type") == "page":
                targets.append(
                    Target(
                        id=entry["id"],
                        title=entry.get("title", ""),
                        url=entry.get("url", ""),
                        ws_url=entry.get("webSocketDebuggerUrl", ""),
                        type=entry.get("type", ""),
                    )
                )
        return targets

    async def connect(self, target_id: str) -> Target:
        """Connect to a specific browser target. target_id is REQUIRED.

        There is no auto-pick. Call list_targets() first to discover available
        tab IDs, then pass the chosen id explicitly. This prevents Specter from
        silently anchoring to whichever tab happens to be first in the list.

        Args:
            target_id: The specific target ID to connect to.

        Returns:
            The connected Target.

        Raises:
            ValueError: target_id is None or empty — Specter never decides which
                tab to operate on; the caller always does.
            ConnectionError: If the browser isn't reachable, no page targets exist,
                or target_id is not found (tab closed/refreshed — caller must
                re-anchor via specter_list_tabs + specter_connect_to_tab).
        """
        if not target_id:
            raise ValueError(
                "Specter requires an explicit target_id. "
                "Call specter_list_tabs() to see available tabs, then "
                "specter_connect_to_tab(<id>) to anchor Specter to the correct tab. "
                "There is no auto-pick — Specter never decides which tab to operate on."
            )

        targets = await self.list_targets()
        if not targets:
            raise ConnectionError("No page targets found. Is a page open in the browser?")

        target = next((t for t in targets if t.id == target_id), None)
        if target is None:
            raise ConnectionError(
                f"Specter target {target_id!r} not found in current targets. "
                f"The tab may have been closed, refreshed, or navigated away. "
                f"Call specter_list_tabs() to see available tabs, then "
                f"specter_connect_to_tab(<new_id>) to re-anchor Specter to the correct tab."
            )

        if not target.ws_url:
            raise ConnectionError(f"Target '{target.title}' has no WebSocket URL.")

        logger.info("Connecting to target: %s (%s)", target.title, target.url)
        self._ws = await websockets.connect(target.ws_url)
        self._connected_target = target
        self._listener_task = asyncio.create_task(self._listen())

        return target

    async def disconnect(self) -> None:
        """Close the WebSocket connection."""
        if self._listener_task:
            self._listener_task.cancel()
            try:
                await self._listener_task
            except asyncio.CancelledError:
                pass
        if self._ws:
            await self._ws.close()
            self._ws = None
        self._connected_target = None

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict:
        """Send a CDP command and wait for the response.

        Args:
            method: CDP method name (e.g., "Runtime.evaluate").
            params: Optional parameters dict.

        Returns:
            The CDP response result dict.
        """
        if not self._ws:
            raise ConnectionError("Not connected. Call connect() first.")

        self._request_id += 1
        request_id = self._request_id

        message = {"id": request_id, "method": method}
        if params:
            message["params"] = params

        future: asyncio.Future = asyncio.get_event_loop().create_future()
        self._pending[request_id] = future

        try:
            await self._ws.send(json.dumps(message))
        except websockets.ConnectionClosed as e:
            self._pending.pop(request_id, None)
            self._ws = None
            raise RuntimeError(
                f"WebSocket connection lost while sending {method}. "
                "The browser tab may have navigated or closed."
            ) from e

        try:
            result = await asyncio.wait_for(future, timeout=30.0)
        except asyncio.TimeoutError:
            self._pending.pop(request_id, None)
            raise TimeoutError(f"CDP command {method} timed out after 30s")

        if "error" in result:
            raise RuntimeError(f"CDP error: {result['error']}")

        return result.get("result", {})

    def on(self, event: str, handler: Callable) -> None:
        """Register an event handler for a CDP event.

        Args:
            event: CDP event name (e.g., "Runtime.consoleAPICalled").
            handler: Callable that receives the event params dict.
        """
        self._event_handlers.setdefault(event, []).append(handler)

    async def _listen(self) -> None:
        """Background task that processes incoming WebSocket messages."""
        try:
            async for raw in self._ws:
                try:
                    message = json.loads(raw)
                except json.JSONDecodeError:
                    continue

                # Response to a command
                if "id" in message:
                    future = self._pending.pop(message["id"], None)
                    if future and not future.done():
                        future.set_result(message)

                # Event
                if "method" in message:
                    event = message["method"]
                    params = message.get("params", {})
                    for handler in self._event_handlers.get(event, []):
                        try:
                            handler(params)
                        except Exception as e:
                            logger.warning("Event handler error for %s: %s", event, e)

        except websockets.ConnectionClosed:
            logger.warning("CDP WebSocket connection closed")
        except asyncio.CancelledError:
            pass
