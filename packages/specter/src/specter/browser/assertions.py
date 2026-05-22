"""First-class assertion primitives for regression testing via CDP.

Each assertion returns a structured {ok: bool, ...} dict. Callers can
use these as test gates: if ok is False, errors contains the offending entries.

Tools:
  assert_no_console_errors  — no JS errors/exceptions since a timestamp
  assert_no_network_errors  — no 4xx/5xx/network failures since a timestamp
  assert_element_visible    — element exists + is visible within a timeout

All three are designed to be lightweight wrappers over existing Specter
primitives rather than new CDP plumbing.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from specter.browser.connection import CDPConnection
    from specter.browser.console import ConsoleCapture
    from specter.browser.network import NetworkCapture

logger = logging.getLogger(__name__)

# JS used by assert_element_visible — polls until visible or timeout.
_VISIBLE_POLL_SCRIPT = """
(async function(selector, timeoutMs) {
    var start = Date.now();
    while (Date.now() - start < timeoutMs) {
        var el = document.querySelector(selector);
        if (el) {
            var style = getComputedStyle(el);
            var visible = el.offsetWidth > 0
                && el.offsetHeight > 0
                && style.visibility !== 'hidden'
                && style.display !== 'none';
            if (visible) return JSON.stringify({visible: true, found_after_ms: Date.now() - start});
        }
        await new Promise(function(r) { setTimeout(r, 100); });
    }
    var finalEl = document.querySelector(selector);
    return JSON.stringify({
        visible: false,
        found_after_ms: timeoutMs,
        element_exists: finalEl !== null,
    });
})('%SELECTOR%', %TIMEOUT%)
"""


class Asserter:
    """Structured assertion primitives for regression use in Specter tooling."""

    def assert_no_console_errors(
        self,
        console: "ConsoleCapture",
        since_ms: int | None = None,
    ) -> dict:
        """Assert no console errors or unhandled exceptions are buffered.

        Checks both console.error() calls and unhandled JS exceptions.
        Use after user interactions or page loads to confirm no regressions.

        Args:
            console: ConsoleCapture instance from the active session.
            since_ms: Only check errors after this Unix timestamp (ms).
                If None, checks all buffered errors.

        Returns:
            {ok: True} if no errors found.
            {ok: False, errors: [...]} with error details if any found.
        """
        since_sec = since_ms / 1000.0 if since_ms is not None else None
        error_logs = console.get_logs(level="error", since=since_sec, limit=50)
        exceptions = console.get_errors(since=since_sec, limit=50)
        all_errors = list(error_logs) + list(exceptions)
        if all_errors:
            return {"ok": False, "errors": all_errors, "error_count": len(all_errors)}
        return {"ok": True, "errors": [], "error_count": 0}

    def assert_no_network_errors(
        self,
        network: "NetworkCapture",
        since_ms: int | None = None,
        url_filter: str | None = None,
    ) -> dict:
        """Assert no failed HTTP requests (4xx, 5xx, network errors) are buffered.

        Use after API calls to confirm all requests succeeded.

        Args:
            network: NetworkCapture instance from the active session.
            since_ms: Only check errors after this Unix timestamp (ms).
                If None, checks all buffered errors.
            url_filter: Only requests whose URL contains this substring.

        Returns:
            {ok: True} if no errors found.
            {ok: False, errors: [...]} with request details if any found.
        """
        since_sec = since_ms / 1000.0 if since_ms is not None else None
        errors = network.get_requests(
            errors_only=True,
            since=since_sec,
            limit=50,
            url_filter=url_filter,
        )
        if errors:
            return {"ok": False, "errors": list(errors), "error_count": len(errors)}
        return {"ok": True, "errors": [], "error_count": 0}

    async def assert_element_visible(
        self,
        conn: "CDPConnection",
        selector: str,
        timeout_ms: int = 5000,
    ) -> dict:
        """Assert that an element exists and is visually visible within a timeout.

        Uses in-browser JS polling (no asyncio.sleep) per SLICE-T pattern.
        Checks: element exists, offsetWidth > 0, offsetHeight > 0, visibility
        not 'hidden', display not 'none'.

        Args:
            conn: Active CDP connection.
            selector: CSS selector for the element to check.
            timeout_ms: Maximum time to wait in milliseconds (default 5000).

        Returns:
            {visible: True, found_after_ms: N} on success.
            {visible: False, found_after_ms: N, element_exists: bool} on failure.
            {error: "..."} if evaluation fails.
        """
        safe_sel = selector.replace("'", "\\'")
        script = _VISIBLE_POLL_SCRIPT.replace("%SELECTOR%", safe_sel).replace(
            "%TIMEOUT%", str(timeout_ms)
        )

        result = await conn.send(
            "Runtime.evaluate",
            {
                "expression": script,
                "returnByValue": True,
                "awaitPromise": True,
            },
        )

        if "exceptionDetails" in result:
            exc = result["exceptionDetails"].get("text", "evaluation error")
            return {"error": f"assert_element_visible failed: {exc}"}

        raw_value = result.get("result", {}).get("value")
        if raw_value is None:
            return {"error": "assert_element_visible returned null"}

        import json

        try:
            parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        except (json.JSONDecodeError, TypeError):
            return {"error": f"could not parse visibility result: {raw_value!r}"}

        # Add `ok` key for consistency with assert_no_console_errors / assert_no_network_errors.
        # All three assertion tools must return {ok: bool, ...} so callers can check uniformly.
        if isinstance(parsed, dict) and "visible" in parsed:
            parsed["ok"] = parsed["visible"]
        return parsed
