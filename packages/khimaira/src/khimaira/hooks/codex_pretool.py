"""khimaira Codex PreToolUse hook — Themis enforcement for Codex sessions.

2026-07-15: ports khimaira.hooks (scripts/hooks/themis_pretool.py)'s role-gated
tool blocking to Codex. Reuses the daemon's existing /api/themis/check
endpoint unmodified — resolve_session_role() there requires the caller's
session_id to be an ACCEPTED member of a chat with a role bound, so:

  - On its first hook invocation, each top-level Codex session discovers or
    creates its own roster chat with an explicit "master" role binding. The
    session_id -> chat_id mapping is cached locally, making later tool calls a
    local lookup rather than a daemon round-trip.

  - spawn_agent subagents have no khimaira session_id of their own — only
    Codex's internal agent_id, which never appears in any chat. To give
    Themis something real to resolve a role against, this hook provisions
    a lightweight "virtual" session per ROLE (deterministic uuid5 of
    parent_session_id + role) and invites+self-accepts it into the
    parent's roster chat with that role bound. This is legitimate
    provisioning (the hook controls both ends of a fabricated identity),
    not impersonation of a real peer. Cached locally after first use.

  - agent_id -> role resolution: spawn_agent's own tool response never
    reveals the assigned agent_id (confirmed empirically), and there's no
    list_agents-style query that maps the two either. The reliable link is
    the subagent's own rollout file: its FIRST record (session_meta)
    contains `agent_path` (the task_name-derived canonical address, e.g.
    "/root/gatekeeper") keyed to the exact same id PreToolUse hook
    payloads use as `agent_id`. Role is derived from agent_path by
    stripping a trailing _<digits> suffix (agent_1/agent_2 -> "agent").

Fail-open throughout, mirroring themis_pretool.py's own philosophy: Themis
is a guardrail, not a security gate — any daemon/lookup failure allows the
tool rather than blocking Codex's own operation.
"""

from __future__ import annotations

import fcntl
import glob
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

DAEMON = os.environ.get("THEMIS_DAEMON", "http://127.0.0.1:8740")
TIMEOUT_S = 1.0

_VIRTUAL_SESSION_NAMESPACE = uuid.UUID("6b1f6b6a-6e8b-4f6b-9c7c-3f6f9b6a6b1f")
_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "codex_watcher"
)
_VIRTUAL_SESSION_CACHE_PATH = _STATE_DIR / "virtual_sessions.json"
_ROSTER_CHAT_CACHE_PATH = _STATE_DIR / "roster_chats.json"
_ROSTER_CHAT_LOCK_PATH = _STATE_DIR / "roster_chats.lock"
_ROSTER_TITLE_PREFIX = "codex-themis-roster:"
_LEGACY_ROSTER_TITLE = "codex-master-roster"


def _fail_open(reason: str) -> None:
    sys.stderr.write(f"[codex-themis] fail-open: {reason}\n")
    sys.exit(0)


def _block(message: str) -> None:
    print(json.dumps({"decision": "block", "reason": message}))
    sys.exit(0)


def _http(method: str, path: str, body: dict | None = None) -> dict | None:
    try:
        data = json.dumps(body).encode() if body is not None else None
        req = Request(
            f"{DAEMON}{path}", data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        with urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.load(resp)
    except Exception:
        return None


def _load_roster_cache() -> dict[str, str]:
    try:
        raw = json.loads(_ROSTER_CHAT_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(raw, dict):
        return {}
    return {
        session_id: chat_id
        for session_id, chat_id in raw.items()
        if isinstance(session_id, str) and isinstance(chat_id, str)
    }


def _save_roster_cache(cache: dict[str, str]) -> None:
    try:
        _ROSTER_CHAT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _ROSTER_CHAT_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(_ROSTER_CHAT_CACHE_PATH)
    except Exception:
        pass


def _roster_title(session_id: str) -> str:
    return f"{_ROSTER_TITLE_PREFIX}{session_id}"


def _accepted_roster_chat(room: dict, session_id: str, title: str) -> str | None:
    meta = room.get("meta")
    members = room.get("members")
    if not isinstance(meta, dict) or not isinstance(members, dict):
        return None
    room_title = meta.get("title")
    owned_title = room_title == title and meta.get("created_by") == session_id
    legacy_title = (
        room_title == _LEGACY_ROSTER_TITLE
        and meta.get("created_by") == session_id
    )
    member_roles = meta.get("member_roles")
    member = members.get(session_id)
    chat_id = meta.get("chat_id")
    if (
        not (owned_title or legacy_title)
        or not isinstance(member_roles, dict)
        or member_roles.get(session_id) != "master"
        or not isinstance(member, dict)
        or member.get("state") != "accepted"
        or not isinstance(chat_id, str)
    ):
        return None
    return chat_id


def _ensure_roster_chat(session_id: str) -> str | None:
    """Discover or create the session's master-bound roster chat once."""
    cache = _load_roster_cache()
    if session_id in cache:
        return cache[session_id]

    try:
        _ROSTER_CHAT_LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(_ROSTER_CHAT_LOCK_PATH, "a+", encoding="utf-8") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

            # Another hook process may have populated the entry while this one
            # waited for the lock.
            cache = _load_roster_cache()
            if session_id in cache:
                return cache[session_id]

            title = _roster_title(session_id)
            listing = _http(
                "GET", f"/api/chats?session_id={quote(session_id, safe='')}"
            )
            if not isinstance(listing, dict) or not isinstance(
                listing.get("chats"), list
            ):
                return None

            for summary in listing["chats"]:
                if not isinstance(summary, dict):
                    continue
                if summary.get("my_state") != "accepted":
                    continue
                if summary.get("title") not in (title, _LEGACY_ROSTER_TITLE):
                    continue
                chat_id = summary.get("chat_id")
                if not isinstance(chat_id, str):
                    continue
                room = _http(
                    "GET",
                    f"/api/chats/{quote(chat_id, safe='')}"
                    f"?session_id={quote(session_id, safe='')}",
                )
                if not isinstance(room, dict):
                    continue
                accepted_chat_id = _accepted_roster_chat(room, session_id, title)
                if accepted_chat_id is not None:
                    cache[session_id] = accepted_chat_id
                    _save_roster_cache(cache)
                    return accepted_chat_id

            created = _http(
                "POST",
                "/api/chats",
                {
                    "creator_session_id": session_id,
                    "member_session_ids": [],
                    "title": title,
                    "fresh": True,
                    "member_roles": {session_id: "master"},
                    "allow_overlap": True,
                },
            )
            if not isinstance(created, dict):
                return None
            chat_id = _accepted_roster_chat(created, session_id, title)
            if chat_id is None:
                return None
            cache[session_id] = chat_id
            _save_roster_cache(cache)
            return chat_id
    except Exception:
        return None


def _derive_role_from_agent_path(agent_path: str) -> str | None:
    name = agent_path.rsplit("/", 1)[-1]
    base = re.sub(r"_\d+$", "", name)
    return base or None


def _resolve_agent_role(agent_id: str) -> str | None:
    """subagent agent_id -> role, via its rollout file's session_meta record."""
    home = Path(os.path.expanduser("~/.codex/sessions"))
    matches = glob.glob(str(home / "**" / f"rollout-*-{agent_id}.jsonl"), recursive=True)
    if not matches:
        return None
    try:
        with open(matches[0], "r", encoding="utf-8") as f:
            first_line = f.readline()
        rec = json.loads(first_line)
        agent_path = (rec.get("payload") or {}).get("agent_path") or ""
        if not agent_path:
            return None
        return _derive_role_from_agent_path(agent_path)
    except Exception:
        return None


def _load_virtual_cache() -> dict[str, str]:
    try:
        return json.loads(_VIRTUAL_SESSION_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_virtual_cache(cache: dict[str, str]) -> None:
    try:
        _VIRTUAL_SESSION_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = _VIRTUAL_SESSION_CACHE_PATH.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(cache), encoding="utf-8")
        tmp.replace(_VIRTUAL_SESSION_CACHE_PATH)
    except Exception:
        pass


def _ensure_virtual_session(parent_session_id: str, chat_id: str, role: str) -> str | None:
    """Return a khimaira session_id that resolves to `role` in `chat_id`,
    provisioning (invite + self-accept) it on first use. Cached by
    "parent:role" so repeat calls for the same role are a local dict lookup,
    not a repeat daemon round-trip.
    """
    cache_key = f"{parent_session_id}:{role}"
    cache = _load_virtual_cache()
    if cache_key in cache:
        return cache[cache_key]

    vsid = str(uuid.uuid5(_VIRTUAL_SESSION_NAMESPACE, cache_key))

    room = _http("GET", f"/api/chats/{chat_id}?session_id={parent_session_id}")
    member = (room or {}).get("members", {}).get(vsid)

    if member is None:
        # Doesn't exist at all — invite it fresh.
        invited = _http(
            "POST", f"/api/chats/{chat_id}/invite",
            {"by_session_id": parent_session_id, "invitee_session_id": vsid, "role": role},
        )
        if invited is None:
            return None
        member_state = "pending"
    else:
        member_state = member.get("state")

    if member_state != "accepted":
        # Already invited (pending) or freshly invited above — re-inviting an
        # existing member 404s ("already a member"), so this branch must go
        # straight to accept, never back through invite.
        accepted = _http("POST", f"/api/chats/{chat_id}/accept", {"session_id": vsid})
        if accepted is None:
            return None

    cache[cache_key] = vsid
    _save_virtual_cache(cache)
    return vsid


def main() -> None:
    try:
        payload = json.loads(sys.stdin.read())
    except Exception as exc:
        _fail_open(f"stdin parse failed: {exc}")
        return

    session_id: str = payload.get("session_id") or ""
    tool_name: str = payload.get("tool_name", "")
    tool_input: dict = payload.get("tool_input", {})
    cwd: str = payload.get("cwd", "")
    agent_id: str | None = payload.get("agent_id")

    if not session_id or not tool_name:
        _fail_open(f"missing session_id or tool_name — session_id={session_id!r} tool={tool_name!r}")
        return

    roster_chat_id = _ensure_roster_chat(session_id)
    if roster_chat_id is None:
        _fail_open(f"could not discover or create roster chat for session_id={session_id[:12]}")
        return

    themis_session_id = session_id
    if agent_id:
        role = _resolve_agent_role(agent_id)
        if role is None:
            _fail_open(f"could not resolve role for agent_id={agent_id[:12]}")
            return
        vsid = _ensure_virtual_session(session_id, roster_chat_id, role)
        if vsid is None:
            _fail_open(f"could not provision virtual session for role={role}")
            return
        themis_session_id = vsid

    try:
        body = json.dumps(
            {
                "session_id": themis_session_id,
                "tool_name": tool_name,
                "tool_input": tool_input,
                "cwd": cwd,
                "recent_tool_calls": [],
            }
        ).encode()
        req = Request(
            f"{DAEMON}/api/themis/check",
            data=body,
            headers={"Content-Type": "application/json", "X-Session-ID": themis_session_id},
        )
        with urlopen(req, timeout=TIMEOUT_S) as resp:
            verdict = json.load(resp)
    except URLError as exc:
        _fail_open(f"daemon unreachable: {exc}")
        return
    except TimeoutError as exc:
        _fail_open(f"daemon timeout: {exc}")
        return
    except Exception as exc:
        _fail_open(f"daemon /api/themis/check failed: {exc}")
        return

    if not isinstance(verdict, dict):
        _fail_open(f"malformed daemon response: {verdict!r}")
        return

    if verdict.get("ok"):
        sys.exit(0)

    violation = verdict.get("violation") or {}
    if violation.get("severity") == "block":
        _block(violation.get("message", "rule violated"))
    else:
        sys.exit(0)


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        _fail_open(f"unhandled exception: {exc}")
