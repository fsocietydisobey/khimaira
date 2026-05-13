"""HTTP-layer tests for /api/sessions/* — regression for today's 500-vs-404 bugs.

The two bugs this file pins:
  - POST /api/sessions/{name}/notice with unknown name → was 500, now 404
  - GET /api/sessions/{name} with unknown name → was 500, now 404

Tests use FastAPI's TestClient so no real daemon is needed.
"""

from __future__ import annotations


def test_post_notice_unknown_session_returns_404(api_client):
    """Regression: was raising 500 from unhandled ValueError."""
    r = api_client.post(
        "/api/sessions/no-such-session/notice",
        json={"text": "hi", "from_session_id": "me"},
    )
    assert r.status_code == 404
    body = r.json()
    # Error message varies depending on whether any sessions exist (empty
    # state vs name-mismatch) — assert on the stable substring rather than
    # the recommendation tail.
    assert "no session" in body["detail"].lower()
    assert "no-such-session" in body["detail"]


def test_get_state_unknown_session_returns_404(api_client):
    """Regression: was 500. Now 404 with helpful 'use session_list' message."""
    r = api_client.get("/api/sessions/087234eb17d2")  # 12-char hex (a question id)
    assert r.status_code == 404
    body = r.json()
    assert "no session" in body["detail"].lower()


def test_get_state_known_session_returns_state(api_client, isolated_state):
    """Happy path — confirm the test setup works end-to-end."""
    isolated_state.log_decision("known-session", "test decision", "because")
    r = api_client.get("/api/sessions/known-session")
    assert r.status_code == 200
    body = r.json()
    assert body["decision_count"] == 1
    assert body["recent_decisions"][0]["text"] == "test decision"


def test_get_summary_known_session(api_client, isolated_state):
    """Happy path: summary returns counts + status, no record bodies."""
    isolated_state.log_decision("summary-session", "d1", "")
    isolated_state.set_status("summary-session", "implementing", "doing")
    r = api_client.get("/api/sessions/summary-session/summary")
    assert r.status_code == 200
    body = r.json()
    assert body["session_id"] == "summary-session"
    assert body["decision_count"] == 1
    assert body["status"]["status"] == "implementing"
    assert "recent_decisions" not in body


def test_get_summary_unknown_session_returns_404(api_client):
    """Regression guard: unknown session → 404, not 500."""
    r = api_client.get("/api/sessions/no-such-session/summary")
    assert r.status_code == 404
    assert "no session" in r.json()["detail"].lower()


def test_post_notice_to_known_session_returns_200(api_client, isolated_state):
    """Happy path counterpart to the 404 regression test."""
    # Materialize the target session
    isolated_state.log_decision("target", "init", "")

    r = api_client.post(
        "/api/sessions/target/notice",
        json={"text": "hello", "from_session_id": "asker"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["kind"] == "notice"
    assert body["text"] == "hello"


def test_post_handoff_returns_record(api_client, tmp_path):
    """Handoffs API end-to-end: POST then GET consume."""
    project = tmp_path / "proj"
    project.mkdir()

    r = api_client.post(
        "/api/handoffs",
        json={
            "from_session_id": "asker",
            "text": "pickup pointers",
            "scope_cwd": str(project),
            "expires_in_hours": 24,
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["text"] == "pickup pointers"
    assert body["scope_cwd"] == str(project)
    assert body["read_by"] == []

    # Consume from a new session in the same cwd
    r2 = api_client.get(
        "/api/handoffs/consume",
        params={"session_id": "new-session", "cwd": str(project)},
    )
    assert r2.status_code == 200
    assert len(r2.json()["handoffs"]) == 1


def test_inbox_archive_search(api_client, isolated_state):
    """search_archive returns past read notes by substring."""
    isolated_state.log_decision("s", "init", "")
    isolated_state.post_notice("s", text="Roboflow latency notes", from_session_id="x")
    isolated_state.pending_notes("s", mark_read=True)  # drain → archive

    r = api_client.get(
        "/api/sessions/s/inbox/archive",
        params={"q": "roboflow"},
    )
    assert r.status_code == 200
    results = r.json()["results"]
    assert len(results) == 1
    assert "Roboflow" in results[0]["text"]


def test_inbox_archive_search_no_match(api_client, isolated_state):
    isolated_state.log_decision("s", "init", "")
    r = api_client.get(
        "/api/sessions/s/inbox/archive",
        params={"q": "nothing-here"},
    )
    assert r.status_code == 200
    assert r.json()["results"] == []


# ----------------------------------------------------------------------------
# Regression: every endpoint that resolves a session name must return 404
# (not 500) when the name doesn't match. The bug class: ValueError from
# resolve_session_id unwrapped at the FastAPI route boundary.
#
# Found 2026-05-10 when a fresh chat's session_id wasn't yet registered;
# MCP tool reported "daemon unreachable" but daemon was up — the actual
# response was 500 (which urllib.error.HTTPError surfaces as URLError,
# which _get caught and mapped to "daemon down"). Fixing all the
# endpoints + the _get pattern is the holistic fix.
# ----------------------------------------------------------------------------


def test_get_pending_unknown_session_returns_404(api_client):
    r = api_client.get("/api/sessions/no-such-session/pending")
    assert r.status_code == 404
    assert "no session" in r.json()["detail"].lower()


def test_get_incoming_unknown_session_returns_404(api_client):
    r = api_client.get("/api/sessions/no-such-session/incoming")
    assert r.status_code == 404
    assert "no session" in r.json()["detail"].lower()


def test_surface_inbox_unknown_session_returns_404(api_client):
    r = api_client.get("/api/sessions/no-such-session/inbox/surface")
    assert r.status_code == 404
    assert "no session" in r.json()["detail"].lower()


def test_ack_inbox_unknown_session_returns_404(api_client):
    r = api_client.post(
        "/api/sessions/no-such-session/inbox/ack",
        json={"note_ids": None},
    )
    assert r.status_code == 404
    assert "no session" in r.json()["detail"].lower()


def test_archive_search_unknown_session_returns_404(api_client):
    r = api_client.get(
        "/api/sessions/no-such-session/inbox/archive",
        params={"q": "anything"},
    )
    assert r.status_code == 404
    assert "no session" in r.json()["detail"].lower()


def test_invite_handoff_happy_path(api_client, isolated_state, tmp_path):
    """POST /handoffs/{id}/invite → 200 + child handoff record."""
    project = tmp_path / "proj"
    project.mkdir()
    parent = isolated_state.post_handoff(
        "asker",
        text="parent",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    isolated_state.consume_handoffs("owner-A", str(project))
    isolated_state.log_decision("invitee-B", "init", "")

    r = api_client.post(
        f"/api/handoffs/{parent['id']}/invite",
        json={
            "owner_session_id": "owner-A",
            "invitee_session_id": "invitee-B",
            "text": "please pick up x",
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["parent_id"] == parent["id"]
    assert body["target_session_id"] == "invitee-B"


def test_invite_handoff_unknown_parent_returns_404(api_client):
    """POST /handoffs/{bad-id}/invite → 404."""
    r = api_client.post(
        "/api/handoffs/deadbeefdead/invite",
        json={
            "owner_session_id": "anyone",
            "invitee_session_id": "anyone",
            "text": "should 404",
        },
    )
    assert r.status_code == 404
    assert "no handoff" in r.json()["detail"].lower()


def test_post_workspace_returns_200(api_client, isolated_state):
    """POST /workspace updates the field; GET returns it."""
    isolated_state.log_decision("ws-sess", "init", "")
    r = api_client.post(
        "/api/sessions/ws-sess/workspace",
        json={"workspace": "client-a"},
    )
    assert r.status_code == 200
    assert r.json()["workspace"] == "client-a"

    r2 = api_client.get("/api/sessions/ws-sess/workspace")
    assert r2.status_code == 200
    assert r2.json() == {"session_id": "ws-sess", "workspace": "client-a"}


def test_post_workspace_invalid_name_returns_422(api_client, isolated_state):
    """Bad workspace names → 422 (validation), not 500."""
    isolated_state.log_decision("ws-sess", "init", "")
    r = api_client.post(
        "/api/sessions/ws-sess/workspace",
        json={"workspace": "Has Spaces"},
    )
    assert r.status_code == 422
    assert "workspace" in r.json()["detail"].lower()


def test_state_workspace_mismatch_returns_404(api_client, isolated_state):
    """Cross-workspace state read without override → 404."""
    isolated_state.log_decision("target", "init", "")
    isolated_state.set_workspace("target", "client-a")
    r = api_client.get("/api/sessions/target?workspace=client-b")
    assert r.status_code == 404


def test_question_cross_workspace_returns_422(api_client, isolated_state):
    """Targeted question across workspaces without flag → 422."""
    isolated_state.log_decision("asker", "init", "")
    isolated_state.log_decision("target", "init", "")
    isolated_state.set_workspace("asker", "client-a")
    isolated_state.set_workspace("target", "client-b")

    r = api_client.post(
        "/api/sessions/asker/question",
        json={"text": "ping?", "target_session_id": "target"},
    )
    assert r.status_code == 422
    assert "workspace" in r.json()["detail"].lower()

    # With cross_workspace flag → success
    r2 = api_client.post(
        "/api/sessions/asker/question",
        json={
            "text": "ping?",
            "target_session_id": "target",
            "cross_workspace": True,
        },
    )
    assert r2.status_code == 200
