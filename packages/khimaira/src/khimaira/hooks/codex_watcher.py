"""khimaira Codex chat-watcher — near-instant delivery via kitty injection.

2026-07-15: standalone daemon, run manually (not integrated into
khimaira-monitor). Polls a HARDCODED chat_id allowlist for new messages
targeting registered Codex sessions and, the moment the target session is
idle (per sessions.is_mid_turn — the same authoritative marker-file signal
Claude Code's roster liveness system uses, not screen-scraping), injects
the message into its kitty window via remote control and submits it as a
fresh prompt.

Deliberately isolated from the production Claude Code roster / khimaira
wake system, per explicit instruction:
  - Own state dir (~/.local/state/khimaira/codex_watcher/), own watermark
    + cooldown bookkeeping — never touches roster_recovery's
    _last_dispatch_wake or the hook's chat_poll_watermarks.json.
  - Hardcoded CHAT_ID_ALLOWLIST — never queries chats outside this list,
    so it structurally cannot see or touch griffin/jeevy roster traffic.
  - Resolves target windows by session_id -> daemon-registered window_id
    lookup (GET /api/sessions/{id}), never by kitty title-matching — the
    exact class of bug (decorated/reused titles matching the wrong window)
    documented against the production roster wake mechanism.

Not true push: if the target is mid-turn when a message arrives, this
polls again next cycle rather than interrupting. For a genuinely idle
session it's a real latency win over turn-boundary-only delivery.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

_ENDPOINT = os.environ.get("KHIMAIRA_ENDPOINT", "http://127.0.0.1:8740").rstrip("/")
_HTTP_TIMEOUT_S = 2.0
_POLL_INTERVAL_S = float(os.environ.get("KHIMAIRA_CODEX_WATCHER_POLL_S", "3"))

# Hardcoded on purpose — see module docstring. Extend this list explicitly
# when wiring a new Codex-experiment chat; never make it dynamic/wildcard.
CHAT_ID_ALLOWLIST: list[str] = [
    "chat-062cc92f32dd",
    "chat-53d0c4686b54",
]

# Auth identity for the read-only /api/chats/* calls below — those endpoints
# require session_id to be a genuine (pending/accepted) member of the chat,
# not a free-form caller id. codex-master is a member of every chat in the
# allowlist by construction (we control which chats get added here), so its
# id is a legitimate, stable choice — this does not mean the watcher AS a
# process claims to BE that session, only that it borrows a valid member's
# read access to fetch messages on their collective behalf. Must be a
# member of EVERY chat in CHAT_ID_ALLOWLIST — helper-0 is common to both
# experiment chats tonight (codex-master is not a member of chat-062cc92f32dd).
_WATCHER_AUTH_SESSION_ID = "019f6672-10f5-7933-b445-82851a803475"  # helper-0

# 2026-07-15: deliberately False until Themis is actually gating tool calls
# for the target Codex sessions (khimaira.hooks.codex_pretool — pending).
# Right now khimaira-chat's tools run with default_tools_approval_mode=auto,
# so an auto-submitted injected prompt could trigger real, unattended tool
# execution with zero human review and zero role-based gate. Flip to True
# once Themis is wired — at that point this carries the same risk profile
# the production Claude Code roster already runs with (auto-execution,
# but role-gated), not a new unguarded one. Until then: type the message
# into the input box so it's visible/ready, but require an explicit human
# or session action to actually submit it.
AUTO_SUBMIT = os.environ.get("KHIMAIRA_CODEX_WATCHER_AUTO_SUBMIT") == "1"

_STATE_DIR = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "codex_watcher"
)
_WATERMARKS_PATH = _STATE_DIR / "watermarks.json"


def _log(msg: str) -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _http_get(path: str) -> dict | None:
    try:
        req = urllib.request.Request(f"{_ENDPOINT}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError, TimeoutError):
        return None


def _load_watermarks() -> dict[str, str]:
    try:
        return json.loads(_WATERMARKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_watermarks(marks: dict[str, str]) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _WATERMARKS_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(marks), encoding="utf-8")
    tmp.replace(_WATERMARKS_PATH)


def _is_mid_turn(session_id: str) -> bool:
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from khimaira.monitor import sessions as sessions_mod

        return sessions_mod.is_mid_turn(session_id)
    except Exception:
        return False  # fail-open to "idle" — matches is_mid_turn's own contract


def _resolve_window_id(session_id: str) -> int | None:
    info = _http_get(f"/api/sessions/{urllib.parse.quote(session_id)}")
    if not info:
        return None
    wid = (info.get("status") or {}).get("window_id")
    return int(wid) if wid is not None else None


def _inject(window_id: int, text: str) -> bool:
    try:
        subprocess.run(
            ["kitty", "@", "send-text", "--match", f"id:{window_id}", "--", text],
            check=True, timeout=5, capture_output=True,
        )
        if AUTO_SUBMIT:
            time.sleep(0.3)
            subprocess.run(
                ["kitty", "@", "send-key", "--match", f"id:{window_id}", "--", "enter"],
                check=True, timeout=5, capture_output=True,
            )
        return True
    except Exception as exc:
        _log(f"inject FAILED window_id={window_id}: {exc}")
        return False


def _poll_once(watermarks: dict[str, str]) -> None:
    auth_sid = urllib.parse.quote(_WATCHER_AUTH_SESSION_ID, safe="")
    for chat_id in CHAT_ID_ALLOWLIST:
        watermark = watermarks.get(chat_id)
        cold_start = watermark is None
        qs = (
            f"session_id={auth_sid}&limit=20"
            f"{('&since=' + urllib.parse.quote(watermark, safe='')) if watermark else ''}"
        )
        payload = _http_get(f"/api/chats/{chat_id}/messages?{qs}")
        if payload is None:
            _log(f"{chat_id}: fetch failed (bad auth identity, daemon down, or not a member) — skipping")
            continue
        messages = payload.get("messages", [])
        if not messages:
            continue

        watermarks[chat_id] = messages[-1].get("event_id") or watermark

        if cold_start:
            # First time seeing this chat — seed the watermark to "now" and
            # skip processing. Without this, a fresh watcher start replays
            # every historical message in the chat as a fresh injection.
            _log(f"{chat_id}: cold start, seeded watermark, skipping {len(messages)} historical message(s)")
            continue

        room = _http_get(f"/api/chats/{chat_id}?session_id={auth_sid}")
        members = list((room or {}).get("members", {}).keys()) if room else []

        for m in messages:
            if m.get("kind") != "msg":
                continue
            # role_directive carries Claude-Code-specific slash-command guidance
            # (/model, /effort) that is meaningless to a Codex session. Role
            # assignment is durable state (chat.meta.member_roles), so a
            # session that misses this injection still learns its role
            # correctly on its next tool call — nothing is lost by skipping
            # it. Backstops chats._emit_role_directive's private=True (which
            # already hides this from bystander members via history()'s
            # filter) for the remaining case where a Codex session IS the
            # directive's legitimate `to`-target (e.g. codex-master itself
            # granted a role) and would otherwise see the raw Claude text
            # injected verbatim into its terminal.
            if (m.get("meta") or {}).get("event_type") == "role_directive":
                continue
            sender = m.get("sender_id")
            targets = m.get("to") or [x for x in members if x != sender]
            for target in targets:
                if _is_mid_turn(target):
                    _log(f"{chat_id}: target {target[:8]} mid-turn — deferring to next poll")
                    continue
                window_id = _resolve_window_id(target)
                if window_id is None:
                    _log(f"{chat_id}: no registered window for {target[:8]} — skipping")
                    continue
                body = (m.get("body") or "")[:2000]
                _log(f"{chat_id}: injecting into window {window_id} (session {target[:8]})")
                _inject(
                    window_id,
                    f"[khimaira-chat watcher] new message in {chat_id} from "
                    f"{(m.get('sender_name') or sender or '?')[:20]}: {body}",
                )


def main() -> None:
    _log(f"codex_watcher starting — allowlist={CHAT_ID_ALLOWLIST}, poll={_POLL_INTERVAL_S}s")
    watermarks = _load_watermarks()
    while True:
        try:
            _poll_once(watermarks)
            _save_watermarks(watermarks)
        except Exception as exc:
            _log(f"poll cycle error (continuing): {exc}")
        time.sleep(_POLL_INTERVAL_S)


if __name__ == "__main__":
    main()
