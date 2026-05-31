"""``khimaira roster`` subcommands — roster session management.

  spawn <role-name>   Launch a new roster session into the khimaira-roster
                      kitty tab, auto-join the roster chat, and role-bind it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path


# ---------------------------------------------------------------------------
# Subparser registration
# ---------------------------------------------------------------------------

def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    parser = subparsers.add_parser(
        "roster",
        help="manage the khimaira roster (spawn sessions, etc.)",
        description="Roster session lifecycle management.",
    )
    sub = parser.add_subparsers(dest="roster_action", required=True)

    spawn = sub.add_parser(
        "spawn",
        help="spawn a new roster session into the khimaira-roster kitty tab",
        description=(
            "Launches a fresh claude-chat session with -n <role-name> (new session, "
            "NOT -r/--resume), places it in the khimaira-roster kitty tab, "
            "and auto-joins + role-binds it in the roster chat."
        ),
    )
    spawn.add_argument("role_name", help="session name/role (e.g. agent-4, deputy-1)")
    spawn.add_argument("--model", default="sonnet", help="model tier (default: sonnet)")
    spawn.add_argument("--effort", default="medium", help="effort level (default: medium)")
    spawn.add_argument(
        "--chat-id",
        default="chat-fdf7c4cbd3bd",
        help="roster chat to join (default: chat-fdf7c4cbd3bd)",
    )
    spawn.add_argument(
        "--cwd",
        default=None,
        help="working directory for the new session (default: current directory)",
    )
    spawn.add_argument(
        "--timeout",
        type=float,
        default=45.0,
        help="seconds to wait for session registration (default: 45)",
    )
    spawn.add_argument(
        "--dry-run",
        action="store_true",
        help="print what would happen without actually spawning",
    )
    parser.set_defaults(func=run)


def run(args: argparse.Namespace) -> int:
    action = getattr(args, "roster_action", None)
    if action == "spawn":
        return _spawn(args)
    print(f"Unknown roster action: {action}", file=sys.stderr)
    return 1


# ---------------------------------------------------------------------------
# spawn implementation
# ---------------------------------------------------------------------------

def _spawn(args: argparse.Namespace) -> int:
    role_name = args.role_name
    model = args.model
    effort = args.effort
    chat_id = args.chat_id
    timeout = args.timeout
    cwd = args.cwd or os.getcwd()
    dry_run = args.dry_run

    # --- Step 1: infer normalized role for member_roles binding
    role = _infer_role(role_name)
    if role is None:
        print(
            f"Error: cannot infer role from {role_name!r}. "
            "Ensure the name follows the roster naming convention (e.g. agent-4, jp-agent-1).",
            file=sys.stderr,
        )
        return 1

    # --- Step 2: find the khimaira-roster tab
    tab_id = _find_roster_tab()
    if tab_id is None:
        print(
            "Error: no kitty tab named 'khimaira-roster' found. "
            "Is kitty running with the roster tab open?",
            file=sys.stderr,
        )
        return 1

    # --- Step 3: find placement anchor (last agent window in the tab)
    anchor_window_id = _find_last_agent_window(tab_id)

    # --- Step 4: build the kitty launch command
    # Use -n (new session, NOT -r/--resume — that would fail for a never-seen name)
    session_cmd = f"claude-chat -n {role_name} --model {model} --effort {effort}; exec bash"
    cd_cmd = f"cd {cwd!r}"
    bash_cmd = f"{cd_cmd} && {session_cmd}"

    kitty_args = ["kitty", "@", "launch", "--type=window", f"--match=tab:id:{tab_id}"]
    if anchor_window_id is not None:
        # Place after the last known agent window in the same column
        kitty_args += [f"--match-window=id:{anchor_window_id}", "--location=after"]
    kitty_args += ["--", "bash", "-ic", bash_cmd]

    if dry_run:
        print(f"Would launch: {' '.join(kitty_args)}")
        print(f"  role_name={role_name!r}  role={role!r}  chat_id={chat_id!r}")
        return 0

    # --- Step 5: launch the window
    print(f"Spawning {role_name!r} in tab {tab_id} (role={role!r})...")
    result = subprocess.run(kitty_args, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"kitty @ launch failed: {result.stderr.strip()}", file=sys.stderr)
        return 1

    # kitty prints the new window ID to stdout
    launched_window_id = result.stdout.strip() or None
    if launched_window_id:
        print(f"  kitty window {launched_window_id} launched")

    # --- Step 6: wait for the session to register with the daemon
    print(f"Waiting for session {role_name!r} to register (timeout={timeout:.0f}s)...")
    session_id = _wait_for_session(role_name, timeout=timeout)
    if session_id is None:
        print(
            f"Warning: session {role_name!r} did not register within {timeout:.0f}s. "
            "The window was launched; run `khimaira sessions list` to check status. "
            "You can manually invite + accept + role-bind once it registers.",
            file=sys.stderr,
        )
        return 0  # window launched; partial success

    print(f"  session registered: {session_id}")

    # --- Step 7: integrate — invite, accept, role-bind
    print(f"Integrating into roster chat {chat_id!r}...")
    try:
        _integrate_session(session_id, role, chat_id)
        print(f"  ✓ {role_name!r} joined + role-bound as {role!r}")
    except Exception as exc:
        print(f"Warning: integration failed: {exc}", file=sys.stderr)
        print(
            f"  The window is running. Manually invite+accept+grant_role "
            f"session {session_id!r} to {chat_id!r} as role {role!r}.",
            file=sys.stderr,
        )
        return 0  # window launched; integration failed non-fatally

    print(f"Done — {role_name!r} is live in the roster.")
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _infer_role(role_name: str) -> str | None:
    """Normalize role_name via infer_role_from_name (handles jp-agent-1 → jp-agent)."""
    try:
        from khimaira.monitor.chats import infer_role_from_name
        return infer_role_from_name(role_name)
    except Exception:
        pass
    # Fallback: strip trailing -N suffix
    parts = role_name.rsplit("-", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return parts[0]
    return role_name


def _kitty(*args: str, timeout: float = 5.0) -> str | None:
    """Run ``kitty @ <args>``; return stdout or None on any failure."""
    cmd = ["kitty", "@", *args]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if r.returncode == 0:
            return r.stdout
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    return None


def _find_roster_tab() -> int | None:
    """Return the tab ID for the 'khimaira-roster' tab, or None."""
    raw = _kitty("ls")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    for os_win in data:
        for tab in os_win.get("tabs", []):
            title = (tab.get("title") or tab.get("name") or "").lower()
            if "roster" in title:
                return tab.get("id")
    return None


def _find_last_agent_window(tab_id: int) -> int | None:
    """Find the last agent-N window in the given tab (for placement anchor)."""
    raw = _kitty("ls")
    if not raw:
        return None
    try:
        data = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None
    agent_windows: list[tuple[int, int]] = []  # (agent_number, window_id)
    for os_win in data:
        for tab in os_win.get("tabs", []):
            if tab.get("id") != tab_id:
                continue
            for win in tab.get("windows", []):
                cmdline = win.get("cmdline") or []
                joined = " ".join(str(c) for c in cmdline)
                if "claude" not in joined:
                    continue
                # kitty cmdline is ["bash", "-ic", "cd '...' && claude-chat -r agent-1 ..."]
                # The flag is embedded in the shell arg, so search the joined string.
                m_flag = re.search(r"claude-chat(?:\s+-\S+)*?\s+(?:-n|-r)\s+(\S+)", joined)
                session_name: str | None = m_flag.group(1) if m_flag else None
                if not session_name:
                    continue
                # Look for agent-N names
                m = re.match(r"(?:.*-)?agent-(\d+)$", session_name)
                if m:
                    agent_windows.append((int(m.group(1)), win.get("id")))
    if not agent_windows:
        return None
    # Return the window ID of the highest-numbered agent
    agent_windows.sort(key=lambda x: x[0])
    return agent_windows[-1][1]


def _wait_for_session(name: str, timeout: float = 45.0) -> str | None:
    """Poll session list until a session with this name appears; return its session_id."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        session_id = _lookup_session_by_name(name)
        if session_id:
            return session_id
        time.sleep(2.0)
    return None


def _lookup_session_by_name(name: str) -> str | None:
    """Find a session_id by friendly name via the daemon HTTP API."""
    try:
        import urllib.request
        port = int(os.environ.get("KHIMAIRA_MONITOR_PORT", "8740"))
        url = f"http://127.0.0.1:{port}/api/sessions"
        with urllib.request.urlopen(url, timeout=3.0) as resp:
            sessions = json.loads(resp.read().decode())
        if isinstance(sessions, dict):
            sessions = sessions.get("sessions") or sessions.get("data") or []
        for s in sessions:
            if isinstance(s, dict) and s.get("name") == name:
                return s.get("session_id")
    except Exception:
        pass
    # Fallback: scan local session state files
    try:
        from khimaira.monitor import sessions as sessions_mod
        for row in sessions_mod.list_sessions(use_cache=False):
            if row.get("name") == name:
                return row.get("session_id")
    except Exception:
        pass
    return None


def _find_master_session_id(chat_id: str) -> str | None:
    """Find the current master's session_id in the chat."""
    try:
        from khimaira.monitor.chats import load_room, ROLE_MASTER
        room = load_room(chat_id)
        member_roles: dict[str, str] = room["meta"].get("member_roles") or {}
        for sid, role in member_roles.items():
            if role == ROLE_MASTER:
                return sid
    except Exception:
        pass
    return None


def _integrate_session(session_id: str, role: str, chat_id: str) -> None:
    """Invite, auto-accept, and role-bind a new session into the roster chat."""
    from khimaira.monitor.chats import invite, accept, chat_grant_role

    master_sid = _find_master_session_id(chat_id)
    if master_sid is None:
        raise ValueError(
            f"Cannot find master session for {chat_id!r}; "
            "role-bind requires a master caller."
        )

    # Invite the new session (adds as pending)
    invite(chat_id, by_session_id=master_sid, invitee_session_id=session_id)

    # Auto-accept on behalf of the new session
    # (spawn has operator authority; the new session accepts as part of the spawn flow)
    accept(chat_id, session_id=session_id)

    # Set the role
    chat_grant_role(
        chat_id,
        by_session_id=master_sid,
        target_session_id=session_id,
        role=role,
    )
