"""Record and replay user interaction sessions via CDP.

Provides two tools:
  record_interaction  — listen to DOM events for N seconds, write to JSONL
  replay_interaction  — read JSONL and re-issue each event in order via CDP

Recording format (one JSON object per line):
  Line 0  — header: {version, label, started_ts, url, user_agent}
  Line 1+ — events: {ts_offset_ms, kind, payload}

Determinism limits (document in tool docstrings, here for reference):
  - Animations / CSS transitions: replay may be visually different if
    any UI waits for an animation frame before responding to input.
  - Debounced inputs: if an app debounces keystroke handlers, replay may
    fire change events on a different schedule than the recording.
  - Date.now() / Math.random(): any JS that reads wall-clock time or
    random numbers will produce different values on replay.
  - Network latency: if an interaction waits for an API response before
    proceeding, replay timing may diverge on a slower/faster network.
  Use record/replay for deterministic flows (counter clicks, form fills
  against static data) — not for timing-sensitive or randomized UIs.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from pathlib import Path
from typing import TYPE_CHECKING

from specter.browser.interact import Interactor

if TYPE_CHECKING:
    from specter.browser.connection import CDPConnection

logger = logging.getLogger(__name__)

_DEFAULT_RECORDINGS_DIR = (
    Path.home() / ".local" / "state" / "khimaira" / "specter" / "recordings"
)

SCHEMA_VERSION = 1

# Labels must be safe filename components: letters, digits, underscores, hyphens only.
# This prevents path traversal attacks (e.g. label="../../etc/passwd").
_LABEL_RE = re.compile(r"^[\w\-]+$")

# JavaScript injected at record start. Listens to DOM events and relays them
# to Python via window._specter_event() (the CDP binding registered before injection).
_RECORD_SCRIPT = r"""
(() => {
    if (window.__specter_recorder_active) return 'already_recording';
    window.__specter_recorder_active = true;

    function dispatch(kind, payload) {
        try { window._specter_event(JSON.stringify({kind, payload})); } catch(e) {}
    }

    function buildSelector(el) {
        if (!el || el.nodeType !== 1) return 'body';
        if (el.dataset && el.dataset.testid) return '[data-testid="' + el.dataset.testid + '"]';
        if (el.id) return '#' + CSS.escape(el.id);
        const tag = el.tagName.toLowerCase();
        const parent = el.parentElement;
        if (parent) {
            const siblings = Array.from(parent.querySelectorAll(':scope > ' + tag));
            if (siblings.length > 1) {
                const idx = siblings.indexOf(el) + 1;
                return tag + ':nth-of-type(' + idx + ')';
            }
        }
        return tag;
    }

    const CAPTURE_KEYS = new Set([
        'Enter', 'Escape', 'Tab', 'Space',
        'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
        'Backspace', 'Delete', 'Home', 'End',
    ]);

    document.addEventListener('click', (e) => {
        const label = (e.target.textContent || '').trim().substring(0, 50) || null;
        dispatch('click', {
            selector: buildSelector(e.target),
            x: Math.round(e.clientX),
            y: Math.round(e.clientY),
            label,
        });
    }, true);

    document.addEventListener('keydown', (e) => {
        if (CAPTURE_KEYS.has(e.key)) {
            dispatch('keydown', {
                key: e.key,
                selector: buildSelector(document.activeElement || document.body),
            });
        }
    }, true);

    document.addEventListener('change', (e) => {
        const el = e.target;
        const tag = el.tagName;
        if (tag === 'SELECT') {
            dispatch('select_change', {selector: buildSelector(el), value: el.value});
        } else if (tag === 'INPUT' || tag === 'TEXTAREA') {
            dispatch('input_change', {selector: buildSelector(el), value: el.value});
        }
    }, true);

    return 'recording_started';
})()
"""


class InteractionRecorder:
    """Record and replay browser interaction sessions.

    Args:
        recordings_dir: Directory for JSONL recording files. Defaults to
            ~/.local/state/khimaira/specter/recordings/.
    """

    def __init__(self, recordings_dir: Path | None = None) -> None:
        self._recordings_dir = recordings_dir or _DEFAULT_RECORDINGS_DIR

    def _recording_path(self, label: str) -> Path:
        return self._recordings_dir / f"{label}.jsonl"

    def _write_recording(self, path: Path, metadata: dict, events: list[dict]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(metadata) + "\n")
            for event in events:
                f.write(json.dumps(event) + "\n")

    def _read_recording(self, label: str) -> tuple[dict, list[dict]]:
        """Parse a recording file into (metadata, events).

        Raises:
            FileNotFoundError: Recording label does not exist.
            ValueError: Header is missing, corrupted, or has an unknown version.
        """
        path = self._recording_path(label)
        if not path.exists():
            raise FileNotFoundError(f"Recording not found: {label} (looked at {path})")

        lines = [l for l in path.read_text(encoding="utf-8").splitlines() if l.strip()]
        if not lines:
            raise ValueError(f"Recording file is empty: {path}")

        try:
            metadata = json.loads(lines[0])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Corrupted recording header in {path}: {exc}") from exc

        version = metadata.get("version")
        if version != SCHEMA_VERSION:
            raise ValueError(
                f"Unknown recording version {version!r} (expected {SCHEMA_VERSION}). "
                "This recording was created by a different version of Specter and "
                "cannot be replayed. Re-record with the current version."
            )

        events: list[dict] = []
        for line in lines[1:]:
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                logger.warning(
                    "Skipping corrupted event line in %s: %r", path, line[:80]
                )

        return metadata, events

    async def record_interaction(
        self,
        conn: "CDPConnection",
        label: str,
        duration_s: float = 60.0,
    ) -> dict:
        """Record CDP-observable interactions for duration_s seconds.

        Injects a DOM event listener into the active page and relays events
        to Python via a CDP binding. Writes a versioned JSONL file on exit.

        Captured events: click, keydown (special keys only), input/change,
        select change, page navigation (via Page.frameNavigated).

        Determinism limits: animations, debounced inputs, Date.now(), and
        Math.random() all reduce replay fidelity. This tool is best-effort
        for time-sensitive or random UIs — see module docstring for details.

        Args:
            conn: Active CDP connection.
            label: Recording name (becomes <label>.jsonl in recordings dir).
            duration_s: How long to record (seconds). Recording stops after
                this timeout; call again with the same label to overwrite.

        Returns:
            Dict with label, recorded_event_count, file_path, duration_s_actual.
            On invalid label: {"error": "...", "label": label} (no file written).
        """
        if not _LABEL_RE.match(label):
            return {
                "error": "label must match [a-zA-Z0-9_-]+ (no path separators or dots)",
                "label": label,
            }

        path = self._recording_path(label)

        url = conn.current_target.url if conn.current_target else ""

        ua_result = await conn.send(
            "Runtime.evaluate",
            {"expression": "navigator.userAgent", "returnByValue": True},
        )
        user_agent = ua_result.get("result", {}).get("value", "") or ""

        started_ts = time.time()
        events: list[dict] = []

        def _on_binding_called(params: dict) -> None:
            if params.get("name") != "_specter_event":
                return
            try:
                data = json.loads(params.get("payload", "{}"))
            except json.JSONDecodeError:
                return
            ts_offset_ms = round((time.time() - started_ts) * 1000, 1)
            events.append({"ts_offset_ms": ts_offset_ms, **data})

        def _on_navigate(params: dict) -> None:
            ts_offset_ms = round((time.time() - started_ts) * 1000, 1)
            frame_url = params.get("frame", {}).get("url", "")
            events.append(
                {
                    "ts_offset_ms": ts_offset_ms,
                    "kind": "navigate",
                    "payload": {"url": frame_url},
                }
            )

        await conn.send("Runtime.addBinding", {"name": "_specter_event"})
        conn.on("Runtime.bindingCalled", _on_binding_called)
        conn.on("Page.frameNavigated", _on_navigate)

        await conn.send(
            "Runtime.evaluate",
            {"expression": _RECORD_SCRIPT, "returnByValue": True},
        )

        await asyncio.sleep(duration_s)
        duration_actual = time.time() - started_ts

        await conn.send("Runtime.removeBinding", {"name": "_specter_event"})

        metadata = {
            "version": SCHEMA_VERSION,
            "label": label,
            "started_ts": started_ts,
            "url": url,
            "user_agent": user_agent,
        }
        self._write_recording(path, metadata, events)
        logger.info("Recorded %d events → %s", len(events), path)

        return {
            "label": label,
            "recorded_event_count": len(events),
            "file_path": str(path),
            "duration_s_actual": round(duration_actual, 2),
        }

    async def replay_interaction(self, conn: "CDPConnection", label: str) -> dict:
        """Replay a recorded interaction session in order, respecting original timing.

        Loads the recording from disk, validates the schema version, then
        re-issues each event via CDP with the original inter-event delays.

        Determinism limits: see module docstring. In particular, any UI that
        depends on timing (animations, debounce) or randomness may not
        reproduce exactly.

        Args:
            conn: Active CDP connection. The page should be in its pre-recording
                state (same URL, same initial data) before replaying.
            label: Recording name to replay (must match a prior record_interaction call).

        Returns:
            Dict with label, replayed_event_count, and end_state_snapshot
            (url + title after the last event fires).
            On error: dict with "error" key describing what failed.
        """
        if not _LABEL_RE.match(label):
            return {
                "error": "label must match [a-zA-Z0-9_-]+ (no path separators or dots)",
                "label": label,
            }

        path = self._recording_path(label)
        if not path.exists():
            return {
                "error": f"Recording not found: {label} (looked at {path})",
                "label": label,
            }

        try:
            metadata, events = self._read_recording(label)
        except ValueError as exc:
            return {"error": str(exc), "label": label}

        if not events:
            end_url = conn.current_target.url if conn.current_target else ""
            return {
                "label": label,
                "replayed_event_count": 0,
                "end_state_snapshot": {"url": end_url, "title": ""},
            }

        interactor = Interactor()
        prev_offset_ms = 0.0

        for idx, event in enumerate(events):
            kind = event.get("kind", "")
            payload = event.get("payload", {})
            offset_ms = float(event.get("ts_offset_ms", 0))

            delay_s = (offset_ms - prev_offset_ms) / 1000
            if delay_s > 0:
                await asyncio.sleep(delay_s)
            prev_offset_ms = offset_ms

            selector = payload.get("selector", "body")

            try:
                if kind == "click":
                    await interactor.click_element(conn, selector)
                elif kind == "keydown":
                    key = payload.get("key", "")
                    if key:
                        await interactor.press_key(conn, key)
                elif kind == "input_change":
                    await interactor.fill_input(
                        conn, selector, payload.get("value", "")
                    )
                elif kind == "select_change":
                    await interactor.select_option(
                        conn, selector, payload.get("value", "")
                    )
                elif kind == "navigate":
                    nav_url = payload.get("url", "")
                    if nav_url:
                        await conn.send("Page.navigate", {"url": nav_url})
                else:
                    logger.debug("Skipping unknown event kind %r", kind)
            except Exception as exc:
                logger.warning("Replay event %d (%r) failed: %s", idx, kind, exc)
                return {
                    "error": str(exc),
                    "label": label,
                    "failed_event_index": idx,
                    "failed_event_kind": kind,
                    "replayed_event_count": idx,
                }

        end_url = conn.current_target.url if conn.current_target else ""
        title_result = await conn.send(
            "Runtime.evaluate",
            {"expression": "document.title", "returnByValue": True},
        )
        title = title_result.get("result", {}).get("value", "") or ""

        return {
            "label": label,
            "replayed_event_count": len(events),
            "end_state_snapshot": {
                "url": end_url,
                "title": title,
            },
        }
