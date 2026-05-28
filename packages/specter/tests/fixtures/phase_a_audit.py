#!/usr/bin/env python3
"""Phase A audit script — Specter resolution-strategy hidden-element audit.

Loads hidden_element_test.html into SPECTER_TEST_PORT Chrome, then invokes
each interaction tool against visible + hidden element variants. Records
pass/fail and the resolution path used.

Usage:
    SPECTER_TEST_PORT=9223 .venv/bin/python3 packages/specter/tests/fixtures/phase_a_audit.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Run from repo root; package src dirs are on sys.path via pyproject.toml
from specter.browser.connection import CDPConnection
from specter.browser.interact import Interactor
from specter.config import SpecterConfig

FIXTURE_PATH = Path(__file__).parent / "hidden_element_test.html"
PORT = int(os.environ.get("SPECTER_TEST_PORT", "9223"))

interact = Interactor()


async def make_connection() -> CDPConnection:
    config = SpecterConfig(debug_port=PORT)
    conn = CDPConnection(config)
    targets = await conn.list_targets()
    if not targets:
        raise RuntimeError(f"No page targets at port {PORT}")
    await conn.connect(targets[0].id)
    return conn


async def navigate_to_fixture(conn: CDPConnection) -> None:
    fixture_url = f"file://{FIXTURE_PATH.absolute()}"
    await conn.send("Page.enable", {})
    await conn.send("Page.navigate", {"url": fixture_url})
    await asyncio.sleep(0.5)


async def test_tool(coro) -> tuple[bool, str]:
    try:
        result = await coro
        if isinstance(result, dict):
            if result.get("error"):
                return False, str(result["error"])[:80]
            if result.get("ok") is False:
                return False, str(result.get("message", "ok=False"))[:80]
        return True, str(result)[:80]
    except Exception as e:
        return False, f"exception: {e}"[:80]


async def main() -> None:
    conn = await make_connection()
    await navigate_to_fixture(conn)

    tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False)
    tmp_path = tmp.name
    tmp.write(b"test content")
    tmp.close()

    rows = [
        (
            "set_file_input",
            "interact.py:721",
            "Runtime.evaluate(returnByValue=False)→objectId→DOM.setFileInputFiles(objectId)",
            lambda: interact.set_file_input(conn, "#visible-file", [tmp_path]),
            lambda: interact.set_file_input(conn, "#hidden-file", [tmp_path]),
        ),
        (
            "click_element",
            "interact.py:665",
            "Runtime.evaluate→CLICK_SCRIPT (document.querySelector in JS)",
            lambda: interact.click_element(conn, "#visible-btn"),
            lambda: interact.click_element(conn, "#hidden-btn"),
        ),
        (
            "fill_input",
            "interact.py:691",
            "Runtime.evaluate→FILL_SCRIPT (document.querySelector in JS)",
            lambda: interact.fill_input(conn, "#visible-text", "hello"),
            lambda: interact.fill_input(conn, "#hidden-text", "hello"),
        ),
        (
            "hover_element",
            "interact.py:493",
            "Runtime.evaluate→HOVER_SCRIPT (document.querySelector in JS)",
            lambda: interact.hover_element(conn, "#visible-hoverable"),
            lambda: interact.hover_element(conn, "#hidden-hoverable"),
        ),
        (
            "scroll_to_element",
            "interact.py:878",
            "Runtime.evaluate→SCROLL_TO_ELEMENT_SCRIPT (document.querySelector in JS)",
            lambda: interact.scroll_to_element(conn, "#visible-btn"),
            lambda: interact.scroll_to_element(conn, "#hidden-btn"),
        ),
        (
            "select_option",
            "interact.py:851",
            "Runtime.evaluate→SELECT_SCRIPT (document.querySelector in JS)",
            lambda: interact.select_option(conn, "#visible-select", "b"),
            lambda: interact.select_option(conn, "#hidden-select", "b"),
        ),
        (
            "press_key (page-global)",
            "interact.py:522",
            "Input.dispatchKeyEvent (page-global; selector→Runtime.evaluate focus only)",
            lambda: interact.press_key(conn, "Tab"),
            lambda: interact.press_key(conn, "Tab"),
        ),
    ]

    print("\n## Phase A audit — Specter resolution-strategy hidden-element (live Chrome test)")
    print()
    for name, fileline, resolution, vis_fn, hid_fn in rows:
        vis_ok, vis_detail = await test_tool(vis_fn())
        hid_ok, hid_detail = await test_tool(hid_fn())
        status = "BROKEN" if not hid_ok else "SAFE"
        print(f"**{name}** ({fileline})")
        print(f"  Resolution: {resolution}")
        print(f"  Visible:  {'✅ PASS' if vis_ok else f'❌ FAIL — {vis_detail}'}")
        print(f"  Hidden:   {'✅ PASS' if hid_ok else f'❌ FAIL — {hid_detail}'}")
        print(f"  Status: **{status}**")
        print()

    import os as _os
    _os.unlink(tmp_path)
    await conn.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
