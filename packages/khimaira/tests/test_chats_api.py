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


def test_get_my_chats_stamps_sse_heartbeat(chats_api_client):
    """GET /chats self-heals IN-MASTER-1 by writing last_sse_heartbeat (substrate fix).

    After /compact, PostToolUse stops firing so the SSE heartbeat goes stale.
    The Themis IN-MASTER-1 condition checks heartbeat against turn_start; a stale
    heartbeat with no recent chat_my_chats call fires a violation. This test
    confirms that GET /chats stamps the heartbeat so the condition can't fire
    immediately after a chat_my_chats call (the two-path fix).
    """
    import json as json_mod
    import importlib
    from khimaira.monitor import sessions as sessions_mod

    client, _ = chats_api_client

    # Verify no heartbeat yet
    sd = sessions_mod._session_dir("alice")
    status_before = json_mod.loads((sd / "status.json").read_text())
    assert "last_sse_heartbeat" not in status_before

    # GET /chats for alice
    resp = client.get("/api/chats?session_id=alice")
    assert resp.status_code == 200

    # Heartbeat should now be present
    status_after = json_mod.loads((sd / "status.json").read_text())
    assert (
        "last_sse_heartbeat" in status_after
    ), "GET /chats must stamp last_sse_heartbeat for the requester (IN-MASTER-1 self-heal)"


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
    # Phase B v1.5: filter system role-directive (sent on chat_create_room
    # to the creator) to assert on user messages only.
    user_msgs = [m for m in msgs if m.get("sender_id") != "khimaira-system"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["body"] == "hello"


def test_sse_event_resolves_sender_name_at_publish_time(chats_api_client):
    """SSE events (via _broadcast) show current name, not the stored snapshot."""
    import asyncio

    from khimaira.monitor import chats as chats_mod, sessions as sessions_mod

    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    # Register bob as a subscriber so _broadcast has a queue to push to.
    import asyncio as _asyncio

    q: asyncio.Queue = _asyncio.Queue()
    chats_mod._subscribers.setdefault("bob", []).append(q)

    # Rename alice to "alice-sse-name" BEFORE sending.
    sessions_mod.set_name("alice", "alice-sse-name")

    # alice sends a message — triggers _broadcast.
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "sse test"},
    )

    # Drain the queue — find alice's msg event.
    events = []
    while not q.empty():
        events.append(q.get_nowait())

    alice_events = [e for e in events if e.get("sender_id") == "alice"]
    assert len(alice_events) == 1
    assert alice_events[0]["sender_name"] == "alice-sse-name"

    # Cleanup subscriber.
    chats_mod._subscribers.get("bob", []).remove(q)


def test_chat_history_shows_current_name_after_rename(chats_api_client):
    """chat_history resolves sender_name from current status.json, not the stored snapshot."""
    from khimaira.monitor import sessions as sessions_mod

    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    # alice posts a message while named "alice".
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "hi"},
    )

    # Rename alice to "alice-renamed".
    sessions_mod.set_name("alice", "alice-renamed")

    history = client.get(f"/api/chats/{chat_id}/messages?session_id=bob").json()["messages"]
    user_msgs = [m for m in history if m.get("sender_id") == "alice"]
    assert len(user_msgs) == 1
    assert user_msgs[0]["sender_name"] == "alice-renamed"


def test_chat_history_falls_back_to_stored_name_for_deleted_session(chats_api_client):
    """Deleted session messages don't crash history; fall back to the stored name snapshot."""
    from khimaira.monitor import sessions as sessions_mod

    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    # alice posts a message (stored sender_name="alice").
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "from alice"},
    )

    # Delete alice's session directory (simulate deleted session).
    import shutil

    shutil.rmtree(sessions_mod._session_dir("alice"), ignore_errors=True)

    # History should not crash; fall back to stored snapshot name.
    resp = client.get(f"/api/chats/{chat_id}/messages?session_id=bob")
    assert resp.status_code == 200
    msgs = resp.json()["messages"]
    alice_msgs = [m for m in msgs if m.get("sender_id") == "alice"]
    assert len(alice_msgs) == 1
    assert alice_msgs[0]["sender_name"] == "alice"  # falls back to stored snapshot


def test_chat_history_name_cache_per_request(chats_api_client, monkeypatch: pytest.MonkeyPatch):
    """Per-request name cache: only 1 status.json read per unique sender per request."""
    import importlib
    from khimaira.monitor.api import chats as api_mod

    importlib.reload(api_mod)
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    # Post 5 messages from alice.
    for i in range(5):
        client.post(
            f"/api/chats/{chat_id}/messages",
            json={"sender_session_id": "alice", "body": f"msg{i}"},
        )

    lookup_count = [0]
    original = api_mod._resolve_sender_name

    def counting_resolve(session_id, fallback):
        lookup_count[0] += 1
        return original(session_id, fallback)

    monkeypatch.setattr(api_mod, "_resolve_sender_name", counting_resolve)

    resp = client.get(f"/api/chats/{chat_id}/messages?session_id=bob")
    assert resp.status_code == 200
    # alice sent 5 messages (+ 1 system msg); only 1 lookup for alice's session.
    # System messages have sender_id="khimaira-system"; that's 1 more lookup.
    # Total unique senders = 2 → at most 2 lookups regardless of message count.
    assert lookup_count[0] <= 2


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


def test_send_to_pending_recipient_retries_until_accepted(
    chats_api_client, monkeypatch: pytest.MonkeyPatch
):
    """chat_send_to a pending recipient retries and succeeds once they accept."""
    import importlib

    from khimaira.monitor.api import chats as api_mod

    importlib.reload(api_mod)
    monkeypatch.setattr(api_mod, "_PENDING_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(api_mod, "_PENDING_WAIT_DEADLINE", 5.0)

    client, chats_mod = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]

    # Accept bob so the real send can succeed on the 3rd attempt.
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    # Patch send_message to simulate pending for first 2 calls, then call through.
    original = chats_mod.send_message
    attempts = [0]

    def patched_send(*args, **kwargs):
        attempts[0] += 1
        if attempts[0] <= 2:
            raise ValueError(
                f"Recipient 'bob' is 'pending' in {chat_id!r}; "
                "only accepted members can be `to` targets."
            )
        return original(*args, **kwargs)

    monkeypatch.setattr(chats_mod, "send_message", patched_send)

    resp = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "hi", "to": ["bob"]},
    )
    assert resp.status_code == 200
    assert attempts[0] == 3


def test_send_to_pending_recipient_times_out_with_408(
    chats_api_client, monkeypatch: pytest.MonkeyPatch
):
    """chat_send_to returns 408 if recipient never accepts within the deadline."""
    import importlib

    from khimaira.monitor.api import chats as api_mod

    importlib.reload(api_mod)
    monkeypatch.setattr(api_mod, "_PENDING_POLL_INTERVAL", 0.01)
    monkeypatch.setattr(api_mod, "_PENDING_WAIT_DEADLINE", 0.001)  # expires on first check

    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    # bob never accepts

    resp = client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "hi", "to": ["bob"]},
    )
    assert resp.status_code == 408
    assert "timed out" in resp.json()["detail"].lower()


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
    # bob is still pending — 403 (forbidden, not just 404) is more correct
    resp = client.post(
        f"/api/chats/{chat_id}/invite",
        json={"by_session_id": "bob", "invitee_session_id": "carol"},
    )
    assert resp.status_code in (403, 404)  # pending member cannot invite


# ---------------------------------------------------------------------------
# Phase B v1.2: transfer_membership endpoint
# ---------------------------------------------------------------------------


def _plant_session(name: str) -> None:
    """Helper — write a state dir for sessions not in the fixture's defaults."""
    from khimaira.monitor import sessions as sessions_mod

    sd = sessions_mod._session_dir(name)
    (sd / "status.json").write_text(
        json.dumps({"status": "implementing", "name": name}), encoding="utf-8"
    )


def test_transfer_membership_happy_path_returns_200(chats_api_client):
    client, _ = chats_api_client
    _plant_session("dave")
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    resp = client.post(
        f"/api/chats/{chat_id}/transfer-membership",
        json={"from_session_id": "bob", "to_session_id": "dave"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["transfer_id"].startswith("xfer-")
    assert body["from"]["state"] == "transferred-out"
    assert body["to"]["state"] == "accepted"


def test_transfer_membership_unknown_target_returns_404(chats_api_client):
    """Required by project CLAUDE.md: every session-resolving endpoint
    needs unknown-name coverage. Resolving 'ghost' raises ValueError →
    handler must map to 404, not let it become a 500."""
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    resp = client.post(
        f"/api/chats/{chat_id}/transfer-membership",
        json={"from_session_id": "bob", "to_session_id": "ghost"},
    )
    assert resp.status_code == 404


def test_transfer_membership_pending_source_returns_403(chats_api_client):
    """A pending session has nothing to transfer — 403 (forbidden), not 404."""
    client, _ = chats_api_client
    _plant_session("dave")
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    # bob is still pending — has not accepted

    resp = client.post(
        f"/api/chats/{chat_id}/transfer-membership",
        json={"from_session_id": "bob", "to_session_id": "dave"},
    )
    assert resp.status_code == 403


def test_transfer_membership_duplicate_target_returns_409(chats_api_client):
    """Recipient is already an accepted member → 409 conflict, not silent
    state overwrite."""
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob", "carol"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "carol"})

    resp = client.post(
        f"/api/chats/{chat_id}/transfer-membership",
        json={"from_session_id": "bob", "to_session_id": "carol"},
    )
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Phase B v1.2: signal-start endpoint
# ---------------------------------------------------------------------------


def test_signal_task_start_returns_200(chats_api_client):
    """Master posts signal-start on a pending task → 200 + task_signal record."""
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    task = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "do thing",
            "assignee_session_id": "bob",
        },
    ).json()

    resp = client.post(
        f"/api/chats/{chat_id}/tasks/{task['id']}/signal-start",
        json={"by_session_id": "alice", "note": "go ahead"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "task_signal"
    assert body["signal"] == "start"
    assert body["task_id"] == task["id"]
    assert body["note"] == "go ahead"


def test_signal_task_start_unknown_task_returns_404(chats_api_client):
    """Unknown task_id → 404 (project CLAUDE.md unknown-resource coverage)."""
    client, _ = chats_api_client
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]

    resp = client.post(
        f"/api/chats/{chat_id}/tasks/task-doesnotexist/signal-start",
        json={"by_session_id": "alice"},
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Expected-reply registry (Pattern 5)
# ---------------------------------------------------------------------------


@pytest.fixture
def registry_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Fixture that provides a chats API client with a clean expected-reply registry."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod

    importlib.reload(chats_mod)
    from khimaira.monitor.api import chats as api_mod

    importlib.reload(api_mod)
    # Clear module-level registry between tests.
    monkeypatch.setattr(api_mod, "_EXPECTED_REPLIES", {})

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_mod.build_router(), prefix="/api")
    client = TestClient(app)

    for sid in ("alice", "bob", "carol"):
        sd = sessions_mod._session_dir(sid)
        (sd / "status.json").write_text(
            json.dumps({"status": "implementing", "name": sid}), encoding="utf-8"
        )
    yield client, api_mod, chats_mod
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)
    importlib.reload(api_mod)


def _setup_chat_with_accepted_bob(client):
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    return chat_id


def test_register_expected_reply_on_send_to(registry_client):
    """chat_send_to(alice→bob) registers (bob, alice) in the expected-reply registry."""
    client, api_mod, _ = registry_client
    chat_id = _setup_chat_with_accepted_bob(client)

    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "hi", "to": ["bob"]},
    )

    assert ("bob", "alice") in api_mod._EXPECTED_REPLIES
    entry = api_mod._EXPECTED_REPLIES[("bob", "alice")]
    assert entry["from"] == "alice"
    assert entry["to"] == "bob"


def test_resolve_expected_reply_on_reply(registry_client):
    """A→B followed by B→A resolves the (B, A) registry entry."""
    client, api_mod, _ = registry_client
    chat_id = _setup_chat_with_accepted_bob(client)

    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "hey", "to": ["bob"]},
    )
    assert ("bob", "alice") in api_mod._EXPECTED_REPLIES

    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "bob", "body": "hey back", "to": ["alice"]},
    )
    assert ("bob", "alice") not in api_mod._EXPECTED_REPLIES


def test_overdue_watcher_fires_notice(registry_client, monkeypatch: pytest.MonkeyPatch):
    """Overdue + silent entries trigger PRESUMED-DEAD notice to master after probe tick."""
    import asyncio
    import time

    _, api_mod, _ = registry_client
    notices_fired: list[tuple[str, str]] = []

    def _mock_post_notice(target_session_id, text, *, from_session_id="external", **_kw):
        notices_fired.append((target_session_id, text))
        return {}

    monkeypatch.setattr("khimaira.monitor.sessions.post_notice", _mock_post_notice)
    # Bob shows no recent activity → presumed dead.
    monkeypatch.setattr(api_mod, "_session_active_within", lambda sid, w: False)
    # Resolve master to alice.
    monkeypatch.setattr(api_mod, "_resolve_master_session_id", lambda chat_id: "alice")

    # Stub probe — no real SSE write needed.
    async def _fake_probe(chat_id, to_id, from_id, elapsed_s):
        return True

    monkeypatch.setattr(api_mod, "_send_diagnostic_probe", _fake_probe)

    # Plant a stale entry (91s old) in new format.
    api_mod._EXPECTED_REPLIES[("bob", "alice")] = {
        "ts": time.time() - 91.0,
        "from": "alice",
        "to": "bob",
        "chat_id": "chat-test",
        "threshold_s": 90.0,
    }

    # Tick 1: probe sent, no notice yet.
    asyncio.run(api_mod._check_overdue_once())
    assert len(notices_fired) == 0
    assert ("bob", "alice") in api_mod._EXPECTED_REPLIES

    # Tick 2: probe already sent, X still silent → PRESUMED-DEAD notice.
    asyncio.run(api_mod._check_overdue_once())

    # Notice goes to master (alice); body contains PRESUMED-DEAD + bob.
    targets = {t for t, _ in notices_fired}
    assert "alice" in targets
    assert any("PRESUMED-DEAD" in txt and "bob" in txt for _, txt in notices_fired)
    assert ("bob", "alice") not in api_mod._EXPECTED_REPLIES
    api_mod._RECENTLY_PRESUMED_DEAD.clear()


def test_overdue_watcher_one_shot(registry_client, monkeypatch: pytest.MonkeyPatch):
    """Overdue silent entry is deleted after presumed-dead notice — watcher doesn't re-fire."""
    import asyncio
    import time

    _, api_mod, _ = registry_client
    notices_fired: list = []

    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices_fired.append(a) or {},
    )
    monkeypatch.setattr(api_mod, "_session_active_within", lambda sid, w: False)
    monkeypatch.setattr(api_mod, "_resolve_master_session_id", lambda chat_id: "alice")

    async def _fake_probe(chat_id, to_id, from_id, elapsed_s):
        return True

    monkeypatch.setattr(api_mod, "_send_diagnostic_probe", _fake_probe)

    api_mod._EXPECTED_REPLIES[("bob", "alice")] = {
        "ts": time.time() - 91.0,
        "from": "alice",
        "to": "bob",
        "chat_id": "chat-test",
        "threshold_s": 90.0,
    }

    # Tick 1: probe (0 notices).
    asyncio.run(api_mod._check_overdue_once())
    assert len(notices_fired) == 0

    # Tick 2: presumed-dead notice fires (1 notice).
    asyncio.run(api_mod._check_overdue_once())
    first_count = len(notices_fired)
    assert first_count == 1

    # Tick 3: entry is gone — no new notices.
    asyncio.run(api_mod._check_overdue_once())
    assert len(notices_fired) == first_count
    api_mod._RECENTLY_PRESUMED_DEAD.clear()


def test_self_send_not_registered(registry_client):
    """A→A (self-send) does not add anything to the registry."""
    client, api_mod, _ = registry_client
    # Alice sends a broadcast (no `to`) so she's the sole member auto-accepted.
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": []},
    ).json()
    chat_id = created["meta"]["chat_id"]

    # Simulate a send where from == to (self-send via _register helper directly).
    import asyncio

    asyncio.run(api_mod._register_expected_reply("alice", ["alice"], ""))
    assert ("alice", "alice") not in api_mod._EXPECTED_REPLIES


def test_broadcast_send_not_registered(registry_client):
    """chat_send (no `to`) does not register an expected reply."""
    client, api_mod, _ = registry_client
    chat_id = _setup_chat_with_accepted_bob(client)

    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "broadcast", "to": None},
    )

    assert len(api_mod._EXPECTED_REPLIES) == 0


def test_broadcast_resolves_expected_reply_from_peer(registry_client):
    """Broadcast chat_send from a peer who owes a reply resolves the registry entry."""
    client, api_mod, _ = registry_client
    chat_id = _setup_chat_with_accepted_bob(client)

    # alice sends targeted to bob → creates (bob, alice) entry.
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "hey", "to": ["bob"]},
    )
    assert ("bob", "alice") in api_mod._EXPECTED_REPLIES

    # bob replies via broadcast (no `to`) → should resolve (bob, alice).
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "bob", "body": "broadcast reply", "to": None},
    )
    assert ("bob", "alice") not in api_mod._EXPECTED_REPLIES


def test_presumed_dead_cleared_when_registered_by_name_replied_by_uuid(
    registry_client, monkeypatch
):
    """REGRESSION (griffin-consultant-1, 2026-06-29): master sends
    chat_send_to(to=['consultant-1']) by NAME; the consultant's reply resolves to its
    UUID. Register + resolve must key on the SAME canonical UUID so the reply CLEARS the
    expectation — otherwise the daemon fires a false presumed-dead despite the reply."""
    import asyncio

    _client, api_mod, chats_mod = registry_client
    NAME, UUID, MASTER = "consultant-1", "5ee33d55-feed-dead-beef", "master-uuid"
    resolve_map = {NAME: UUID, UUID: UUID, MASTER: MASTER}
    monkeypatch.setattr(
        chats_mod, "_resolve_or_uuid", lambda sid, *, chat_id=None: resolve_map.get(sid, sid)
    )
    monkeypatch.setattr(api_mod, "_threshold_for_session", lambda to_id, chat_id: 90.0)

    async def _noop(_sender):
        return None

    monkeypatch.setattr(api_mod, "_maybe_supersede_presumed_dead", _noop)

    # Master registers an expected reply addressing the consultant by NAME.
    asyncio.run(api_mod._register_expected_reply(MASTER, [NAME], "chat-x"))
    # Keyed by the canonical UUID, NOT the name (the bug keyed by the raw name).
    assert (UUID, MASTER) in api_mod._EXPECTED_REPLIES
    assert (NAME, MASTER) not in api_mod._EXPECTED_REPLIES

    # The consultant replies — its sender id resolves to the UUID. Must clear → no dead.
    asyncio.run(api_mod._resolve_expected_reply(UUID, [], "chat-x"))
    assert (UUID, MASTER) not in api_mod._EXPECTED_REPLIES


def test_reply_clears_all_expectations_awaiting_sender(registry_client, monkeypatch):
    """A reply clears EVERY expectation awaiting that sender (directed OR broadcast),
    not only those directed back at the asker — a session that sends anything is alive.
    Expectations awaiting a DIFFERENT session are left intact."""
    import asyncio

    _client, api_mod, chats_mod = registry_client
    monkeypatch.setattr(chats_mod, "_resolve_or_uuid", lambda sid, *, chat_id=None: sid)
    monkeypatch.setattr(api_mod, "_threshold_for_session", lambda to_id, chat_id: 90.0)

    async def _noop(_sender):
        return None

    monkeypatch.setattr(api_mod, "_maybe_supersede_presumed_dead", _noop)

    # Two askers await bob; bob itself awaits carol (a different awaited-replier).
    asyncio.run(api_mod._register_expected_reply("m1", ["bob"], "c"))
    asyncio.run(api_mod._register_expected_reply("m2", ["bob"], "c"))
    asyncio.run(api_mod._register_expected_reply("bob", ["carol"], "c"))
    assert ("bob", "m1") in api_mod._EXPECTED_REPLIES
    assert ("bob", "m2") in api_mod._EXPECTED_REPLIES
    assert ("carol", "bob") in api_mod._EXPECTED_REPLIES

    # bob sends ANY message → clears both (bob, *); (carol, bob) is untouched.
    asyncio.run(api_mod._resolve_expected_reply("bob", [], "c"))
    assert ("bob", "m1") not in api_mod._EXPECTED_REPLIES
    assert ("bob", "m2") not in api_mod._EXPECTED_REPLIES
    assert ("carol", "bob") in api_mod._EXPECTED_REPLIES


def test_broadcast_resolves_multiple_pending_replies(registry_client):
    """Broadcast from a peer resolves all pending entries where that peer owes replies."""
    import asyncio
    from khimaira.monitor import sessions as sessions_mod

    client, api_mod, _ = registry_client
    # Plant carol session.
    sessions_mod._session_dir("carol").mkdir(parents=True, exist_ok=True)
    (sessions_mod._session_dir("carol") / "status.json").write_text(
        json.dumps({"status": "idle", "name": "carol"}), encoding="utf-8"
    )
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob", "carol"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "carol"})

    # alice sends to bob; carol sends to bob — both create (bob, X) entries.
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "q1", "to": ["bob"]},
    )
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "carol", "body": "q2", "to": ["bob"]},
    )
    assert ("bob", "alice") in api_mod._EXPECTED_REPLIES
    assert ("bob", "carol") in api_mod._EXPECTED_REPLIES

    # bob broadcasts → resolves both.
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "bob", "body": "answer to all", "to": None},
    )
    assert ("bob", "alice") not in api_mod._EXPECTED_REPLIES
    assert ("bob", "carol") not in api_mod._EXPECTED_REPLIES


def test_broadcast_does_not_register_new_expectation(registry_client):
    """Broadcast chat_send never creates new expected-reply entries."""
    client, api_mod, _ = registry_client
    chat_id = _setup_chat_with_accepted_bob(client)

    initial_count = len(api_mod._EXPECTED_REPLIES)
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "broadcast", "to": None},
    )
    assert len(api_mod._EXPECTED_REPLIES) == initial_count


def test_multi_recipient_partial_resolution(registry_client):
    """A→[B, C] creates two entries; B's reply only resolves B's entry, C's stays."""
    client, api_mod, chats_mod = registry_client
    # Create chat with all three members.
    from khimaira.monitor import sessions as sessions_mod

    sessions_mod._session_dir("carol").mkdir(parents=True, exist_ok=True)
    (sessions_mod._session_dir("carol") / "status.json").write_text(
        json.dumps({"status": "idle", "name": "carol"}), encoding="utf-8"
    )
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob", "carol"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "carol"})

    # Alice sends to both bob and carol — creates two registry entries.
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "alice", "body": "consult", "to": ["bob", "carol"]},
    )
    assert ("bob", "alice") in api_mod._EXPECTED_REPLIES
    assert ("carol", "alice") in api_mod._EXPECTED_REPLIES

    # Bob replies to alice — resolves only bob's entry, carol's stays.
    client.post(
        f"/api/chats/{chat_id}/messages",
        json={"sender_session_id": "bob", "body": "bob reply", "to": ["alice"]},
    )
    assert ("bob", "alice") not in api_mod._EXPECTED_REPLIES
    assert ("carol", "alice") in api_mod._EXPECTED_REPLIES


# ---------------------------------------------------------------------------
# Per-chat cursor delivery (Change 1+2)
# ---------------------------------------------------------------------------


def test_advance_cursor_and_cursor_for(chats_api_client):
    """_advance_cursor updates _cursor_for for the correct (session, chat) key."""
    _, chats_mod = chats_api_client
    chats_mod._advance_cursor("alice", "chat-abc", "evt-001")
    chats_mod._advance_cursor("alice", "chat-abc", "evt-002")  # overwrite
    chats_mod._advance_cursor("bob", "chat-abc", "evt-003")  # different session
    assert chats_mod._cursor_for("alice", "chat-abc") == "evt-002"
    assert chats_mod._cursor_for("bob", "chat-abc") == "evt-003"
    assert chats_mod._cursor_for("carol", "chat-abc") is None  # no entry


def test_cursor_persists_across_daemon_restart(chats_api_client, tmp_path, monkeypatch):
    """Cursors written by save_cursors() are read back by load_cursors() correctly."""
    _, chats_mod = chats_api_client
    # Advance two cursors.
    chats_mod._advance_cursor("alice", "chat-x", "evt-100")
    chats_mod._advance_cursor("bob", "chat-y", "evt-200")

    # Persist to disk.
    chats_mod.save_cursors()

    # Simulate daemon restart: clear in-memory state and reload.
    chats_mod._CURSORS.clear()
    assert chats_mod._cursor_for("alice", "chat-x") is None

    chats_mod.load_cursors()
    assert chats_mod._cursor_for("alice", "chat-x") == "evt-100"
    assert chats_mod._cursor_for("bob", "chat-y") == "evt-200"


def _drain_backfill(subscribe_coro, max_events: int = 100, timeout_s: float = 0.3) -> list[dict]:
    """Drain backfill events from an async generator using per-event timeouts.

    Stops collecting when an event takes longer than timeout_s (meaning we've
    crossed into the real-time queue phase, which blocks indefinitely in tests).
    """
    import asyncio

    collected: list[dict] = []

    async def _run(gen):
        try:
            while len(collected) < max_events:
                try:
                    record = await asyncio.wait_for(gen.__anext__(), timeout=timeout_s)
                    collected.append(record)
                except asyncio.TimeoutError:
                    break
                except StopAsyncIteration:
                    break
        finally:
            await gen.aclose()

    asyncio.run(_run(subscribe_coro))
    return collected


def test_multi_chat_backfill_uses_per_chat_cursors(chats_api_client):
    """Session in chats A+B: event broadcast to B during disconnect → backfilled on reconnect."""
    client, chats_mod = chats_api_client
    from khimaira.monitor import sessions as sessions_mod

    # Ensure carol session exists.
    sd = sessions_mod._session_dir("carol")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "status.json").write_text(
        json.dumps({"status": "idle", "name": "carol"}), encoding="utf-8"
    )

    # Create chat A (alice+bob) and chat B (alice+carol).
    resp_a = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_a = resp_a["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_a}/accept", json={"session_id": "bob"})

    resp_b = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["carol"],
            "allow_overlap": True,
        },
    ).json()
    chat_b = resp_b["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_b}/accept", json={"session_id": "carol"})

    # Set alice's cursor for chat A at its last event (simulates prior session).
    lines_a = chats_mod._read(chat_a)
    last_a = next((l for l in reversed(lines_a) if l.get("event_id")), None)
    if last_a:
        chats_mod._advance_cursor("alice", chat_a, last_a["event_id"])

    # Send a message in chat B while alice is "disconnected" (no cursor for B).
    client.post(
        f"/api/chats/{chat_b}/messages",
        json={"sender_session_id": "carol", "body": "hello alice"},
    )
    lines_b = chats_mod._read(chat_b)
    new_msg = next((l for l in reversed(lines_b) if l.get("kind") == "msg"), None)
    assert new_msg is not None, "message should appear in chat B JSONL"

    # Drain backfill events — chat B message should be present.
    collected = _drain_backfill(chats_mod.subscribe("alice"))
    msg_event_ids = {r.get("event_id") for r in collected}
    assert new_msg["event_id"] in msg_event_ids, (
        "expected new chat B message in backfill;\n"
        f"  new_msg event_id: {new_msg['event_id']}\n"
        f"  collected event_ids: {msg_event_ids}"
    )


def test_backfill_without_cursor_uses_last_50(chats_api_client):
    """Reconnecting subscriber with no cursor delivers last ≤50 events per chat.

    Fresh connects (no hint, no cursor) skip backfill. This test simulates
    a reconnect by providing a since_event_id hint, then verifies the last-50
    fallback triggers when the since_event_id isn't found in this chat.
    """
    client, chats_mod = chats_api_client

    # Create two chats: A (where since_event_id will come from) and B (where backfill happens).
    resp_a = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_a = resp_a["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_a}/accept", json={"session_id": "bob"})
    client.post(
        f"/api/chats/{chat_a}/messages",
        json={"sender_session_id": "alice", "body": "anchor"},
    )
    lines_a = [l for l in chats_mod._read(chat_a) if l.get("kind") == "msg"]
    anchor_event_id = lines_a[0]["event_id"]

    from khimaira.monitor import sessions as sessions_mod

    sd = sessions_mod._session_dir("carol")
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "status.json").write_text(json.dumps({"status": "idle", "name": "carol"}), "utf-8")

    resp_b = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["carol"],
            "allow_overlap": True,
        },
    ).json()
    chat_b = resp_b["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_b}/accept", json={"session_id": "carol"})
    for i in range(5):
        client.post(
            f"/api/chats/{chat_b}/messages",
            json={"sender_session_id": "carol", "body": f"msg {i}"},
        )

    # Subscribe with anchor from chat A as the since_event_id hint.
    # Chat B has no cursor → falls to last-50 backfill.
    collected = _drain_backfill(chats_mod.subscribe("alice", since_event_id=anchor_event_id))
    chat_b_events = [r for r in collected if r.get("chat_id") == chat_b]
    assert len(chat_b_events) > 0, "expected backfill events from chat B"
    assert len(chat_b_events) <= 50, "backfill capped at 50"


def test_broadcast_logs_warning_on_disconnected_subscriber(chats_api_client, caplog):
    """_broadcast emits a warning when a member has no SSE subscriber queue."""
    import logging

    client, chats_mod = chats_api_client

    # Create chat and accept.
    resp = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = resp["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    # Send message — bob has no subscriber queue (not connected via SSE).
    with caplog.at_level(logging.WARNING, logger="khimaira.monitor.chats"):
        client.post(
            f"/api/chats/{chat_id}/messages",
            json={"sender_session_id": "alice", "body": "hi bob"},
        )

    warn_msgs = [r.message for r in caplog.records if "no_subscriber" in r.message]
    assert warn_msgs, "expected warning for disconnected subscriber"
    # Warnings may fire for alice (creator) AND bob — at least one must name the chat.
    assert any(chat_id in m for m in warn_msgs), "warning should name the chat_id"


def test_last_event_id_hint_used_when_no_cursor(chats_api_client):
    """When no daemon-side cursor exists, Last-Event-ID header is used as hint."""
    client, chats_mod = chats_api_client

    # Create chat and post 3 messages.
    resp = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = resp["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})
    for i in range(3):
        client.post(
            f"/api/chats/{chat_id}/messages",
            json={"sender_session_id": "alice", "body": f"msg {i}"},
        )

    # Find the event_id of the second message.
    lines = [l for l in chats_mod._read(chat_id) if l.get("kind") == "msg"]
    assert len(lines) >= 2
    pivot_event_id = lines[1]["event_id"]  # last-event-id hint

    # No cursor set. Subscribe with since_event_id = pivot → only last msg delivered.
    collected = _drain_backfill(chats_mod.subscribe("alice", since_event_id=pivot_event_id))
    msg_bodies = [r["body"] for r in collected if r.get("kind") == "msg"]
    # msg 2 should appear (it comes after the pivot), msg 0 should NOT (it's before the pivot).
    assert "msg 2" in msg_bodies, f"expected 'msg 2' in backfill; got: {msg_bodies}"
    assert (
        "msg 0" not in msg_bodies
    ), f"msg 0 predates the pivot and must not appear; got: {msg_bodies}"


# ---------------------------------------------------------------------------
# Guard-4 + #13b-light — escalate-on-stall + throttle grace window
# ---------------------------------------------------------------------------


@pytest.fixture
def guard4_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated state for Guard-4 tests."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    import importlib
    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod

    importlib.reload(chats_mod)
    from khimaira.monitor.api import chats as api_mod

    importlib.reload(api_mod)

    yield chats_mod, sessions_mod, api_mod

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)
    importlib.reload(api_mod)


def _setup_guard4_scenario(
    chats_mod, sessions_mod, assignee_sid: str, silence_s: float = 300.0
) -> str:
    """Create a chat with a pending task assigned to assignee_sid.

    Returns the chat_id.
    """
    import time, json, uuid

    master_sid = str(uuid.uuid4())

    # Create session dirs
    for sid in (master_sid, assignee_sid):
        sd = sessions_mod._BASE_DIR / sid
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "status.json").write_text(
            json.dumps({"status": "idle", "name": sid[:8]}), encoding="utf-8"
        )

    # Create a chat and task
    chat_id = chats_mod.create_room(
        creator_session_id=master_sid,
        member_session_ids=[assignee_sid],
        title="test-roster",
        member_roles={master_sid: "master", assignee_sid: "agent"},
    )["meta"]["chat_id"]
    chats_mod.accept(chat_id, assignee_sid)
    chats_mod.create_task(
        chat_id=chat_id,
        sender_session_id=master_sid,
        body="do the thing",
        assignee_session_id=assignee_sid,
    )
    return chat_id


def test_guard4_escalates_when_process_dead(guard4_env, monkeypatch):
    """Guard-4 AC-1 (live-daemon): session with pending task + dead process → escalates.

    Tests the full _guard4_check_once() → _guard4_escalate() path.
    """
    import asyncio, json, uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    assignee_sid = str(uuid.uuid4())
    chat_id = _setup_guard4_scenario(chats_mod, sessions_mod, assignee_sid)

    notices: list[dict] = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append({"args": a, "kw": kw}) or {},
    )

    # Mock: process is dead
    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: False)

    # Mock: session is silent (last_active_age_s > GUARD4_MIN_SILENCE_S)
    real_list = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw_):
        rows = real_list(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = 300.0
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list)
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(api_mod._guard4_check_once())

    assert len(notices) >= 1, (
        "Guard-4 must escalate when session has obligation + dead process. " f"notices={notices}"
    )
    # Assert the escalation mentions crash and the session
    all_text = " ".join(
        str(n.get("kw", {}).get("text", "")) + " " + str(n.get("args", "")) for n in notices
    )
    assert (
        "crash" in all_text or assignee_sid[:8] in all_text
    ), f"Escalation text should mention crash or session; got: {all_text!r}"


def test_guard4_suppresses_when_alive_within_ceiling(guard4_env, monkeypatch):
    """Guard-4 AC-2 + #13b-light AC-2: process alive + silence ≤ ceiling → suppress."""
    import asyncio, uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    assignee_sid = str(uuid.uuid4())
    _setup_guard4_scenario(chats_mod, sessions_mod, assignee_sid)

    notices: list[tuple] = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(a) or {},
    )

    # Mock: process is alive
    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: True)

    # Silence is within the grace ceiling (small)
    small_silence = 150.0  # > GUARD4_MIN_SILENCE_S (120) but small
    monkeypatch.setattr(api_mod, "_compute_throttle_ceiling_s", lambda: 600.0)

    def _mock_list(use_cache=True, **kw):
        rows = (
            sessions_mod.list_sessions.__wrapped__(use_cache=False)
            if hasattr(sessions_mod.list_sessions, "__wrapped__")
            else sessions_mod.list_sessions(use_cache=False)
        )
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = small_silence
        return rows

    real_list_sessions = sessions_mod.list_sessions

    def _mock_list2(use_cache=True, **kw):
        rows = real_list_sessions(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = small_silence
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list2)
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(api_mod._guard4_check_once())

    assert len(notices) == 0, (
        f"Guard-4 must suppress when process alive + silence ({small_silence}s) ≤ ceiling (600s). "
        f"Got {len(notices)} escalation(s)."
    )


def test_guard4_no_escalation_without_obligation(guard4_env, monkeypatch):
    """Guard-4 AC-4: session silent + no obligations → no escalation."""
    import asyncio, uuid, json

    chats_mod, sessions_mod, api_mod = guard4_env

    # Create a session with NO tasks assigned
    lone_sid = str(uuid.uuid4())
    sd = sessions_mod._BASE_DIR / lone_sid
    sd.mkdir(parents=True, exist_ok=True)
    (sd / "status.json").write_text(json.dumps({"status": "idle"}), encoding="utf-8")

    notices: list = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(a) or {},
    )
    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: False)

    real_list = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw):
        rows = real_list(use_cache=False)
        for r in rows:
            if r.get("session_id") == lone_sid:
                r["last_active_age_s"] = 300.0
        return rows

    real_fn = sessions_mod.list_sessions
    monkeypatch.setattr(sessions_mod, "list_sessions", lambda **kw: real_fn(use_cache=False))
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(api_mod._guard4_check_once())

    # No tasks → no escalation regardless of liveness
    task_related = [n for n in notices if lone_sid[:8] in str(n)]
    assert len(task_related) == 0, "Guard-4 must be silent when session has no task obligations."


def test_guard4_debounce_fires_once(guard4_env, monkeypatch):
    """Guard-4 AC-5 (debounce): repeated scans with same stalled session fire escalation once."""
    import asyncio, uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    assignee_sid = str(uuid.uuid4())
    _setup_guard4_scenario(chats_mod, sessions_mod, assignee_sid)

    notices: list = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(a) or {},
    )
    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: False)

    real_fn = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw):
        rows = real_fn(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = 300.0
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list)
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(api_mod._guard4_check_once())
    count_after_first = len(notices)

    asyncio.run(api_mod._guard4_check_once())
    count_after_second = len(notices)


# ---------------------------------------------------------------------------
# #13b-heavy — _handle_throttle_escalation unit tests
# ---------------------------------------------------------------------------


@pytest.fixture
def throttle_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Isolated state for throttle-escalation unit tests."""
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    import importlib
    from khimaira.monitor import sessions as sessions_mod

    importlib.reload(sessions_mod)
    from khimaira.monitor import chats as chats_mod

    importlib.reload(chats_mod)
    from khimaira.monitor.api import chats as api_mod

    importlib.reload(api_mod)

    yield chats_mod, sessions_mod, api_mod

    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(sessions_mod)
    importlib.reload(chats_mod)
    importlib.reload(api_mod)


_THROTTLE_PAYLOAD = {
    "retry_attempt": 10,
    "max_retries": 10,
    "overload_count": 27,
    "last_timestamp": "2026-05-31T02:00:00.000Z",
    "message": "Overloaded. Retry.",
}


def test_throttle_escalation_cooldown_suppresses(throttle_env, monkeypatch):
    """Second POST within cooldown window → escalated:False, reason:cooldown."""
    import asyncio, time

    chats_mod, sessions_mod, api_mod = throttle_env

    notices: list = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(kw) or {},
    )
    monkeypatch.setattr(api_mod, "_get_session_obligations", lambda sid: [])

    async def _fake_post(chat_id, body, kind=None):
        return {}

    monkeypatch.setattr("khimaira.monitor.chats._post_synthetic_message", _fake_post)

    api_mod._THROTTLE_STATE.clear()

    sid = "aaaaaaaa-bbbb-4000-8000-000000000001"
    # First call — should escalate
    r1 = asyncio.run(api_mod._handle_throttle_escalation(sid, _THROTTLE_PAYLOAD))
    assert r1["escalated"] is True

    # Second call within cooldown — should be suppressed
    r2 = asyncio.run(api_mod._handle_throttle_escalation(sid, _THROTTLE_PAYLOAD))
    assert r2["escalated"] is False
    assert r2["reason"] == "cooldown"
    assert "cooldown_remaining_s" in r2


def test_throttle_escalation_obligation_scoped(throttle_env, monkeypatch):
    """Session with in_progress task → obligation-scoped escalation + chat broadcast."""
    import asyncio

    chats_mod, sessions_mod, api_mod = throttle_env

    posted_bodies: list = []
    notices: list = []

    async def _fake_post(chat_id, body):
        posted_bodies.append((chat_id, body))
        return {}

    monkeypatch.setattr("khimaira.monitor.chats._post_synthetic_message", _fake_post)
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(kw) or {},
    )

    obligation = {
        "task_id": "task-aabbccdd1234",
        "chat_id": "chat-test01",
        "status": "in_progress",
        "begin_fired": True,
    }
    monkeypatch.setattr(api_mod, "_get_session_obligations", lambda sid: [obligation])
    monkeypatch.setattr(api_mod, "_resolve_intake_session_id", lambda cid: "intake-sid")
    monkeypatch.setattr(api_mod, "_resolve_master_session_id", lambda cid: None)

    api_mod._THROTTLE_STATE.clear()

    sid = "bbbbbbbb-cccc-4000-8000-000000000002"
    r = asyncio.run(api_mod._handle_throttle_escalation(sid, _THROTTLE_PAYLOAD))

    assert r["escalated"] is True
    assert r["obligation_scoped"] is True
    assert r["obligations"] == 1
    # Chat broadcast happened
    assert any("THROTTLE-ESCALATION" in b for _, b in posted_bodies)
    # Notice posted to intake
    assert len(notices) >= 1


def test_throttle_escalation_bare_idle(throttle_env, monkeypatch):
    """Session with no obligations → informational alert to membership chats, no Guard-4."""
    import asyncio

    chats_mod, sessions_mod, api_mod = throttle_env

    posted_bodies: list = []
    guard4_calls: list = []

    async def _fake_post(chat_id, body):
        posted_bodies.append((chat_id, body))
        return {}

    async def _fake_guard4(*a, **kw):
        guard4_calls.append(a)

    monkeypatch.setattr("khimaira.monitor.chats._post_synthetic_message", _fake_post)
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: {},
    )
    monkeypatch.setattr(api_mod, "_get_session_obligations", lambda sid: [])
    monkeypatch.setattr(api_mod, "_guard4_escalate", _fake_guard4)
    monkeypatch.setattr(api_mod, "_chats_for_session", lambda sid: ["chat-idle01", "chat-idle02"])

    api_mod._THROTTLE_STATE.clear()

    sid = "cccccccc-dddd-4000-8000-000000000003"
    r = asyncio.run(api_mod._handle_throttle_escalation(sid, _THROTTLE_PAYLOAD))

    assert r["escalated"] is True
    assert r["obligation_scoped"] is False
    assert r["chats"] == 2
    # No Guard-4 for bare-idle
    assert len(guard4_calls) == 0
    # Informational alert was broadcast to both chats
    assert any("THROTTLE-ALERT" in b for _, b in posted_bodies)


def test_throttle_escalation_guard4_harder_after_n(throttle_env, monkeypatch):
    """After N obligation-scoped escalations, _guard4_escalate is called."""
    import asyncio

    chats_mod, sessions_mod, api_mod = throttle_env

    guard4_calls: list = []

    async def _fake_post(chat_id, body):
        return {}

    async def _fake_guard4(*a, **kw):
        guard4_calls.append(a)

    monkeypatch.setattr("khimaira.monitor.chats._post_synthetic_message", _fake_post)
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: {},
    )

    obligation = {
        "task_id": "task-gghhii111222",
        "chat_id": "chat-test02",
        "status": "in_progress",
        "begin_fired": True,
    }
    monkeypatch.setattr(api_mod, "_get_session_obligations", lambda sid: [obligation])
    monkeypatch.setattr(api_mod, "_resolve_intake_session_id", lambda cid: None)
    monkeypatch.setattr(api_mod, "_resolve_master_session_id", lambda cid: None)
    monkeypatch.setattr(api_mod, "_guard4_escalate", _fake_guard4)

    # Reduce cooldown to 0 so each call fires
    monkeypatch.setattr(api_mod, "_THROTTLE_COOLDOWN_S", 0.0)
    n = api_mod._THROTTLE_ESCALATE_AFTER_N

    api_mod._THROTTLE_STATE.clear()

    sid = "dddddddd-eeee-4000-8000-000000000004"
    for _ in range(n - 1):
        asyncio.run(api_mod._handle_throttle_escalation(sid, _THROTTLE_PAYLOAD))
    assert len(guard4_calls) == 0, "Guard-4 must not fire before N escalations"

    asyncio.run(api_mod._handle_throttle_escalation(sid, _THROTTLE_PAYLOAD))
    assert len(guard4_calls) >= 1, "Guard-4 must fire after N escalations"


def test_throttle_http_endpoint_live(throttle_env, monkeypatch):
    """AC: live-daemon path — POST /api/sessions/{id}/throttle reaches handler."""
    import importlib

    chats_mod, sessions_mod, api_mod = throttle_env

    importlib.reload(api_mod)
    handled: list = []

    original = api_mod._handle_throttle_escalation

    async def _capture(session_id, payload):
        handled.append(session_id)
        return {"escalated": False, "reason": "test_capture"}

    monkeypatch.setattr(api_mod, "_handle_throttle_escalation", _capture)

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    app = FastAPI()
    app.include_router(api_mod.build_router(), prefix="/api")
    client = TestClient(app)

    sid = "eeeeeeee-ffff-4000-8000-000000000005"
    resp = client.post(
        f"/api/sessions/{sid}/throttle",
        json={"retry_attempt": 10, "max_retries": 10, "overload_count": 27},
    )
    assert resp.status_code == 200
    assert handled == [sid], "POST /throttle must reach _handle_throttle_escalation"


# ---------------------------------------------------------------------------
# Guard-4 refinement tests — 2D pending-gate (Fix A) + obligation-scoped debounce (Fix B)
# ---------------------------------------------------------------------------


def test_guard4_pending_no_begin_alive_does_not_escalate(guard4_env, monkeypatch):
    """Fix A AC-1: pending + NO-BEGIN + alive → NO escalate (compliant BEGIN-waiting).

    Agent-1's exact false-positive case: the agent is correctly holding until
    master fires BEGIN. Guard-4 must not penalize this compliance.
    """
    import asyncio, uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    assignee_sid = str(uuid.uuid4())
    _setup_guard4_scenario(chats_mod, sessions_mod, assignee_sid)
    # No BEGIN fired (pending task, no signal_task_start)

    notices: list = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(a) or {},
    )

    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: True)
    monkeypatch.setattr(api_mod, "_compute_throttle_ceiling_s", lambda: 60.0)

    real_list = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw):
        rows = real_list(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = 300.0  # > ceiling
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list)
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(api_mod._guard4_check_once())

    assert len(notices) == 0, (
        "Guard-4 must NOT escalate pending+no-BEGIN+alive (compliant BEGIN-waiting). "
        f"Got {len(notices)} escalation(s). agent-1's false-positive case."
    )


def test_guard4_pending_no_begin_unknown_does_not_escalate(guard4_env, monkeypatch):
    """Fix A AC-2: pending + NO-BEGIN + unknown liveness → NO escalate."""
    import asyncio, uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    assignee_sid = str(uuid.uuid4())
    _setup_guard4_scenario(chats_mod, sessions_mod, assignee_sid)

    notices: list = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(a) or {},
    )

    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: None)  # unknown
    monkeypatch.setattr(api_mod, "_compute_throttle_ceiling_s", lambda: 60.0)

    real_list = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw):
        rows = real_list(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = 300.0
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list)
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(api_mod._guard4_check_once())

    assert len(notices) == 0, (
        "Guard-4 must NOT escalate pending+no-BEGIN+unknown-liveness. "
        f"Got {len(notices)} escalation(s)."
    )


def test_guard4_pending_no_begin_dead_escalates(guard4_env, monkeypatch):
    """Fix A AC-3: pending + NO-BEGIN + confirmed-dead → ESCALATE.

    A dead agent never posts a ready-ack → #14a auto-BEGIN can't fire → reassign needed.
    """
    import asyncio, uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    assignee_sid = str(uuid.uuid4())
    _setup_guard4_scenario(chats_mod, sessions_mod, assignee_sid)

    notices: list = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append({"args": a, "kw": kw}) or {},
    )

    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: False)  # dead

    real_list = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw):
        rows = real_list(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = 300.0
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list)
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(api_mod._guard4_check_once())

    assert len(notices) >= 1, (
        "Guard-4 must escalate pending+no-BEGIN+confirmed-dead (dead agent can't self-recover). "
        f"Got {len(notices)} escalations."
    )


def test_guard4_pending_begin_fired_alive_escalates(guard4_env, monkeypatch):
    """Fix A AC-4: pending + BEGIN-fired + not-started + alive → ESCALATE (when > ceiling).

    Agent got BEGIN, #14b nagged it, still not started → wedged → Guard-4 escalates to master.
    """
    import asyncio, uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    master_sid = str(uuid.uuid4())
    assignee_sid = str(uuid.uuid4())

    for sid in (master_sid, assignee_sid):
        import json

        sd = sessions_mod._BASE_DIR / sid
        sd.mkdir(parents=True, exist_ok=True)
        (sd / "status.json").write_text(
            json.dumps({"status": "idle", "name": sid[:8]}), encoding="utf-8"
        )

    chat_id = chats_mod.create_room(
        creator_session_id=master_sid,
        member_session_ids=[assignee_sid],
        title="begin-test",
        member_roles={master_sid: "master", assignee_sid: "agent"},
    )["meta"]["chat_id"]
    chats_mod.accept(chat_id, assignee_sid)
    task = chats_mod.create_task(
        chat_id=chat_id,
        sender_session_id=master_sid,
        body="do the thing",
        assignee_session_id=assignee_sid,
    )
    # Fire BEGIN
    chats_mod.signal_task_start(chat_id, task["id"], master_sid)

    notices: list = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(a) or {},
    )

    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: True)  # alive
    monkeypatch.setattr(api_mod, "_compute_throttle_ceiling_s", lambda: 60.0)

    real_list = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw):
        rows = real_list(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = 300.0  # > ceiling
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list)
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(api_mod._guard4_check_once())

    assert len(notices) >= 1, (
        "Guard-4 must escalate pending+BEGIN-fired+alive+silence>ceiling (wedged post-BEGIN). "
        f"Got {len(notices)} escalations."
    )


def test_guard4_debounce_not_reset_by_activity_blip(guard4_env, monkeypatch):
    """Fix B AC-9: a legitimate escalation fires ONCE; an activity-blip does NOT re-arm.

    After escalation, if the session briefly becomes active (last_active_age_s drops
    below MIN_SILENCE_S) but the obligation persists, the next sweep must NOT re-escalate.
    """
    import asyncio, uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    assignee_sid = str(uuid.uuid4())
    _setup_guard4_scenario(chats_mod, sessions_mod, assignee_sid)

    notices: list = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda *a, **kw: notices.append(a) or {},
    )

    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: False)  # dead

    silence = [300.0]  # mutable for sweep-by-sweep control

    real_list = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw):
        rows = real_list(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = silence[0]
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list)
    api_mod._GUARD4_STALLED.clear()

    # First sweep → escalates
    asyncio.run(api_mod._guard4_check_once())
    count_after_first = len(notices)
    assert count_after_first >= 1, "First sweep must escalate."

    # Activity-blip: session briefly active (silence drops below MIN_SILENCE_S)
    silence[0] = 10.0  # below _GUARD4_MIN_SILENCE_S
    asyncio.run(api_mod._guard4_check_once())
    # Blip doesn't re-arm the debounce (session still has the same obligation)

    # Session goes silent again beyond ceiling
    silence[0] = 300.0
    asyncio.run(api_mod._guard4_check_once())
    count_after_blip = len(notices)

    assert count_after_blip == count_after_first, (
        f"Guard-4 debounce must persist through activity-blips. "
        f"First={count_after_first}, after blip+re-silence={count_after_blip}. "
        "The obligation hasn't cleared — no re-escalation should fire."
    )


# ---------------------------------------------------------------------------
# POST /chats/{chat_id}/grant-role
# ---------------------------------------------------------------------------


def test_post_grant_role_happy_path(chats_api_client):
    """Master grants a new role to an accepted member → 200 + member_roles updated."""
    client, chats_mod = chats_api_client
    # Create room: alice=master, bob=agent
    resp = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["bob"],
            "title": "grant-role test",
            "member_roles": {"alice": "master", "bob": "agent"},
        },
    )
    assert resp.status_code == 200
    chat_id = resp.json()["meta"]["chat_id"]

    # Bob accepts
    client.post("/api/chats/{}/accept".format(chat_id), json={"session_id": "bob"})

    # Alice grants bob critic role
    r = client.post(
        f"/api/chats/{chat_id}/grant-role",
        json={
            "by_session_id": "alice",
            "target_session_id": "bob",
            "role": "critic",
        },
    )
    assert r.status_code == 200
    new_meta = r.json()
    assert new_meta["member_roles"]["bob"] == "critic"


def test_post_grant_role_non_master_returns_403(chats_api_client):
    """Non-master caller → 403 Forbidden."""
    client, _ = chats_api_client
    resp = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["bob"],
            "title": "grant-role 403 test",
            "member_roles": {"alice": "master", "bob": "agent"},
        },
    )
    chat_id = resp.json()["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    r = client.post(
        f"/api/chats/{chat_id}/grant-role",
        json={
            "by_session_id": "bob",  # bob is agent, not master
            "target_session_id": "alice",
            "role": "agent",
        },
    )
    assert r.status_code == 403


# ---------------------------------------------------------------------------
# Guard-5 Part A — gates-as-tasks primitives
# ---------------------------------------------------------------------------


def _setup_room_with_master(client, chats_mod, master="alice", members=("bob",), member_roles=None):
    """Helper: create a room, accept all members, return chat_id."""
    roles = member_roles or {master: "master"}
    resp = client.post(
        "/api/chats",
        json={
            "creator_session_id": master,
            "member_session_ids": list(members),
            "title": "guard5-test",
            "member_roles": roles,
        },
    )
    chat_id = resp.json()["meta"]["chat_id"]
    for m in members:
        client.post(f"/api/chats/{chat_id}/accept", json={"session_id": m})
    return chat_id


def test_create_task_with_assignee_role(chats_api_client):
    """create_task with assignee_role='critic' stores the role-class assignee."""
    client, chats_mod = chats_api_client
    chat_id = _setup_room_with_master(
        client, chats_mod, member_roles={"alice": "master", "bob": "critic"}
    )
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "Review this",
            "assignee_role": "critic",
            "gate_required": False,
            "gate_for": None,
            "verdict_role": "critic",
        },
    )
    assert r.status_code == 200
    task = r.json()
    assert task["assignee_role"] == "critic"
    assert task["verdict_role"] == "critic"
    assert task["gate_for"] is None
    assert task["status"] == "pending"


def test_auto_create_review_tasks_on_done(chats_api_client):
    """LEGACY roster: a gate_required task → done AUTO-creates critic + verifier
    review-tasks (the wrapper pair is created only for verdict-roles PRESENT in
    member_roles)."""
    client, chats_mod = chats_api_client
    _plant_session("dave")
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        members=("bob", "carol", "dave"),
        member_roles={
            "alice": "master",
            "bob": "agent",
            "carol": "critic",
            "dave": "verifier",
        },
    )
    # Create a gate_required work-task
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "Do the thing",
            "assignee_session_id": "bob",
            "gate_required": True,
        },
    )
    work_task_id = r.json()["id"]

    # bob moves it in_progress
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )

    # bob marks it done — AUTO-create should fire
    r = client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )
    assert r.status_code == 200

    # Check review-tasks were created
    room = chats_mod.load_room(chat_id)
    review_tasks = [
        m
        for m in room["messages"]
        if m.get("kind") == chats_mod.TASK and m.get("gate_for") == work_task_id
    ]
    verdict_roles = {t["verdict_role"] for t in review_tasks}
    assert "critic" in verdict_roles
    assert "verifier" in verdict_roles


def _gate_required_done(client, chat_id):
    """Helper: create a gate_required work-task assigned to bob and drive it to done."""
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "Do the thing",
            "assignee_session_id": "bob",
            "gate_required": True,
        },
    )
    work_task_id = r.json()["id"]
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )
    return work_task_id


def _wrapper_roles(chats_mod, chat_id, work_task_id):
    room = chats_mod.load_room(chat_id)
    return {
        m["verdict_role"]
        for m in room["messages"]
        if m.get("kind") == chats_mod.TASK and m.get("gate_for") == work_task_id
    }


def test_auto_create_review_tasks_lean_gatekeeper_creates_zero(chats_api_client):
    """LEAN roster (gatekeeper, no critic/verifier): a gate_required task → done creates
    ZERO wrapper review-tasks — the gatekeeper discharges the gate via a DIRECT verdict
    on the work-task, so a wrapper for an absent critic/verifier role (which nobody
    could satisfy) must NOT be created."""
    client, chats_mod = chats_api_client
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        members=("bob", "carol"),
        member_roles={"alice": "master", "bob": "agent", "carol": "gatekeeper"},
    )
    work_task_id = _gate_required_done(client, chat_id)
    assert _wrapper_roles(chats_mod, chat_id, work_task_id) == set()


def test_auto_create_review_tasks_present_roles_only(chats_api_client):
    """A roster with critic but NO verifier creates ONLY the critic wrapper — never a
    wrapper for an absent role."""
    client, chats_mod = chats_api_client
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        members=("bob", "carol"),
        member_roles={"alice": "master", "bob": "agent", "carol": "critic"},
    )
    work_task_id = _gate_required_done(client, chat_id)
    assert _wrapper_roles(chats_mod, chat_id, work_task_id) == {"critic"}


def test_master_override_verdict_audited(chats_api_client):
    """master_override_verdict with reason+trigger → 200 and is_override=True on record."""
    client, chats_mod = chats_api_client
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        member_roles={"alice": "master", "bob": "agent"},
    )
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={"sender_session_id": "alice", "body": "task for override"},
    )
    task_id = r.json()["id"]

    r = client.post(
        f"/api/chats/{chat_id}/tasks/{task_id}/override-verdict",
        json={
            "by_session_id": "alice",
            "verdict": "approve",
            "reason": "quorum timed out after 30min, no reachable critic",
            "trigger": "quorum_timeout",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["is_override"] is True
    assert body["override_trigger"] == "quorum_timeout"
    assert "quorum" in body["override_reason"].lower()


def test_master_override_verdict_non_master_blocked(chats_api_client):
    """Non-master cannot post override verdict → 403."""
    client, chats_mod = chats_api_client
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        member_roles={"alice": "master", "bob": "agent"},
    )
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={"sender_session_id": "alice", "body": "task"},
    )
    task_id = r.json()["id"]

    r = client.post(
        f"/api/chats/{chat_id}/tasks/{task_id}/override-verdict",
        json={
            "by_session_id": "bob",  # bob is agent, not master
            "verdict": "approve",
            "reason": "trying to bypass",
            "trigger": "manual_deadlock",
        },
    )
    assert r.status_code == 403


def test_auto_create_review_tasks_no_duplicate_while_open(chats_api_client):
    """LEGACY roster: a second AUTO-create doesn't duplicate while prior review-tasks
    are still OPEN."""
    client, chats_mod = chats_api_client
    _plant_session("dave")
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        members=("bob", "carol", "dave"),
        member_roles={
            "alice": "master",
            "bob": "agent",
            "carol": "critic",
            "dave": "verifier",
        },
    )
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "Do the thing",
            "assignee_session_id": "bob",
            "gate_required": True,
        },
    )
    work_task_id = r.json()["id"]

    # First done → creates 2 open review-tasks
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )

    # changes_requested → in_progress → done again (review-tasks still OPEN)
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "alice", "new_status": "changes_requested"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )

    # Open review-tasks still exist → no duplicates; should still be exactly 2
    room = chats_mod.load_room(chat_id)
    review_tasks = [
        m
        for m in room["messages"]
        if m.get("kind") == chats_mod.TASK and m.get("gate_for") == work_task_id
    ]
    assert len(review_tasks) == 2, f"Expected 2 review-tasks (no dup), got {len(review_tasks)}"


def test_gate_bypass_prevented_on_satisfied_prior_round(chats_api_client):
    """B3 gate-bypass prevention: stale approve+ship from round-1 must NOT satisfy round-2.

    Scenario: critic approves + verifier ships in round-1 → master sends changes_requested
    despite verdicts → agent re-submits (done again) → gate must NOT be satisfied by
    the stale round-1 verdicts (that would let un-reviewed changes bypass B3).

    This is the DISTINGUISHING test: dedup-on-ANY vs dedup-on-OPEN diverge ONLY
    on the satisfied-prior path. 'exactly 2 tasks' passes under both implementations;
    this test does not.
    """
    client, chats_mod = chats_api_client
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        members=("bob", "carol"),
        member_roles={"alice": "master", "bob": "agent", "carol": "critic"},
    )
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "Do the thing",
            "assignee_session_id": "bob",
            "gate_required": True,
        },
    )
    work_task_id = r.json()["id"]

    # Round 1: bob does task → done
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )

    # Round-1 verdicts: critic approves + verifier ships on the WORK-TASK
    chats_mod.record_gate_verdict(chat_id, "carol", work_task_id, "approve")
    # Simulate verifier shipping (override role check for test simplicity by
    # directly inserting a verdict record via _append)
    import uuid

    chats_mod._append(
        chat_id,
        {
            "kind": chats_mod.TASK_VERDICT,
            "event_id": chats_mod._new_event_id(),
            "ts": chats_mod._now_iso(),
            "chat_id": chat_id,
            "task_id": work_task_id,
            "verdict": "ship",
            "by_session_id": "alice",  # not a real verifier — test only
            "by_name": "alice",
        },
    )

    # Verify round-1 gate is satisfied
    verdicts_r1 = chats_mod.get_gate_verdicts_by_task("alice", work_task_id)
    assert isinstance(verdicts_r1, dict)
    assert verdicts_r1["critic_approved"] is True
    assert verdicts_r1["verifier_shipped"] is True

    # Master requests changes despite satisfied verdicts → round-2 begins
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "alice", "new_status": "changes_requested"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )

    # Round-2 gate must be ABSENT (stale round-1 verdicts invalidated)
    verdicts_r2 = chats_mod.get_gate_verdicts_by_task("alice", work_task_id)
    assert (
        verdicts_r2 == "absent"
    ), f"Expected gate ABSENT for round-2 (stale verdicts must be invalidated), got {verdicts_r2}"


def test_gate_satisfied_when_verdicts_precede_auto_advance_done(chats_api_client):
    """IN-MASTER-9 false-block regression (audit 2026-06-21, muther task 625).

    When critic APPROVE + verifier SHIP arrive BEFORE the task transitions to done
    — e.g. the daemon's gate-auto-advance writes the done FROM those very verdicts —
    the master approve gate must STILL see them satisfied. The old round filter keyed
    on the last DONE transition, so the gate-completing verdicts (now earlier than the
    auto-advance done) were wrongly treated as 'prior round' → 'absent' → IN-MASTER-9
    blocked master's →approved. The fix keys the round on REOPEN (done→active), not
    on last-done; a done-transition is the gate closing, not a new round. This test
    FAILS on the old last-done filter and PASSES on the reopen-based one.
    """
    client, chats_mod = chats_api_client
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        members=("bob", "carol"),
        member_roles={"alice": "master", "bob": "agent", "carol": "critic"},
    )
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "Do the thing",
            "assignee_session_id": "bob",
            "gate_required": True,
        },
    )
    work_task_id = r.json()["id"]
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )

    # Verdicts arrive while still in_progress (critic approve + verifier ship).
    chats_mod.record_gate_verdict(chat_id, "carol", work_task_id, "approve")
    chats_mod._append(
        chat_id,
        {
            "kind": chats_mod.TASK_VERDICT,
            "event_id": chats_mod._new_event_id(),
            "ts": chats_mod._now_iso(),
            "chat_id": chat_id,
            "task_id": work_task_id,
            "verdict": "ship",
            "by_session_id": "alice",  # not a real verifier — test only
            "by_name": "alice",
        },
    )

    # THEN the task transitions to done AFTER the verdicts — the gate-auto-advance
    # shape that poisoned the old last-done filter (no reopen follows).
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )

    # The approve gate must STILL be satisfied (NOT 'absent').
    verdicts = chats_mod.get_gate_verdicts_by_task("alice", work_task_id)
    assert isinstance(
        verdicts, dict
    ), f"expected satisfied dict (verdicts precede the done), got {verdicts!r}"
    assert verdicts["critic_approved"] is True
    assert verdicts["verifier_shipped"] is True


def test_auto_create_review_tasks_fresh_on_re_done_after_closed(chats_api_client):
    """LEGACY roster: after prior review-tasks are closed, re-done creates fresh
    obligations."""
    client, chats_mod = chats_api_client
    _plant_session("dave")
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        members=("bob", "carol", "dave"),
        member_roles={
            "alice": "master",
            "bob": "agent",
            "carol": "critic",
            "dave": "verifier",
        },
    )
    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "Do the thing",
            "assignee_session_id": "bob",
            "gate_required": True,
        },
    )
    work_task_id = r.json()["id"]

    # First done → creates 2 review-tasks
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )

    room = chats_mod.load_room(chat_id)
    review_task_ids = [
        m["id"]
        for m in room["messages"]
        if m.get("kind") == chats_mod.TASK and m.get("gate_for") == work_task_id
    ]
    assert len(review_task_ids) == 2

    # Close review-tasks (mark them done — simulating reviewer completed them)
    for rtid in review_task_ids:
        client.post(
            f"/api/chats/{chat_id}/tasks/{rtid}/status",
            json={"by_session_id": "alice", "new_status": "cancelled"},
        )

    # Work-task: changes_requested → in_progress → done again (review-tasks now CLOSED)
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "alice", "new_status": "changes_requested"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )
    client.post(
        f"/api/chats/{chat_id}/tasks/{work_task_id}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )

    # Should now have 4 review-tasks total (2 closed + 2 fresh)
    room = chats_mod.load_room(chat_id)
    all_review_tasks = [
        m
        for m in room["messages"]
        if m.get("kind") == chats_mod.TASK and m.get("gate_for") == work_task_id
    ]
    assert (
        len(all_review_tasks) == 4
    ), f"Expected 4 review-tasks (2 fresh), got {len(all_review_tasks)}"


def test_is_reachable_false_when_not_subscribed(chats_api_client):
    """is_reachable returns False for a session with no open SSE subscription."""
    client, chats_mod = chats_api_client
    # alice has no SSE subscriber (test environment — no live /events connection)
    assert chats_mod.is_reachable("alice") is False


def test_guard4_suppressed_during_wind_down(chats_api_client, monkeypatch):
    """Guard-4 does not escalate when roster is in wind-down."""
    import asyncio
    from khimaira.monitor import sessions as sessions_mod
    from khimaira.monitor.api import chats as api_mod

    importlib.reload(sessions_mod)
    importlib.reload(api_mod)

    sessions_mod.set_roster_wind_down(True)
    try:
        # Patch escalation to verify it never fires
        escalated = []

        async def _mock_escalate(*args, **kwargs):
            escalated.append(args)

        monkeypatch.setattr(api_mod, "_guard4_escalate", _mock_escalate)

        asyncio.run(api_mod._guard4_check_once())
        assert escalated == [], "Guard-4 should not escalate during wind-down"
    finally:
        sessions_mod.set_roster_wind_down(False)


# ---------------------------------------------------------------------------
# K3b — API-layer roster-overlap guard (HTTP 409)
# ---------------------------------------------------------------------------


def test_create_room_api_rejects_overlapping_roster(chats_api_client):
    """POST /api/chats with ≥50% member overlap returns 409 with existing_chat_id."""
    client, _ = chats_api_client

    # Create roster A.
    resp_a = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["bob", "carol"],
            "title": "Roster A",
        },
    )
    assert resp_a.status_code == 200
    chat_a_id = resp_a.json()["meta"]["chat_id"]

    # Attempt roster B with the same members — expect 409.
    resp_b = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["bob", "carol"],
            "title": "Roster B",
            "fresh": True,
        },
    )
    assert resp_b.status_code == 409
    body = resp_b.json()
    # FastAPI wraps HTTPException detail in {"detail": ...}
    detail = body.get("detail", body)
    assert detail["existing_chat_id"] == chat_a_id
    assert detail["overlap_count"] >= 2
    assert isinstance(detail["overlap_members"], list)


def test_create_room_api_allow_overlap_creates_second_chat(chats_api_client):
    """POST /api/chats with allow_overlap=true bypasses the 409 guard."""
    client, _ = chats_api_client

    client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["bob", "carol"],
            "title": "Roster A",
        },
    )
    resp_b = client.post(
        "/api/chats",
        json={
            "creator_session_id": "alice",
            "member_session_ids": ["bob", "carol"],
            "title": "Roster B",
            "fresh": True,
            "allow_overlap": True,
        },
    )
    assert resp_b.status_code == 200
    assert resp_b.json()["meta"]["title"] == "Roster B"


# ---------------------------------------------------------------------------
# #14 — auto-BEGIN HTTP-layer wire-up
# ---------------------------------------------------------------------------


def test_auto_begin_fires_through_http(chats_api_client):
    """POST /api/chats/{id}/tasks with required_agents → compliant ack via POST /messages
    → TASK_SIGNAL start written.  Verifies the HTTP-layer field threading from
    CreateTaskReq through to _try_auto_begin (verifier gap, 2026-06-06).
    """
    client, chats_mod = chats_api_client

    # Room: alice (master / creator) + bob (agent).
    created = client.post(
        "/api/chats",
        json={"creator_session_id": "alice", "member_session_ids": ["bob"]},
    ).json()
    chat_id = created["meta"]["chat_id"]
    client.post(f"/api/chats/{chat_id}/accept", json={"session_id": "bob"})

    # Create task with auto-BEGIN fields.
    task_resp = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "implement #14 http test",
            "assignee_session_id": "bob",
            "required_agents": ["bob"],
            "auto_begin": True,
            "required_model": "sonnet",
            "required_effort": "medium",
        },
    )
    assert task_resp.status_code == 200
    task_id = task_resp.json()["id"]
    assert not chats_mod._is_task_begun(chat_id, task_id)

    # Bob sends a compliant ready-ack via the HTTP messages endpoint.
    ack_resp = client.post(
        f"/api/chats/{chat_id}/messages",
        json={
            "sender_session_id": "bob",
            "body": f"✅ ready [task-id: {task_id}] | model=sonnet effort=medium",
        },
    )
    assert ack_resp.status_code == 200

    # BEGIN must have fired synchronously inside send_message.
    assert chats_mod._is_task_begun(chat_id, task_id)


def test_session_has_in_progress_assigned_task(chats_api_client):
    """IN-AGENT-7 detection (session_has_in_progress_assigned_task): True ONLY when the
    session holds an in_progress task assigned to it. False for no task, a pending
    (assigned-but-not-started) task — jumping BEGIN does not license edits — and done.
    """
    client, chats_mod = chats_api_client
    chat_id = _setup_room_with_master(
        client,
        chats_mod,
        members=("bob",),
        member_roles={"alice": "master", "bob": "agent"},
    )
    # no task at all → False (the self-dispatch case)
    assert chats_mod.session_has_in_progress_assigned_task("bob") is False

    r = client.post(
        f"/api/chats/{chat_id}/tasks",
        json={
            "sender_session_id": "alice",
            "body": "do it",
            "assignee_session_id": "bob",
        },
    )
    tid = r.json()["id"]
    # pending (assigned, BEGIN not signalled) → still False (jumping BEGIN ≠ licensed)
    assert chats_mod.session_has_in_progress_assigned_task("bob") is False

    client.post(
        f"/api/chats/{chat_id}/tasks/{tid}/status",
        json={"by_session_id": "bob", "new_status": "in_progress"},
    )
    # in_progress assigned to bob → True (legitimate work, no warn)
    assert chats_mod.session_has_in_progress_assigned_task("bob") is True

    client.post(
        f"/api/chats/{chat_id}/tasks/{tid}/status",
        json={"by_session_id": "bob", "new_status": "done"},
    )
    # done → False (no longer an active assignment)
    assert chats_mod.session_has_in_progress_assigned_task("bob") is False


# ---------------------------------------------------------------------------
# Freeze-class invariant (2026-07-13) — live production incident: py-spy
# caught MainThread (the event loop) stuck synchronously inside
# _get_session_obligations, called directly (no asyncio.to_thread) from
# _guard4_check_once's 60s-interval watcher, once per session in
# sessions_mod.list_sessions() (128 sessions observed live) — every tick
# re-globbing and re-parsing every chat-*.jsonl file for every session, on
# the event loop. Same bug class as the kitty-subprocess freeze (ca89f97),
# an entirely different code path the earlier audit never covered. Fixed by
# wrapping every call site in asyncio.to_thread. This test proves the CLASS
# (event loop stays live during the call), not just a return-value check —
# every other Guard-4 test above mocks obligations away entirely and
# wouldn't have caught a missing to_thread wrap.
# ---------------------------------------------------------------------------


async def _assert_guard4_check_once_does_not_block_loop(api_mod, sessions_mod) -> None:
    import asyncio
    import threading

    release = threading.Event()
    entered = threading.Event()
    real_obligations = api_mod._get_session_obligations

    def _blocking_obligations(session_id):
        entered.set()
        if not release.wait(timeout=5.0):
            raise AssertionError("test never released the blocking obligations call")
        return real_obligations(session_id)

    api_mod._get_session_obligations = _blocking_obligations
    try:
        task = asyncio.create_task(api_mod._guard4_check_once())

        for _ in range(500):
            if entered.is_set():
                break
            await asyncio.sleep(0.01)
        assert entered.is_set(), "_guard4_check_once never reached _get_session_obligations"

        # PROOF: the background thread is still parked inside the blocking
        # call (release not yet set), yet the event loop keeps running.
        ticked = False
        for _ in range(5):
            await asyncio.sleep(0)
            ticked = True
        assert ticked
        assert not task.done(), (
            "_guard4_check_once completed while _get_session_obligations "
            "should still be blocked — this means the call ran "
            "SYNCHRONOUSLY on the event loop instead of being offloaded, "
            "i.e. the freeze-class bug is back"
        )

        release.set()
        await asyncio.wait_for(task, timeout=5.0)
    finally:
        api_mod._get_session_obligations = real_obligations
        release.set()


def test_guard4_check_once_offloads_obligations_without_blocking_loop(guard4_env, monkeypatch):
    import asyncio
    import uuid

    chats_mod, sessions_mod, api_mod = guard4_env

    assignee_sid = str(uuid.uuid4())
    _setup_guard4_scenario(chats_mod, sessions_mod, assignee_sid)

    real_list = sessions_mod.list_sessions

    def _mock_list(use_cache=True, **kw):
        rows = real_list(use_cache=False)
        for r in rows:
            if r.get("session_id") == assignee_sid:
                r["last_active_age_s"] = 300.0
        return rows

    monkeypatch.setattr(sessions_mod, "list_sessions", _mock_list)
    monkeypatch.setattr(api_mod, "_is_process_alive_for_session", lambda sid: True)
    api_mod._GUARD4_STALLED.clear()

    asyncio.run(_assert_guard4_check_once_does_not_block_loop(api_mod, sessions_mod))
