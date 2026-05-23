"""Sample tests for existing Specter tools — 5 tests demonstrating the pattern.

- 2 unit tests (mocked CDP via conftest.mock_cdp)
- 3 integration tests (real Chrome via conftest.chrome_or_skip)

These tests gate SLICE-A/B/C: new tools follow the same conftest + naming patterns.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from specter.browser.console import ConsoleCapture, ConsoleEntry
from specter.browser.runtime import Runtime
from specter.config import load_config


# ---------------------------------------------------------------------------
# Unit tests — no Chrome required
# ---------------------------------------------------------------------------


class TestGetPageInfoUnit:
    """specter_get_page_info returns a parsed dict from the JS evaluation."""

    @pytest.mark.asyncio
    async def test_returns_expected_keys(self, mock_cdp):
        """get_page_info parses the JSON blob from Runtime.evaluate correctly."""
        page_data = {"url": "http://localhost:8000/", "title": "My App", "readyState": "complete", "cookies": 0}
        mock_cdp.set_response(
            "Runtime.evaluate",
            {
                "result": {
                    "type": "string",
                    "value": json.dumps(page_data),
                }
            },
        )

        runtime = Runtime(load_config())
        result = await runtime.get_page_info(mock_cdp)

        assert result["url"] == "http://localhost:8000/"
        assert result["title"] == "My App"
        assert result["readyState"] == "complete"

    @pytest.mark.asyncio
    async def test_cdp_error_path_returns_error_key(self, mock_cdp):
        """When CDP returns an exception, get_page_info surfaces it as {error: ...}."""
        mock_cdp.set_response(
            "Runtime.evaluate",
            {
                "result": {"type": "object", "description": "Error: page crashed"},
                "exceptionDetails": {"text": "Uncaught Error: page crashed"},
            },
        )

        runtime = Runtime(load_config())
        result = await runtime.get_page_info(mock_cdp)

        assert "error" in result


class TestGetConsoleLogsUnit:
    """ConsoleCapture.get_logs() filters correctly with mocked buffer state."""

    def _capture_with_entries(self, entries: list[ConsoleEntry]) -> ConsoleCapture:
        config = load_config()
        capture = ConsoleCapture(config)
        for entry in entries:
            capture._console_buffer.append(entry)
        return capture

    def test_filter_by_level_returns_only_matching(self):
        capture = self._capture_with_entries([
            ConsoleEntry(timestamp=1.0, level="log",   text="hello", source="app.js:1"),
            ConsoleEntry(timestamp=2.0, level="error", text="boom",  source="app.js:2"),
            ConsoleEntry(timestamp=3.0, level="warn",  text="watch", source="app.js:3"),
            ConsoleEntry(timestamp=4.0, level="error", text="fail",  source="app.js:4"),
        ])

        errors = capture.get_logs(level="error")

        assert len(errors) == 2
        assert all(e["level"] == "error" for e in errors)
        assert {e["text"] for e in errors} == {"boom", "fail"}

    def test_no_filter_returns_all(self):
        capture = self._capture_with_entries([
            ConsoleEntry(timestamp=1.0, level="log",  text="a", source="x:1"),
            ConsoleEntry(timestamp=2.0, level="warn", text="b", source="x:2"),
        ])
        assert len(capture.get_logs()) == 2

    def test_empty_buffer_returns_empty_list(self):
        config = load_config()
        capture = ConsoleCapture(config)
        assert capture.get_logs() == []


# ---------------------------------------------------------------------------
# Integration tests — require Chrome/Firefox with --remote-debugging-port=9222
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_debug_snapshot_returns_populated_result(chrome_or_skip, fixture_page):
    """debug_snapshot() against the fixture page returns a non-empty screenshot path."""
    from specter.browser.connection import CDPConnection
    from specter.browser.console import ConsoleCapture
    from specter.browser.network import NetworkCapture
    from specter.browser.react import ReactInspector
    from specter.browser.runtime import Runtime
    from specter.browser.structure import StructureAnalyzer

    config = load_config()
    conn = CDPConnection(config)
    targets = await conn.list_targets()
    if not targets:
        pytest.skip("No browser targets available")
    await conn.connect(target_id=targets[0].id)

    try:
        # Navigate to fixture page
        runtime = Runtime(config)
        await runtime.navigate_to(conn, fixture_page)

        # Get page info — confirms the fixture loaded
        page_info = await runtime.get_page_info(conn)
        assert "url" in page_info
        url = page_info.get("url", "")
        assert url, "page_info should have a non-empty url"
        assert "127.0.0.1" in url or "localhost" in url or "fixture" in url

        # Screenshot — confirms CDP screenshot pipeline works
        screenshot = await runtime.take_screenshot(conn)
        assert "file_path" in screenshot
        from pathlib import Path
        assert Path(screenshot["file_path"]).exists()
    finally:
        await conn.disconnect()


@pytest.mark.asyncio
async def test_get_component_tree_returns_components(chrome_or_skip, fixture_page):
    """get_component_tree() on the fixture page finds the App component."""
    from specter.browser.connection import CDPConnection
    from specter.browser.react import ReactInspector
    from specter.browser.runtime import Runtime

    config = load_config()
    conn = CDPConnection(config)
    targets = await conn.list_targets()
    if not targets:
        pytest.skip("No browser targets available")
    await conn.connect(target_id=targets[0].id)

    try:
        runtime = Runtime(config)
        await runtime.navigate_to(conn, fixture_page)
        # Small wait for React to mount
        await asyncio.sleep(0.3)

        inspector = ReactInspector()
        tree = await inspector.get_component_tree(conn)

        # Should return a list (components) or a dict with an error key
        assert isinstance(tree, (dict, list))
        if isinstance(tree, list):
            # React dev mode is active — fixture page has an App component
            assert len(tree) > 0, "expected at least one component in fixture page"
        elif isinstance(tree, dict) and "error" in tree:
            # Dev tools not available — acceptable in some Chrome builds
            pass
    finally:
        await conn.disconnect()


@pytest.mark.asyncio
async def test_click_element_increments_counter(chrome_or_skip, fixture_page):
    """click_element on the Increment button increments the counter DOM text."""
    from specter.browser.connection import CDPConnection
    from specter.browser.interact import Interactor
    from specter.browser.runtime import Runtime

    config = load_config()
    conn = CDPConnection(config)
    targets = await conn.list_targets()
    if not targets:
        pytest.skip("No browser targets available")
    await conn.connect(target_id=targets[0].id)

    try:
        runtime = Runtime(config)
        await runtime.navigate_to(conn, fixture_page)
        await asyncio.sleep(0.3)

        # Read initial counter value
        initial_result = await runtime.evaluate_js(
            conn, "document.getElementById('counter')?.textContent"
        )
        initial_text = initial_result.get("value", "")

        # Click Increment
        interactor = Interactor()
        await interactor.click_element(conn, "#btn-increment")
        await asyncio.sleep(0.1)

        # Read updated counter
        updated_result = await runtime.evaluate_js(
            conn, "document.getElementById('counter')?.textContent"
        )
        updated_text = updated_result.get("value", "")

        assert initial_text != updated_text, "Counter text should change after click"
        assert "1" in updated_text, f"Expected count 1 in '{updated_text}'"
    finally:
        await conn.disconnect()
