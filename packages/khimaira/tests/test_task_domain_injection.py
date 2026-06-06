"""Assign-time domain injection (tasks/domain-specialist/IMPLEMENTATION.md).

create_task(domain=...) appends PROVISIONAL mnemosyne context to the task
body — fail-open (mnemosyne down → task created without the block), validated
(unknown domain → ValueError), default-off (no domain → byte-identical body).
"""

from __future__ import annotations

import importlib
from pathlib import Path

import pytest


@pytest.fixture
def isolated_chats(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod

    importlib.reload(chats_mod)
    yield chats_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)


MASTER = "11111111-0000-0000-0000-000000000001"
WORKER = "22222222-0000-0000-0000-000000000002"


def _room_with_task_rights(c):
    room = c.create_room(MASTER, [WORKER], title="t", topology="hierarchical")
    chat_id = room["meta"]["chat_id"]
    c.accept(chat_id, WORKER)
    return chat_id


def test_domain_injects_mnemosyne_context(isolated_chats, monkeypatch):
    c = isolated_chats
    chat_id = _room_with_task_rights(c)

    import khimaira.hooks.mnemosyne_client as mc

    monkeypatch.setattr(
        mc, "query", lambda domain, **kw: {"answer": f"wisdom for {domain}"}
    )
    task = c.create_task(chat_id, MASTER, "fix the API", domain="backend")
    assert "fix the API" in task["body"]
    assert "🧠 domain context" in task["body"]
    assert "wisdom for backend" in task["body"]  # bare key (no workspace recorded)
    assert task["domain"] == "backend"


def test_domain_answer_is_capped(isolated_chats, monkeypatch):
    c = isolated_chats
    chat_id = _room_with_task_rights(c)

    import khimaira.hooks.mnemosyne_client as mc

    monkeypatch.setattr(mc, "query", lambda domain, **kw: {"answer": "z" * 50000})
    task = c.create_task(chat_id, MASTER, "fix it", domain="backend")
    assert len(task["body"]) < 4000
    assert "truncated" in task["body"]


def test_mnemosyne_down_fails_open(isolated_chats, monkeypatch):
    c = isolated_chats
    chat_id = _room_with_task_rights(c)

    import khimaira.hooks.mnemosyne_client as mc

    monkeypatch.setattr(mc, "query", lambda domain, **kw: None)  # unreachable
    task = c.create_task(chat_id, MASTER, "fix it", domain="backend")
    assert task["body"] == "fix it"  # created WITHOUT the block, no error
    assert task["domain"] == "backend"


def test_unknown_domain_raises_value_error(isolated_chats):
    c = isolated_chats
    chat_id = _room_with_task_rights(c)
    with pytest.raises(ValueError, match="Unknown task domain"):
        c.create_task(chat_id, MASTER, "fix it", domain="blockchain")


def test_no_domain_is_byte_identical(isolated_chats, monkeypatch):
    c = isolated_chats
    chat_id = _room_with_task_rights(c)

    import khimaira.hooks.mnemosyne_client as mc

    def _boom(*a, **kw):
        raise AssertionError("mnemosyne must not be queried without a domain")

    monkeypatch.setattr(mc, "query", _boom)
    task = c.create_task(chat_id, MASTER, "plain task")
    assert task["body"] == "plain task"
    assert task["domain"] is None
