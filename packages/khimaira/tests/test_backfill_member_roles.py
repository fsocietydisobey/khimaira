"""Tests for backfill_member_roles — #58 Part 2.

Covers acceptance criteria:
1. After backfill: every ACCEPTED member has a member_roles entry.
2. Idempotent + non-destructive: run twice → same result; existing roles preserved.
3. No new lockouts: STATE-A chats that gain member_roles never produce _UNRESOLVABLE.
4. Creator resolves to master; role-named sessions (agent-1) resolve to their role.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest


@pytest.fixture
def isolated_backfill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    state_root = tmp_path / "state"
    state_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("XDG_STATE_HOME", str(state_root))

    from khimaira.monitor import chats as c
    from khimaira.monitor import sessions as s

    # Force reload of module-level state that depends on XDG_STATE_HOME.
    monkeypatch.setattr(c, "_chat_dir", lambda: state_root / "khimaira" / "chats")
    (state_root / "khimaira" / "chats").mkdir(parents=True, exist_ok=True)
    (state_root / "khimaira" / "sessions").mkdir(parents=True, exist_ok=True)
    return c, s


def _write_chat(chat_dir: Path, chat_id: str, meta: dict, members: list[dict]) -> None:
    """Helper: write a minimal chat JSONL for tests."""
    path = chat_dir / f"{chat_id}.jsonl"
    with path.open("w") as f:
        f.write(json.dumps(meta) + "\n")
        for m in members:
            f.write(json.dumps(m) + "\n")


# ---------------------------------------------------------------------------
# AC 1 — After backfill, every ACCEPTED member has a member_roles entry
# ---------------------------------------------------------------------------


def test_state_a_backfill_assigns_roles(isolated_backfill):
    """STATE-A chat (no member_roles) gets entries for all accepted members."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    _write_chat(
        chat_dir,
        "chat-test-a1",
        {
            "kind": "meta",
            "event_id": "e1",
            "chat_id": "chat-test-a1",
            "ts": "2026-01-01T00:00:00+00:00",
            "created_by": "creator-uuid",
            "created_by_name": "alice",
            "title": "test",
        },
        [
            {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-a1", "session_id": "creator-uuid",
             "session_name": "alice", "state": "accepted"},
            {"kind": "member", "event_id": "e3", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-a1", "session_id": "agent-uuid",
             "session_name": "agent-1", "state": "accepted"},
            {"kind": "member", "event_id": "e4", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-a1", "session_id": "unknown-uuid",
             "session_name": "unknown-session", "state": "accepted"},
        ],
    )

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=False)

    room = c.load_room("chat-test-a1")
    roles = room["meta"].get("member_roles", {})
    accepted = [sid for sid, m in room["members"].items() if m["state"] == "accepted"]
    for sid in accepted:
        assert sid in roles, f"Missing role for {sid}"


def test_state_b_backfill_fills_gaps_only(isolated_backfill):
    """STATE-B chat (partial member_roles) fills only missing entries."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    _write_chat(
        chat_dir,
        "chat-test-b1",
        {
            "kind": "meta",
            "event_id": "e1",
            "chat_id": "chat-test-b1",
            "ts": "2026-01-01T00:00:00+00:00",
            "created_by": "creator-uuid",
            "created_by_name": "master",
            "title": "test",
            "member_roles": {"creator-uuid": "master"},  # agent-uuid missing
        },
        [
            {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-b1", "session_id": "creator-uuid",
             "session_name": "master", "state": "accepted"},
            {"kind": "member", "event_id": "e3", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-b1", "session_id": "agent-uuid",
             "session_name": "agent-1", "state": "accepted"},
        ],
    )

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=False)

    room = c.load_room("chat-test-b1")
    roles = room["meta"]["member_roles"]
    assert roles["creator-uuid"] == "master"
    assert roles["agent-uuid"] == "agent"


# ---------------------------------------------------------------------------
# AC 2 — Idempotent + non-destructive
# ---------------------------------------------------------------------------


def test_idempotent_double_run(isolated_backfill):
    """Running backfill twice produces byte-identical member_roles."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    _write_chat(
        chat_dir,
        "chat-test-idem",
        {
            "kind": "meta", "event_id": "e1", "chat_id": "chat-test-idem",
            "ts": "2026-01-01T00:00:00+00:00", "created_by": "creator-uuid",
            "created_by_name": "alice", "title": "test",
        },
        [
            {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-idem", "session_id": "creator-uuid",
             "session_name": "alice", "state": "accepted"},
        ],
    )

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=False)
    roles_first = dict(c.load_room("chat-test-idem")["meta"]["member_roles"])

    run(dry_run=False)
    roles_second = dict(c.load_room("chat-test-idem")["meta"]["member_roles"])

    assert roles_first == roles_second


def test_non_destructive_existing_role_preserved(isolated_backfill):
    """Existing explicit role assignments are never overwritten."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    _write_chat(
        chat_dir,
        "chat-test-preserve",
        {
            "kind": "meta", "event_id": "e1", "chat_id": "chat-test-preserve",
            "ts": "2026-01-01T00:00:00+00:00", "created_by": "creator-uuid",
            "created_by_name": "alice", "title": "test",
            "member_roles": {"critic-uuid": "critic"},  # explicit grant
        },
        [
            {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-preserve", "session_id": "critic-uuid",
             "session_name": "critic-1", "state": "accepted"},
        ],
    )

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=False)

    room = c.load_room("chat-test-preserve")
    assert room["meta"]["member_roles"]["critic-uuid"] == "critic"


# ---------------------------------------------------------------------------
# AC 3 — No new lockouts (STATE-A → gains member_roles)
# ---------------------------------------------------------------------------


def test_no_lockouts_after_state_a_backfill(isolated_backfill):
    """After backfill, no member resolves to None or _UNRESOLVABLE."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    _write_chat(
        chat_dir,
        "chat-test-nolockout",
        {
            "kind": "meta", "event_id": "e1", "chat_id": "chat-test-nolockout",
            "ts": "2026-01-01T00:00:00+00:00", "created_by": "creator-uuid",
            "created_by_name": "alice", "title": "test",
        },
        [
            {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-nolockout", "session_id": "creator-uuid",
             "session_name": "alice", "state": "accepted"},
            {"kind": "member", "event_id": "e3", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-nolockout", "session_id": "mystery-uuid",
             "session_name": "mystery-session", "state": "accepted"},
        ],
    )

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=False)

    room = c.load_room("chat-test-nolockout")
    roles = room["meta"].get("member_roles", {})
    for sid, member in room["members"].items():
        if member["state"] == "accepted":
            assert roles.get(sid) is not None, f"Member {sid} has no role after backfill"
            assert roles[sid] != "_UNRESOLVABLE", f"Member {sid} got _UNRESOLVABLE"


# ---------------------------------------------------------------------------
# AC 4 — Resolution ladder correctness
# ---------------------------------------------------------------------------


def test_creator_resolves_to_master(isolated_backfill):
    """Creator omitted from member_roles resolves to master, not member."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    _write_chat(
        chat_dir,
        "chat-test-creator",
        {
            "kind": "meta", "event_id": "e1", "chat_id": "chat-test-creator",
            "ts": "2026-01-01T00:00:00+00:00", "created_by": "creator-uuid",
            "created_by_name": "alice", "title": "test",
            "member_roles": {},  # omits creator — the pre-#67 lockout case
        },
        [
            {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-creator", "session_id": "creator-uuid",
             "session_name": "alice", "state": "accepted"},
        ],
    )

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=False)

    roles = c.load_room("chat-test-creator")["meta"]["member_roles"]
    assert roles["creator-uuid"] == "master", (
        "Creator omitted from member_roles should resolve to master, not member"
    )


def test_role_named_session_resolves_correctly(isolated_backfill):
    """agent-1 resolves to agent; observer-1 resolves to observer."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    _write_chat(
        chat_dir,
        "chat-test-named",
        {
            "kind": "meta", "event_id": "e1", "chat_id": "chat-test-named",
            "ts": "2026-01-01T00:00:00+00:00", "created_by": "master-uuid",
            "created_by_name": "master", "title": "test",
        },
        [
            {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-named", "session_id": "master-uuid",
             "session_name": "master", "state": "accepted"},
            {"kind": "member", "event_id": "e3", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-named", "session_id": "agent-uuid",
             "session_name": "agent-1", "state": "accepted"},
            {"kind": "member", "event_id": "e4", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-named", "session_id": "observer-uuid",
             "session_name": "observer-1", "state": "accepted"},
        ],
    )

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=False)

    roles = c.load_room("chat-test-named")["meta"]["member_roles"]
    assert roles["master-uuid"] == "master"
    assert roles["agent-uuid"] == "agent", "agent-1 should resolve to agent, not member"
    assert roles["observer-uuid"] == "observer", "observer-1 should resolve to observer"


def test_unresolvable_session_gets_member_role(isolated_backfill):
    """Session with no name-role match gets neutral 'member' role."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    _write_chat(
        chat_dir,
        "chat-test-unknown",
        {
            "kind": "meta", "event_id": "e1", "chat_id": "chat-test-unknown",
            "ts": "2026-01-01T00:00:00+00:00", "created_by": "master-uuid",
            "created_by_name": "master", "title": "test",
        },
        [
            {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-unknown", "session_id": "master-uuid",
             "session_name": "master", "state": "accepted"},
            {"kind": "member", "event_id": "e3", "ts": "2026-01-01T00:00:00+00:00",
             "chat_id": "chat-test-unknown", "session_id": "random-uuid",
             "session_name": "janice-42", "state": "accepted"},
        ],
    )

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=False)

    roles = c.load_room("chat-test-unknown")["meta"]["member_roles"]
    assert roles["random-uuid"] == "member"


def test_dry_run_makes_no_changes(isolated_backfill):
    """--dry-run reports changes but writes nothing to disk."""
    c, _ = isolated_backfill
    chat_dir = c._chat_dir()

    chat_path = chat_dir / "chat-test-dryrun.jsonl"
    meta = {
        "kind": "meta", "event_id": "e1", "chat_id": "chat-test-dryrun",
        "ts": "2026-01-01T00:00:00+00:00", "created_by": "creator-uuid",
        "created_by_name": "alice", "title": "test",
    }
    members = [
        {"kind": "member", "event_id": "e2", "ts": "2026-01-01T00:00:00+00:00",
         "chat_id": "chat-test-dryrun", "session_id": "creator-uuid",
         "session_name": "alice", "state": "accepted"},
    ]
    content_before = "\n".join(json.dumps(r) for r in [meta] + members) + "\n"
    chat_path.write_text(content_before, encoding="utf-8")

    from khimaira.monitor.backfill_member_roles import run

    run(dry_run=True)

    content_after = chat_path.read_text(encoding="utf-8")
    assert content_after == content_before, "dry-run must not modify any file"
