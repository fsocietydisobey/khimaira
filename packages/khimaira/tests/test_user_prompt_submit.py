"""Tests for khimaira.hooks.user_prompt_submit — focused on _poll_missed_chat_events."""

from __future__ import annotations

import importlib
import io
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


SESSION_ID = "aaaaaaaa-0000-0000-0000-000000000001"
CHAT_ID = "chat-dfa8121d87b9"


@pytest.fixture
def hook_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    import khimaira.hooks.user_prompt_submit as m

    importlib.reload(m)
    return m


def _mock_urlopen(responses: list[dict]):
    """Return a context-manager mock that yields successive JSON payloads."""
    calls = iter(responses)

    class _Resp:
        def __init__(self, payload):
            self._data = json.dumps(payload).encode()

        def read(self):
            return self._data

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

    def _urlopen(req, timeout=None):
        return _Resp(next(calls))

    return _urlopen


def test_poll_missed_chat_events_empty(hook_module):
    """No chats → empty string returned, no crash."""
    with patch(
        "khimaira.hooks.user_prompt_submit.urllib.request.urlopen",
        side_effect=_mock_urlopen([{"chats": []}]),
    ):
        result = hook_module._poll_missed_chat_events(SESSION_ID)
    assert result == ""


def test_poll_missed_chat_events_formats_correctly(hook_module):
    """Two new messages from another session → correct formatted block."""
    from datetime import datetime, timezone, timedelta

    now = datetime.now(timezone.utc)
    ts1 = (now - timedelta(minutes=2)).isoformat()
    ts2 = (now - timedelta(minutes=1)).isoformat()

    chats_payload = {
        "chats": [{"chat_id": CHAT_ID, "title": "test chat", "my_state": "accepted"}]
    }
    messages_payload = {
        "messages": [
            {
                "kind": "msg",
                "event_id": "evt-001",
                "sender_id": "bbbbbbbb-0000-0000-0000-000000000002",
                "sender_name": "agent-2",
                "ts": ts1,
                "body": "first message",
            },
            {
                "kind": "msg",
                "event_id": "evt-002",
                "sender_id": "cccccccc-0000-0000-0000-000000000003",
                "sender_name": "agent-3",
                "ts": ts2,
                "body": "second message",
            },
        ]
    }

    with patch(
        "khimaira.hooks.user_prompt_submit.urllib.request.urlopen",
        side_effect=_mock_urlopen([chats_payload, messages_payload]),
    ):
        result = hook_module._poll_missed_chat_events(SESSION_ID)

    assert f"💬 MISSED CHAT EVENTS — {CHAT_ID} (2 new)" in result
    assert "agent-2" in result
    assert "first message" in result
    assert "agent-3" in result
    assert "second message" in result


# ---------------------------------------------------------------------------
# Task #66 — dynamic per-prompt context injection
# ---------------------------------------------------------------------------


def test_classify_prompt_simple(hook_module):
    """Short interrogative lookups → 'simple' (the only class that strips
    ambient reminders)."""
    m = hook_module
    assert m._classify_prompt("what is a closure?") == "simple"
    assert m._classify_prompt("what time is it?") == "simple"
    assert m._classify_prompt("how does memoization work?") == "simple"


def test_classify_prompt_architecture(hook_module):
    m = hook_module
    assert m._classify_prompt("how should we design the auth module?") == "architecture"
    assert (
        m._classify_prompt("what's the best way to structure this refactor?")
        == "architecture"
    )
    assert m._classify_prompt("walk me through the module boundaries here") == "architecture"


def test_classify_prompt_bugfix(hook_module):
    m = hook_module
    assert m._classify_prompt("the login flow throws a TypeError on submit") == "bugfix"
    assert m._classify_prompt("fix the failing test in sessions.py") == "bugfix"
    assert m._classify_prompt("there's a regression in the resolver") == "bugfix"


def test_classify_prompt_coordination_and_default(hook_module):
    m = hook_module
    # Explicit coordination keywords.
    assert m._classify_prompt("delegate this to agent-2 in the roster") == "coordination"
    # Non-trivial work with no clear class → safe default (full context).
    assert (
        m._classify_prompt(
            "implement the new pagination endpoint with cursor support "
            "and add integration tests covering the empty and overflow cases"
        )
        == "coordination"
    )


def test_classify_prompt_channel_event_and_empty_are_coordination(hook_module):
    """Channel-only roster events + empty prompts must never be 'simple'
    (their context must not be stripped)."""
    m = hook_module
    channel = (
        '<channel source="khimaira-chat" chat_id="chat-x" sender="agent-1">'
        "done</channel>"
    )
    assert m._classify_prompt(channel) == "coordination"
    assert m._classify_prompt("") == "coordination"
    assert m._classify_prompt("   ") == "coordination"


def _run_main(hook_module, prompt: str, monkeypatch, counter_value: int = 5) -> str:
    """Drive main() with `prompt`, stubbing all daemon-backed blocks empty
    except a fake role-budget block. Returns the injected additionalContext
    string (or "" when the hook emits nothing)."""
    m = hook_module
    sid = SESSION_ID

    # Pre-seed the per-session counter so this isn't turn 1 (turn-1-only blocks
    # would otherwise fire) and isn't a reminder turn (counter_value+1 % 8 != 0).
    safe = sid.replace("/", "_").replace("..", "_")
    counter_file = m._COUNTER_DIR / f"{safe}.count"
    counter_file.parent.mkdir(parents=True, exist_ok=True)
    counter_file.write_text(str(counter_value), encoding="utf-8")

    monkeypatch.setattr(m, "_sync_rename_to_khimaira", lambda *a, **k: None)
    monkeypatch.setattr(m, "_fetch_pending_notes", lambda *a, **k: [])
    monkeypatch.setattr(m, "_fetch_incoming_questions", lambda *a, **k: [])
    monkeypatch.setattr(m, "_poll_missed_chat_events", lambda *a, **k: "")
    monkeypatch.setattr(m, "_discover_pending_assignments", lambda *a, **k: [])
    monkeypatch.setattr(m, "_discover_unfired_acks", lambda *a, **k: [])
    monkeypatch.setattr(m, "_check_stale_acks", lambda *a, **k: [])
    monkeypatch.setattr(m, "_check_bottleneck", lambda *a, **k: "")

    # role_budget path imports these from session_start at call time.
    import khimaira.hooks.session_start as ss

    monkeypatch.setattr(
        ss, "_discover_chat_roles", lambda sid_: [{"chat_id": "chat-x", "role": "master"}]
    )
    monkeypatch.setattr(
        ss, "_format_chat_roles", lambda roles: "🎚️ ROLE BUDGET: master → /model opus"
    )

    payload = json.dumps({"session_id": sid, "prompt": prompt, "cwd": ""})
    monkeypatch.setattr("sys.stdin", io.StringIO(payload))
    buf = io.StringIO()
    monkeypatch.setattr("sys.stdout", buf)

    m.main()
    out = buf.getvalue()
    if not out:
        return ""
    return json.loads(out)["hookSpecificOutput"]["additionalContext"]


def test_main_coordination_prompt_includes_role_budget(hook_module, monkeypatch):
    """DoD: a coordination prompt keeps roster state (role-budget block)."""
    ctx = _run_main(hook_module, "delegate the pagination task to agent-2 in the roster", monkeypatch)
    assert "ROLE BUDGET" in ctx


def test_main_simple_prompt_suppresses_role_budget(hook_module, monkeypatch):
    """DoD: a simple prompt does NOT get the roster-state dump."""
    ctx = _run_main(hook_module, "what is a closure?", monkeypatch)
    assert "ROLE BUDGET" not in ctx


def test_main_architecture_prompt_injects_context_pointer(hook_module, monkeypatch):
    """Architecture prompts surface a relevant-context pointer (and keep
    roster state — only 'simple' suppresses)."""
    ctx = _run_main(hook_module, "how should we design the module boundaries here?", monkeypatch)
    assert "architecture/design prompt" in ctx
    assert "CLAUDE.md" in ctx
    assert "ROLE BUDGET" in ctx


def test_main_dynamic_context_opt_out_restores_full_behavior(hook_module, monkeypatch):
    """KHIMAIRA_DYNAMIC_CONTEXT=0 → pre-#66 behavior: even a simple prompt
    keeps the full context (no suppression)."""
    monkeypatch.setenv("KHIMAIRA_DYNAMIC_CONTEXT", "0")
    ctx = _run_main(hook_module, "what is a closure?", monkeypatch)
    assert "ROLE BUDGET" in ctx


# ---------------------------------------------------------------------------
# #14b — un-missable BEGIN banner: _discover_begun_not_started
# ---------------------------------------------------------------------------


def _write_chat_jsonl(chats_dir: Path, chat_id: str, records: list[dict]) -> None:
    chats_dir.mkdir(parents=True, exist_ok=True)
    path = chats_dir / f"{chat_id}.jsonl"
    with path.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def _make_task_signal_begin_scenario(
    tmp_path: Path,
    session_id: str,
    task_id: str,
    *,
    signal_fired: bool = True,
    task_in_progress: bool = False,
) -> Path:
    """Write minimal JSONL for a BEGUN-not-started scenario."""
    state_root = tmp_path / "state"
    chats_dir = state_root / "khimaira" / "chats"
    records = [
        {
            "kind": "meta",
            "chat_id": "chat-test-begun",
            "title": "test",
            "event_id": "e1",
            "ts": "2026-01-01T00:00:00+00:00",
        },
        {
            "kind": "task",
            "id": task_id,
            "chat_id": "chat-test-begun",
            "assignee_id": session_id,
            "assignee_name": "agent-3",
            "sender_id": "master-uuid",
            "sender_name": "master",
            "body": "implement the feature",
            "status": "pending",
            "event_id": "e2",
            "ts": "2026-01-01T00:01:00+00:00",
        },
    ]
    if signal_fired:
        records.append({
            "kind": "task_signal",
            "task_id": task_id,
            "chat_id": "chat-test-begun",
            "signal": "start",
            "event_id": "e3",
            "ts": "2026-01-01T00:02:00+00:00",
        })
    if task_in_progress:
        records.append({
            "kind": "task_update",
            "task_id": task_id,
            "chat_id": "chat-test-begun",
            "status": "in_progress",
            "event_id": "e4",
            "ts": "2026-01-01T00:03:00+00:00",
        })
    _write_chat_jsonl(chats_dir, "chat-test-begun", records)
    return state_root


def test_discover_begun_not_started_fires_when_begin_not_acked(
    hook_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC-1: TASK_SIGNAL start fired + status=pending → returned as begun-not-started."""
    task_id = "task-aabbccdd1234"
    state_root = _make_task_signal_begin_scenario(
        tmp_path, SESSION_ID, task_id, signal_fired=True, task_in_progress=False
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    importlib.reload(hook_module)

    results = hook_module._discover_begun_not_started(SESSION_ID)
    assert len(results) == 1
    assert results[0]["task_id"] == task_id


def test_discover_begun_not_started_silent_when_in_progress(
    hook_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC-2: agent marked in_progress → banner stops (task not returned)."""
    task_id = "task-aabbccdd5678"
    state_root = _make_task_signal_begin_scenario(
        tmp_path, SESSION_ID, task_id, signal_fired=True, task_in_progress=True
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    importlib.reload(hook_module)

    results = hook_module._discover_begun_not_started(SESSION_ID)
    assert results == []


def test_discover_begun_not_started_silent_when_no_begin_signal(
    hook_module, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """AC-3: task assigned but BEGIN not yet fired → not returned (still pending banner)."""
    task_id = "task-aabbccdd9999"
    state_root = _make_task_signal_begin_scenario(
        tmp_path, SESSION_ID, task_id, signal_fired=False, task_in_progress=False
    )
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))
    importlib.reload(hook_module)

    results = hook_module._discover_begun_not_started(SESSION_ID)
    assert results == []


def test_format_begun_not_started_names_task_id(hook_module):
    """AC-5: banner names the task-id."""
    tasks = [{"task_id": "task-abcdef123456", "task_body": "do the thing", "signal_ts": ""}]
    banner = hook_module._format_begun_not_started(tasks)
    assert "task-abcdef123456" in banner
    assert "START NOW" in banner
    assert "in_progress" in banner


def test_format_begun_not_started_empty(hook_module):
    """Empty list → empty string."""
    assert hook_module._format_begun_not_started([]) == ""
