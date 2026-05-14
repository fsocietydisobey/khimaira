"""Runtime tools: JS evaluation, page info, screenshots.

Provides the active-debugging tools that complement the passive event
capture (console + network). These are request/response — Claude calls
them on demand, they return immediately.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from specter.browser.connection import CDPConnection
from specter.config import SpecterConfig

logger = logging.getLogger(__name__)


class Runtime:
    """Active debugging tools via CDP."""

    def __init__(self, config: SpecterConfig) -> None:
        self._config = config

    async def evaluate_js(self, connection: CDPConnection, expression: str) -> dict:
        """Evaluate a JavaScript expression in the page context.

        Args:
            connection: Active CDP connection.
            expression: JS expression to evaluate.

        Returns:
            Dict with type, value, and description of the result.
        """
        result = await connection.send(
            "Runtime.evaluate",
            {
                "expression": expression,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )

        remote_object = result.get("result", {})
        exception = result.get("exceptionDetails")

        if exception:
            return {
                "error": True,
                "message": exception.get("text", "Evaluation error"),
                "exception": remote_object.get("description", ""),
            }

        return {
            "type": remote_object.get("type", "undefined"),
            "value": remote_object.get("value"),
            "description": remote_object.get("description", ""),
        }

    async def get_page_info(self, connection: CDPConnection) -> dict:
        """Get current page info: URL, title, document state."""
        result = await self.evaluate_js(
            connection,
            "JSON.stringify({url: location.href, title: document.title, readyState: document.readyState, cookies: document.cookie.length})",
        )

        if result.get("error"):
            return {"error": result["message"]}

        import json

        try:
            info = json.loads(result.get("value", "{}"))
        except (json.JSONDecodeError, TypeError):
            info = {"url": "unknown", "title": "unknown"}

        return info

    async def take_screenshot(
        self,
        connection: CDPConnection,
        full_page: bool = False,
        selector: str | None = None,
    ) -> dict:
        """Capture a screenshot of the current page.

        The screenshot is saved as a PNG to the configured screenshot
        directory. Returns the file path so Claude can read it with
        the Read tool (Claude Code is multimodal and can view images).

        Args:
            connection: Active CDP connection.
            full_page: If True, capture the entire scrollable page.
            selector: Optional CSS selector to screenshot a specific element.

        Returns:
            Dict with file_path, dimensions, and timestamp.
        """
        params: dict[str, Any] = {"format": "png"}

        if selector:
            # Get the element's bounding box first
            box_result = await self.evaluate_js(
                connection,
                f"""
                (() => {{
                    const el = document.querySelector('{selector}');
                    if (!el) return null;
                    const rect = el.getBoundingClientRect();
                    return JSON.stringify({{
                        x: rect.x, y: rect.y,
                        width: rect.width, height: rect.height
                    }});
                }})()
                """,
            )

            if box_result.get("value"):
                import json

                box = json.loads(box_result["value"])
                params["clip"] = {
                    "x": box["x"],
                    "y": box["y"],
                    "width": box["width"],
                    "height": box["height"],
                    "scale": 1,
                }

        if full_page and not selector:
            # Get full page dimensions
            metrics = await self.evaluate_js(
                connection,
                "JSON.stringify({width: document.documentElement.scrollWidth, height: document.documentElement.scrollHeight})",
            )
            if metrics.get("value"):
                import json

                dims = json.loads(metrics["value"])
                params["clip"] = {
                    "x": 0,
                    "y": 0,
                    "width": dims["width"],
                    "height": dims["height"],
                    "scale": 1,
                }

        result = await connection.send("Page.captureScreenshot", params)
        image_data = result.get("data", "")

        if not image_data:
            return {"error": "Screenshot capture returned no data"}

        # Save to file
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{timestamp}.png"
        file_path = self._config.screenshot_dir / filename

        file_path.write_bytes(base64.b64decode(image_data))

        logger.info("Screenshot saved: %s", file_path)

        return {
            "file_path": str(file_path),
            "timestamp": timestamp,
            "full_page": full_page,
            "selector": selector,
        }

    async def get_dom_html(
        self,
        connection: CDPConnection,
        selector: str = "body",
        outer: bool = False,
    ) -> dict:
        """Get the HTML content of an element.

        Args:
            connection: Active CDP connection.
            selector: CSS selector for the element.
            outer: If True, return outerHTML; if False, innerHTML.

        Returns:
            Dict with the HTML content (truncated if very large).
        """
        prop = "outerHTML" if outer else "innerHTML"
        result = await self.evaluate_js(
            connection,
            f"document.querySelector('{selector}')?.{prop} ?? 'Element not found: {selector}'",
        )

        if result.get("error"):
            return {"error": result["message"]}

        html = result.get("value", "")
        truncated = False
        if isinstance(html, str) and len(html) > 50000:
            html = html[:50000]
            truncated = True

        return {
            "selector": selector,
            "html": html,
            "truncated": truncated,
            "length": len(html),
        }

    async def navigate_to(self, connection: CDPConnection, url: str) -> dict:
        """Hard navigate the current tab to a URL (full page reload).

        Uses CDP Page.navigate — equivalent to typing a URL in the address
        bar. Triggers a full page load. Use router_navigate for client-side
        navigation that preserves app state.

        Args:
            connection: Active CDP connection.
            url: The URL to navigate to.

        Returns:
            Dict with the navigation result.
        """
        result = await connection.send("Page.navigate", {"url": url})

        error = result.get("errorText")
        if error:
            return {"error": error, "url": url}

        return {
            "navigated": True,
            "url": url,
            "type": "hard",
            "frame_id": result.get("frameId", ""),
        }

    async def router_navigate(self, connection: CDPConnection, path: str) -> dict:
        """Client-side navigate using the app's router (Next.js, React Router, etc.).

        Uses the app's own routing — no full reload, preserves state,
        respects the app's navigation rules. This is the same navigation
        that happens when a user clicks an internal link.

        Tries in order:
          1. Next.js App Router (window.next.router.push)
          2. Next.js Pages Router (window.__NEXT_ROUTER__.push)
          3. React Router (history.pushState + popstate event)
          4. Fallback: window.location.assign (soft reload)

        Args:
            connection: Active CDP connection.
            path: The path to navigate to (e.g., "/shop/quote/6/description").
                  Can include query params: "/shop/quote/6/description?source=9"

        Returns:
            Dict with navigation result and which router was used.
        """
        result = await self.evaluate_js(
            connection,
            f"""
            (() => {{
                const path = '{path.replace("'", "\\'")}';
                const currentPath = window.location.pathname + window.location.search;

                // If same path, nothing to do
                if (currentPath === path) {{
                    return JSON.stringify({{ navigated: false, path, reason: 'already on this path' }});
                }}

                // 1. Try clicking an existing link with this href (most reliable
                //    for Next.js — uses the app's own Link component behavior)
                const links = document.querySelectorAll('a[href]');
                for (const link of links) {{
                    try {{
                        const url = new URL(link.href);
                        if (url.pathname + url.search === path) {{
                            link.click();
                            return JSON.stringify({{ navigated: true, path, router: 'link-click' }});
                        }}
                    }} catch (e) {{}}
                }}

                // 2. Use location.href for Next.js — triggers the app's client-side
                //    routing without a full hard reload. Next.js intercepts same-origin
                //    navigations and does a soft transition.
                window.location.href = path;
                return JSON.stringify({{ navigated: true, path, router: 'location-href' }});
            }})()
            """,
        )

        if result.get("error"):
            return {"error": result.get("message", "Router navigation failed"), "path": path}

        import json as _json

        value = result.get("value")
        if isinstance(value, str):
            try:
                return _json.loads(value)
            except _json.JSONDecodeError:
                return {"navigated": True, "path": path, "router": "unknown"}

        return {"navigated": True, "path": path, "router": "unknown"}
