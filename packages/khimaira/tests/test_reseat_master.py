"""reseat_master — dead-master roster recovery (the substrate gap).

When a roster's master SESSION dies (window/process exits; registry-GC'd), a
fresh replacement cannot self-seat: chat_grant_role is master-only and
chat_transfer_membership needs the dead session as live donor. reseat_master
fills that gap — but must REFUSE while the incumbent master is still live, so it
can't hijack an active roster. These tests guard both behaviors.

Real incident this models: jeevy `muther` master died mid-roster (2026-06-12),
leaving chat-9d7336b4f090 with member_roles[dead]=master and the replacement
session not a member at all.
"""

from __future__ import annotations

import importlib
import json
import shutil
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


def _make(name: str) -> None:
    """Register a session (dir + status.json) — keyed by name in tests."""
    from khimaira.monitor import sessions as sessions_mod

    sd = sessions_mod._session_dir(name)
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "status.json").write_text(
        json.dumps({"status": "idle", "detail": "", "name": name}),
        encoding="utf-8",
    )


def _kill(name: str) -> None:
    """Simulate a dead/registry-GC'd session: remove its registry dir."""
    from khimaira.monitor import sessions as sessions_mod

    sd = sessions_mod._session_dir(name)
    if sd.exists():
        shutil.rmtree(sd)


def _role(chats, chat_id: str, sid: str) -> str | None:
    room = chats.load_room(chat_id)
    return (room["meta"].get("member_roles") or {}).get(sid)


def _state(chats, chat_id: str, sid: str) -> str | None:
    room = chats.load_room(chat_id)
    return (room["members"].get(sid) or {}).get("state")


# --------------------------------------------------------------------------- #


def test_reseat_recovers_dead_master_existing_member(isolated_chats):
    """Incumbent master is dead; new master is already an accepted member →
    new master is promoted, dead incumbent demoted to agent, created_by moved."""
    chats = isolated_chats
    for n in ("deadmaster", "newmaster", "agent1"):
        _make(n)
    room = chats.create_room("deadmaster", ["newmaster", "agent1"], title="r")
    cid = room["meta"]["chat_id"]
    chats.accept(cid, "newmaster")
    chats.accept(cid, "agent1")

    _kill("deadmaster")  # the master session dies / is reaped
    chats.reseat_master(cid, "newmaster")

    assert _role(chats, cid, "newmaster") == "master"
    assert _role(chats, cid, "deadmaster") == "agent"  # demoted, not left as master
    assert chats.load_room(cid)["meta"]["created_by"] == "newmaster"
    assert chats._is_master(chats.load_room(cid), "newmaster")


def test_reseat_adds_new_master_when_not_a_member(isolated_chats):
    """The real incident: replacement session isn't in the chat at all. reseat
    must add it as an accepted member, then seat it as master."""
    chats = isolated_chats
    for n in ("deadmaster", "newmaster", "agent1"):
        _make(n)
    room = chats.create_room("deadmaster", ["agent1"], title="r")
    cid = room["meta"]["chat_id"]
    chats.accept(cid, "agent1")

    assert _state(chats, cid, "newmaster") is None  # not a member yet
    _kill("deadmaster")
    chats.reseat_master(cid, "newmaster")

    assert _state(chats, cid, "newmaster") == "accepted"
    assert _role(chats, cid, "newmaster") == "master"
    assert chats.load_room(cid)["meta"]["created_by"] == "newmaster"


def test_reseat_refuses_live_master(isolated_chats):
    """Guard: an ACTIVE incumbent master must not be displaced — that's a live
    handoff (chat_grant_role / transfer), not a recovery."""
    chats = isolated_chats
    for n in ("livemaster", "newmaster"):
        _make(n)
    room = chats.create_room("livemaster", ["newmaster"], title="r")
    cid = room["meta"]["chat_id"]
    chats.accept(cid, "newmaster")

    # livemaster's session dir is intact + freshly active → live → refuse.
    with pytest.raises(ValueError, match="still live"):
        chats.reseat_master(cid, "newmaster")
    assert chats.load_room(cid)["meta"].get("created_by") == "livemaster"


def test_reseat_refuses_unregistered_new_master(isolated_chats):
    """The new master must be a real registered session (phantom-member guard)."""
    chats = isolated_chats
    _make("deadmaster")
    _make("agent1")
    room = chats.create_room("deadmaster", ["agent1"], title="r")
    cid = room["meta"]["chat_id"]
    _kill("deadmaster")

    with pytest.raises(ValueError):  # _assert_session_registered fails
        chats.reseat_master(cid, "ghostmaster")


def test_reseat_stale_master_is_recoverable(isolated_chats, monkeypatch):
    """A master whose dir exists but is stale beyond KHIMAIRA_MASTER_LIVE_S is
    treated as dead (covers a crashed session not yet registry-GC'd)."""
    chats = isolated_chats
    for n in ("stalemaster", "newmaster"):
        _make(n)
    room = chats.create_room("stalemaster", ["newmaster"], title="r")
    cid = room["meta"]["chat_id"]
    chats.accept(cid, "newmaster")

    # Force the liveness threshold to 0 so any age counts as stale/dead.
    monkeypatch.setenv("KHIMAIRA_MASTER_LIVE_S", "0")
    chats.reseat_master(cid, "newmaster")
    assert _role(chats, cid, "newmaster") == "master"
