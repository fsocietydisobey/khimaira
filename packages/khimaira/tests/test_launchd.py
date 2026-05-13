"""Tests for the launchd LaunchAgent install path (macOS supervisor).

The install command itself shells out to launchctl, which we can't
exercise on Linux CI. These tests cover the parts that are platform-
independent: plist content rendering, path resolution, and platform
guard rails.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from khimaira.monitor.cli import (
    _LAUNCHD_LABEL,
    _cmd_install_launchd,
    _cmd_uninstall_launchd,
    _launchd_plist_content,
    _launchd_plist_path,
)


def test_launchd_label_matches_convention():
    """launchd labels are reverse-DNS; `com.khimaira.monitor` is what
    /api callers (doctor.py, attach.py) check via `launchctl list`."""
    assert _LAUNCHD_LABEL == "com.khimaira.monitor"


def test_plist_path_under_library_launch_agents():
    """User-scope LaunchAgent path. Matches Apple's documented layout."""
    p = _launchd_plist_path()
    assert isinstance(p, Path)
    assert p.name == f"{_LAUNCHD_LABEL}.plist"
    assert "Library/LaunchAgents" in str(p)


def test_plist_content_is_valid_xml_plist_skeleton():
    """Content must parse as a plist. We render by string concatenation;
    this test guards against a syntax slip introducing a malformed plist."""
    import plistlib

    content = _launchd_plist_content()
    parsed = plistlib.loads(content.encode("utf-8"))

    assert parsed["Label"] == _LAUNCHD_LABEL
    assert parsed["RunAtLoad"] is True
    assert isinstance(parsed["ProgramArguments"], list)
    assert len(parsed["ProgramArguments"]) > 0
    # KeepAlive set so the daemon restarts even on clean exits (matches
    # the systemd unit's "Restart=always with comment about rc=0" intent).
    assert parsed["KeepAlive"]["SuccessfulExit"] is False
    # Throttled so a crash-loop doesn't spin.
    assert isinstance(parsed["ThrottleInterval"], int)


def test_plist_content_routes_logs_to_library_logs():
    """Logs should land where macOS users expect: ~/Library/Logs/."""
    import plistlib

    parsed = plistlib.loads(_launchd_plist_content().encode("utf-8"))
    assert "Library/Logs" in parsed["StandardOutPath"]
    assert "Library/Logs" in parsed["StandardErrorPath"]
    assert parsed["StandardOutPath"].endswith("khimaira-monitor.out.log")
    assert parsed["StandardErrorPath"].endswith("khimaira-monitor.err.log")


def test_install_launchd_refuses_on_non_darwin():
    """The command guards against being run on Linux/Windows."""
    if sys.platform == "darwin":
        pytest.skip("test asserts behavior on non-Darwin hosts")

    args = argparse.Namespace(enable=False, force=False)
    rc = _cmd_install_launchd(args)
    assert rc == 1


def test_uninstall_launchd_refuses_on_non_darwin():
    if sys.platform == "darwin":
        pytest.skip("test asserts behavior on non-Darwin hosts")

    args = argparse.Namespace()
    rc = _cmd_uninstall_launchd(args)
    assert rc == 1


def test_install_launchd_on_darwin_without_launchctl_returns_1():
    """If we're on macOS but launchctl is missing (broken PATH), refuse
    cleanly rather than crashing later in subprocess.run."""
    args = argparse.Namespace(enable=False, force=False)
    with (
        patch("khimaira.monitor.cli.sys.platform", "darwin"),
        patch("khimaira.monitor.cli.shutil_which", return_value=None),
    ):
        rc = _cmd_install_launchd(args)
    assert rc == 1
