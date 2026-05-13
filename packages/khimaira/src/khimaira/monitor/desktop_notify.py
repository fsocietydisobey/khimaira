"""Cross-platform desktop notifications for khimaira cross-session events.

Fires when a session-to-session event lands (handoff arrives, invite,
notice, answer) so the user sees a popup regardless of which chat
window has focus. Closes the "I forgot to check the other window" gap.

Backends:
  - Linux:  notify-send (libnotify, ubiquitous on GNOME/KDE/etc.)
  - macOS:  osascript -e 'display notification ...'
  - Other:  no-op (return without erroring)

Disable globally with KHIMAIRA_DESKTOP_NOTIFY=0. Broadcast-on-decision
fan-out is OFF by default (too noisy); set KHIMAIRA_DESKTOP_NOTIFY_BROADCAST=1
to opt in.

All calls are non-blocking (subprocess.Popen, no wait()) and swallow
errors — desktop notifications are best-effort, never critical-path.
"""

from __future__ import annotations

import os
import platform
import shlex
import subprocess

_APP_NAME = "khimaira"


def _enabled() -> bool:
    """Master switch. Default ON; set KHIMAIRA_DESKTOP_NOTIFY=0 to disable."""
    return os.environ.get("KHIMAIRA_DESKTOP_NOTIFY", "1") not in ("0", "false", "no")


def _broadcast_enabled() -> bool:
    """Subscriber-broadcast fan-out gate. Default OFF (too noisy).

    Each decision logged by a handoff owner fans out to every subscriber's
    inbox. If desktop notifications also fired for every fan-out, a busy
    owner would spam the user. Opt in explicitly when you want to track
    owner activity in real time.
    """
    return os.environ.get("KHIMAIRA_DESKTOP_NOTIFY_BROADCAST", "0") in (
        "1",
        "true",
        "yes",
    )


def notify(title: str, message: str) -> None:
    """Send a desktop notification. Best-effort, non-blocking, silent on failure."""
    if not _enabled():
        return

    system = platform.system()
    try:
        if system == "Linux":
            subprocess.Popen(
                ["notify-send", f"--app-name={_APP_NAME}", title, message],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        elif system == "Darwin":
            # osascript handles escaping if we pass quoted args via shell.
            # Use shlex.quote on user-provided strings.
            script = (
                f"display notification {shlex.quote(message)} "
                f"with title {shlex.quote(_APP_NAME)} "
                f"subtitle {shlex.quote(title)}"
            )
            subprocess.Popen(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        # Other platforms (Windows, BSD, etc.) — no-op until a user asks.
    except (FileNotFoundError, OSError):
        # notify-send / osascript not installed → skip silently.
        pass


def notify_handoff(from_session_id: str, scope_cwd: str, text: str) -> None:
    """New handoff posted in a project."""
    project = os.path.basename(scope_cwd.rstrip("/")) or scope_cwd
    notify(
        f"📦 Handoff in {project}",
        f"from {from_session_id[:8]}: {text[:160]}",
    )


def notify_invite(owner_session_id: str, invitee_session_id: str, text: str) -> None:
    """Owner invited a specific session to take a slice of work."""
    notify(
        f"🤝 Invite for {invitee_session_id[:12]}",
        f"from {owner_session_id[:8]}: {text[:160]}",
    )


def notify_notice(target_session_id: str, from_session_id: str, text: str) -> None:
    """A direct FYI / notice landed in a session's inbox."""
    notify(
        f"💬 Notice for {target_session_id[:12]}",
        f"from {from_session_id[:8]}: {text[:160]}",
    )


def notify_answer(
    target_session_id: str, from_session_id: str, question_text: str
) -> None:
    """A session answered another session's question."""
    notify(
        f"✅ Answer for {target_session_id[:12]}",
        f"from {from_session_id[:8]} re: {question_text[:140]}",
    )


def notify_broadcast(
    owner_session_id: str, subscriber_session_id: str, kind: str, text: str
) -> None:
    """Owner's decision fanned out to a subscriber. Off by default."""
    if not _broadcast_enabled():
        return
    notify(
        f"📡 {owner_session_id[:8]} → {subscriber_session_id[:12]} ({kind})",
        text[:160],
    )
