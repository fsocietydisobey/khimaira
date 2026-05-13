"""Unit tests for khimaira.monitor.sessions — direct function calls.

Tests target the bugs we shipped + caught in one day on 2026-05-09:
  - post_notice with unknown session name → ValueError (then 404 at API layer)
  - log_question with target_session_id → resolved at write time
  - post_handoff + consume_handoffs → cwd-match + read tracking
  - ack_notes → archive move
  - search_archive → substring matching

If these regress, /inbox / handoffs / cross-session ack break in user-
facing ways. Run with `uv run pytest packages/khimaira/tests/`.
"""

from __future__ import annotations

import pytest


def test_log_decision_round_trip(isolated_state):
    """Sanity check fixture works + happy path."""
    sid = "test-session-1"
    rec = isolated_state.log_decision(sid, "use Postgres", "ACID")
    assert rec["text"] == "use Postgres"
    assert rec["why"] == "ACID"
    state = isolated_state.state(sid)
    assert state["decision_count"] == 1
    assert state["recent_decisions"][0]["id"] == rec["id"]


def test_log_question_targeted(isolated_state):
    """Targeted question stamps target_session_id (resolved-or-literal)."""
    sid = "asker"
    # Target doesn't exist as a name yet — should fall back to literal
    q = isolated_state.log_question(
        sid,
        "approach a or b?",
        target_session_id="not-yet-named",
    )
    assert q["target_session_id"] == "not-yet-named"


def test_post_notice_unknown_target_raises(isolated_state):
    """Regression for 2026-05-09 bug: post_notice 500'd on bad name.

    The function should raise ValueError; the API layer wraps this into
    a 404. This test asserts the underlying contract.
    """
    with pytest.raises(ValueError, match="No session named or id'd"):
        isolated_state.post_notice(
            "definitely-not-a-real-session",
            text="this should fail",
            from_session_id="me",
        )


def test_post_notice_lands_in_inbox(isolated_state):
    """Notice posts → inbox, kind=notice, surface_count=0, unread."""
    target = "target-session"
    # Materialize the session by logging a decision
    isolated_state.log_decision(target, "init", "")

    note = isolated_state.post_notice(
        target,
        text="FYI thing happened",
        from_session_id="me",
    )
    assert note["kind"] == "notice"
    assert note["read"] is False
    assert note["surface_count"] == 0

    pending = isolated_state.pending_notes(target, mark_read=False)
    assert len(pending) == 1
    assert pending[0]["text"] == "FYI thing happened"


def test_pending_notes_marks_read_and_archives(isolated_state):
    """Drained notes → archive.jsonl, removed from inbox.jsonl."""
    target = "target-2"
    isolated_state.log_decision(target, "init", "")
    isolated_state.post_notice(target, text="msg 1", from_session_id="me")
    isolated_state.post_notice(target, text="msg 2", from_session_id="me")

    drained = isolated_state.pending_notes(target, mark_read=True)
    assert len(drained) == 2
    # Inbox now empty
    assert isolated_state.pending_notes(target, mark_read=False) == []
    # Archived
    archived = isolated_state.search_archive(target)
    assert len(archived) == 2
    assert {n["text"] for n in archived} == {"msg 1", "msg 2"}


def test_search_archive_substring(isolated_state):
    """search_archive matches body substring case-insensitively."""
    target = "target-3"
    isolated_state.log_decision(target, "init", "")
    isolated_state.post_notice(
        target, text="Roboflow concurrency check", from_session_id="me"
    )
    isolated_state.post_notice(target, text="something unrelated", from_session_id="me")
    isolated_state.pending_notes(target, mark_read=True)

    hits = isolated_state.search_archive(target, query="roboflow")
    assert len(hits) == 1
    assert "Roboflow" in hits[0]["text"]


def test_post_handoff_cwd_inferred_from_files(isolated_state, tmp_path):
    """When scope_cwd not given, infer from from_session's file_touched dir."""
    asker = "asker"
    project = tmp_path / "myproject"
    project.mkdir()
    isolated_state.log_touch(asker, str(project / "main.py"), "edited")

    h = isolated_state.post_handoff(asker, "pickup notes", scope_cwd=None)
    assert h["scope_cwd"] == str(project)


def test_consume_handoffs_cwd_match_and_child(isolated_state, tmp_path):
    """Handoffs match exact scope_cwd OR any cwd under it. Read tracking."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    sub = project / "subdir"
    sub.mkdir()

    isolated_state.post_handoff(
        asker,
        text="hand off",
        scope_cwd=str(project),
        expires_in_hours=24,
    )

    # Exact match
    matched = isolated_state.consume_handoffs("future-session-1", str(project))
    assert len(matched) == 1
    # Same session re-consumes → empty (read tracking)
    matched_again = isolated_state.consume_handoffs("future-session-1", str(project))
    assert matched_again == []
    # Different session, child cwd → still matches
    matched_child = isolated_state.consume_handoffs("future-session-2", str(sub))
    assert len(matched_child) == 1


def test_consume_handoffs_unrelated_cwd_no_match(isolated_state, tmp_path):
    """Handoffs with non-matching scope_cwd are not surfaced."""
    asker = "asker"
    project = tmp_path / "project-a"
    project.mkdir()
    other = tmp_path / "project-b"
    other.mkdir()

    isolated_state.post_handoff(
        asker,
        text="for project-a",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    matched = isolated_state.consume_handoffs("session", str(other))
    assert matched == []


def test_list_sessions_caches_within_ttl(isolated_state, monkeypatch):
    """list_sessions should return cached result within 2s TTL."""
    isolated_state.log_decision("s1", "init", "")
    isolated_state.log_decision("s2", "init", "")

    first = isolated_state.list_sessions()
    assert len(first) == 2

    # Now mutate state by adding a new session, but don't invalidate cache
    # (simulating: write happened without going through khimaira write path)
    sd = isolated_state._BASE_DIR / "s3-fake"
    sd.mkdir()
    (sd / "status.json").write_text('{"name":"x","status":"idle","detail":""}')

    # Cache should still return 2 sessions
    cached = isolated_state.list_sessions()
    assert len(cached) == 2

    # Force fresh scan → sees 3
    fresh = isolated_state.list_sessions(use_cache=False)
    assert len(fresh) == 3


def test_list_sessions_cache_invalidated_on_log_decision(isolated_state):
    """log_decision should bust the cache so next list sees the new session."""
    isolated_state.log_decision("s1", "init", "")
    first = isolated_state.list_sessions()
    assert len(first) == 1

    # New session via log_decision → cache should be busted
    isolated_state.log_decision("s2", "new session", "")
    second = isolated_state.list_sessions()
    assert len(second) == 2


def test_list_sessions_cache_invalidated_on_set_name(isolated_state):
    """set_name should bust the cache so renames appear immediately."""
    isolated_state.log_decision("s1", "init", "")
    isolated_state.list_sessions()  # warm cache

    isolated_state.set_name("s1", "renamed")
    after_rename = isolated_state.list_sessions()
    assert after_rename[0]["name"] == "renamed"


def test_broadcast_returns_immediately_when_no_subscribers(isolated_state, tmp_path):
    """log_decision shouldn't spawn a thread if no subscribers exist."""
    import time

    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    # Handoff without subscribers
    h = isolated_state.post_handoff(
        asker,
        text="t",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    isolated_state.consume_handoffs("owner", str(project))  # claim ownership
    isolated_state.log_decision(
        "subscriber-1", "init", ""
    )  # materialize but DON'T subscribe

    # Time log_decision — should be fast (no broadcast work)
    t0 = time.perf_counter()
    for _ in range(10):
        isolated_state.log_decision("owner", "decision text", "why")
    elapsed = time.perf_counter() - t0

    # 10 decisions, no broadcast → should be well under 100ms total
    assert elapsed < 0.5, f"10 unsubscribed broadcasts took {elapsed:.2f}s (too slow)"


def test_consume_handoffs_auto_claims_first_consumer_as_owner(isolated_state, tmp_path):
    """First session in scope auto-claims as owner; subsequent sessions are
    observers. Closes the collision pattern where multiple sessions in the
    same project cwd each tried to act on the same handoff."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    isolated_state.post_handoff(
        asker,
        text="test",
        scope_cwd=str(project),
        expires_in_hours=24,
    )

    # First consumer becomes owner
    first = isolated_state.consume_handoffs("session-A", str(project))
    assert len(first) == 1
    assert first[0]["_claim_role"] == "owner"
    assert first[0]["owner_session_id"] == "session-A"

    # Second consumer is observer
    second = isolated_state.consume_handoffs("session-B", str(project))
    assert len(second) == 1
    assert second[0]["_claim_role"] == "observer"
    assert second[0]["_owner_session_id"] == "session-A"


def test_subscribe_handoff_idempotent(isolated_state, tmp_path):
    """subscribe_handoff is idempotent — calling twice doesn't duplicate."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    h = isolated_state.post_handoff(
        asker,
        text="t",
        scope_cwd=str(project),
        expires_in_hours=24,
    )

    isolated_state.subscribe_handoff(h["id"], "observer-session")
    isolated_state.subscribe_handoff(h["id"], "observer-session")  # duplicate

    handoffs = isolated_state._read_jsonl(isolated_state._HANDOFFS_PATH)
    found = next(x for x in handoffs if x["id"] == h["id"])
    assert found["subscribers"] == ["observer-session"]


def test_log_decision_broadcasts_to_handoff_subscribers(isolated_state, tmp_path):
    """When owner logs a decision, subscribers' inboxes receive a notice
    (eventually — broadcast runs in a background thread, so we poll).
    """
    import time

    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    h = isolated_state.post_handoff(
        asker,
        text="t",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    # Consume claims owner
    isolated_state.consume_handoffs("owner-session", str(project))
    # Materialize subscriber session + subscribe
    isolated_state.log_decision("subscriber-session", "init", "")
    isolated_state.subscribe_handoff(h["id"], "subscriber-session")

    # Owner logs a decision — fanout is async; poll up to 2s for delivery
    isolated_state.log_decision("owner-session", "starting bug fix in foo.py", "step 1")

    deadline = time.time() + 2.0
    broadcasts: list[dict] = []
    while time.time() < deadline:
        pending = isolated_state.pending_notes("subscriber-session", mark_read=False)
        broadcasts = [n for n in pending if "handoff" in (n.get("text") or "")]
        if broadcasts:
            break
        time.sleep(0.05)

    assert len(broadcasts) == 1, f"broadcast didn't arrive in 2s: {broadcasts}"
    assert "decision" in broadcasts[0]["text"]
    assert "starting bug fix" in broadcasts[0]["text"]


def test_release_handoff_clears_owner(isolated_state, tmp_path):
    """Owner releases → owner_session_id becomes None → next consumer claims."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    h = isolated_state.post_handoff(
        asker,
        text="t",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    isolated_state.consume_handoffs("session-A", str(project))
    isolated_state.release_handoff(h["id"], "session-A")

    # Re-read; owner should be None
    handoffs = isolated_state._read_jsonl(isolated_state._HANDOFFS_PATH)
    found = next(x for x in handoffs if x["id"] == h["id"])
    assert found["owner_session_id"] is None

    # Next consumer (different session) becomes the new owner
    next_consume = isolated_state.consume_handoffs("session-C", str(project))
    # session-A is already in read_by, so they don't re-receive. session-C
    # is fresh.
    assert len(next_consume) == 1
    assert next_consume[0]["_claim_role"] == "owner"
    assert next_consume[0]["owner_session_id"] == "session-C"


def test_release_handoff_rejects_non_owner(isolated_state, tmp_path):
    """Only the owner can release a handoff."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    h = isolated_state.post_handoff(
        asker,
        text="t",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    isolated_state.consume_handoffs("session-A", str(project))

    with pytest.raises(ValueError, match="doesn't own"):
        isolated_state.release_handoff(h["id"], "not-the-owner")


def test_consume_handoffs_expired_dropped(isolated_state, tmp_path, monkeypatch):
    """Expired handoffs are not surfaced AND dropped on next consume."""
    import time as time_mod

    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()

    # Negative TTL → expires_at is in the past
    isolated_state.post_handoff(
        asker,
        text="stale",
        scope_cwd=str(project),
        expires_in_hours=-1.0,
    )
    matched = isolated_state.consume_handoffs("session", str(project))
    assert matched == []
    # File should now be empty (or no longer contain the stale entry)
    if isolated_state._HANDOFFS_PATH.exists():
        contents = isolated_state._HANDOFFS_PATH.read_text()
        assert "stale" not in contents


def test_summary_returns_counts_not_bodies(isolated_state):
    """summary() returns aggregate counts + status, no record bodies."""
    sid = "summary-test"
    isolated_state.log_decision(sid, "d1", "")
    isolated_state.log_decision(sid, "d2", "")
    isolated_state.log_touch(sid, "/x/y.py", "edit")
    isolated_state.log_question(sid, "q?")
    isolated_state.set_status(sid, "implementing", "doing the thing")

    s = isolated_state.summary(sid)

    assert s["session_id"] == sid
    assert s["decision_count"] == 2
    assert s["file_touch_count"] == 1
    assert s["open_question_count"] == 1
    assert s["status"]["status"] == "implementing"
    assert s["last_active"] > 0
    assert s["last_active_age_s"] >= 0
    # Lightweight contract — no record bodies leaked
    assert "recent_decisions" not in s
    assert "recent_files" not in s
    assert "open_questions" not in s


def test_summary_unknown_session_raises(isolated_state):
    """Regression: unknown name/id → ValueError (API layer wraps to 404)."""
    with pytest.raises(ValueError, match="No session named or id'd"):
        isolated_state.summary("no-such-session")


def test_summary_resolves_friendly_name(isolated_state):
    """summary() accepts a name set via set_name, just like state()."""
    sid = "uuid-style-id-abc"
    isolated_state.log_decision(sid, "init", "")
    isolated_state.set_name(sid, "friendly")

    s = isolated_state.summary("friendly")
    assert s["session_id"] == sid
    assert s["decision_count"] == 1


def test_invite_handoff_owner_creates_child(isolated_state, tmp_path):
    """Owner of a parent handoff invites an invitee → child handoff with
    parent_id + target_session_id, inheriting parent's scope_cwd."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()

    parent = isolated_state.post_handoff(
        asker,
        text="parent work",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    isolated_state.consume_handoffs("owner-A", str(project))  # claims ownership

    # Materialize the invitee so post_notice in invite path doesn't 404
    isolated_state.log_decision("invitee-B", "init", "")

    child = isolated_state.invite_handoff(
        parent["id"],
        owner_session_id="owner-A",
        invitee_session_id="invitee-B",
        text="please handle subtask 2",
    )
    assert child["parent_id"] == parent["id"]
    assert child["target_session_id"] == "invitee-B"
    assert child["scope_cwd"] == str(project)
    assert child["from_session_id"] == "owner-A"
    # Invite is a distinct handoff with its own id
    assert child["id"] != parent["id"]


def test_invite_handoff_non_owner_rejected(isolated_state, tmp_path):
    """Only the current owner can invite. Non-owner → ValueError."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    parent = isolated_state.post_handoff(
        asker,
        text="t",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    isolated_state.consume_handoffs("owner-A", str(project))

    with pytest.raises(ValueError, match="doesn't own"):
        isolated_state.invite_handoff(
            parent["id"],
            owner_session_id="not-the-owner",
            invitee_session_id="anyone",
            text="should fail",
        )


def test_invite_handoff_unknown_parent_raises(isolated_state):
    """Inviting against a non-existent parent → ValueError."""
    with pytest.raises(ValueError, match="No handoff"):
        isolated_state.invite_handoff(
            "deadbeefdead",
            owner_session_id="me",
            invitee_session_id="you",
            text="nope",
        )


def test_invite_handoff_only_invitee_can_consume(isolated_state, tmp_path):
    """Targeted invite: only the named invitee may consume; cwd-peers skip."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    parent = isolated_state.post_handoff(
        asker,
        text="parent",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    isolated_state.consume_handoffs("owner-A", str(project))
    isolated_state.log_decision("invitee-B", "init", "")
    isolated_state.invite_handoff(
        parent["id"],
        owner_session_id="owner-A",
        invitee_session_id="invitee-B",
        text="for B only",
    )

    # A peer session in same cwd consumes → sees the parent (already
    # read_by=[owner-A], so peer sees it fresh) but NOT the invite.
    peer = isolated_state.consume_handoffs("session-C", str(project))
    invite_ids = [h.get("target_session_id") for h in peer]
    assert "invitee-B" not in invite_ids
    assert all(h.get("target_session_id") in (None, "session-C") for h in peer)

    # The invitee themselves consumes → sees the invite
    invitee_matched = isolated_state.consume_handoffs("invitee-B", str(project))
    invitee_targets = [h.get("target_session_id") for h in invitee_matched]
    assert "invitee-B" in invitee_targets


def test_invite_handoff_posts_inbox_notice(isolated_state, tmp_path):
    """Invite path drops an inbox notice on a live invitee for mid-session
    surfacing (in addition to the SessionStart-hook path)."""
    asker = "asker"
    project = tmp_path / "p"
    project.mkdir()
    parent = isolated_state.post_handoff(
        asker,
        text="parent",
        scope_cwd=str(project),
        expires_in_hours=24,
    )
    isolated_state.consume_handoffs("owner-A", str(project))
    isolated_state.log_decision("invitee-B", "init", "")

    isolated_state.invite_handoff(
        parent["id"],
        owner_session_id="owner-A",
        invitee_session_id="invitee-B",
        text="please pick up subtask",
    )

    pending = isolated_state.pending_notes("invitee-B", mark_read=False)
    invite_notices = [n for n in pending if "INVITE" in (n.get("text") or "")]
    assert len(invite_notices) == 1
    assert "please pick up subtask" in invite_notices[0]["text"]


# ---------------------------------------------------------------------------
# Workspaces — privacy boundary for multi-session isolation
# ---------------------------------------------------------------------------


def test_set_workspace_persists_and_is_round_trip(isolated_state):
    """set_workspace writes the workspace field; get_workspace reads it."""
    sid = "ws-test"
    isolated_state.log_decision(sid, "init", "")
    rec = isolated_state.set_workspace(sid, "client-a")
    assert rec["workspace"] == "client-a"
    assert isolated_state.get_workspace(sid) == "client-a"


def test_get_workspace_defaults_for_unset(isolated_state):
    """Sessions without a workspace field resolve to DEFAULT_WORKSPACE."""
    sid = "no-ws-yet"
    isolated_state.log_decision(sid, "init", "")
    # No set_workspace called → must default
    assert isolated_state.get_workspace(sid) == isolated_state.DEFAULT_WORKSPACE


def test_set_workspace_validates_name(isolated_state):
    """Invalid workspace names raise ValueError (path-injection guard)."""
    sid = "ws-test"
    isolated_state.log_decision(sid, "init", "")
    for bad in ("Has Spaces", "UPPER", "../etc", "x" * 41, ""):
        with pytest.raises(ValueError, match="workspace"):
            isolated_state.set_workspace(sid, bad)


def test_log_question_rejects_cross_workspace_by_default(isolated_state):
    """Targeted questions across workspace boundaries fail without flag."""
    isolated_state.log_decision("asker", "init", "")
    isolated_state.log_decision("target", "init", "")
    isolated_state.set_workspace("asker", "client-a")
    isolated_state.set_workspace("target", "client-b")

    with pytest.raises(ValueError, match="crosses workspaces"):
        isolated_state.log_question("asker", "are you up?", target_session_id="target")


def test_log_question_allows_cross_workspace_with_flag(isolated_state):
    """cross_workspace=True overrides the workspace guard."""
    isolated_state.log_decision("asker", "init", "")
    isolated_state.log_decision("target", "init", "")
    isolated_state.set_workspace("asker", "client-a")
    isolated_state.set_workspace("target", "client-b")

    q = isolated_state.log_question(
        "asker",
        "are you up?",
        target_session_id="target",
        cross_workspace=True,
    )
    assert q["target_session_id"] == "target"


def test_list_sessions_workspace_filter(isolated_state):
    """list_sessions(workspace='X') returns only sessions in workspace X."""
    isolated_state.log_decision("s1", "init", "")
    isolated_state.log_decision("s2", "init", "")
    isolated_state.log_decision("s3", "init", "")
    isolated_state.set_workspace("s1", "alpha")
    isolated_state.set_workspace("s2", "alpha")
    # s3 stays default

    alpha = isolated_state.list_sessions(use_cache=False, workspace="alpha")
    assert {r["session_id"] for r in alpha} == {"s1", "s2"}

    default_only = isolated_state.list_sessions(use_cache=False, workspace="default")
    assert {r["session_id"] for r in default_only} == {"s3"}

    # None / "*" return all
    everything = isolated_state.list_sessions(use_cache=False, workspace=None)
    assert {r["session_id"] for r in everything} == {"s1", "s2", "s3"}
    star = isolated_state.list_sessions(use_cache=False, workspace="*")
    assert {r["session_id"] for r in star} == {"s1", "s2", "s3"}


def test_state_workspace_mismatch_raises(isolated_state):
    """state(id, workspace='X') raises when target is in a different workspace."""
    isolated_state.log_decision("target", "decided X", "")
    isolated_state.set_workspace("target", "client-a")

    with pytest.raises(ValueError, match="workspace"):
        isolated_state.state("target", workspace="client-b")

    # No workspace arg → no filter, no raise
    s = isolated_state.state("target")
    assert s["session_id"] == "target"


def test_recent_decisions_workspace_filter(isolated_state):
    """recent_decisions(workspace='X') only returns decisions from X."""
    isolated_state.log_decision("s1", "d1", "")
    isolated_state.log_decision("s2", "d2", "")
    isolated_state.set_workspace("s1", "alpha")
    # s2 stays default

    alpha_only = isolated_state.recent_decisions(workspace="alpha")
    assert {d["session_id"] for d in alpha_only} == {"s1"}

    default_only = isolated_state.recent_decisions(workspace="default")
    assert {d["session_id"] for d in default_only} == {"s2"}

    everything = isolated_state.recent_decisions(workspace=None)
    assert {d["session_id"] for d in everything} == {"s1", "s2"}
