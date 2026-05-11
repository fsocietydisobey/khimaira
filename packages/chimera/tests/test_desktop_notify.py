"""Unit tests for chimera.monitor.desktop_notify.

We mock subprocess.Popen so tests don't actually fire popups, then assert
the command we built. The point is to verify:
  - The env-var gates work (CHIMERA_DESKTOP_NOTIFY=0 → silent)
  - The platform branch picks the right backend
  - The notify_* convenience wrappers format their messages correctly
  - Missing notify-send / osascript fails silently (best-effort contract)
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from chimera.monitor import desktop_notify


@pytest.fixture
def linux_platform(monkeypatch):
    monkeypatch.setattr(desktop_notify.platform, "system", lambda: "Linux")


@pytest.fixture
def macos_platform(monkeypatch):
    monkeypatch.setattr(desktop_notify.platform, "system", lambda: "Darwin")


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("CHIMERA_DESKTOP_NOTIFY", "1")
    monkeypatch.delenv("CHIMERA_DESKTOP_NOTIFY_BROADCAST", raising=False)


def test_notify_disabled_when_env_var_zero(linux_platform, monkeypatch):
    monkeypatch.setenv("CHIMERA_DESKTOP_NOTIFY", "0")
    with patch.object(desktop_notify.subprocess, "Popen") as mock_popen:
        desktop_notify.notify("title", "message")
    mock_popen.assert_not_called()


def test_notify_linux_uses_notify_send(linux_platform, enabled):
    with patch.object(desktop_notify.subprocess, "Popen") as mock_popen:
        desktop_notify.notify("hello", "world")
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "notify-send"
    assert cmd[1] == "--app-name=chimera"
    assert cmd[2] == "hello"
    assert cmd[3] == "world"


def test_notify_macos_uses_osascript(macos_platform, enabled):
    with patch.object(desktop_notify.subprocess, "Popen") as mock_popen:
        desktop_notify.notify("hello", "world")
    mock_popen.assert_called_once()
    cmd = mock_popen.call_args[0][0]
    assert cmd[0] == "osascript"
    assert cmd[1] == "-e"
    script = cmd[2]
    assert "display notification" in script
    assert "world" in script  # message
    assert "hello" in script  # subtitle


def test_notify_swallows_missing_backend(linux_platform, enabled):
    """If notify-send isn't installed (FileNotFoundError) we don't crash."""
    with patch.object(
        desktop_notify.subprocess, "Popen", side_effect=FileNotFoundError
    ):
        desktop_notify.notify("hello", "world")  # must not raise


def test_notify_handoff_includes_project_and_text(linux_platform, enabled):
    with patch.object(desktop_notify.subprocess, "Popen") as mock_popen:
        desktop_notify.notify_handoff(
            "abc123",
            "/home/user/my-project",
            "the body of the handoff",
        )
    cmd = mock_popen.call_args[0][0]
    title = cmd[2]
    body = cmd[3]
    assert "my-project" in title
    assert "the body of the handoff" in body


def test_notify_broadcast_default_off(linux_platform, enabled):
    """Broadcast notifications are OFF by default (too noisy)."""
    with patch.object(desktop_notify.subprocess, "Popen") as mock_popen:
        desktop_notify.notify_broadcast("owner", "sub", "decision", "did a thing")
    mock_popen.assert_not_called()


def test_notify_broadcast_on_when_opt_in(linux_platform, monkeypatch):
    monkeypatch.setenv("CHIMERA_DESKTOP_NOTIFY", "1")
    monkeypatch.setenv("CHIMERA_DESKTOP_NOTIFY_BROADCAST", "1")
    with patch.object(desktop_notify.subprocess, "Popen") as mock_popen:
        desktop_notify.notify_broadcast("owner", "sub", "decision", "did a thing")
    mock_popen.assert_called_once()


def test_post_handoff_fires_notification(isolated_state, tmp_path, monkeypatch):
    """Integration: post_handoff triggers notify_handoff at the daemon level."""
    monkeypatch.setenv("CHIMERA_DESKTOP_NOTIFY", "1")
    project = tmp_path / "proj"
    project.mkdir()
    with patch.object(isolated_state.desktop_notify, "notify_handoff") as mock_n:
        isolated_state.post_handoff(
            "asker",
            text="please pick up",
            scope_cwd=str(project),
            expires_in_hours=24,
        )
    mock_n.assert_called_once()
    args = mock_n.call_args[0]
    assert args[0] == "asker"
    assert args[1] == str(project)
