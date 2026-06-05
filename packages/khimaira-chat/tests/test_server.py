"""Tests for khimaira_chat.server._route_record.

The MCP subprocess's SSE loops both delegate routing to the pure
`_route_record(record, my_session_id)` helper. Testing the helper
directly avoids spinning up the SSE pipeline.

Phase B v1.1 extended routing to cover `kind=task` and `kind=task_update`
records so assignees see new tasks in their channel feed and masters see
agents' transitions without polling chat_task_status.
"""

from __future__ import annotations

from khimaira_chat.server import _route_record

MY_SID = "session-me"
OTHER_SID = "session-other"


# ---------------------------------------------------------------------------
# Existing routes (msg, invite) — kept covered to guard against regressions
# ---------------------------------------------------------------------------


def test_msg_from_other_session_emits():
    record = {
        "kind": "msg",
        "chat_id": "chat-1",
        "sender_id": OTHER_SID,
        "sender_name": "other",
        "id": "msg-abc",
        "body": "hello",
    }
    decision = _route_record(record, MY_SID)
    assert decision is not None
    content, meta = decision
    assert content == "hello"
    assert meta == {"chat_id": "chat-1", "sender": "other", "msg_id": "msg-abc"}


def test_msg_from_self_skipped():
    record = {
        "kind": "msg",
        "chat_id": "chat-1",
        "sender_id": MY_SID,
        "body": "my own message",
    }
    assert _route_record(record, MY_SID) is None


def test_pending_invite_for_me_emits():
    record = {
        "kind": "member",
        "state": "pending",
        "chat_id": "chat-1",
        "session_id": MY_SID,
        "invited_by": "boss",
    }
    decision = _route_record(record, MY_SID)
    assert decision is not None
    content, meta = decision
    assert "boss invited you to chat chat-1" in content
    assert meta == {"chat_id": "chat-1", "kind": "invite", "from": "boss"}


def test_pending_invite_for_other_skipped():
    record = {
        "kind": "member",
        "state": "pending",
        "chat_id": "chat-1",
        "session_id": OTHER_SID,
        "invited_by": "boss",
    }
    assert _route_record(record, MY_SID) is None


# ---------------------------------------------------------------------------
# Phase B v1.1: kind=task routes to assignee (or broadcasts if unassigned)
# ---------------------------------------------------------------------------


def test_task_assigned_to_me_emits_with_pending_status_and_body():
    record = {
        "kind": "task",
        "chat_id": "chat-1",
        "id": "task-abc",
        "sender_id": OTHER_SID,
        "sender_name": "master",
        "assignee_id": MY_SID,
        "assignee_name": "me",
        "body": "implement the foo",
        "status": "pending",
    }
    decision = _route_record(record, MY_SID)
    assert decision is not None
    content, meta = decision
    # Channel-block format spec: "📋 task <id> [<status>] from <by_name>: <body>"
    assert content == "📋 task task-abc [pending] from master: implement the foo"
    assert meta["kind"] == "task"
    assert meta["task_id"] == "task-abc"
    assert meta["status"] == "pending"
    assert meta["sender"] == "master"
    assert meta["chat_id"] == "chat-1"


def test_task_assigned_to_other_skipped():
    """A task assigned to bob shouldn't channel-block carol's feed."""
    record = {
        "kind": "task",
        "chat_id": "chat-1",
        "id": "task-abc",
        "sender_id": OTHER_SID,
        "sender_name": "master",
        "assignee_id": "session-bob",
        "body": "implement the foo",
    }
    assert _route_record(record, MY_SID) is None


def test_unassigned_task_emits_to_non_creator():
    """Unassigned task = broadcast-to-accepted shape; everyone except the
    creator gets a channel block so the open task is visible."""
    record = {
        "kind": "task",
        "chat_id": "chat-1",
        "id": "task-abc",
        "sender_id": OTHER_SID,
        "sender_name": "master",
        "assignee_id": None,
        "body": "anyone want this",
    }
    decision = _route_record(record, MY_SID)
    assert decision is not None
    content, _ = decision
    assert "task-abc" in content
    assert "[pending]" in content


def test_unassigned_task_skipped_for_creator():
    """Creator of an unassigned task doesn't see their own creation echo."""
    record = {
        "kind": "task",
        "chat_id": "chat-1",
        "id": "task-abc",
        "sender_id": MY_SID,
        "sender_name": "me",
        "assignee_id": None,
        "body": "anyone want this",
    }
    assert _route_record(record, MY_SID) is None


# ---------------------------------------------------------------------------
# Phase B v1.1: kind=task_update routes to non-actors
# ---------------------------------------------------------------------------


def test_task_update_done_by_other_emits_to_master():
    """The spec'd test #2 — agent marks task done; master (everyone but
    the actor) sees a channel block. Closes the master-side polling gap."""
    record = {
        "kind": "task_update",
        "chat_id": "chat-1",
        "task_id": "task-abc",
        "status": "done",
        "by_session_id": OTHER_SID,
        "by_name": "agent",
        "note": "PR #042",
    }
    decision = _route_record(record, MY_SID)
    assert decision is not None
    content, meta = decision
    assert content == "📋 task task-abc [done] from agent: PR #042"
    assert meta["kind"] == "task_update"
    assert meta["task_id"] == "task-abc"
    assert meta["status"] == "done"


def test_task_update_by_self_skipped():
    """Actor doesn't see their own transition echoed."""
    record = {
        "kind": "task_update",
        "chat_id": "chat-1",
        "task_id": "task-abc",
        "status": "done",
        "by_session_id": MY_SID,
        "by_name": "me",
        "note": "PR #042",
    }
    assert _route_record(record, MY_SID) is None


def test_task_update_without_note_omits_suffix():
    """When transition has no note, the channel block ends after the actor
    name — no dangling colon."""
    record = {
        "kind": "task_update",
        "chat_id": "chat-1",
        "task_id": "task-abc",
        "status": "in_progress",
        "by_session_id": OTHER_SID,
        "by_name": "agent",
        "note": None,
    }
    decision = _route_record(record, MY_SID)
    assert decision is not None
    content, _ = decision
    assert content == "📋 task task-abc [in_progress] from agent"


# ---------------------------------------------------------------------------
# Unknown / unhandled kinds are skipped cleanly
# ---------------------------------------------------------------------------


def test_unknown_kind_skipped():
    assert _route_record({"kind": "meta"}, MY_SID) is None
    assert _route_record({}, MY_SID) is None


# ---------------------------------------------------------------------------
# Phase B v1.2: task_signal routing
# ---------------------------------------------------------------------------


def test_task_signal_routes_to_assignee():
    """Master sends signal-start on a task assigned to me → I get a
    `🟢 ... [ready to start]` channel block."""
    record = {
        "kind": "task_signal",
        "chat_id": "chat-1",
        "task_id": "task-abc",
        "signal": "start",
        "by_session_id": OTHER_SID,
        "by_name": "master",
        "assignee_id": MY_SID,
        "note": "all blockers cleared",
    }
    decision = _route_record(record, MY_SID)
    assert decision is not None
    content, meta = decision
    assert content == "🟢 task task-abc [ready to start] from master: all blockers cleared"
    assert meta == {
        "chat_id": "chat-1",
        "kind": "task_signal",
        "task_id": "task-abc",
        "sender": "master",
        "signal": "start",
    }


def test_task_signal_skips_non_assignee():
    """Task has assignee X; I'm not X → skip. Prevents siblings spam in
    multi-agent chats where the signal is targeted."""
    record = {
        "kind": "task_signal",
        "chat_id": "chat-1",
        "task_id": "task-abc",
        "signal": "start",
        "by_session_id": OTHER_SID,
        "by_name": "master",
        "assignee_id": "session-someone-else",
    }
    assert _route_record(record, MY_SID) is None


def test_task_signal_broadcasts_when_unassigned():
    """Unassigned task signal → broadcast (any accepted member could claim
    it). Mirrors the kind=task unassigned broadcast precedent from v1.1.a."""
    record = {
        "kind": "task_signal",
        "chat_id": "chat-1",
        "task_id": "task-abc",
        "signal": "start",
        "by_session_id": OTHER_SID,
        "by_name": "master",
        "assignee_id": None,
        "note": None,
    }
    decision = _route_record(record, MY_SID)
    assert decision is not None
    content, _ = decision
    assert content == "🟢 task task-abc [ready to start] from master"


def test_task_signal_skips_own_signal():
    """Master who sent the signal shouldn't see their own echo."""
    record = {
        "kind": "task_signal",
        "chat_id": "chat-1",
        "task_id": "task-abc",
        "signal": "start",
        "by_session_id": MY_SID,
        "by_name": "me",
        "assignee_id": OTHER_SID,
    }
    assert _route_record(record, MY_SID) is None


# ---------------------------------------------------------------------------
# Phase B v1.3: subscriber watchdog self-healing
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402

import pytest  # noqa: E402


@pytest.mark.asyncio
async def test_watchdog_restarts_crashed_subscriber(monkeypatch):
    """When `subscriber_task` crashes, the watchdog must replace it with
    a fresh task within one tick interval. The replacement task must be
    a different object AND still running (not instantly done — guards
    against false positives from a stub coroutine that returns immediately)."""
    import contextlib

    from khimaira_chat import server

    # Stub out _proactive_sse_loop so the restart doesn't try to hit a
    # real daemon. Sleeps indefinitely; cancellation handles cleanup.
    async def stub_subscriber():
        await asyncio.sleep(60)

    monkeypatch.setattr(server, "_proactive_sse_loop", stub_subscriber)
    monkeypatch.setattr(server, "_WATCHDOG_INTERVAL_S", 0.05)

    # Snapshot the live _state's mutated fields so we restore them
    # after the test — _state is module-level singleton.
    orig = (
        server._state.session_id,
        server._state.subscriber_task,
        server._state.watchdog_task,
        server._state.write_stream,
        server._state.subscriber_restart_count,
    )
    try:
        server._state.session_id = "test-session"
        server._state.write_stream = object()  # just needs to be non-None
        server._state.subscriber_restart_count = 0

        # Inject a subscriber task that crashes immediately.
        async def crashing_sub():
            raise RuntimeError("simulated subscriber crash")

        crashed = asyncio.create_task(crashing_sub())
        await asyncio.sleep(0.01)
        assert crashed.done()
        server._state.subscriber_task = crashed

        # Start the watchdog. It should detect the crashed task on its
        # first tick (~0.05s) and reincarnate it.
        wd = asyncio.create_task(server._subscriber_watchdog())
        try:
            # Poll for restart, max ~1s wall-time.
            for _ in range(40):
                await asyncio.sleep(0.05)
                if server._state.subscriber_task is not crashed:
                    break

            new_task = server._state.subscriber_task
            assert new_task is not crashed, "watchdog did not replace crashed subscriber_task"
            assert not new_task.done(), "replacement task ended instantly (stub-coroutine smell)"
            assert server._state.subscriber_restart_count == 1, "restart counter not bumped"
        finally:
            wd.cancel()
            new_task = server._state.subscriber_task
            if new_task is not None and not new_task.done():
                new_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await wd
            if new_task is not None:
                with contextlib.suppress(asyncio.CancelledError):
                    await new_task
    finally:
        (
            server._state.session_id,
            server._state.subscriber_task,
            server._state.watchdog_task,
            server._state.write_stream,
            server._state.subscriber_restart_count,
        ) = orig


@pytest.mark.asyncio
async def test_dispatch_tool_restarts_dead_subscriber(monkeypatch):
    """Phase B v1.3 Lane B: when an agent calls a chat_* tool and the
    subscriber_task is dead, _dispatch_tool restarts it before dispatching.
    Complements the passive watchdog: agents that wake and immediately
    call chat tools shouldn't wait 30s for the next watchdog tick."""
    import contextlib

    from khimaira_chat import server

    # Stub _proactive_sse_loop so the restart doesn't hit a real daemon.
    async def stub_subscriber():
        await asyncio.sleep(60)

    monkeypatch.setattr(server, "_proactive_sse_loop", stub_subscriber)

    # Stub daemon_client.my_chats so the dispatch call doesn't try to
    # talk to a real daemon — only the precheck behavior matters here.
    monkeypatch.setattr(server.daemon_client, "my_chats", lambda sid: [])

    orig = (
        server._state.session_id,
        server._state.subscriber_task,
        server._state.write_stream,
        server._state.subscriber_restart_count,
    )
    try:
        server._state.session_id = "test-session"
        server._state.write_stream = object()
        server._state.subscriber_restart_count = 0

        # Inject a subscriber task that has already exited.
        async def dead_sub():
            return

        dead = asyncio.create_task(dead_sub())
        await asyncio.sleep(0.01)
        assert dead.done()
        server._state.subscriber_task = dead

        # Dispatch a chat tool — precheck should restart the subscriber.
        await server._dispatch_tool("chat_my_chats", {"session_id": "test-session"})

        new_task = server._state.subscriber_task
        assert new_task is not dead, "precheck did not replace dead subscriber_task"
        assert not new_task.done(), "replacement task ended instantly"
        assert server._state.subscriber_restart_count == 1, "restart counter not bumped"

        # Healthy subscriber on subsequent dispatch → no further restart.
        await server._dispatch_tool("chat_my_chats", {"session_id": "test-session"})
        assert server._state.subscriber_restart_count == 1, (
            "precheck should be a no-op when subscriber is alive"
        )

        # Cleanup.
        if not new_task.done():
            new_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await new_task
    finally:
        (
            server._state.session_id,
            server._state.subscriber_task,
            server._state.write_stream,
            server._state.subscriber_restart_count,
        ) = orig


# ---------------------------------------------------------------------------
# Phase B v1.3 Lane D: async ppid-bridge eager subscription
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_async_ppid_bridge_sets_session_id_when_lookup_succeeds(monkeypatch):
    """When the daemon's ppid registry has an entry for one of our
    ancestors, `_async_try_auto_register_from_ppid` should populate
    `_state.session_id` so the caller (_serve) can spawn the subscriber.
    Regression-prevention for the v1.2 dogfood bug — subprocess never
    saw the chat_transfer_membership block because the eager-reg never
    fired."""
    from khimaira_chat import server

    orig = (
        server._state.session_id,
        server._state.subscriber_task,
        server._state.write_stream,
        server._state.subscriber_restart_count,
    )
    try:
        server._state.session_id = None  # force the bridge to actually run
        expected_sid = "test-async-ppid-success"
        monkeypatch.setattr(server, "_ancestor_pids", lambda max_depth=6: [12345])
        monkeypatch.setattr(
            server.daemon_client, "lookup_session_by_ppid", lambda ppid: expected_sid
        )
        # Don't bother with display-name registration in tests — stub out.
        monkeypatch.setattr(server, "_maybe_register_display_name", lambda sid: None)

        await server._async_try_auto_register_from_ppid()

        assert server._state.session_id == expected_sid
    finally:
        (
            server._state.session_id,
            server._state.subscriber_task,
            server._state.write_stream,
            server._state.subscriber_restart_count,
        ) = orig


@pytest.mark.asyncio
async def test_async_ppid_bridge_gives_up_when_lookup_always_none(monkeypatch):
    """When the daemon never has an entry (hook never posted), the
    bridge must exhaust its budget and return cleanly — `_state.session_id`
    stays None, lazy-reg takes over on the agent's first tool call.
    No exceptions raised; this is the existing-behavior preservation
    guarantee that lets the watchdog (Lane A) be the only restart path
    for the not-yet-registered case."""
    from khimaira_chat import server

    orig = (
        server._state.session_id,
        server._state.subscriber_task,
        server._state.write_stream,
        server._state.subscriber_restart_count,
    )
    try:
        server._state.session_id = None
        monkeypatch.setattr(server, "_ancestor_pids", lambda max_depth=6: [99999])
        monkeypatch.setattr(server.daemon_client, "lookup_session_by_ppid", lambda ppid: None)
        # Shrink the budget so the test doesn't actually sleep 5s.
        monkeypatch.setattr(server, "_ASYNC_PPID_BUDGET_S", 0.3)

        await server._async_try_auto_register_from_ppid()

        assert server._state.session_id is None, (
            "session_id must stay None when bridge exhausts budget; "
            "otherwise lazy-reg fallback would be unreachable"
        )
    finally:
        (
            server._state.session_id,
            server._state.subscriber_task,
            server._state.write_stream,
            server._state.subscriber_restart_count,
        ) = orig


@pytest.mark.asyncio
async def test_async_ppid_bridge_noop_when_session_id_already_set(monkeypatch):
    """If main()'s sync attempt already populated `_state.session_id`,
    the async retry must be a no-op — must not re-fetch, must not
    overwrite. Otherwise we'd risk double-registration races."""
    from khimaira_chat import server

    orig = (
        server._state.session_id,
        server._state.subscriber_task,
        server._state.write_stream,
        server._state.subscriber_restart_count,
    )
    try:
        preset_sid = "test-already-registered"
        server._state.session_id = preset_sid

        call_count = {"n": 0}

        def counting_lookup(ppid):
            call_count["n"] += 1
            return "should-never-be-set"

        monkeypatch.setattr(server, "_ancestor_pids", lambda max_depth=6: [12345])
        monkeypatch.setattr(server.daemon_client, "lookup_session_by_ppid", counting_lookup)

        await server._async_try_auto_register_from_ppid()

        assert server._state.session_id == preset_sid, (
            "must not overwrite an established session_id"
        )
        assert call_count["n"] == 0, "must not call lookup when session_id is already set"
    finally:
        (
            server._state.session_id,
            server._state.subscriber_task,
            server._state.write_stream,
            server._state.subscriber_restart_count,
        ) = orig


# ---------------------------------------------------------------------------
# Subscriber-side dedup (Change 2)
# ---------------------------------------------------------------------------


def test_subscriber_dedup_skips_seen_event():
    """seen_event_ids prevents processing the same event_id twice."""
    import collections
    from khimaira_chat import server

    # Reset seen_event_ids to a clean state for this test.
    orig_seen = server._state.seen_event_ids
    server._state.seen_event_ids = collections.OrderedDict()
    try:
        state = server._state
        evt_id = "evt-dedup-001"

        # First time: not seen, should NOT be in dict yet.
        assert evt_id not in state.seen_event_ids

        # Simulate the dedup logic from _proactive_sse_loop.
        def _process(eid: str) -> bool:
            """Returns True if event was new (should be processed)."""
            if eid in state.seen_event_ids:
                return False
            state.seen_event_ids[eid] = None
            state.seen_event_ids.move_to_end(eid)
            if len(state.seen_event_ids) > server._SubprocessState._DEDUP_MAX:
                state.seen_event_ids.popitem(last=False)
            return True

        assert _process(evt_id) is True, "first occurrence should be processed"
        assert _process(evt_id) is False, "second occurrence should be skipped"
        assert evt_id in state.seen_event_ids, "event_id should be in seen set"
        assert len(state.seen_event_ids) == 1
    finally:
        server._state.seen_event_ids = orig_seen


def test_subscriber_dedup_lru_eviction():
    """seen_event_ids caps at _DEDUP_MAX and evicts oldest first."""
    import collections
    from khimaira_chat import server

    orig_seen = server._state.seen_event_ids
    server._state.seen_event_ids = collections.OrderedDict()
    try:
        state = server._state
        cap = server._SubprocessState._DEDUP_MAX

        # Fill to cap + 1 to trigger eviction.
        for i in range(cap + 1):
            eid = f"evt-{i:04d}"
            state.seen_event_ids[eid] = None
            state.seen_event_ids.move_to_end(eid)
            if len(state.seen_event_ids) > cap:
                state.seen_event_ids.popitem(last=False)

        assert len(state.seen_event_ids) == cap
        # Oldest (evt-0000) should have been evicted.
        assert "evt-0000" not in state.seen_event_ids
        # Most-recent should still be present.
        assert f"evt-{cap:04d}" in state.seen_event_ids
    finally:
        server._state.seen_event_ids = orig_seen


@pytest.mark.asyncio
async def test_ensure_subscriber_uses_proactive_loop_not_session(monkeypatch):
    """Regression (SSE-deaf-on-compaction): the lazy-start `_ensure_subscriber`
    must spawn the write_stream-based `_proactive_sse_loop`, NOT a
    request-context-session-bound loop.

    The old `_sse_loop(ctx.session)` captured the MCP request-context session
    from the first tool call and kept emitting through it; after a context
    compaction that handle went stale, so the subscriber stayed alive (never
    crashed → the watchdog never replaced it) while every channel notification
    silently went nowhere — the agent appeared online but received nothing.
    The fix repoints the lazy-start onto `_proactive_sse_loop` (the same loop
    the watchdog + force-resubscribe already used) and removes the session
    capture entirely. This test locks both invariants in."""
    import contextlib
    import inspect

    from khimaira_chat import server

    # Invariant 1: no session capture — `_ensure_subscriber` takes no args.
    assert inspect.signature(server._ensure_subscriber).parameters == {}, (
        "_ensure_subscriber must not capture a request-context session"
    )

    started = {"proactive": False}

    async def stub_proactive():
        started["proactive"] = True
        await asyncio.sleep(60)

    monkeypatch.setattr(server, "_proactive_sse_loop", stub_proactive)

    orig = (server._state.subscriber_task, server._state.write_stream)
    task = None
    try:
        # Invariant 2: with the transport up (write_stream set), it starts
        # the proactive loop.
        server._state.subscriber_task = None
        server._state.write_stream = object()  # non-None → stdio transport up

        server._ensure_subscriber()
        task = server._state.subscriber_task
        assert task is not None, "_ensure_subscriber did not start a subscriber"
        await asyncio.sleep(0.01)
        assert started["proactive"], (
            "_ensure_subscriber started the wrong loop (not _proactive_sse_loop)"
        )

        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

        # Invariant 3: transport not up yet (write_stream None) → no-op, so
        # the proactive loop's `assert write_stream is not None` can't trip.
        server._state.subscriber_task = None
        server._state.write_stream = None
        server._ensure_subscriber()
        assert server._state.subscriber_task is None, (
            "_ensure_subscriber must no-op when write_stream is None (transport not up)"
        )
    finally:
        task = server._state.subscriber_task
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        (server._state.subscriber_task, server._state.write_stream) = orig


# ---------------------------------------------------------------------------
# Session entanglement fence
# ---------------------------------------------------------------------------


class TestSessionEntanglementFence:
    """The PID-based fence prevents two live subprocesses from dual-subscribing
    to the same khimaira session_id's SSE stream.

    Tests are unit-level (no real subprocess spawning): they call
    _acquire_session_claim directly and verify the fence decisions.
    """

    def test_no_prior_claim_allowed(self, tmp_path, monkeypatch):
        """No prior claim file → subprocess may subscribe."""
        from khimaira_chat import server as srv
        monkeypatch.setattr(srv, "_SSE_CLAIM_DIR", tmp_path)
        monkeypatch.setattr(srv, "_MY_PID", 99999)
        result = srv._acquire_session_claim("test-session-1")
        assert result is True, "No prior claim should allow subscription"
        claim = tmp_path / "test-session-1.pid"
        assert claim.exists(), "Claim file should be written"
        assert claim.read_text().strip() == "99999"

    def test_live_prior_fenced(self, tmp_path, monkeypatch):
        """Claim file holds a live PID → this subprocess is fenced (not allowed).

        Uses os.getpid() as the 'live' PID — this process is definitely alive.
        The fence must return False (block subscription), not True (allow).
        This is the reproduce-the-bug test: without the fence, _acquire_session_claim
        would overwrite the claim and return True regardless of prior liveness.
        After the fix, the second claimant is blocked by the live-PID check.
        """
        import os
        from khimaira_chat import server as srv

        live_pid = os.getpid()
        monkeypatch.setattr(srv, "_SSE_CLAIM_DIR", tmp_path)
        monkeypatch.setattr(srv, "_MY_PID", live_pid + 1)  # different PID = different subprocess
        # Write a claim as if the live_pid subprocess already claimed it.
        (tmp_path / "test-session-live.pid").write_text(str(live_pid))
        result = srv._acquire_session_claim("test-session-live")
        assert result is False, (
            "A live prior claimant should FENCE this subprocess (entanglement prevented)"
        )

    def test_dead_prior_reclaim_allowed(self, tmp_path, monkeypatch):
        """Claim file holds a dead PID → this subprocess may reclaim and subscribe.

        Uses a PID beyond the kernel's PID_MAX — guaranteed not to exist.
        The fence should see it as dead and allow the reclaim on Linux.
        """
        import pathlib
        from khimaira_chat import server as srv

        monkeypatch.setattr(srv, "_SSE_CLAIM_DIR", tmp_path)
        monkeypatch.setattr(srv, "_MY_PID", 88888)
        dead_pid = 99999999  # beyond kernel PID_MAX (typically 4194304 on Linux)
        (tmp_path / "test-session-dead.pid").write_text(str(dead_pid))
        result = srv._acquire_session_claim("test-session-dead")
        # If /proc is available (Linux), the dead PID should not exist → reclaim.
        # On non-Linux, _pid_alive returns None → fence (ambiguity policy).
        if pathlib.Path("/proc").exists():
            assert result is True, "Dead prior PID should allow reclaim on Linux"
            claim = tmp_path / "test-session-dead.pid"
            assert claim.read_text().strip() == "88888", "Claim file should hold our PID"
        else:
            # Non-Linux: /proc unavailable → ambiguous → fence (recoverable-default)
            assert result is False, "Non-Linux: ambiguous liveness → fence"

    def test_ambiguous_liveness_fenced(self, tmp_path, monkeypatch):
        """_pid_alive returning None (non-Linux or /proc-unreadable) must FENCE.

        This is the load-bearing safety path: the code says alive=None → fence,
        but the original commit message said 'Non-Linux: fail-open'. This test
        pins the CODE (err-fence-on-ambiguity) so a future edit can't silently
        flip it to the wrong direction.
        """
        from khimaira_chat import server as srv

        monkeypatch.setattr(srv, "_SSE_CLAIM_DIR", tmp_path)
        monkeypatch.setattr(srv, "_MY_PID", 77777)
        # A claim exists from some other PID.
        (tmp_path / "test-session-ambiguous.pid").write_text("55555")
        # Force _pid_alive to return None (simulates /proc-unavailable).
        monkeypatch.setattr(srv, "_pid_alive", lambda pid: None)
        result = srv._acquire_session_claim("test-session-ambiguous")
        assert result is False, (
            "Ambiguous liveness (alive=None) must FENCE, not fail-open — "
            "silent dual-subscribe is the undiagnosable failure"
        )

    def test_sse_fenced_skips_subscriber(self):
        """When sse_fenced=True, _ensure_subscriber must not spawn a task."""
        from khimaira_chat import server as srv

        orig_fenced = srv._state.sse_fenced
        orig_task = srv._state.subscriber_task
        orig_stream = srv._state.write_stream
        try:
            srv._state.sse_fenced = True
            srv._state.subscriber_task = None
            srv._state.write_stream = object()  # non-None (transport up)
            srv._ensure_subscriber()
            assert srv._state.subscriber_task is None, (
                "_ensure_subscriber must not start SSE when sse_fenced=True"
            )
        finally:
            srv._state.sse_fenced = orig_fenced
            srv._state.subscriber_task = orig_task
            srv._state.write_stream = orig_stream
