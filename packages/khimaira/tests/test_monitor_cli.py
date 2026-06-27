"""monitor lifecycle control (#28) — systemd-aware stop/restart + unit content.

The bug: `monitor stop/restart/status` keyed only off the PID file, but the
`--foreground` start path (how the systemd unit launches the daemon) never
wrote one — so those commands silently no-op'd against the live daemon. And the
generated unit used `Restart=on-failure` despite a comment promising "restart
even on clean exits", so a clean rc=0 exit left the unit dead all day while a
stale manual instance served. These tests guard both.
"""

from __future__ import annotations

import argparse
import subprocess

from khimaira.monitor import cli


def test_unit_content_restart_always():
    """The generated systemd unit must use Restart=always — on-failure leaves
    the daemon dead on a clean rc=0 exit (the stale-daemon hazard)."""
    content = cli._systemd_unit_content()
    assert "Restart=always" in content
    assert "Restart=on-failure" not in content


def test_stop_delegates_to_systemd_when_unit_active(monkeypatch):
    """Under Restart=always, SIGTERM-ing the PID just respawns it — stop must
    go through systemctl when the unit is active."""
    monkeypatch.setattr(cli, "_systemd_unit_active", lambda: True)
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, *a, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    rc = cli._cmd_stop(argparse.Namespace())
    assert rc == 0
    assert calls == [["systemctl", "--user", "stop", "khimaira-monitor"]]


def test_restart_delegates_to_systemd_when_unit_active(monkeypatch):
    monkeypatch.setattr(cli, "_systemd_unit_active", lambda: True)
    calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, *a, **k: calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    rc = cli._cmd_restart(argparse.Namespace())
    assert rc == 0
    assert calls == [["systemctl", "--user", "restart", "khimaira-monitor"]]


def test_stop_uses_pidfile_path_when_unit_inactive(monkeypatch):
    """No systemd unit → the legacy PID-file stop path runs (no systemctl)."""
    monkeypatch.setattr(cli, "_systemd_unit_active", lambda: False)
    monkeypatch.setattr(cli, "_read_pid", lambda: None)  # nothing running
    sysctl_calls = []
    monkeypatch.setattr(
        subprocess, "run",
        lambda cmd, *a, **k: sysctl_calls.append(cmd) or subprocess.CompletedProcess(cmd, 0),
    )
    rc = cli._cmd_stop(argparse.Namespace())
    assert rc == 0
    assert sysctl_calls == [], "must not call systemctl when unit is inactive"
