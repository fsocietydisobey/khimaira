"""Tests for specter_record_interaction and specter_replay_interaction (SLICE-C).

Unit tests use MockCDPConnection from conftest — no Chrome required.
Integration test (TestRoundTrip) uses chrome_or_skip + fixture_page and is
auto-skipped when Chrome is not reachable.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest

from specter.browser import is_safe_to_clean
from specter.browser.record_replay import SCHEMA_VERSION, InteractionRecorder

# ---------------------------------------------------------------------------
# Unit tests — record produces correct JSONL
# ---------------------------------------------------------------------------


class TestRecordProducesJsonl:
    """record_interaction writes a valid versioned JSONL file."""

    @pytest.mark.asyncio
    async def test_header_has_required_fields(self, mock_cdp, tmp_path):
        """First JSONL line must be a valid metadata header with version=1."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        mock_cdp.set_default_response({"result": {"type": "string", "value": ""}})

        await recorder.record_interaction(mock_cdp, "hdr-test", duration_s=0.01)

        path = tmp_path / "hdr-test.jsonl"
        assert path.exists()
        header = json.loads(path.read_text().splitlines()[0])
        assert header["version"] == SCHEMA_VERSION
        assert header["label"] == "hdr-test"
        assert "started_ts" in header
        assert "url" in header
        assert "user_agent" in header

    @pytest.mark.asyncio
    async def test_event_fired_during_recording_appears_in_file(
        self, mock_cdp, tmp_path
    ):
        """Events relayed via Runtime.bindingCalled become event lines in the JSONL."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        mock_cdp.set_default_response({"result": {"type": "string", "value": ""}})

        async def fire_click():
            await asyncio.sleep(0.005)
            mock_cdp.fire_event(
                "Runtime.bindingCalled",
                {
                    "name": "_specter_event",
                    "payload": json.dumps(
                        {
                            "kind": "click",
                            "payload": {
                                "selector": "#btn-increment",
                                "x": 10,
                                "y": 20,
                                "label": "Increment",
                            },
                        }
                    ),
                },
            )

        task = asyncio.create_task(fire_click())
        result = await recorder.record_interaction(
            mock_cdp, "event-test", duration_s=0.02
        )
        await task

        assert result["recorded_event_count"] == 1
        lines = (tmp_path / "event-test.jsonl").read_text().splitlines()
        assert len(lines) == 2  # header + 1 event

        event = json.loads(lines[1])
        assert event["kind"] == "click"
        assert event["payload"]["selector"] == "#btn-increment"
        assert "ts_offset_ms" in event

    @pytest.mark.asyncio
    async def test_navigate_event_captured(self, mock_cdp, tmp_path):
        """Page.frameNavigated events appear as navigate events in the JSONL."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        mock_cdp.set_default_response({"result": {"type": "string", "value": ""}})

        async def fire_nav():
            await asyncio.sleep(0.005)
            mock_cdp.fire_event(
                "Page.frameNavigated",
                {"frame": {"url": "http://localhost/new-page"}},
            )

        task = asyncio.create_task(fire_nav())
        result = await recorder.record_interaction(
            mock_cdp, "nav-test", duration_s=0.02
        )
        await task

        assert result["recorded_event_count"] == 1
        event = json.loads((tmp_path / "nav-test.jsonl").read_text().splitlines()[1])
        assert event["kind"] == "navigate"
        assert event["payload"]["url"] == "http://localhost/new-page"

    @pytest.mark.asyncio
    async def test_result_shape(self, mock_cdp, tmp_path):
        """record_interaction return dict has all required fields."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        mock_cdp.set_default_response({"result": {"type": "string", "value": ""}})

        result = await recorder.record_interaction(
            mock_cdp, "shape-test", duration_s=0.01
        )

        assert result["label"] == "shape-test"
        assert result["recorded_event_count"] == 0
        assert "file_path" in result
        assert "duration_s_actual" in result
        assert isinstance(result["duration_s_actual"], float)

    @pytest.mark.asyncio
    async def test_binding_registration_called(self, mock_cdp, tmp_path):
        """record_interaction calls Runtime.addBinding before the recording window."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        mock_cdp.set_default_response({"result": {"type": "string", "value": ""}})

        await recorder.record_interaction(mock_cdp, "binding-test", duration_s=0.01)

        binding_calls = [
            c for c in mock_cdp.calls if c["method"] == "Runtime.addBinding"
        ]
        assert len(binding_calls) == 1
        assert binding_calls[0]["params"]["name"] == "_specter_event"

    @pytest.mark.asyncio
    async def test_binding_removed_after_recording(self, mock_cdp, tmp_path):
        """Runtime.removeBinding is called after the duration expires."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        mock_cdp.set_default_response({"result": {"type": "string", "value": ""}})

        await recorder.record_interaction(mock_cdp, "rm-binding-test", duration_s=0.01)

        remove_calls = [
            c for c in mock_cdp.calls if c["method"] == "Runtime.removeBinding"
        ]
        assert len(remove_calls) == 1
        assert remove_calls[0]["params"]["name"] == "_specter_event"


# ---------------------------------------------------------------------------
# Unit tests — replay parses JSONL and dispatches CDP correctly
# ---------------------------------------------------------------------------


class TestReplayParsesJsonl:
    """replay_interaction reads JSONL and dispatches the right CDP messages."""

    def _write_recording(self, path: Path, metadata: dict, events: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            f.write(json.dumps(metadata) + "\n")
            for e in events:
                f.write(json.dumps(e) + "\n")

    def _make_metadata(self, label: str = "test") -> dict:
        return {
            "version": SCHEMA_VERSION,
            "label": label,
            "started_ts": time.time(),
            "url": "http://localhost/",
            "user_agent": "test-agent",
        }

    @pytest.mark.asyncio
    async def test_replay_click_sends_runtime_evaluate(self, mock_cdp, tmp_path):
        """Replaying a click event issues a Runtime.evaluate CDP call."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        events = [
            {
                "ts_offset_ms": 0,
                "kind": "click",
                "payload": {
                    "selector": "#btn-increment",
                    "x": 10,
                    "y": 20,
                    "label": "Inc",
                },
            }
        ]
        self._write_recording(
            tmp_path / "click-test.jsonl", self._make_metadata("click-test"), events
        )

        mock_cdp.set_default_response(
            {
                "result": {
                    "type": "string",
                    "value": json.dumps(
                        {"clicked": True, "label": "Inc", "tag": "BUTTON"}
                    ),
                }
            }
        )

        result = await recorder.replay_interaction(mock_cdp, "click-test")

        assert result["replayed_event_count"] == 1
        evaluate_calls = [
            c for c in mock_cdp.calls if c["method"] == "Runtime.evaluate"
        ]
        assert len(evaluate_calls) >= 1

    @pytest.mark.asyncio
    async def test_replay_unknown_version_returns_error(self, mock_cdp, tmp_path):
        """Recordings with an unrecognised version field return a structured error."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        metadata = {**self._make_metadata("ver-test"), "version": 99}
        self._write_recording(tmp_path / "ver-test.jsonl", metadata, [])

        result = await recorder.replay_interaction(mock_cdp, "ver-test")

        assert "error" in result
        assert "99" in result["error"] or "version" in result["error"].lower()

    @pytest.mark.asyncio
    async def test_replay_missing_file_returns_error(self, mock_cdp, tmp_path):
        """Replaying a label with no matching file returns a structured error."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)

        result = await recorder.replay_interaction(mock_cdp, "does-not-exist")

        assert "error" in result
        assert "does-not-exist" in result["error"]

    @pytest.mark.asyncio
    async def test_replay_empty_recording_is_noop(self, mock_cdp, tmp_path):
        """A header-only recording (no events) replays 0 events without error."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        self._write_recording(
            tmp_path / "empty.jsonl", self._make_metadata("empty"), []
        )
        mock_cdp.set_default_response({"result": {"type": "string", "value": ""}})

        result = await recorder.replay_interaction(mock_cdp, "empty")

        assert result["replayed_event_count"] == 0
        assert "end_state_snapshot" in result

    @pytest.mark.asyncio
    async def test_replay_multiple_events_dispatches_all(self, mock_cdp, tmp_path):
        """Each event in the recording produces a CDP interaction call."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        events = [
            {
                "ts_offset_ms": 0,
                "kind": "click",
                "payload": {"selector": "#btn-increment", "x": 0, "y": 0, "label": ""},
            },
            {
                "ts_offset_ms": 10,
                "kind": "click",
                "payload": {"selector": "#btn-increment", "x": 0, "y": 0, "label": ""},
            },
            {
                "ts_offset_ms": 20,
                "kind": "click",
                "payload": {"selector": "#btn-increment", "x": 0, "y": 0, "label": ""},
            },
        ]
        self._write_recording(
            tmp_path / "multi.jsonl", self._make_metadata("multi"), events
        )

        click_response = json.dumps({"clicked": True, "label": "Inc", "tag": "BUTTON"})
        mock_cdp.set_default_response(
            {"result": {"type": "string", "value": click_response}}
        )

        result = await recorder.replay_interaction(mock_cdp, "multi")

        assert result["replayed_event_count"] == 3

    @pytest.mark.asyncio
    async def test_replay_end_state_snapshot_has_url_and_title(
        self, mock_cdp, tmp_path
    ):
        """end_state_snapshot always contains url and title keys."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        self._write_recording(tmp_path / "snap.jsonl", self._make_metadata("snap"), [])
        mock_cdp.set_default_response(
            {"result": {"type": "string", "value": "My Page Title"}}
        )

        result = await recorder.replay_interaction(mock_cdp, "snap")

        assert "url" in result["end_state_snapshot"]
        assert "title" in result["end_state_snapshot"]


# ---------------------------------------------------------------------------
# Security tests — label path traversal validation
# ---------------------------------------------------------------------------


class TestLabelValidation:
    """record_interaction and replay_interaction reject labels with path components."""

    @pytest.mark.asyncio
    async def test_record_rejects_traversal_label(self, mock_cdp, tmp_path):
        """label='../../foo' → structured error, no file written."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        result = await recorder.record_interaction(
            mock_cdp, "../../foo", duration_s=0.01
        )

        assert "error" in result
        assert result["label"] == "../../foo"
        # No file should have been written outside the recordings dir
        assert not list(tmp_path.glob("*.jsonl"))

    @pytest.mark.asyncio
    async def test_replay_rejects_traversal_label(self, mock_cdp, tmp_path):
        """Replaying label='../../foo' → structured error without touching disk."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        result = await recorder.replay_interaction(mock_cdp, "../../foo")

        assert "error" in result
        assert result["label"] == "../../foo"

    @pytest.mark.asyncio
    async def test_record_rejects_label_with_dot(self, mock_cdp, tmp_path):
        """label='foo.bar' contains a dot → rejected."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        result = await recorder.record_interaction(mock_cdp, "foo.bar", duration_s=0.01)
        assert "error" in result

    @pytest.mark.asyncio
    async def test_record_accepts_valid_label(self, mock_cdp, tmp_path):
        """label='my-recording_01' is valid and proceeds normally."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        mock_cdp.set_default_response({"result": {"type": "string", "value": ""}})
        result = await recorder.record_interaction(
            mock_cdp, "my-recording_01", duration_s=0.01
        )
        assert "error" not in result
        assert result["label"] == "my-recording_01"


# ---------------------------------------------------------------------------
# Replay event-kind dispatch tests (unit)
# ---------------------------------------------------------------------------


class TestReplayEventKinds:
    """Each event kind triggers the correct CDP method during replay."""

    def _write_recording(self, path: Path, metadata: dict, events: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            f.write(json.dumps(metadata) + "\n")
            for e in events:
                f.write(json.dumps(e) + "\n")

    def _make_metadata(self, label: str = "test") -> dict:
        return {
            "version": SCHEMA_VERSION,
            "label": label,
            "started_ts": time.time(),
            "url": "http://localhost/",
            "user_agent": "test-agent",
        }

    @pytest.mark.asyncio
    async def test_replay_keydown_sends_input_dispatchkeyevent(
        self, mock_cdp, tmp_path
    ):
        """Replaying a keydown event calls Input.dispatchKeyEvent via CDP."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        events = [
            {
                "ts_offset_ms": 0,
                "kind": "keydown",
                "payload": {"key": "Enter", "selector": "body"},
            }
        ]
        self._write_recording(tmp_path / "kd.jsonl", self._make_metadata("kd"), events)
        mock_cdp.set_default_response({"result": {"type": "undefined"}})

        result = await recorder.replay_interaction(mock_cdp, "kd")

        assert result["replayed_event_count"] == 1
        key_calls = [
            c for c in mock_cdp.calls if c["method"] == "Input.dispatchKeyEvent"
        ]
        assert len(key_calls) >= 1
        assert any(c["params"].get("key") == "Enter" for c in key_calls)

    @pytest.mark.asyncio
    async def test_replay_input_change_sends_runtime_evaluate(self, mock_cdp, tmp_path):
        """Replaying an input_change event calls Runtime.evaluate (fill_input)."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        events = [
            {
                "ts_offset_ms": 0,
                "kind": "input_change",
                "payload": {"selector": "#text-input", "value": "hello"},
            }
        ]
        self._write_recording(tmp_path / "ic.jsonl", self._make_metadata("ic"), events)
        fill_response = json.dumps(
            {"filled": True, "selector": "#text-input", "value": "hello"}
        )
        mock_cdp.set_default_response(
            {"result": {"type": "string", "value": fill_response}}
        )

        result = await recorder.replay_interaction(mock_cdp, "ic")

        assert result["replayed_event_count"] == 1
        evaluate_calls = [
            c for c in mock_cdp.calls if c["method"] == "Runtime.evaluate"
        ]
        assert len(evaluate_calls) >= 1

    @pytest.mark.asyncio
    async def test_replay_select_change_sends_runtime_evaluate(
        self, mock_cdp, tmp_path
    ):
        """Replaying a select_change event calls Runtime.evaluate (select_option)."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        events = [
            {
                "ts_offset_ms": 0,
                "kind": "select_change",
                "payload": {"selector": "#my-select", "value": "opt2"},
            }
        ]
        self._write_recording(tmp_path / "sc.jsonl", self._make_metadata("sc"), events)
        select_response = json.dumps(
            {"selected": True, "value": "opt2", "text": "Option 2"}
        )
        mock_cdp.set_default_response(
            {"result": {"type": "string", "value": select_response}}
        )

        result = await recorder.replay_interaction(mock_cdp, "sc")

        assert result["replayed_event_count"] == 1
        evaluate_calls = [
            c for c in mock_cdp.calls if c["method"] == "Runtime.evaluate"
        ]
        assert len(evaluate_calls) >= 1

    @pytest.mark.asyncio
    async def test_replay_navigate_sends_page_navigate(self, mock_cdp, tmp_path):
        """Replaying a navigate event calls Page.navigate with the recorded URL."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        events = [
            {
                "ts_offset_ms": 0,
                "kind": "navigate",
                "payload": {"url": "http://localhost/new"},
            }
        ]
        self._write_recording(
            tmp_path / "nav.jsonl", self._make_metadata("nav"), events
        )
        mock_cdp.set_default_response({"result": {"type": "undefined"}})

        result = await recorder.replay_interaction(mock_cdp, "nav")

        assert result["replayed_event_count"] == 1
        nav_calls = [c for c in mock_cdp.calls if c["method"] == "Page.navigate"]
        assert len(nav_calls) >= 1
        assert nav_calls[0]["params"]["url"] == "http://localhost/new"


# ---------------------------------------------------------------------------
# Replay error handling and malformed input
# ---------------------------------------------------------------------------


class TestReplayEdgeCases:
    """Replay is robust against CDP failures and malformed JSONL."""

    def _write_recording(
        self, path: Path, metadata: dict, raw_lines: list[str]
    ) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w") as f:
            f.write(json.dumps(metadata) + "\n")
            for line in raw_lines:
                f.write(line + "\n")

    def _make_metadata(self, label: str = "test") -> dict:
        return {
            "version": SCHEMA_VERSION,
            "label": label,
            "started_ts": time.time(),
            "url": "http://localhost/",
            "user_agent": "test-agent",
        }

    @pytest.mark.asyncio
    async def test_replay_handles_event_failure(self, mock_cdp, tmp_path):
        """When a CDP call raises during replay, a structured error is returned."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        events_json = [
            json.dumps(
                {
                    "ts_offset_ms": 0,
                    "kind": "click",
                    "payload": {"selector": "#missing"},
                }
            ),
        ]
        self._write_recording(
            tmp_path / "fail.jsonl", self._make_metadata("fail"), events_json
        )

        # Make Runtime.evaluate raise to simulate element-not-found failure
        original_send = mock_cdp.send

        async def _failing_send(method, params=None):
            if method == "Runtime.evaluate":
                raise RuntimeError("CDP: element not found: #missing")
            return await original_send(method, params)

        mock_cdp.send = _failing_send

        result = await recorder.replay_interaction(mock_cdp, "fail")

        assert "error" in result
        assert result["failed_event_index"] == 0
        assert result["failed_event_kind"] == "click"
        assert result["replayed_event_count"] == 0

    @pytest.mark.asyncio
    async def test_replay_skips_malformed_event_lines(self, mock_cdp, tmp_path):
        """Malformed JSONL event lines are silently skipped; valid events replay."""
        recorder = InteractionRecorder(recordings_dir=tmp_path)
        valid_event = json.dumps(
            {
                "ts_offset_ms": 0,
                "kind": "click",
                "payload": {"selector": "#btn", "x": 0, "y": 0, "label": ""},
            }
        )
        lines = [
            valid_event,  # line 1: valid
            "NOT_VALID_JSON{{",  # line 2: malformed — should be skipped
            valid_event,  # line 3: valid
        ]
        self._write_recording(tmp_path / "mal.jsonl", self._make_metadata("mal"), lines)

        click_response = json.dumps({"clicked": True, "label": "", "tag": "BUTTON"})
        mock_cdp.set_default_response(
            {"result": {"type": "string", "value": click_response}}
        )

        result = await recorder.replay_interaction(mock_cdp, "mal")

        # 2 valid events replayed; malformed line skipped silently
        assert result["replayed_event_count"] == 2


# ---------------------------------------------------------------------------
# URL safety helper tests
# ---------------------------------------------------------------------------


class TestIsSafeToClean:
    """is_safe_to_clean() protects active user workspace URLs."""

    def test_localhost_3000_is_unsafe(self):
        assert is_safe_to_clean("http://localhost:3000/shop") is False

    def test_localhost_3000_root_is_unsafe(self):
        assert is_safe_to_clean("http://localhost:3000") is False

    def test_127_0_0_1_3000_is_unsafe(self):
        assert is_safe_to_clean("http://127.0.0.1:3000/anything") is False

    def test_jeevy_url_is_unsafe(self):
        assert is_safe_to_clean("https://jeevy.local") is False

    def test_jeevy_in_path_is_unsafe(self):
        assert is_safe_to_clean("http://localhost:8080/jeevy/api") is False

    def test_about_blank_is_safe(self):
        assert is_safe_to_clean("about:blank") is True

    def test_file_url_is_safe(self):
        assert is_safe_to_clean("file:///tmp/fixture.html") is True

    def test_empty_string_is_safe(self):
        assert is_safe_to_clean("") is True

    def test_localhost_other_port_is_safe(self):
        assert is_safe_to_clean("http://localhost:8740/api/health") is True

    def test_127_0_0_1_other_port_is_safe(self):
        assert is_safe_to_clean("http://127.0.0.1:52323/fixture.html") is True


# ---------------------------------------------------------------------------
# Integration test — record → replay round-trip (Chrome required)
# ---------------------------------------------------------------------------


class TestRoundTrip:
    """Full record → replay cycle against the fixture_page React SPA.

    Verifies:
      1. record_interaction captures 3 Increment clicks
      2. reset_button brings counter back to 0
      3. replay_interaction re-clicks 3 times
      4. Counter DOM shows "3" after replay (no asyncio.sleep — JS polling)
    """

    @pytest.mark.asyncio
    async def test_record_replay_counter_round_trip(self, chrome_or_skip, fixture_page):
        import asyncio

        from specter.browser.connection import CDPConnection
        from specter.browser.interact import Interactor
        from specter.config import load_config

        config = load_config()
        conn = CDPConnection(config)
        await conn.connect()
        recorder = InteractionRecorder()
        label = "round-trip-test-rc"

        try:
            # Navigate to the React fixture page
            await conn.send("Page.enable", {})
            await conn.send("Page.navigate", {"url": fixture_page})

            # Wait for React to mount — poll until #btn-increment is in the DOM
            interactor = Interactor()
            found = await interactor.wait_for_element(
                conn, "#btn-increment", timeout_ms=5000
            )
            assert found.get("found"), "fixture did not mount within 5s"

            # Record 3 Increment clicks in a background task while recording runs
            async def _click_three_times():
                await asyncio.sleep(0.05)
                for _ in range(3):
                    await interactor.click_element(conn, "#btn-increment")
                    await asyncio.sleep(0.05)

            task = asyncio.create_task(_click_three_times())
            record_result = await recorder.record_interaction(
                conn, label, duration_s=0.5
            )
            await task

            assert (
                record_result["recorded_event_count"] >= 3
            ), f"Expected ≥3 events, got {record_result['recorded_event_count']}"

            # Reset counter to 0
            await interactor.click_element(conn, "#btn-reset")

            # Wait for counter to show 0 via in-browser JS polling (no asyncio.sleep)
            reset_check = await conn.send(
                "Runtime.evaluate",
                {
                    "expression": """(async () => {
                        const start = Date.now();
                        while (Date.now() - start < 2000) {
                            const el = document.querySelector('#counter');
                            if (el && el.textContent.includes('0') && !el.textContent.includes('10')) return 'ok';
                            await new Promise(r => setTimeout(r, 50));
                        }
                        return document.querySelector('#counter')?.textContent || 'timeout';
                    })()""",
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            assert (
                reset_check.get("result", {}).get("value") == "ok"
            ), "Counter did not reset to 0"

            # Replay
            replay_result = await recorder.replay_interaction(conn, label)
            assert (
                replay_result["replayed_event_count"] >= 3
            ), f"Expected ≥3 replayed events, got {replay_result['replayed_event_count']}"

            # Wait for counter to reach 3 via in-browser JS polling (no asyncio.sleep)
            count_check = await conn.send(
                "Runtime.evaluate",
                {
                    "expression": """(async () => {
                        const start = Date.now();
                        while (Date.now() - start < 3000) {
                            const el = document.querySelector('#counter');
                            if (el && el.textContent.includes('3')) return el.textContent.trim();
                            await new Promise(r => setTimeout(r, 100));
                        }
                        return document.querySelector('#counter')?.textContent || 'timeout';
                    })()""",
                    "returnByValue": True,
                    "awaitPromise": True,
                },
            )
            counter_text = count_check.get("result", {}).get("value", "")
            assert (
                "3" in counter_text
            ), f"Counter should show 3 after replay, got: {counter_text!r}"

        finally:
            # NOTE: do NOT navigate away — we don't know if the user has a real
            # app open in this tab. State bleed between tests is acceptable given
            # the risk of clobbering work in progress. Each integration test should
            # navigate to its own fixture_page at the start instead.

            # Clean up recording file
            path = recorder._recording_path(label)
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    pass
            try:
                await conn.disconnect()
            except Exception:
                pass
