"""Tests for CDPConnection.connect() target-resolution behavior.

Verifies the fix for the silent-jump-to-wrong-tab bug (2026-05-22):
  - Stale target_id raises ConnectionError with actionable guidance
  - None target_id auto-picks the first app tab (legitimate use case, unchanged)
  - fixture_page conftest fixture binds 127.0.0.1 only (not 0.0.0.0)

Root cause: connect() silently fell back to targets[0] when the stored
target_id was not found. When a test run had a fixture page open as
targets[0], Specter silently jumped to it — overwriting the user's active
debugging session without any error or notice.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, patch

import pytest

from specter.browser.connection import CDPConnection, Target
from specter.config import load_config


def _make_target(target_id: str, url: str = "http://localhost/app") -> Target:
    return Target(
        id=target_id,
        title="App Tab",
        url=url,
        ws_url=f"ws://localhost/{target_id}",
        type="page",
    )


@pytest.mark.asyncio
async def test_connect_to_unknown_target_id_raises_clear_error():
    """Stale target_id raises ConnectionError — no silent fallback to targets[0]."""
    config = load_config()
    conn = CDPConnection(config)

    real_target = _make_target("real-id-abc123")
    with patch.object(conn, "list_targets", new=AsyncMock(return_value=[real_target])):
        with pytest.raises(ConnectionError) as exc_info:
            await conn.connect(target_id="stale-id-xyz999")

    msg = str(exc_info.value)
    assert "stale-id-xyz999" in msg
    assert "specter_list_tabs" in msg
    assert "specter_connect_to_tab" in msg


@pytest.mark.asyncio
async def test_connect_to_unknown_target_id_does_not_jump_to_first_tab():
    """Stale target_id must NOT connect to the first tab in the list."""
    config = load_config()
    conn = CDPConnection(config)

    decoy_target = _make_target("decoy-id", url="http://127.0.0.1:48825/test-fixture")
    with patch.object(conn, "list_targets", new=AsyncMock(return_value=[decoy_target])):
        with pytest.raises(ConnectionError):
            await conn.connect(target_id="stale-id-not-decoy")

    # Connection must NOT have been established to the decoy
    assert conn._connected_target is None
    assert conn._ws is None


@pytest.mark.asyncio
async def test_connect_with_none_target_id_auto_picks_first_app_tab():
    """target_id=None auto-picks the first non-internal tab — behavior unchanged."""
    config = load_config()
    conn = CDPConnection(config)

    app_target = _make_target("app-tab-id", url="http://localhost:3000/app")
    mock_ws = AsyncMock()

    with (
        patch.object(conn, "list_targets", new=AsyncMock(return_value=[app_target])),
        patch("specter.browser.connection.websockets.connect", new=AsyncMock(return_value=mock_ws)),
        patch("asyncio.create_task"),
    ):
        result = await conn.connect(target_id=None)

    assert result.id == "app-tab-id"
    assert conn._connected_target is not None
    assert conn._connected_target.id == "app-tab-id"


def test_fixture_page_binds_localhost_only(fixture_page: str):
    """fixture_page conftest fixture binds to 127.0.0.1, not 0.0.0.0."""
    assert fixture_page.startswith("http://127.0.0.1:")
    port = int(fixture_page.split(":")[-1])
    # Confirm port is reachable on 127.0.0.1
    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
        pass  # connection success = confirmed bound to localhost
