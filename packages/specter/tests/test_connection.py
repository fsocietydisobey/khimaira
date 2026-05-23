"""Tests for CDPConnection.connect() target-resolution behavior + server reconnect anchoring.

Verifies two related bugs fixed in 2026-05-22:
1. connection.py silent-jump fix (task-4675e83bce68 / ba7bd89):
   - Stale target_id raises ConnectionError with actionable guidance
   - None target_id auto-picks the first app tab (legitimate use case, unchanged)
   - fixture_page conftest fixture binds 127.0.0.1 only (not 0.0.0.0)

2. server.py auto-pick-on-reconnect fix (task-3c2e2b3a9d36):
   - After connecting to a tab, reconnects re-anchor to that same tab
   - If the previous tab is gone on reconnect, raises ConnectionError (no silent jump)
   - First-ever connect (no _last_target_id) still auto-picks
   - specter_connect_to_tab() updates _last_target_id for future reconnects

Root cause (original): connect() silently fell back to targets[0] on stale IDs.
Root cause (reconnect): _ensure_connected() always passed target_id=None, so every
reconnect after a connection drop would auto-pick targets[0] regardless of what tab
the user was previously debugging.
"""

from __future__ import annotations

import socket
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import specter.server as server_mod
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
async def test_connect_with_none_target_id_raises_value_error():
    """connect(target_id=None) raises ValueError — no auto-pick after removing that path."""
    config = load_config()
    conn = CDPConnection(config)

    with pytest.raises(ValueError) as exc_info:
        await conn.connect(target_id=None)

    msg = str(exc_info.value)
    assert "specter_list_tabs" in msg
    assert "specter_connect_to_tab" in msg


def test_fixture_page_binds_localhost_only(fixture_page: str):
    """fixture_page conftest fixture binds to 127.0.0.1, not 0.0.0.0."""
    assert fixture_page.startswith("http://127.0.0.1:")
    port = int(fixture_page.split(":")[-1])
    # Confirm port is reachable on 127.0.0.1
    with socket.create_connection(("127.0.0.1", port), timeout=1.0):
        pass  # connection success = confirmed bound to localhost


# ---------------------------------------------------------------------------
# server.py reconnect-anchor tests (task-3c2e2b3a9d36)
# ---------------------------------------------------------------------------


def _make_connected_mock(target_id: str, url: str = "http://localhost:3000/app") -> AsyncMock:
    """Return an AsyncMock for CDPConnection.connect() that returns the given target."""
    target = _make_target(target_id, url)
    mock = AsyncMock(return_value=target)
    return mock


# ---------------------------------------------------------------------------
# connection.py no-auto-pick tests (task-6e23555f7f3b)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_raises_value_error_when_no_target_id():
    """connect(target_id=None) and connect(target_id='') both raise ValueError."""
    config = load_config()
    conn = CDPConnection(config)

    with pytest.raises(ValueError) as exc_info:
        await conn.connect(target_id=None)
    assert "specter_list_tabs" in str(exc_info.value)
    assert "specter_connect_to_tab" in str(exc_info.value)

    with pytest.raises(ValueError):
        await conn.connect(target_id="")


@pytest.mark.asyncio
async def test_ensure_connected_raises_when_no_anchor(monkeypatch):
    """_ensure_connected() raises ConnectionError with guidance when _last_target_id is None."""
    monkeypatch.setattr(server_mod, "_last_target_id", None)
    monkeypatch.setattr(server_mod, "_connection", None)

    with pytest.raises(ConnectionError) as exc_info:
        await server_mod._ensure_connected()

    msg = str(exc_info.value)
    assert "specter_list_tabs" in msg
    assert "specter_connect_to_tab" in msg


@pytest.mark.asyncio
async def test_list_tabs_works_without_anchor(monkeypatch):
    """list_tabs() bypasses _ensure_connected and works even with no anchor."""
    monkeypatch.setattr(server_mod, "_last_target_id", None)
    monkeypatch.setattr(server_mod, "_connection", None)

    tab_a = _make_target("tab-a", url="http://localhost:3000/app")

    mock_temp_conn = MagicMock()
    mock_temp_conn.list_targets = AsyncMock(return_value=[tab_a])

    with patch("specter.server.CDPConnection", return_value=mock_temp_conn):
        result = await server_mod.list_tabs()

    assert len(result) == 1
    assert result[0]["id"] == "tab-a"
    assert result[0]["connected"] is False  # no anchor set


@pytest.mark.asyncio
async def test_connect_to_tab_sets_anchor_and_unblocks_subsequent_calls(monkeypatch):
    """After connect_to_tab succeeds, _last_target_id is set and _ensure_connected works."""
    monkeypatch.setattr(server_mod, "_last_target_id", None)
    monkeypatch.setattr(server_mod, "_connection", None)

    # _ensure_connected must fail before connect_to_tab
    with pytest.raises(ConnectionError):
        await server_mod._ensure_connected()

    # connect_to_tab should succeed and set the anchor
    tab_a = _make_target("tab-a")
    mock_conn = MagicMock()
    mock_conn.is_connected = True

    async def mock_connect(target_id):
        return tab_a

    mock_conn.connect = mock_connect
    mock_conn.register = MagicMock()

    async def mock_enable(conn):
        pass

    mock_console = MagicMock()
    mock_console.register = MagicMock()
    mock_console.enable = mock_enable
    mock_network = MagicMock()
    mock_network.register = MagicMock()
    mock_network.enable = mock_enable

    with (
        patch("specter.server.CDPConnection", return_value=mock_conn),
        patch("specter.server.ConsoleCapture", return_value=mock_console),
        patch("specter.server.NetworkCapture", return_value=mock_network),
        patch("specter.server.Runtime"),
        patch("specter.server.ReactInspector"),
        patch("specter.server.Interactor"),
        patch("specter.server.StructureAnalyzer"),
    ):
        await server_mod.connect_to_tab("tab-a")

    # Anchor is now set
    assert server_mod._last_target_id == "tab-a"

    # _ensure_connected should now return without error (connection is live)
    mock_conn.is_connected = True
    monkeypatch.setattr(server_mod, "_connection", mock_conn)
    result = await server_mod._ensure_connected()
    assert result is not None


@pytest.mark.asyncio
async def test_reconnect_anchors_to_previous_target(monkeypatch):
    """After connecting to tab-A, a reconnect re-anchors to tab-A, not auto-pick."""
    monkeypatch.setattr(server_mod, "_last_target_id", "tab-A")
    monkeypatch.setattr(server_mod, "_connection", None)

    target_a = _make_target("tab-A", url="http://localhost:3000/app")

    mock_conn = MagicMock()
    mock_conn.is_connected = False

    # CDPConnection.connect() should be called with target_id="tab-A"
    async def mock_connect(target_id=None):
        assert target_id == "tab-A", f"Expected re-anchor to tab-A, got {target_id!r}"
        return target_a

    mock_conn.connect = mock_connect
    mock_conn.register = MagicMock()

    async def mock_enable(conn):
        pass

    mock_console = MagicMock()
    mock_console.register = MagicMock()
    mock_console.enable = mock_enable

    mock_network = MagicMock()
    mock_network.register = MagicMock()
    mock_network.enable = mock_enable

    with (
        patch("specter.server.CDPConnection", return_value=mock_conn),
        patch("specter.server.ConsoleCapture", return_value=mock_console),
        patch("specter.server.NetworkCapture", return_value=mock_network),
        patch("specter.server.Runtime"),
        patch("specter.server.ReactInspector"),
        patch("specter.server.Interactor"),
        patch("specter.server.StructureAnalyzer"),
        patch.object(mock_conn, "send", new=AsyncMock()),
    ):
        await server_mod._ensure_connected()

    assert server_mod._last_target_id == "tab-A"


@pytest.mark.asyncio
async def test_reconnect_raises_when_previous_target_gone(monkeypatch):
    """If the previous tab is gone on reconnect, raises ConnectionError and clears _last_target_id."""
    monkeypatch.setattr(server_mod, "_last_target_id", "tab-gone")
    monkeypatch.setattr(server_mod, "_connection", None)

    mock_conn = MagicMock()
    mock_conn.is_connected = False

    async def mock_connect_raises(target_id=None):
        raise ConnectionError(f"Specter target {target_id!r} not found in current targets.")

    mock_conn.connect = mock_connect_raises
    mock_conn.register = MagicMock()

    mock_console = MagicMock()
    mock_console.register = MagicMock()
    mock_network = MagicMock()
    mock_network.register = MagicMock()

    with (
        patch("specter.server.CDPConnection", return_value=mock_conn),
        patch("specter.server.ConsoleCapture", return_value=mock_console),
        patch("specter.server.NetworkCapture", return_value=mock_network),
        patch("specter.server.Runtime"),
        patch("specter.server.ReactInspector"),
        patch("specter.server.Interactor"),
        patch("specter.server.StructureAnalyzer"),
    ):
        with pytest.raises(ConnectionError):
            await server_mod._ensure_connected()

    # _last_target_id cleared so agent can re-anchor explicitly
    assert server_mod._last_target_id is None


@pytest.mark.asyncio
async def test_no_anchor_raises_before_any_connection(monkeypatch):
    """Fresh state (_last_target_id=None) → _ensure_connected raises ConnectionError.

    Replaces the old auto-pick test. Auto-pick is removed; fresh sessions must
    call specter_list_tabs + specter_connect_to_tab before any tool works.
    """
    monkeypatch.setattr(server_mod, "_last_target_id", None)
    monkeypatch.setattr(server_mod, "_connection", None)

    with pytest.raises(ConnectionError) as exc_info:
        await server_mod._ensure_connected()

    msg = str(exc_info.value)
    assert "specter_list_tabs" in msg
    assert "specter_connect_to_tab" in msg
