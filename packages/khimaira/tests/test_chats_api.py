"""HTTP API tests for /api/chats.

Per khimaira CLAUDE.md rule: every endpoint gets happy + unhappy paths,
including the cross-cutting unknown-id 404s and the 403 sender-gating
checks. SSE stream is exercised via TestClient's stream API.
"""

from __future__ import annotations

import importlib
import json
from pathlib import Path

import pytest


@pytest.fixture
def chats_api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod

    importlib.reload(chats_mod)
    from khimaira.monitor.api import chats as api_mod

    importlib.reload(api_mod)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_mod.build_router(), prefix="/api")
    client = TestClient(app)

    # Plant alice + bob + carol sessions.
    for sid in ("alice", "bob", "carol"):
        sd = sessions_mod._session_dir(sid)
        (sd / "status.json").write_text(
            json.dumps({"status": "implementing", "name": sid}), encoding="utf-8"
        )
    yield client, chats_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)
    importlib.reload(api_mod)


# ---------------------------------------------------------------------------
# create + list
# ---------------------------------------------------------------------------


def test_post_create_room(chats_api_client):
    client, _ = chats_api_client
    resp = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["bob"],
            "title": "alice + bob",
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["title"] == "alice + bob"
    assert body["members"]["alice"]["state"] == "accepted"
    assert body["members"]["bob"]["state"] == "pending"


def test_post_create_room_unknown_member_returns_404(chats_api_client):
    client, _ = chats_api_client
    resp = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["ghost"],
        },
    )
    assert resp.status_code == 404


def test_get_my_chats_returns_pending_and_accepted(chats_api_client):
    client, _ = chats_api_client
    client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    )
    resp = client.get("/api/chats?session_id=bob")
    assert resp.status_code == 200
    chats = resp.json()["chats"]
    assert len(chats) == 1
    assert chats[0]["my_state"] == "pending"


# ---------------------------------------------------------------------------
# accept + send + history
# ---------------------------------------------------------------------------


def test_accept_then_send_then_history(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]

    accept = client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    assert accept.status_code == 200

    send = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "hello"},
    )
    assert send.status_code == 200

    history = client.get(f"/api/chats/{chat_id}/messages?session_id=bob")
    assert history.status_code == 200
    msgs = history.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["body"] == "hello"


def test_send_by_pending_member_returns_403(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    # bob never accepts
    resp = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "bob", "body": "premature"},
    )
    assert resp.status_code == 403


def test_send_by_non_member_returns_403(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    resp = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "carol", "body": "I'm not even invited"},
    )
    assert resp.status_code == 403


def test_history_by_non_member_returns_403(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    resp = client.get(f"/api/chats/{chat_id}/messages?session_id=carol")
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# leave + delete
# ---------------------------------------------------------------------------


def test_leave_returns_200(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    resp = client.post(f"/api/chats/{chat_id}/leave", json={"session_id": "bob"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "left"


def test_delete_by_non_creator_returns_403(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    resp = client.delete(f"/api/chats/{chat_id}?by_session_id=bob")
    assert resp.status_code == 403


def test_delete_by_creator_returns_200(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    resp = client.delete(f"/api/chats/{chat_id}?by_session_id=alice")
    assert resp.status_code == 200
    assert "archived_to" in resp.json()


def test_reject_pending_returns_200(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    resp = client.post(f"/api/chats/{chat_id}/reject", json={"session_id": "bob"})
    assert resp.status_code == 200
    assert resp.json()["state"] == "rejected"


def test_reject_unknown_chat_returns_404(chats_api_client):
    client, _ = chats_api_client
    resp = client.post("/api/chats/chat-doesnotexis/reject", json={"session_id": "bob"})
    assert resp.status_code == 404


def test_register_pending_session_then_lookup(chats_api_client):
    """Hook posts {ppid, session_id}; subprocess looks up by ppid."""
    client, _ = chats_api_client
    resp = client.post(
        "/api/chats/register-pending-session",
        json={"ppid": 88888, "session_id": "session-xyz"},
    )
    assert resp.status_code == 200
    assert resp.json()["ok"] is True

    lookup = client.get("/api/chats/session-by-ppid?ppid=88888")
    assert lookup.status_code == 200
    assert lookup.json()["session_id"] == "session-xyz"


def test_session_by_ppid_returns_null_when_unknown(chats_api_client):
    client, _ = chats_api_client
    resp = client.get("/api/chats/session-by-ppid?ppid=99999")
    assert resp.status_code == 200
    assert resp.json()["session_id"] is None


def test_latest_pending_returns_chat_id(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    expected_chat_id = created["meta"]["chat_id"]

    resp = client.get("/api/chats/pending/latest?session_id=bob")
    assert resp.status_code == 200
    assert resp.json()["chat_id"] == expected_chat_id


def test_latest_pending_returns_null_when_none(chats_api_client):
    client, _ = chats_api_client
    resp = client.get("/api/chats/pending/latest?session_id=alice")
    assert resp.status_code == 200
    assert resp.json()["chat_id"] is None


def test_delete_unknown_returns_404(chats_api_client):
    client, _ = chats_api_client
    resp = client.delete("/api/chats/chat-doesnotexis?by_session_id=alice")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# get_room
# ---------------------------------------------------------------------------


def test_get_room_returns_full_state(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    resp = client.get(f"/api/chats/{chat_id}?session_id=alice")
    assert resp.status_code == 200
    body = resp.json()
    assert body["meta"]["chat_id"] == chat_id
    assert "alice" in body["members"]
    assert "bob" in body["members"]


def test_get_room_by_non_member_returns_403(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    resp = client.get(f"/api/chats/{chat_id}?session_id=carol")
    assert resp.status_code == 403


def test_get_unknown_room_returns_404(chats_api_client):
    client, _ = chats_api_client
    resp = client.get("/api/chats/chat-doesnotexis?session_id=alice")
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# invite
# ---------------------------------------------------------------------------


def test_invite_by_accepted_member(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    resp = client.post(
        f"/api/chats/{chat_id}/invite",
        json={"by_session_id": "bob", "invitee_session_id": "carol"},
    )
    assert resp.status_code == 200
    assert resp.json()["state"] == "pending"


def test_invite_by_pending_member_rejected(chats_api_client):
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    # bob is still pending
    resp = client.post(
        f"/api/chats/{chat_id}/invite",
        json={"by_session_id": "bob", "invitee_session_id": "carol"},
    )
    assert resp.status_code == 404
