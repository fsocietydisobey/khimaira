"""khimaira Codex SessionStart hook — identity + chat registration.

2026-07-15: first cut of the Codex adapter. Codex's hook I/O contract
(JSON stdin with `session_id`, stdout `{hookSpecificOutput: {hookEventName,
additionalContext}}`, exit 0/2) matches Claude Code's byte-for-byte —
verified live against a running Codex CLI session. That means the daemon
HTTP calls in `khimaira.hooks.session_start` port with zero changes; only
the Claude-Code-specific bits (reading `-n`/--name off /proc/<ppid>/cmdline,
`claude mcp` self-heal, kitty roster-slot binding) don't apply to Codex and
are skipped here.

Scope: identity + the chat_my_chats registration instruction only — the
minimum needed for a Codex session to become an addressable khimaira-chat
participant. Pending-notes/handoff/task surfacing (session_start.py's full
feature set) is deliberately deferred until a Codex session actually has
work assigned to it; porting it now would be premature — see the
UserPromptSubmit hook (codex_user_prompt_submit.py) for the piece that
actually matters at this stage: live message delivery.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

_ENDPOINT = os.environ.get("KHIMAIRA_ENDPOINT", "http://127.0.0.1:8740").rstrip("/")
_HTTP_TIMEOUT_S = 1.5


def _register_kitty_window(session_id: str) -> None:
    """POST {session_id}/window with KITTY_WINDOW_ID, same endpoint Claude
    Code sessions already use. Lets a watcher resolve session_id -> kitty
    window_id via daemon lookup instead of title-matching (the class of bug
    documented for the Claude Code roster wake mechanism — a decorated or
    reused window title can match the wrong window). Codex inherits
    KITTY_WINDOW_ID from its own kitty-launched environment the same way
    Claude Code does; no Codex-specific plumbing needed here.

    Best-effort, fire-and-forget — never blocks session boot.
    """
    kitty_wid = os.environ.get("KITTY_WINDOW_ID", "").strip()
    if not kitty_wid:
        return
    try:
        url = f"{_ENDPOINT}/api/sessions/{session_id}/window"
        data = json.dumps({"window_id": int(kitty_wid)}).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST", headers={"Content-Type": "application/json"}
        )
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S):
            pass
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        pass


def main() -> int:
    try:
        raw = sys.stdin.read()
        if not raw.strip():
            return 0
        data = json.loads(raw)
    except (json.JSONDecodeError, OSError):
        return 0

    session_id = data.get("session_id") or ""
    if not session_id:
        return 0

    # 2026-07-15 finding: KITTY_WINDOW_ID env-var self-detection does NOT
    # work for Codex the way it does for Claude Code. Codex hooks execute
    # server-side inside the long-lived `codex app-server` daemon process,
    # not inside the interactive TUI's own process tree — so this env var
    # reflects wherever the DAEMON was originally launched from, frozen at
    # daemon-start time, not whichever window is currently driving a given
    # session. Confirmed live: this hook fired with KITTY_WINDOW_ID pointing
    # at the daemon's launch window, not the session's actual kitty window.
    # Left disabled rather than removed — a future daemon version might fix
    # env propagation, at which point this becomes correct again for free.
    _REGISTER_WINDOW_VIA_ENV = False
    if _REGISTER_WINDOW_VIA_ENV:
        try:
            _register_kitty_window(session_id)
        except Exception:
            pass

    block = (
        f"🆔 khimaira session_id: `{session_id}`\n"
        "This is a Codex CLI session bridged into khimaira-chat. When calling "
        "khimaira-chat MCP tools, pass this id as `session_id`.\n\n"
        "⚡ ACTION REQUIRED — real-time chat registration:\n"
        f'Call the MCP tool `khimaira-chat.chat_my_chats(session_id="{session_id}")` '
        "now, before anything else. This registers you with the khimaira daemon "
        "so chat activity addressed to you is visible on your next turn."
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": block,
        }
    }
    sys.stdout.write(json.dumps(output))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        sys.exit(0)
