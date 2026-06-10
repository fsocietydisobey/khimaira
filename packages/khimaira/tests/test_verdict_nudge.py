"""Verdict-via-prose nudge (2026-06-09).

critic/verifier repeatedly post a prose review but never make the structured
chat_task_verdict call the B3 gate reads, so the task sits done-not-approved.
When a verdict-role posts to a chat with a done gate-task awaiting THEIR verdict
and they haven't recorded it, the daemon nudges them once.
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_chats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    from khimaira.monitor import sessions as sess
    importlib.reload(sess)
    from khimaira.monitor import chats as c
    importlib.reload(c)
    yield c
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sess)


MASTER = "11111111-0000-0000-0000-000000000001"
CRITIC = "22222222-0000-0000-0000-000000000002"


def _room_with_done_gate_task(c, monkeypatch):
    notices = []
    import khimaira.monitor.sessions as sess
    monkeypatch.setattr(sess, "post_notice",
                        lambda target_session_id, text, **k: notices.append((target_session_id, text)))
    room = c.create_room(MASTER, [CRITIC], title="t", topology="hierarchical",
                         member_roles={MASTER: c.ROLE_MASTER, CRITIC: c.ROLE_CRITIC})
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, CRITIC)
    # master creates a review-task for critic, then it goes done
    task = c.create_task(chat_id, MASTER, "review the diff",
                         assignee_role="critic", verdict_role="critic")
    tid = task["id"]
    c.update_task_status(chat_id, tid, CRITIC, c.TASK_IN_PROGRESS)
    c.update_task_status(chat_id, tid, CRITIC, c.TASK_DONE)
    return chat_id, tid, notices


def test_prose_verdict_triggers_nudge(isolated_chats, monkeypatch):
    c = isolated_chats
    chat_id, tid, notices = _room_with_done_gate_task(c, monkeypatch)
    notices.clear()
    # critic posts a PROSE review, no chat_task_verdict
    c.send_message(chat_id, CRITIC, "Reviewed thoroughly — looks good, approved.")
    assert len(notices) == 1, "verdict-role prose on a done gate-task must nudge"
    target, text = notices[0]
    assert target == CRITIC
    assert "VERDICT NOT RECORDED" in text
    assert "chat_task_verdict" in text
    assert tid in text


def test_nudge_is_deduped(isolated_chats, monkeypatch):
    c = isolated_chats
    chat_id, tid, notices = _room_with_done_gate_task(c, monkeypatch)
    notices.clear()
    c.send_message(chat_id, CRITIC, "first prose review")
    c.send_message(chat_id, CRITIC, "second prose message")
    assert len(notices) == 1, "must nudge once per (chat, task, reviewer), not per message"


def test_no_nudge_after_structured_verdict_recorded(isolated_chats, monkeypatch):
    c = isolated_chats
    chat_id, tid, notices = _room_with_done_gate_task(c, monkeypatch)
    c.record_gate_verdict(chat_id, CRITIC, tid, "approve")  # did it right
    notices.clear()
    c.send_message(chat_id, CRITIC, "follow-up note about the review")
    assert notices == [], "no nudge once the structured verdict is recorded"


def test_no_nudge_for_non_reviewer_role(isolated_chats, monkeypatch):
    c = isolated_chats
    chat_id, tid, notices = _room_with_done_gate_task(c, monkeypatch)
    notices.clear()
    # master posts — not a reviewer role
    c.send_message(chat_id, MASTER, "any update?")
    assert notices == [], "non-reviewer roles never get the verdict nudge"


def test_no_nudge_when_task_not_done(isolated_chats, monkeypatch):
    c = isolated_chats
    import khimaira.monitor.sessions as sess
    notices = []
    monkeypatch.setattr(sess, "post_notice",
                        lambda target_session_id, text, **k: notices.append((target_session_id, text)))
    room = c.create_room(MASTER, [CRITIC], title="t", topology="hierarchical",
                         member_roles={MASTER: c.ROLE_MASTER, CRITIC: c.ROLE_CRITIC})
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, CRITIC)
    task = c.create_task(chat_id, MASTER, "review", assignee_role="critic", verdict_role="critic")
    c.update_task_status(chat_id, task["id"], CRITIC, c.TASK_IN_PROGRESS)  # in_progress, NOT done
    notices.clear()
    c.send_message(chat_id, CRITIC, "still reviewing")
    assert notices == [], "no nudge until the task is actually done-awaiting-verdict"
