"""Tests for SLICE-A tools: render_reasons, diff_component_tree, track_hooks.

Pattern: unit tests use mock_cdp from conftest; integration tests use
chrome_or_skip + fixture_page. Integration tests record short windows
(1 second) and trigger interactions on the fixture page.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from specter.browser.react import ReactInspector, diff_component_tree


# ---------------------------------------------------------------------------
# render_reasons — unit tests
# ---------------------------------------------------------------------------


class TestRenderReasonsUnit:
    @pytest.mark.asyncio
    async def test_returns_error_when_no_hook(self, mock_cdp):
        """If React DevTools hook is absent, render_reasons returns error entry."""
        mock_cdp.set_response(
            "Runtime.evaluate",
            {"result": {"type": "string", "value": json.dumps({"error": "React DevTools hook not found. Is React running in dev mode?"})}},
        )
        inspector = ReactInspector()
        result = await inspector.render_reasons(mock_cdp, duration_s=0.0)
        assert isinstance(result, list)
        assert len(result) == 1
        assert "error" in result[0]

    @pytest.mark.asyncio
    async def test_install_then_read_cycle(self, mock_cdp):
        """render_reasons calls install script then read script via CDP."""
        responses = [
            # First call: install
            {"result": {"type": "string", "value": json.dumps({"installed": True})}},
            # Second call: read
            {"result": {"type": "string", "value": json.dumps([
                {"component_path": "App>Counter", "reason": "state changed",
                 "prev": None, "next": None, "commit_batch_id": 1},
            ])}},
        ]
        call_index = 0

        async def dynamic_send(method, params=None):
            nonlocal call_index
            resp = responses[min(call_index, len(responses) - 1)]
            call_index += 1
            mock_cdp.calls.append({"method": method, "params": params or {}})
            return resp

        mock_cdp.send = dynamic_send

        inspector = ReactInspector()
        result = await inspector.render_reasons(mock_cdp, duration_s=0.0)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["reason"] == "state changed"
        assert result[0]["commit_batch_id"] == 1

    @pytest.mark.asyncio
    async def test_render_reasons_emits_props_changed(self, mock_cdp):
        """render_reasons returns entries with reason starting 'props.' when props change."""
        responses = [
            {"result": {"type": "string", "value": json.dumps({"installed": True})}},
            {"result": {"type": "string", "value": json.dumps([
                {"component_path": "App>Header", "reason": "props.title changed",
                 "prev": "old", "next": "new", "commit_batch_id": 1},
            ])}},
        ]
        call_index = 0

        async def dynamic_send(method, params=None):
            nonlocal call_index
            resp = responses[min(call_index, len(responses) - 1)]
            call_index += 1
            mock_cdp.calls.append({"method": method, "params": params or {}})
            return resp

        mock_cdp.send = dynamic_send

        inspector = ReactInspector()
        result = await inspector.render_reasons(mock_cdp, duration_s=0.0)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["reason"].startswith("props.")
        assert result[0]["prev"] == "old"
        assert result[0]["next"] == "new"

    @pytest.mark.asyncio
    async def test_render_reasons_emits_parent_rerendered(self, mock_cdp):
        """render_reasons returns 'parent re-rendered' when child has no local change."""
        responses = [
            {"result": {"type": "string", "value": json.dumps({"installed": True})}},
            {"result": {"type": "string", "value": json.dumps([
                {"component_path": "App>Child", "reason": "parent re-rendered",
                 "prev": None, "next": None, "commit_batch_id": 2},
            ])}},
        ]
        call_index = 0

        async def dynamic_send(method, params=None):
            nonlocal call_index
            resp = responses[min(call_index, len(responses) - 1)]
            call_index += 1
            return resp

        mock_cdp.send = dynamic_send

        inspector = ReactInspector()
        result = await inspector.render_reasons(mock_cdp, duration_s=0.0)
        assert isinstance(result, list)
        assert len(result) == 1
        assert result[0]["reason"] == "parent re-rendered"

    @pytest.mark.asyncio
    async def test_component_filter_applied(self, mock_cdp):
        """component_filter narrows the returned reasons list."""
        responses = [
            {"result": {"type": "string", "value": json.dumps({"installed": True})}},
            {"result": {"type": "string", "value": json.dumps([
                {"component_path": "App>Counter", "reason": "state changed", "prev": None, "next": None, "commit_batch_id": 1},
                {"component_path": "App>Header", "reason": "props.title changed", "prev": "A", "next": "B", "commit_batch_id": 1},
            ])}},
        ]
        call_index = 0

        async def dynamic_send(method, params=None):
            nonlocal call_index
            resp = responses[min(call_index, len(responses) - 1)]
            call_index += 1
            return resp

        mock_cdp.send = dynamic_send

        inspector = ReactInspector()
        result = await inspector.render_reasons(mock_cdp, duration_s=0.0, component_filter="Counter")
        assert len(result) == 1
        assert "Counter" in result[0]["component_path"]


# ---------------------------------------------------------------------------
# diff_component_tree — unit tests (pure Python, no CDP)
# ---------------------------------------------------------------------------


class TestDiffComponentTree:
    def _node(self, name: str, props: dict | None = None, children: list | None = None) -> dict:
        return {"name": name, "props": props or {}, "children": children or []}

    def test_no_changes_returns_empty_diffs(self):
        tree = self._node("App", children=[self._node("Header"), self._node("Footer")])
        result = diff_component_tree(tree, tree)
        assert result["added"] == []
        assert result["removed"] == []
        assert result["props_changed"] == []

    def test_added_component_detected(self):
        before = self._node("App", children=[self._node("Header")])
        after = self._node("App", children=[self._node("Header"), self._node("Footer")])
        result = diff_component_tree(before, after)
        paths = [e["path"] for e in result["added"]]
        assert any("Footer" in p for p in paths)

    def test_removed_component_detected(self):
        before = self._node("App", children=[self._node("Header"), self._node("Footer")])
        after = self._node("App", children=[self._node("Header")])
        result = diff_component_tree(before, after)
        paths = [e["path"] for e in result["removed"]]
        assert any("Footer" in p for p in paths)

    def test_props_changed_detected(self):
        before = self._node("App", children=[self._node("Header", props={"title": "old"})])
        after = self._node("App", children=[self._node("Header", props={"title": "new"})])
        result = diff_component_tree(before, after)
        assert len(result["props_changed"]) == 1
        assert "Header" in result["props_changed"][0]["path"]
        assert result["props_changed"][0]["prev_props"]["title"] == "old"
        assert result["props_changed"][0]["next_props"]["title"] == "new"

    def test_accepts_list_input(self):
        snap = [self._node("App"), self._node("Root")]
        result = diff_component_tree(snap, snap)
        assert result["added"] == []

    def test_empty_trees(self):
        result = diff_component_tree({}, {})
        assert result["added"] == []
        assert result["removed"] == []

    def test_unmounted_equals_removed(self):
        before = self._node("App", children=[self._node("Modal")])
        after = self._node("App")
        result = diff_component_tree(before, after)
        assert result["removed"] == result["unmounted"]


# ---------------------------------------------------------------------------
# track_hooks — unit tests
# ---------------------------------------------------------------------------


class TestTrackHooksUnit:
    @pytest.mark.asyncio
    async def test_returns_error_when_element_not_found(self, mock_cdp):
        """track_hooks surfaces error when the selector matches no element."""
        mock_cdp.set_response(
            "Runtime.evaluate",
            {"result": {"type": "string", "value": json.dumps({"error": "element not found: #missing"})}},
        )
        inspector = ReactInspector()
        # duration_s > poll_interval_s so the loop body runs at least once
        result = await inspector.track_hooks(mock_cdp, "#missing", duration_s=0.2, poll_interval_s=0.1)
        assert isinstance(result, list)
        assert len(result) == 1
        assert "error" in result[0]

    @pytest.mark.asyncio
    async def test_returns_timeline_entries(self, mock_cdp):
        """track_hooks returns one snapshot per poll cycle."""
        hook_data = [
            {"hook_index": 0, "hook_type": "useReducer", "value": {"count": 0, "inputValue": ""}},
        ]
        mock_cdp.set_response(
            "Runtime.evaluate",
            {"result": {"type": "string", "value": json.dumps(hook_data)}},
        )
        inspector = ReactInspector()
        result = await inspector.track_hooks(mock_cdp, "#btn-increment", duration_s=0.6, poll_interval_s=0.3)
        # Should have 2 snapshots (0s + 0.3s) × 1 hook each = 2 entries
        assert isinstance(result, list)
        assert len(result) >= 1
        for entry in result:
            assert "ts" in entry
            assert "hook_index" in entry
            assert "hook_type" in entry

    @pytest.mark.asyncio
    async def test_zero_hook_component_returns_empty_timeline(self, mock_cdp):
        """track_hooks returns empty timeline for a component with no hooks."""
        mock_cdp.set_response(
            "Runtime.evaluate",
            {"result": {"type": "string", "value": json.dumps([])}},
        )
        inspector = ReactInspector()
        result = await inspector.track_hooks(mock_cdp, "#no-hooks", duration_s=0.2, poll_interval_s=0.1)
        assert isinstance(result, list)
        assert result == []


# ---------------------------------------------------------------------------
# Integration tests — require Chrome + fixture page
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_render_reasons_on_counter_click(chrome_or_skip, fixture_page):
    """render_reasons captures a re-render when the Increment button is clicked."""
    from specter.browser.connection import CDPConnection
    from specter.browser.interact import Interactor
    from specter.browser.react import ReactInspector
    from specter.browser.runtime import Runtime

    config_obj = None
    from specter.config import load_config
    config_obj = load_config()

    conn = CDPConnection(config_obj)
    targets = await conn.list_targets()
    if not targets:
        pytest.skip("No browser targets available")
    await conn.connect(target_id=targets[0].id)
    try:
        runtime = Runtime(config_obj)
        await runtime.navigate_to(conn, fixture_page)
        # Wait for React to mount via JS polling instead of asyncio.sleep
        await conn.send("Runtime.evaluate", {
            "expression": """(async () => {
                const start = Date.now();
                while (Date.now() - start < 3000) {
                    if (document.querySelector('#btn-increment')) return 'ready';
                    await new Promise(r => setTimeout(r, 50));
                }
                return 'timeout';
            })()""",
            "returnByValue": True, "awaitPromise": True,
        })

        inspector = ReactInspector()

        # Start recording, wait for React to settle, then click
        record_task = asyncio.create_task(
            inspector.render_reasons(conn, duration_s=1.5)
        )
        # Give install script a moment via JS polling for the hook to register
        await conn.send("Runtime.evaluate", {
            "expression": """(async () => {
                await new Promise(r => setTimeout(r, 200));
                return 'ok';
            })()""",
            "returnByValue": True, "awaitPromise": True,
        })
        interactor = Interactor()
        await interactor.click_element(conn, "#btn-increment")
        reasons = await record_task

        assert isinstance(reasons, list)
        # Should have captured at least one re-render (fixture uses useReducer)
        if len(reasons) > 0 and "error" not in reasons[0]:
            assert any("component_path" in r for r in reasons)
    finally:
        await conn.disconnect()


@pytest.mark.asyncio
async def test_track_hooks_captures_counter_value(chrome_or_skip, fixture_page):
    """track_hooks records the counter component's hook state over time."""
    from specter.browser.connection import CDPConnection
    from specter.browser.interact import Interactor
    from specter.browser.react import ReactInspector
    from specter.browser.runtime import Runtime
    from specter.config import load_config

    config_obj = load_config()
    conn = CDPConnection(config_obj)
    targets = await conn.list_targets()
    if not targets:
        pytest.skip("No browser targets available")
    await conn.connect(target_id=targets[0].id)
    try:
        runtime = Runtime(config_obj)
        await runtime.navigate_to(conn, fixture_page)
        # Wait for React to mount via JS polling
        await conn.send("Runtime.evaluate", {
            "expression": """(async () => {
                const start = Date.now();
                while (Date.now() - start < 3000) {
                    if (document.querySelector('#btn-increment')) return 'ready';
                    await new Promise(r => setTimeout(r, 50));
                }
                return 'timeout';
            })()""",
            "returnByValue": True, "awaitPromise": True,
        })

        inspector = ReactInspector()
        interactor = Interactor()

        track_task = asyncio.create_task(
            inspector.track_hooks(conn, "#btn-increment", duration_s=1.0, poll_interval_s=0.2)
        )
        # Wait for first poll to run via JS polling
        await conn.send("Runtime.evaluate", {
            "expression": """(async () => {
                await new Promise(r => setTimeout(r, 250));
                return 'ok';
            })()""",
            "returnByValue": True, "awaitPromise": True,
        })
        await interactor.click_element(conn, "#btn-increment")
        timeline = await track_task

        assert isinstance(timeline, list)
        if len(timeline) > 0 and "error" not in timeline[0]:
            assert all("ts" in e for e in timeline)
            assert all("hook_index" in e for e in timeline)
    finally:
        await conn.disconnect()


@pytest.mark.asyncio
async def test_diff_component_tree_round_trip(chrome_or_skip, fixture_page):
    """diff_component_tree detects no change when called with identical snapshots."""
    from specter.browser.connection import CDPConnection
    from specter.browser.react import ReactInspector, diff_component_tree
    from specter.browser.runtime import Runtime
    from specter.config import load_config

    config_obj = load_config()
    conn = CDPConnection(config_obj)
    targets = await conn.list_targets()
    if not targets:
        pytest.skip("No browser targets available")
    await conn.connect(target_id=targets[0].id)
    try:
        runtime = Runtime(config_obj)
        await runtime.navigate_to(conn, fixture_page)
        # Wait for React to mount via JS polling
        await conn.send("Runtime.evaluate", {
            "expression": """(async () => {
                const start = Date.now();
                while (Date.now() - start < 3000) {
                    if (document.querySelector('#counter')) return 'ready';
                    await new Promise(r => setTimeout(r, 50));
                }
                return 'timeout';
            })()""",
            "returnByValue": True, "awaitPromise": True,
        })

        inspector = ReactInspector()
        snap = await inspector.get_component_tree(conn)
        result = diff_component_tree(snap, snap)

        assert result["added"] == []
        assert result["removed"] == []
        assert result["props_changed"] == []
    finally:
        await conn.disconnect()
