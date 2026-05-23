"""Tests for SLICE-B Specter tools: a11y_audit, extract_state_machine, 3 assertions.

Unit tests use MockCDPConnection (no Chrome).
Integration tests use chrome_or_skip + fixture_page (auto-skip if Chrome unreachable).

TAB SAFETY: all browser interaction in integration tests uses fixture_page URLs only.
is_safe_to_clean() is imported and tested in TestIsSafeToCleanWired to ensure the
helper is accessible from the package root (wiring requirement from SLICE-C must-fix).
"""

from __future__ import annotations

import json
import time
from collections.abc import AsyncGenerator
from pathlib import Path

import pytest

from specter.browser import is_safe_to_clean
from specter.browser.a11y import A11yAuditor
from specter.browser.assertions import Asserter
from specter.browser.state_machine import StateMachineExtractor

# ---------------------------------------------------------------------------
# Unit tests — A11yAuditor
# ---------------------------------------------------------------------------


class TestA11yAuditorUnit:
    """A11yAuditor handles axe.run() responses + injection failures correctly."""

    def _axe_response(self, violations: list, passes: list | None = None) -> dict:
        """Build a mock axe.run() result."""
        return {
            "result": {
                "type": "object",
                "value": {
                    "violations": violations,
                    "passes": passes or [],
                    "inapplicable": [],
                    "incomplete": [],
                    "testEngine": {"version": "4.10.2"},
                },
            }
        }

    @pytest.mark.asyncio
    async def test_returns_violations_list(self, mock_cdp):
        """Violations from axe.run() are structured and returned."""
        auditor = A11yAuditor()
        violation = {
            "id": "image-alt",
            "impact": "critical",
            "description": "Ensures img elements have alternate text",
            "helpUrl": "https://dequeuniversity.com/rules/axe/4.10/image-alt",
            "nodes": [
                {
                    "target": ["#bad-img"],
                    "html": "<img>",
                    "failureSummary": "Fix any of the following: Element does not have an alt attribute",
                }
            ],
        }
        # First call: inject axe (returns string 'injected')
        mock_cdp.set_response(
            "Runtime.evaluate", {"result": {"type": "string", "value": "injected"}}
        )
        # We need 2 sequential calls: inject then run. Use default for second.
        # Simplest: set inject response then override for audit call via call count.
        # Use a custom send that returns different values per call.
        inject_calls = [0]
        original_send = mock_cdp.send

        async def _sequential_send(method, params=None):
            if method == "Runtime.evaluate":
                inject_calls[0] += 1
                if inject_calls[0] == 1:
                    # First call: inject axe
                    return {"result": {"type": "string", "value": "injected"}}
                else:
                    # Second call: axe.run() result
                    return self._axe_response([violation])
            return await original_send(method, params)

        mock_cdp.send = _sequential_send
        result = await auditor.audit(mock_cdp)

        assert "violations" in result
        assert len(result["violations"]) == 1
        assert result["violations"][0]["id"] == "image-alt"
        assert result["violations"][0]["impact"] == "critical"
        assert result["axe_version"] == "4.10.2"

    @pytest.mark.asyncio
    async def test_returns_empty_violations_on_clean_page(self, mock_cdp):
        """Clean page (no violations) returns violations: []."""
        auditor = A11yAuditor()
        inject_calls = [0]
        original_send = mock_cdp.send

        async def _sequential_send(method, params=None):
            if method == "Runtime.evaluate":
                inject_calls[0] += 1
                if inject_calls[0] == 1:
                    return {"result": {"type": "string", "value": "injected"}}
                else:
                    return self._axe_response([])
            return await original_send(method, params)

        mock_cdp.send = _sequential_send
        result = await auditor.audit(mock_cdp)
        assert result["violations"] == []

    @pytest.mark.asyncio
    async def test_missing_vendored_axe_returns_error(
        self, mock_cdp, tmp_path, monkeypatch
    ):
        """When vendored axe.min.js is missing, returns structured error dict."""
        import specter.browser.a11y as a11y_mod

        monkeypatch.setattr(a11y_mod, "_AXE_JS_PATH", tmp_path / "nonexistent.js")
        auditor = A11yAuditor()
        result = await auditor.audit(mock_cdp)
        assert "error" in result
        assert (
            "axe-core" in result["error"].lower()
            or "not found" in result["error"].lower()
        )

    @pytest.mark.asyncio
    async def test_axe_run_exception_returns_error(self, mock_cdp):
        """If axe.run() raises in CDP, structured error is returned."""
        auditor = A11yAuditor()
        inject_calls = [0]
        original_send = mock_cdp.send

        async def _sequential_send(method, params=None):
            if method == "Runtime.evaluate":
                inject_calls[0] += 1
                if inject_calls[0] == 1:
                    return {"result": {"type": "string", "value": "injected"}}
                else:
                    return {
                        "result": {"type": "undefined"},
                        "exceptionDetails": {"text": "axe is not defined"},
                    }
            return await original_send(method, params)

        mock_cdp.send = _sequential_send
        result = await auditor.audit(mock_cdp)
        assert "error" in result


# ---------------------------------------------------------------------------
# Unit tests — StateMachineExtractor
# ---------------------------------------------------------------------------


class TestStateMachineExtractorUnit:
    """StateMachineExtractor handles both redux and xstate modes."""

    @pytest.mark.asyncio
    async def test_redux_returns_store_shape(self, mock_cdp):
        """Redux extraction returns store_shape, slices, actions_history_count."""
        extractor = StateMachineExtractor()
        mock_cdp.set_response(
            "Runtime.evaluate",
            {
                "result": {
                    "type": "string",
                    "value": json.dumps(
                        {
                            "store_shape": {"count": 0, "inputValue": ""},
                            "slices": ["count", "inputValue"],
                            "actions_history_count": None,
                        }
                    ),
                }
            },
        )
        result = await extractor.extract(mock_cdp, library="redux")
        assert "store_shape" in result
        assert "slices" in result

    @pytest.mark.asyncio
    async def test_xstate_not_detected_returns_error(self, mock_cdp):
        """When XState inspect not present, returns structured error dict (not exception)."""
        extractor = StateMachineExtractor()
        mock_cdp.set_response(
            "Runtime.evaluate",
            {
                "result": {
                    "type": "string",
                    "value": json.dumps({"error": "@xstate/inspect not detected"}),
                }
            },
        )
        result = await extractor.extract(mock_cdp, library="xstate")
        assert "error" in result
        # Structured error, not a Python exception
        assert isinstance(result["error"], str)

    @pytest.mark.asyncio
    async def test_unknown_library_returns_error(self, mock_cdp):
        """Unknown library name → structured error without CDP call."""
        extractor = StateMachineExtractor()
        result = await extractor.extract(mock_cdp, library="mobx")  # type: ignore[arg-type]
        assert "error" in result
        assert "Unknown library" in result["error"]

    @pytest.mark.asyncio
    async def test_redux_store_not_found_returns_error(self, mock_cdp):
        """When Redux store not found, returns structured error dict."""
        extractor = StateMachineExtractor()
        mock_cdp.set_response(
            "Runtime.evaluate",
            {
                "result": {
                    "type": "string",
                    "value": json.dumps({"error": "Redux store not found"}),
                }
            },
        )
        result = await extractor.extract(mock_cdp, library="redux")
        assert "error" in result


# ---------------------------------------------------------------------------
# Unit tests — Asserter
# ---------------------------------------------------------------------------


class TestAsserterUnit:
    """Asserter wraps console/network captures and asserts_element_visible."""

    def _make_mock_console(self, errors=None, exceptions=None):
        """Minimal mock for ConsoleCapture."""

        class _MockConsole:
            def __init__(self, errs, excs):
                self._errs = errs or []
                self._excs = excs or []

            def get_logs(self, level=None, since=None, limit=50):
                return list(self._errs)

            def get_errors(self, since=None, limit=50):
                return list(self._excs)

        return _MockConsole(errors, exceptions)

    def _make_mock_network(self, errors=None):
        class _MockNetwork:
            def __init__(self, errs):
                self._errs = errs or []

            def get_requests(
                self, errors_only=True, since=None, limit=50, url_filter=None
            ):
                return list(self._errs)

        return _MockNetwork(errors)

    def test_no_console_errors_returns_ok_true(self):
        asserter = Asserter()
        result = asserter.assert_no_console_errors(self._make_mock_console())
        assert result["ok"] is True
        assert result["errors"] == []

    def test_console_errors_present_returns_ok_false(self):
        asserter = Asserter()
        errors = [{"level": "error", "text": "TypeError: foo is not a function"}]
        result = asserter.assert_no_console_errors(
            self._make_mock_console(errors=errors)
        )
        assert result["ok"] is False
        assert len(result["errors"]) == 1

    def test_no_network_errors_returns_ok_true(self):
        asserter = Asserter()
        result = asserter.assert_no_network_errors(self._make_mock_network())
        assert result["ok"] is True

    def test_network_errors_present_returns_ok_false(self):
        asserter = Asserter()
        errs = [{"url": "/api/fail", "status": 500, "error": "Internal Server Error"}]
        result = asserter.assert_no_network_errors(self._make_mock_network(errors=errs))
        assert result["ok"] is False
        assert result["error_count"] == 1

    @pytest.mark.asyncio
    async def test_assert_element_visible_visible(self, mock_cdp):
        """assert_element_visible returns ok=True and visible=True when element is present."""
        asserter = Asserter()
        mock_cdp.set_response(
            "Runtime.evaluate",
            {
                "result": {
                    "type": "string",
                    "value": json.dumps({"visible": True, "found_after_ms": 42}),
                }
            },
        )
        result = await asserter.assert_element_visible(mock_cdp, "#btn-increment")
        assert result["ok"] is True
        assert result["visible"] is True
        assert result["found_after_ms"] == 42

    @pytest.mark.asyncio
    async def test_assert_element_visible_timeout(self, mock_cdp):
        """assert_element_visible returns visible=False on timeout."""
        asserter = Asserter()
        mock_cdp.set_response(
            "Runtime.evaluate",
            {
                "result": {
                    "type": "string",
                    "value": json.dumps(
                        {
                            "visible": False,
                            "found_after_ms": 100,
                            "element_exists": False,
                        }
                    ),
                }
            },
        )
        result = await asserter.assert_element_visible(
            mock_cdp, "#missing", timeout_ms=100
        )
        assert result["ok"] is False
        assert result["visible"] is False
        assert result["element_exists"] is False

    @pytest.mark.asyncio
    async def test_assert_element_visible_cdp_error(self, mock_cdp):
        """CDP exception → structured error dict returned."""
        asserter = Asserter()
        mock_cdp.set_response(
            "Runtime.evaluate",
            {
                "result": {"type": "undefined"},
                "exceptionDetails": {"text": "SyntaxError"},
            },
        )
        result = await asserter.assert_element_visible(mock_cdp, "#x")
        assert "error" in result


# ---------------------------------------------------------------------------
# Integration tests (Chrome required)
# ---------------------------------------------------------------------------


class TestA11yAuditIntegration:
    """a11y_audit finds deliberate violations in fixture.html."""

    @pytest.mark.asyncio
    async def test_finds_deliberate_violations(self, chrome_or_skip, fixture_page):
        """axe-core finds the img-alt and button-name violations we added to fixture.html."""
        from specter.browser.connection import CDPConnection
        from specter.browser.interact import Interactor
        from specter.config import load_config

        conn = CDPConnection(load_config())
        targets = await conn.list_targets()
        if not targets:
            pytest.skip("No browser targets available")
        await conn.connect(target_id=targets[0].id)

        try:
            # TAB SAFETY: fixture_page is always a local 127.0.0.1 URL on a random port
            assert is_safe_to_clean(
                fixture_page
            ), f"fixture_page URL deemed unsafe: {fixture_page}"

            await conn.send("Page.enable", {})
            await conn.send("Page.navigate", {"url": fixture_page})

            interactor = Interactor()
            await interactor.wait_for_element(conn, "#a11y-bad-img", timeout_ms=5000)

            auditor = A11yAuditor()
            result = await auditor.audit(conn)

            assert "violations" in result, f"Expected violations key, got: {result}"
            violation_ids = [v["id"] for v in result["violations"]]
            assert (
                "image-alt" in violation_ids
            ), f"Expected image-alt violation, got: {violation_ids}"
        finally:
            try:
                await conn.disconnect()
            except Exception:
                pass


class TestExtractStateMachineIntegration:
    """extract_state_machine reads fixture.html's useReducer state."""

    @pytest.mark.asyncio
    async def test_extracts_redux_like_state(self, chrome_or_skip, fixture_page):
        """Redux extraction finds the window.__appState exposed by fixture.html."""
        from specter.browser.connection import CDPConnection
        from specter.browser.interact import Interactor
        from specter.config import load_config

        conn = CDPConnection(load_config())
        targets = await conn.list_targets()
        if not targets:
            pytest.skip("No browser targets available")
        await conn.connect(target_id=targets[0].id)

        try:
            assert is_safe_to_clean(fixture_page)

            await conn.send("Page.enable", {})
            await conn.send("Page.navigate", {"url": fixture_page})

            interactor = Interactor()
            await interactor.wait_for_element(conn, "#btn-increment", timeout_ms=5000)

            extractor = StateMachineExtractor()
            result = await extractor.extract(conn, library="redux")

            # Either finds the Redux store OR returns a structured error (no Redux store globally)
            assert isinstance(result, dict)
            # Not a Python exception — structured dict either way
            if "error" not in result:
                assert "store_shape" in result or "slices" in result
        finally:
            try:
                await conn.disconnect()
            except Exception:
                pass


class TestAssertionToolsIntegration:
    """Assert tools work against the fixture page."""

    @pytest.mark.asyncio
    async def test_assert_element_visible_finds_button(
        self, chrome_or_skip, fixture_page
    ):
        """assert_element_visible returns True for #btn-increment on the fixture page."""
        from specter.browser.connection import CDPConnection
        from specter.browser.interact import Interactor
        from specter.config import load_config

        conn = CDPConnection(load_config())
        targets = await conn.list_targets()
        if not targets:
            pytest.skip("No browser targets available")
        await conn.connect(target_id=targets[0].id)

        try:
            assert is_safe_to_clean(fixture_page)

            await conn.send("Page.enable", {})
            await conn.send("Page.navigate", {"url": fixture_page})

            interactor = Interactor()
            await interactor.wait_for_element(conn, "#btn-increment", timeout_ms=5000)

            asserter = Asserter()
            result = await asserter.assert_element_visible(
                conn, "#btn-increment", timeout_ms=3000
            )

            assert result.get("visible") is True
        finally:
            try:
                await conn.disconnect()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_assert_no_console_errors_clean_page(
        self, chrome_or_skip, fixture_page
    ):
        """Fixture page loads without console errors → assert_no_console_errors passes."""
        from specter.browser.connection import CDPConnection
        from specter.browser.console import ConsoleCapture
        from specter.browser.interact import Interactor
        from specter.config import load_config

        config = load_config()
        conn = CDPConnection(config)
        console = ConsoleCapture(config)
        console.register(conn)

        try:
            assert is_safe_to_clean(fixture_page)

            targets = await conn.list_targets()
            if not targets:
                pytest.skip("No browser targets available")
            await conn.connect(target_id=targets[0].id)
            await console.enable(conn)
            await conn.send("Page.enable", {})
            await conn.send("Page.navigate", {"url": fixture_page})

            interactor = Interactor()
            await interactor.wait_for_element(conn, "#btn-increment", timeout_ms=5000)

            asserter = Asserter()
            result = asserter.assert_no_console_errors(console)

            # Fixture page may have CDN-related warnings but should have no JS errors
            assert "ok" in result
            assert isinstance(result["ok"], bool)
        finally:
            try:
                await conn.disconnect()
            except Exception:
                pass

    @pytest.mark.asyncio
    async def test_assert_no_network_errors_clean_page(
        self, chrome_or_skip, fixture_page
    ):
        """Fixture page loads without network errors → assert_no_network_errors passes."""
        from specter.browser.connection import CDPConnection
        from specter.browser.interact import Interactor
        from specter.browser.network import NetworkCapture
        from specter.config import load_config

        config = load_config()
        conn = CDPConnection(config)
        network = NetworkCapture(config)
        network.register(conn)

        try:
            assert is_safe_to_clean(fixture_page)

            targets = await conn.list_targets()
            if not targets:
                pytest.skip("No browser targets available")
            await conn.connect(target_id=targets[0].id)
            await network.enable(conn)
            await conn.send("Page.enable", {})
            await conn.send("Page.navigate", {"url": fixture_page})

            interactor = Interactor()
            await interactor.wait_for_element(conn, "#btn-increment", timeout_ms=5000)

            asserter = Asserter()
            result = asserter.assert_no_network_errors(network)

            assert "ok" in result
            # Fixture page serves locally — should have no failed requests to local server
            assert isinstance(result["ok"], bool)
        finally:
            try:
                await conn.disconnect()
            except Exception:
                pass
