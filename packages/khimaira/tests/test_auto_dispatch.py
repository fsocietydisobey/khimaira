"""Tests for monitor.auto_dispatch — #14 auto-dispatch sweep.

Coverage:
  Rail #1 — backlog scan (_get_backlog_tasks)
  Rail #2 — available-agent scan (_get_available_agents)
  Rail #3 — role-fit ranking (_rank_task_agent_pairs, _role_fit_score)
  Rail #4 — dispatch proposal (_emit_dispatch_proposal)
  Rail #5 — TTL fallback: assigned-no-BEGIN → auto-BEGIN; unassigned → escalate
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call

import pytest

import khimaira.monitor.auto_dispatch as ad


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_jsonl_lines(tasks: list[dict], signals: list[dict] | None = None) -> list[dict]:
    """Produce a minimal JSONL event stream for a single chat."""
    lines = [{"kind": "task", **t} for t in tasks]
    for sig in (signals or []):
        lines.append({"kind": "task_signal", "signal": "start", **sig})
    return lines


def _session_row(
    session_id: str,
    status: str = "idle",
    idle_s: float = 300.0,
    name: str | None = None,
) -> dict:
    return {
        "session_id": session_id,
        "status": status,
        "last_active_age_s": idle_s,
        "name": name,
    }


MASTER_UUID = "aaaa-1111-master"
AGENT_UUID = "bbbb-2222-agent"
TASK_ID_1 = "task-abc123"
TASK_ID_2 = "task-def456"
CHAT_ID = "chat-deadbeef1234"


# ---------------------------------------------------------------------------
# Rail #1 — _get_backlog_tasks
# ---------------------------------------------------------------------------

class TestGetBacklogTasks:
    def _make_chat_dir(self, tmp_path: Path) -> Path:
        chat_dir = tmp_path / "chats"
        chat_dir.mkdir()
        return chat_dir

    def test_returns_unassigned_task(self, tmp_path):
        """Unassigned TASK_PENDING → risk=unassigned in backlog."""
        chat_dir = self._make_chat_dir(tmp_path)
        chat_file = chat_dir / f"{CHAT_ID}.jsonl"
        chat_file.write_text(
            json.dumps({
                "kind": "task",
                "id": TASK_ID_1,
                "body": "implement X",
                "assignee_id": None,
                "status": "pending",
            }) + "\n"
        )

        import khimaira.monitor.chats as chats_mod

        with (
            patch.object(chats_mod, "_chat_dir", return_value=chat_dir),
            patch.object(chats_mod, "_read", return_value=[
                {"kind": "task", "id": TASK_ID_1, "body": "implement X",
                 "assignee_id": None, "status": "pending"},
            ]),
        ):
            backlog = ad._get_backlog_tasks()

        assert len(backlog) == 1
        assert backlog[0]["task_id"] == TASK_ID_1
        assert backlog[0]["risk"] == "unassigned"
        assert backlog[0]["assignee_id"] is None

    def test_returns_assigned_no_begin_task(self, tmp_path):
        """Assigned TASK_PENDING with no start signal → risk=assigned_no_begin."""
        import khimaira.monitor.chats as chats_mod

        chat_dir = self._make_chat_dir(tmp_path)

        with (
            patch.object(chats_mod, "_chat_dir", return_value=chat_dir),
            patch.object(chats_mod, "_read", return_value=[
                {"kind": "task", "id": TASK_ID_1, "body": "fix bug",
                 "assignee_id": AGENT_UUID, "status": "pending"},
            ]),
        ):
            # Need a real .jsonl file for glob to pick up
            (chat_dir / f"{CHAT_ID}.jsonl").write_text("")
            backlog = ad._get_backlog_tasks()

        assert len(backlog) == 1
        assert backlog[0]["risk"] == "assigned_no_begin"
        assert backlog[0]["assignee_id"] == AGENT_UUID

    def test_skips_begin_fired_task(self, tmp_path):
        """Task with a TASK_SIGNAL start event is excluded from backlog."""
        import khimaira.monitor.chats as chats_mod

        chat_dir = self._make_chat_dir(tmp_path)
        (chat_dir / f"{CHAT_ID}.jsonl").write_text("")

        with (
            patch.object(chats_mod, "_chat_dir", return_value=chat_dir),
            patch.object(chats_mod, "_read", return_value=[
                {"kind": "task", "id": TASK_ID_1, "body": "work",
                 "assignee_id": AGENT_UUID, "status": "pending"},
                {"kind": "task_signal", "task_id": TASK_ID_1, "signal": "start"},
            ]),
        ):
            backlog = ad._get_backlog_tasks()

        assert backlog == []

    def test_skips_non_pending_task(self, tmp_path):
        """Tasks in in_progress / done / cancelled are not backlog."""
        import khimaira.monitor.chats as chats_mod

        chat_dir = self._make_chat_dir(tmp_path)
        (chat_dir / f"{CHAT_ID}.jsonl").write_text("")

        for status in ("in_progress", "done", "cancelled"):
            with (
                patch.object(chats_mod, "_chat_dir", return_value=chat_dir),
                patch.object(chats_mod, "_read", return_value=[
                    {"kind": "task", "id": TASK_ID_1, "body": "x",
                     "assignee_id": AGENT_UUID, "status": status},
                ]),
            ):
                assert ad._get_backlog_tasks() == [], f"status={status} should not be backlog"

    def test_returns_empty_when_no_chats(self, tmp_path):
        """No chat directory → empty backlog."""
        import khimaira.monitor.chats as chats_mod

        empty_dir = tmp_path / "chats"
        empty_dir.mkdir()

        with patch.object(chats_mod, "_chat_dir", return_value=empty_dir):
            assert ad._get_backlog_tasks() == []


# ---------------------------------------------------------------------------
# Rail #2 — _get_available_agents
# ---------------------------------------------------------------------------

class TestGetAvailableAgents:
    def _patch_agent_checks(self, monkeypatch, *, alive=True, heartbeat=True, obligations=None, role="agent"):
        """Patch all per-session liveness checks to return given values."""
        monkeypatch.setattr(
            "khimaira.monitor.api.chats._is_process_alive_for_session",
            lambda sid: alive,
        )
        monkeypatch.setattr(
            "khimaira.monitor.auto_dispatch._session_heartbeat_fresh",
            lambda sid: heartbeat,
        )
        monkeypatch.setattr(
            "khimaira.monitor.api.chats._get_session_obligations",
            lambda sid: obligations or [],
        )
        monkeypatch.setattr(
            "khimaira.monitor.api.themis.resolve_session_role",
            lambda sid: role,
        )

    def test_returns_idle_session_with_no_obligations(self, monkeypatch):
        import khimaira.monitor.sessions as sessions_mod

        self._patch_agent_checks(monkeypatch)
        monkeypatch.setattr(
            sessions_mod,
            "list_sessions",
            lambda **kw: [_session_row(AGENT_UUID, "idle", 300)],
        )

        agents = ad._get_available_agents()

        assert len(agents) == 1
        assert agents[0]["session_id"] == AGENT_UUID
        assert agents[0]["role"] == "agent"

    def test_excludes_busy_sessions(self, monkeypatch):
        import khimaira.monitor.sessions as sessions_mod

        self._patch_agent_checks(monkeypatch)
        monkeypatch.setattr(
            sessions_mod,
            "list_sessions",
            lambda **kw: [_session_row(AGENT_UUID, "implementing", 300)],
        )

        assert ad._get_available_agents() == []

    def test_excludes_session_with_obligations(self, monkeypatch):
        import khimaira.monitor.sessions as sessions_mod

        self._patch_agent_checks(monkeypatch, obligations=[{"task_id": "t1"}])
        monkeypatch.setattr(
            sessions_mod,
            "list_sessions",
            lambda **kw: [_session_row(AGENT_UUID, "idle", 300)],
        )

        assert ad._get_available_agents() == []

    def test_excludes_dead_process(self, monkeypatch):
        import khimaira.monitor.sessions as sessions_mod

        self._patch_agent_checks(monkeypatch, alive=False)
        monkeypatch.setattr(
            sessions_mod,
            "list_sessions",
            lambda **kw: [_session_row(AGENT_UUID, "idle", 300)],
        )

        assert ad._get_available_agents() == []

    def test_excludes_stale_heartbeat(self, monkeypatch):
        import khimaira.monitor.sessions as sessions_mod

        self._patch_agent_checks(monkeypatch, heartbeat=False)
        monkeypatch.setattr(
            sessions_mod,
            "list_sessions",
            lambda **kw: [_session_row(AGENT_UUID, "idle", 300)],
        )

        assert ad._get_available_agents() == []

    def test_excludes_recently_active_session(self, monkeypatch):
        """Session idle for less than _IDLE_MIN_S → excluded."""
        import khimaira.monitor.sessions as sessions_mod

        self._patch_agent_checks(monkeypatch)
        monkeypatch.setattr(
            sessions_mod,
            "list_sessions",
            lambda **kw: [_session_row(AGENT_UUID, "idle", idle_s=10)],  # too fresh
        )

        assert ad._get_available_agents() == []


# ---------------------------------------------------------------------------
# Rail #3 — _role_fit_score + _rank_task_agent_pairs
# ---------------------------------------------------------------------------

class TestRoleFitScore:
    def test_keyword_match_scores_2(self):
        assert ad._role_fit_score("frontend-lead", "fix the React component CSS") == 2

    def test_generic_agent_scores_1(self):
        assert ad._role_fit_score("agent", "do some work") == 1

    def test_no_match_scores_0(self):
        # backend-lead has no match in a frontend task
        assert ad._role_fit_score("backend-lead", "react tsx tailwind css") == 0

    def test_none_role_scores_0(self):
        assert ad._role_fit_score(None, "anything") == 0


class TestRankTaskAgentPairs:
    def test_returns_best_fit_pair(self):
        tasks = [
            {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "React component",
             "assignee_id": None, "risk": "unassigned"},
        ]
        agents = [
            {"session_id": "A", "role": "backend-lead", "idle_s": 300, "status": "idle", "name": None},
            {"session_id": "B", "role": "frontend-lead", "idle_s": 300, "status": "idle", "name": None},
        ]
        pairs = ad._rank_task_agent_pairs(tasks, agents)

        assert len(pairs) == 1
        _, agent = pairs[0]
        assert agent["session_id"] == "B"  # frontend-lead is the better fit

    def test_skips_already_proposed_live_proposal(self):
        """Task with a non-expired proposal is excluded from new pairs."""
        task = {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "x",
                "assignee_id": None, "risk": "unassigned"}
        ad._PENDING_PROPOSALS[TASK_ID_1] = {
            "ts": time.time(),  # fresh — not expired
            "agent_id": AGENT_UUID,
            "chat_id": CHAT_ID,
            "risk": "unassigned",
            "escalated": False,
        }
        try:
            pairs = ad._rank_task_agent_pairs(
                [task],
                [{"session_id": AGENT_UUID, "role": "agent", "idle_s": 300, "status": "idle", "name": None}],
            )
            assert pairs == []
        finally:
            ad._PENDING_PROPOSALS.clear()

    def test_assigned_no_begin_gets_empty_agent(self):
        """Assigned-no-BEGIN tasks pair with empty agent dict (no new assignment needed)."""
        tasks = [
            {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "work",
             "assignee_id": AGENT_UUID, "risk": "assigned_no_begin"},
        ]
        agents = [{"session_id": "C", "role": "agent", "idle_s": 300, "status": "idle", "name": None}]

        pairs = ad._rank_task_agent_pairs(tasks, agents)

        assert len(pairs) == 1
        task, agent = pairs[0]
        assert task["task_id"] == TASK_ID_1
        assert agent == {}


# ---------------------------------------------------------------------------
# Rail #4 — _emit_dispatch_proposal (proposal fires)
# ---------------------------------------------------------------------------

class TestEmitDispatchProposal:
    def test_proposal_fires_to_master(self, monkeypatch):
        """Unassigned task + idle agent → post_notice called for master, proposal recorded."""
        notices = []
        monkeypatch.setattr(
            "khimaira.monitor.sessions.post_notice",
            lambda target, text, **kw: notices.append((target, text)),
        )

        task = {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "implement X",
                "assignee_id": None, "risk": "unassigned"}
        agent = {"session_id": AGENT_UUID, "role": "agent", "idle_s": 300.0, "name": "agent-2"}

        ad._PENDING_PROPOSALS.clear()
        asyncio.get_event_loop().run_until_complete(
            ad._emit_dispatch_proposal(MASTER_UUID, task, agent)
        )

        assert len(notices) == 1
        target, text = notices[0]
        assert target == MASTER_UUID
        assert TASK_ID_1 in text
        assert "agent-2" in text
        assert TASK_ID_1 in ad._PENDING_PROPOSALS
        ad._PENDING_PROPOSALS.clear()

    def test_proposal_text_for_assigned_no_begin(self, monkeypatch):
        """Assigned-no-BEGIN task uses different notice text (shows assignee)."""
        notices = []
        monkeypatch.setattr(
            "khimaira.monitor.sessions.post_notice",
            lambda target, text, **kw: notices.append((target, text)),
        )

        task = {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "fix bug",
                "assignee_id": AGENT_UUID, "risk": "assigned_no_begin"}

        ad._PENDING_PROPOSALS.clear()
        asyncio.get_event_loop().run_until_complete(
            ad._emit_dispatch_proposal(MASTER_UUID, task, {})
        )

        _, text = notices[0]
        assert "BEGIN not fired" in text
        assert AGENT_UUID[:8] in text
        ad._PENDING_PROPOSALS.clear()


# ---------------------------------------------------------------------------
# Rail #5 — TTL fallback
# ---------------------------------------------------------------------------

class TestTtlFallback:
    def test_ttl_low_risk_auto_begin(self, monkeypatch):
        """assigned_no_begin + TTL expired → signal_task_start called as master."""
        begin_calls = []
        monkeypatch.setattr(
            "khimaira.monitor.chats.signal_task_start",
            lambda chat_id, task_id, by_sid, note=None: begin_calls.append(
                (chat_id, task_id, by_sid)
            ),
        )

        proposal = {
            "ts": time.time() - (ad._PROPOSAL_TTL_S + 10),  # expired
            "agent_id": AGENT_UUID,
            "chat_id": CHAT_ID,
            "risk": "assigned_no_begin",
            "escalated": False,
        }
        task = {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "work",
                "assignee_id": AGENT_UUID, "risk": "assigned_no_begin"}

        asyncio.get_event_loop().run_until_complete(
            ad._handle_ttl_expiry(MASTER_UUID, task, proposal)
        )

        assert len(begin_calls) == 1
        chat_id, task_id, by_sid = begin_calls[0]
        assert task_id == TASK_ID_1
        assert by_sid == MASTER_UUID  # daemon acts as master
        assert TASK_ID_1 not in ad._PENDING_PROPOSALS  # cleared after auto-BEGIN

    def test_ttl_high_risk_escalates(self, monkeypatch):
        """unassigned + TTL expired → escalation notice to master, escalated=True."""
        notices = []
        monkeypatch.setattr(
            "khimaira.monitor.sessions.post_notice",
            lambda target, text, **kw: notices.append((target, text)),
        )

        proposal = {
            "ts": time.time() - (ad._PROPOSAL_TTL_S + 10),
            "agent_id": None,
            "chat_id": CHAT_ID,
            "risk": "unassigned",
            "escalated": False,
        }
        task = {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "unassigned work",
                "assignee_id": None, "risk": "unassigned"}

        asyncio.get_event_loop().run_until_complete(
            ad._handle_ttl_expiry(MASTER_UUID, task, proposal)
        )

        assert len(notices) == 1
        target, text = notices[0]
        assert target == MASTER_UUID
        assert "ESCALATION" in text
        assert proposal["escalated"] is True

    def test_ttl_high_risk_does_not_double_escalate(self, monkeypatch):
        """already-escalated unassigned proposal is not re-escalated."""
        notices = []
        monkeypatch.setattr(
            "khimaira.monitor.sessions.post_notice",
            lambda target, text, **kw: notices.append((target, text)),
        )

        proposal = {
            "ts": time.time() - (ad._PROPOSAL_TTL_S + 10),
            "agent_id": None,
            "chat_id": CHAT_ID,
            "risk": "unassigned",
            "escalated": True,  # already escalated
        }
        task = {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "x",
                "assignee_id": None, "risk": "unassigned"}

        asyncio.get_event_loop().run_until_complete(
            ad._handle_ttl_expiry(MASTER_UUID, task, proposal)
        )

        assert notices == []


# ---------------------------------------------------------------------------
# Integration — auto_dispatch_sweep end-to-end
# ---------------------------------------------------------------------------

class TestAutoDispatchSweep:
    def _setup_sweep(self, monkeypatch, *, backlog, available, master_id=MASTER_UUID):
        monkeypatch.setattr(
            "khimaira.monitor.roster_recovery._resolve_session_for_role",
            lambda role: master_id if role == "master" else None,
        )
        monkeypatch.setattr("khimaira.monitor.auto_dispatch._get_backlog_tasks", lambda: backlog)
        monkeypatch.setattr("khimaira.monitor.auto_dispatch._get_available_agents", lambda: available)

    def test_no_backlog_is_a_noop(self, monkeypatch):
        """Empty backlog → no notices, proposals cleared."""
        notices = []
        monkeypatch.setattr(
            "khimaira.monitor.sessions.post_notice",
            lambda *a, **kw: notices.append(a),
        )
        ad._PENDING_PROPOSALS["stale-task"] = {"ts": 0, "risk": "unassigned", "escalated": False}
        self._setup_sweep(monkeypatch, backlog=[], available=[])

        asyncio.get_event_loop().run_until_complete(ad.auto_dispatch_sweep())

        assert notices == []
        assert ad._PENDING_PROPOSALS == {}

    def test_proposal_fires_for_idle_agent_and_backlog(self, monkeypatch):
        """Backlog task + available agent → proposal notice sent to master."""
        notices = []
        monkeypatch.setattr(
            "khimaira.monitor.sessions.post_notice",
            lambda target, text, **kw: notices.append((target, text)),
        )
        ad._PENDING_PROPOSALS.clear()

        backlog = [
            {"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "implement Y",
             "assignee_id": None, "risk": "unassigned"},
        ]
        agents = [
            {"session_id": AGENT_UUID, "role": "agent", "idle_s": 300, "status": "idle", "name": "agent-1"},
        ]
        self._setup_sweep(monkeypatch, backlog=backlog, available=agents)

        asyncio.get_event_loop().run_until_complete(ad.auto_dispatch_sweep())

        assert len(notices) == 1
        target, text = notices[0]
        assert target == MASTER_UUID
        assert TASK_ID_1 in text
        assert TASK_ID_1 in ad._PENDING_PROPOSALS
        ad._PENDING_PROPOSALS.clear()

    def test_no_master_session_skips_sweep(self, monkeypatch):
        """No master session found → sweep is a no-op (no exception)."""
        notices = []
        monkeypatch.setattr(
            "khimaira.monitor.sessions.post_notice",
            lambda *a, **kw: notices.append(a),
        )
        self._setup_sweep(monkeypatch, backlog=[], available=[], master_id=None)
        # Patch _resolve to always return None
        monkeypatch.setattr(
            "khimaira.monitor.roster_recovery._resolve_session_for_role",
            lambda role: None,
        )

        asyncio.get_event_loop().run_until_complete(ad.auto_dispatch_sweep())

        assert notices == []

    def test_opt_out_disables_sweep(self, monkeypatch):
        """KHIMAIRA_AUTO_DISPATCH=0 → sweep is a no-op."""
        notices = []
        monkeypatch.setenv("KHIMAIRA_AUTO_DISPATCH", "0")
        monkeypatch.setattr(
            "khimaira.monitor.sessions.post_notice",
            lambda *a, **kw: notices.append(a),
        )
        self._setup_sweep(
            monkeypatch,
            backlog=[{"task_id": TASK_ID_1, "chat_id": CHAT_ID, "body": "x",
                      "assignee_id": None, "risk": "unassigned"}],
            available=[{"session_id": AGENT_UUID, "role": "agent", "idle_s": 300,
                        "status": "idle", "name": None}],
        )

        asyncio.get_event_loop().run_until_complete(ad.auto_dispatch_sweep())

        assert notices == []
        monkeypatch.delenv("KHIMAIRA_AUTO_DISPATCH")
