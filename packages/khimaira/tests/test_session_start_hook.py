"""Tests for khimaira.hooks.session_start — HTTP-primary, file fallback.

The hook used to maintain file-direct duplicates of daemon logic
(_consume_inbox, _consume_handoffs, _discover_other_active_sessions).
Each daemon-side change required a parallel hook update — 2 bugs in 24h
came from this drift. The refactor prefers HTTP and only falls back to
file-direct ops when the daemon is unreachable.

These tests verify both paths:
  - HTTP path: with the daemon responding, hook calls the daemon, period
  - Fallback: when daemon is unreachable, the file-direct path still
    archives inbox correctly + applies the target-session filter on
    handoffs.

The hook now lives inside the khimaira package (khimaira.hooks.session_start)
rather than as a top-level script at workspace root, so tests can do a
plain `import` — no path hackery — and the module ships in wheel
installs without separate file copying.
"""

from __future__ import annotations

import importlib
import os
from pathlib import Path
from unittest.mock import patch

import pytest


@pytest.fixture
def hook_module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Import khimaira.hooks.session_start with XDG_STATE_HOME isolated.

    Reloaded per-test so module-level path constants pick up the env var.
    """
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.hooks import session_start as mod

    importlib.reload(mod)
    yield mod
    # Reload one more time post-test so the module's path constants
    # don't carry the tmp_path state into other tests in the suite.
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(mod)


def test_consume_inbox_uses_http_when_available(hook_module):
    """Happy path: HTTP succeeds → hook returns daemon's response, never
    touches the filesystem."""
    fake_notes = [{"id": "abc", "text": "hello"}]
    with patch.object(
        hook_module, "_http_get_json", return_value={"notes": fake_notes}
    ) as mock_http:
        result = hook_module._consume_inbox("sess-1")

    assert result == fake_notes
    mock_http.assert_called_once()
    assert "/api/sessions/sess-1/pending?mark_read=true" in mock_http.call_args[0][0]


def test_consume_inbox_falls_back_to_file_when_daemon_down(hook_module):
    """Daemon down (HTTP returns None) → fall back to direct file drain."""
    # Pre-populate inbox.jsonl with an unread note
    sid = "sess-fallback"
    inbox = hook_module._session_dir(sid) / "inbox.jsonl"
    inbox.parent.mkdir(parents=True, exist_ok=True)
    inbox.write_text(
        '{"id":"n1","text":"fallback test","read":false,"surface_count":0}\n',
        encoding="utf-8",
    )

    with patch.object(hook_module, "_http_get_json", return_value=None):
        result = hook_module._consume_inbox(sid)

    assert len(result) == 1
    assert result[0]["id"] == "n1"
    # Inbox should have been atomically rewritten to empty
    assert inbox.read_text(encoding="utf-8") == ""
    # Archive should have the drained note
    archive = hook_module._session_dir(sid) / "archive.jsonl"
    assert archive.exists()
    assert "n1" in archive.read_text(encoding="utf-8")


def test_consume_handoffs_uses_http_when_available(hook_module):
    fake_handoffs = [{"id": "hand1", "text": "do thing", "_claim_role": "owner"}]
    with patch.object(
        hook_module,
        "_http_get_json",
        return_value={"handoffs": fake_handoffs},
    ) as mock_http:
        result = hook_module._consume_handoffs("sess-1", "/some/cwd")

    assert result == fake_handoffs
    mock_http.assert_called_once()
    call_url = mock_http.call_args[0][0]
    assert "/api/handoffs/consume" in call_url
    assert "session_id=sess-1" in call_url
    assert "cwd=" in call_url


def test_consume_handoffs_fallback_applies_target_filter(hook_module, tmp_path):
    """Regression: targeted invites must NOT surface on peer sessions even
    on the fallback path. This is the bug the addendum warned about."""
    project = tmp_path / "p"
    project.mkdir()
    project_str = os.path.abspath(str(project))

    # Construct a targeted handoff manually
    import time as time_mod

    hook_module._HANDOFFS_PATH.parent.mkdir(parents=True, exist_ok=True)
    handoff = {
        "id": "targeted1",
        "ts": "2026-05-11T00:00:00Z",
        "from_session_id": "owner",
        "text": "for invitee only",
        "scope_cwd": project_str,
        "target_session_id": "invitee-only",
        "expires_at": time_mod.time() + 3600,
        "read_by": [],
    }
    import json as json_mod

    with hook_module._HANDOFFS_PATH.open("w", encoding="utf-8") as f:
        f.write(json_mod.dumps(handoff) + "\n")

    # Peer session consumes — should NOT see the invite (target filter)
    with patch.object(hook_module, "_http_get_json", return_value=None):
        peer = hook_module._consume_handoffs("some-peer", project_str)
    assert peer == []

    # The named invitee consumes — should see it
    with patch.object(hook_module, "_http_get_json", return_value=None):
        invitee = hook_module._consume_handoffs("invitee-only", project_str)
    assert len(invitee) == 1
    assert invitee[0]["id"] == "targeted1"


def test_discover_uses_http_when_available(hook_module):
    """list_sessions HTTP path returns the daemon's cached digest."""
    fake = {
        "sessions": [
            {
                "session_id": "other-sess",
                "last_active_age_s": 10,
                "status": {"status": "implementing"},
                "decision_count": 3,
                "file_touch_count": 5,
                "open_question_count": 0,
            },
            {
                "session_id": "myself",  # should be filtered
                "last_active_age_s": 1,
                "status": None,
                "decision_count": 0,
                "file_touch_count": 0,
                "open_question_count": 0,
            },
            {
                "session_id": "stale-sess",  # too old, filtered
                "last_active_age_s": 9999,
                "status": None,
                "decision_count": 0,
                "file_touch_count": 0,
                "open_question_count": 0,
            },
        ]
    }
    with patch.object(hook_module, "_http_get_json", return_value=fake):
        result = hook_module._discover_other_active_sessions(
            "myself", within_minutes=30
        )

    assert len(result) == 1
    assert result[0]["session_id"] == "other-sess"
    assert result[0]["decision_count"] == 3


# ---------------------------------------------------------------------------
# _inject_domain_memory
# ---------------------------------------------------------------------------


def test_inject_domain_memory_returns_block_for_lead_session(hook_module, tmp_path):
    """Lead session with mnemosyne data → PROVISIONAL block returned."""
    fake_session = {"name": "backend-lead-1"}
    fake_query = {
        "domain": "khimaira:backend",
        "answer": "# Domain memory — khimaira:backend (2 of 2 pairs)\nQ: q\nA: a",
        "training_pairs_available": 2,
    }

    with patch.object(hook_module, "_http_get_json", return_value=fake_session):
        with patch(
            "khimaira.hooks.session_start._mnemosyne_query", return_value=fake_query
        ):
            with patch(
                "khimaira.hooks.session_start.detect_project", return_value="khimaira"
            ):
                result = hook_module._inject_domain_memory("sess-abc", str(tmp_path))

    assert "PROVISIONAL" in result
    assert "khimaira:backend" in result
    assert "2 pair" in result
    assert "Q: q" in result


def test_inject_domain_memory_returns_empty_for_non_lead(hook_module, tmp_path):
    """Non-lead session name → empty string, no mnemosyne call."""
    fake_session = {"name": "khimaira-0"}

    with patch.object(hook_module, "_http_get_json", return_value=fake_session):
        with patch("khimaira.hooks.session_start._mnemosyne_query") as mock_query:
            result = hook_module._inject_domain_memory("sess-abc", str(tmp_path))

    assert result == ""
    mock_query.assert_not_called()


def test_inject_domain_memory_returns_empty_when_session_unnamed(hook_module, tmp_path):
    """Daemon returns no name (fresh session) → empty string."""
    with patch.object(hook_module, "_http_get_json", return_value={"name": ""}):
        with patch("khimaira.hooks.session_start._mnemosyne_query") as mock_query:
            result = hook_module._inject_domain_memory("sess-abc", str(tmp_path))

    assert result == ""
    mock_query.assert_not_called()


def test_inject_domain_memory_returns_empty_when_mnemosyne_down(hook_module, tmp_path):
    """mnemosyne unavailable (query returns None) → empty string, fail-open."""
    fake_session = {"name": "data-lead-2"}

    with patch.object(hook_module, "_http_get_json", return_value=fake_session):
        with patch("khimaira.hooks.session_start._mnemosyne_query", return_value=None):
            with patch(
                "khimaira.hooks.session_start.detect_project", return_value="khimaira"
            ):
                result = hook_module._inject_domain_memory("sess-abc", str(tmp_path))

    assert result == ""


def test_inject_domain_memory_fail_open_on_exception(hook_module, tmp_path):
    """Any unexpected exception in injection → empty string, never raises."""
    with patch.object(hook_module, "_http_get_json", side_effect=RuntimeError("boom")):
        result = hook_module._inject_domain_memory("sess-abc", str(tmp_path))

    assert result == ""


# ---------------------------------------------------------------------------
# _format_handoffs caps — the surfaces must stay bounded no matter how the
# data layer misbehaves (2026-06-06: 412 stale guard handoffs → 118KB block,
# ~30k tokens injected into every boot in the project).
# ---------------------------------------------------------------------------


def _mk_handoff(i: int, role: str) -> dict:
    return {
        "id": f"{i:012x}",
        "ts": f"2026-06-06T10:{i % 60:02d}:00",
        "from_session_id": "sender-uuid",
        "text": f"handoff number {i}",
        "_claim_role": role,
    }


def test_format_handoffs_caps_observed_at_5(hook_module):
    observed = [_mk_handoff(i, "observer") for i in range(40)]
    out = hook_module._format_handoffs(observed, "/proj")
    # Header reports the TRUE total; body renders only the newest 5.
    assert "40 ALREADY-CLAIMED" in out
    assert out.count("- [handoff ") == 5
    assert "+35 more already-claimed" in out


def test_format_handoffs_caps_owned_at_10(hook_module):
    owned = [_mk_handoff(i, "owner") for i in range(25)]
    out = hook_module._format_handoffs(owned, "/proj")
    assert "25 directive(s)" in out
    assert out.count("- [handoff ") == 10
    assert "+15 more owned" in out


def test_format_handoffs_no_overflow_line_under_cap(hook_module):
    owned = [_mk_handoff(i, "owner") for i in range(3)]
    observed = [_mk_handoff(100 + i, "observer") for i in range(2)]
    out = hook_module._format_handoffs(owned + observed, "/proj")
    assert "more owned" not in out
    assert "more already-claimed" not in out
    assert out.count("- [handoff ") == 5  # 3 owned + 2 observed


def test_format_handoffs_caps_keep_newest(hook_module):
    """The newest entries (by ts) survive the cap, not the oldest."""
    observed = [_mk_handoff(i, "observer") for i in range(10)]
    out = hook_module._format_handoffs(observed, "/proj")
    # ts minute = i % 60 → newest are 9..5; oldest 0..4 must be cut
    assert "handoff number 9" in out
    assert "handoff number 0" not in out


# ---------------------------------------------------------------------------
# Role-gated handoff consumption — workers must not consume (and therefore
# cannot auto-claim) handoffs. Handoffs are context for the coordinator.
# ---------------------------------------------------------------------------


def _run_main(hook_module, monkeypatch, capsys, *, chat_roles):
    """Drive main() with stubbed collaborators; return parsed additionalContext."""
    import io
    import json as _json

    monkeypatch.setattr(
        "sys.stdin", io.StringIO(_json.dumps({"session_id": "sess-x", "cwd": "/proj"}))
    )
    consume_calls: list[str] = []

    def _fake_consume(session_id, cwd):
        consume_calls.append(session_id)
        return [_mk_handoff(1, "owner")]

    with (
        patch.object(hook_module, "_post_ppid_bridge", lambda *a, **k: None, create=True),
        patch.object(hook_module, "_ensure_chat_mcp_registered", lambda: None),
        patch.object(hook_module, "_consume_inbox", lambda *a, **k: None),
        patch.object(hook_module, "_discover_other_active_sessions", lambda *a, **k: None),
        patch.object(hook_module, "_discover_chat_roles", lambda *a, **k: chat_roles),
        patch.object(hook_module, "_consume_handoffs", _fake_consume),
        patch.object(hook_module, "_fetch_hook_safe_tasks", lambda *a, **k: None),
        patch.object(hook_module, "_inject_domain_memory", lambda *a, **k: None),
        patch.object(hook_module, "_http_post_json", lambda *a, **k: None),
    ):
        rc = hook_module.main()
    assert rc == 0
    out = capsys.readouterr().out
    ctx = _json.loads(out)["hookSpecificOutput"]["additionalContext"] if out.strip() else ""
    return ctx, consume_calls


def test_worker_role_skips_handoff_consume(hook_module, monkeypatch, capsys):
    """agent-role session: _consume_handoffs is NEVER called (no claim, no bloat)."""
    ctx, calls = _run_main(
        hook_module, monkeypatch, capsys, chat_roles=[{"role": "agent", "chat_id": "c1"}]
    )
    assert calls == [], "worker boot must not consume handoffs"
    assert "khimaira handoffs" not in ctx


def test_master_role_consumes_handoffs(hook_module, monkeypatch, capsys):
    ctx, calls = _run_main(
        hook_module, monkeypatch, capsys, chat_roles=[{"role": "master", "chat_id": "c1"}]
    )
    assert calls == ["sess-x"]
    assert "khimaira handoffs" in ctx


def test_roleless_session_consumes_handoffs(hook_module, monkeypatch, capsys):
    """Solo sessions (no chat role) keep the full handoff surface."""
    ctx, calls = _run_main(hook_module, monkeypatch, capsys, chat_roles=None)
    assert calls == ["sess-x"]
    assert "khimaira handoffs" in ctx


def test_format_inbox_caps_count_and_answer_length(hook_module):
    notes = [
        {
            "from_session_id": f"s{i}",
            "question_text": f"q{i}",
            "answer": "x" * 5000 if i == 0 else f"a{i}",
            "ts": f"2026-06-06T10:{i % 60:02d}:00",
        }
        for i in range(25)
    ]
    out = hook_module._format_inbox(notes)
    assert "25 unread" in out          # true total in header
    assert out.count("- (from ") == 10  # newest 10 rendered
    assert "+15 more note(s)" in out
    assert "session_search_archive" in out
    # the 5000-char answer belongs to the OLDEST note (i=0) → cut by the cap;
    # length-cap is verified separately below
    long_note = [{"from_session_id": "s", "question_text": "q", "answer": "y" * 5000, "ts": "t"}]
    out2 = hook_module._format_inbox(long_note)
    assert "truncated; full text via session_search_archive" in out2
    assert len(out2) < 3000


def test_format_tasks_caps_at_15(hook_module):
    tasks = [{"id": f"t{i:03}", "title": f"task {i}", "state": "todo"} for i in range(40)]
    out = hook_module._format_tasks(tasks)
    assert "40 open assignment(s)" in out
    assert out.count("  • ") == 15
    assert "+25 more" in out


# ---------------------------------------------------------------------------
# Domain-memory routing — leads retired (2026-06-06): master is the boot
# reader of project:orchestration; output is length-capped.
# ---------------------------------------------------------------------------


def test_inject_domain_memory_master_gets_orchestration(hook_module, monkeypatch):
    captured = {}

    def _fake_query(domain):
        captured["domain"] = domain
        return {"answer": "orchestration wisdom", "training_pairs_available": 9}

    monkeypatch.setattr(hook_module, "_mnemosyne_query", _fake_query)
    monkeypatch.setattr(hook_module, "detect_project", lambda cwd: "khimaira")
    out = hook_module._inject_domain_memory("sess-x", "/proj", chat_role="master")
    assert captured["domain"] == "khimaira:orchestration"
    assert "orchestration wisdom" in out
    assert "PROVISIONAL domain memory" in out


def test_inject_domain_memory_worker_role_gets_nothing(hook_module, monkeypatch):
    # agent role, non-lead name → '' (no daemon/mnemosyne calls matter)
    monkeypatch.setattr(
        hook_module, "_http_get_json", lambda *a, **k: {"name": "agent-3"}
    )
    out = hook_module._inject_domain_memory("sess-x", "/proj", chat_role="agent")
    assert out == ""


def test_inject_domain_memory_answer_is_capped(hook_module, monkeypatch):
    monkeypatch.setattr(
        hook_module,
        "_mnemosyne_query",
        lambda d: {"answer": "z" * 20000, "training_pairs_available": 1},
    )
    monkeypatch.setattr(hook_module, "detect_project", lambda cwd: "khimaira")
    out = hook_module._inject_domain_memory("sess-x", "/proj", chat_role="master")
    assert len(out) < 5000
    assert "truncated" in out
