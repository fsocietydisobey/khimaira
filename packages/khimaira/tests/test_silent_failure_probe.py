"""Active-probe class-invariant tests for Pattern 5 silent-failure diagnostic.

Asserts each outcome of the probe flow per architect's enumeration:
1. alive_responds_to_probe → probe sent, X broadcasts within window → false-alarm to Y
2. presumed_dead_no_probe_response → probe sent, X silent through window → master notice
3. late_reply_after_presumed_dead → probe + presumed-dead + X replies T+N → supersede to master
4. probe_during_already_active_session → recent activity → no probe fired

See ~/.claude/rules/personal/bug-class-enumeration.md for the discipline.
"""
import time

import pytest

from khimaira.monitor.api import chats as api_chats


@pytest.fixture(autouse=True)
def reset_state():
    """Reset module-level state between tests."""
    api_chats._EXPECTED_REPLIES.clear()
    api_chats._RECENTLY_PRESUMED_DEAD.clear()
    yield
    api_chats._EXPECTED_REPLIES.clear()
    api_chats._RECENTLY_PRESUMED_DEAD.clear()


def _setup_mocks(monkeypatch, active: bool, probe_succeeds: bool = True):
    """Common setup: mock liveness, probe, master-resolve, post_notice."""
    monkeypatch.setattr(
        api_chats,
        "_session_active_within",
        lambda sid, window: active,
    )
    monkeypatch.setattr(
        api_chats,
        "_resolve_master_session_id",
        lambda chat_id: "master-sid",
    )
    probes = []

    async def fake_probe(chat_id, to_id, from_id, elapsed_s):
        probes.append({"chat_id": chat_id, "to": to_id, "from": from_id, "elapsed": elapsed_s})
        return probe_succeeds

    monkeypatch.setattr(api_chats, "_send_diagnostic_probe", fake_probe)

    notices = []

    def fake_post_notice(target_session_id, text, from_session_id="khimaira-daemon", **kw):
        notices.append({"target": target_session_id, "from": from_session_id, "body": text})

    import khimaira.monitor.sessions as sessions_mod

    monkeypatch.setattr(sessions_mod, "post_notice", fake_post_notice)

    return probes, notices


@pytest.mark.asyncio
async def test_probe_during_already_active_session(monkeypatch):
    """If X has recent activity, no probe fires; entry rescheduled."""
    probes, notices = _setup_mocks(monkeypatch, active=True)
    ts_now = time.time()
    key = ("to-sid", "from-sid")
    entry = {
        "ts": ts_now - 200,
        "from": "from-sid",
        "to": "to-sid",
        "chat_id": "chat-abc",
        "threshold_s": 90.0,
    }
    api_chats._EXPECTED_REPLIES[key] = entry

    await api_chats._diagnose_and_dispose(key, entry, ts_now)

    assert len(probes) == 0
    assert len(notices) == 0
    assert key in api_chats._EXPECTED_REPLIES
    assert api_chats._EXPECTED_REPLIES[key]["ts"] == ts_now


@pytest.mark.asyncio
async def test_alive_responds_to_probe(monkeypatch):
    """Probe sent, X broadcasts before next tick → broadcast-resolve clears entry → no presumed-dead."""
    probes, notices = _setup_mocks(monkeypatch, active=False)
    ts_now = time.time()
    key = ("to-sid", "from-sid")
    entry = {
        "ts": ts_now - 200,
        "from": "from-sid",
        "to": "to-sid",
        "chat_id": "chat-abc",
        "threshold_s": 90.0,
    }
    api_chats._EXPECTED_REPLIES[key] = entry

    # Phase 2: probe sent
    await api_chats._diagnose_and_dispose(key, entry, ts_now)
    assert len(probes) == 1
    assert api_chats._EXPECTED_REPLIES[key]["probe_sent_at"] == ts_now
    assert len(notices) == 0  # no notice yet

    # Simulate X broadcasting in response (broadcast-resolve clears entry)
    await api_chats._resolve_expected_reply("to-sid", ["from-sid"], chat_id="chat-abc")
    assert key not in api_chats._EXPECTED_REPLIES
    # No presumed-dead notice ever fired
    assert len(notices) == 0
    # No supersede either (entry not in _RECENTLY_PRESUMED_DEAD — never marked dead)
    assert ("to-sid", "from-sid") not in api_chats._RECENTLY_PRESUMED_DEAD


@pytest.mark.asyncio
async def test_presumed_dead_no_probe_response(monkeypatch):
    """Probe sent, X silent through next tick → presumed-dead notice to master."""
    probes, notices = _setup_mocks(monkeypatch, active=False)
    ts_now = time.time()
    key = ("to-sid", "from-sid")
    entry = {
        "ts": ts_now - 200,
        "from": "from-sid",
        "to": "to-sid",
        "chat_id": "chat-abc",
        "threshold_s": 90.0,
    }
    api_chats._EXPECTED_REPLIES[key] = entry

    # Phase 2: probe sent on first call
    await api_chats._diagnose_and_dispose(key, entry, ts_now)
    assert len(probes) == 1
    assert len(notices) == 0

    # Phase 3: still silent on next tick → presumed-dead
    entry = api_chats._EXPECTED_REPLIES[key]  # has probe_sent_at now
    ts_next = ts_now + 30  # one watcher tick later
    await api_chats._diagnose_and_dispose(key, entry, ts_next)
    assert key not in api_chats._EXPECTED_REPLIES
    assert len(notices) == 1
    assert notices[0]["target"] == "master-sid"
    assert "PRESUMED-DEAD" in notices[0]["body"]
    assert "to-sid" in notices[0]["body"]
    # Recorded for supersede tracking
    assert ("to-sid", "from-sid") in api_chats._RECENTLY_PRESUMED_DEAD


@pytest.mark.asyncio
async def test_late_reply_after_presumed_dead(monkeypatch):
    """Probe + presumed-dead fires. X replies at T+N → supersede notice to master."""
    probes, notices = _setup_mocks(monkeypatch, active=False)
    ts_now = time.time()
    key = ("to-sid", "from-sid")
    entry = {
        "ts": ts_now - 200,
        "from": "from-sid",
        "to": "to-sid",
        "chat_id": "chat-abc",
        "threshold_s": 90.0,
    }
    api_chats._EXPECTED_REPLIES[key] = entry

    # Phase 2 + 3: probe then presumed-dead
    await api_chats._diagnose_and_dispose(key, entry, ts_now)
    entry = api_chats._EXPECTED_REPLIES[key]
    await api_chats._diagnose_and_dispose(key, entry, ts_now + 30)
    assert len(notices) == 1  # presumed-dead
    assert "PRESUMED-DEAD" in notices[0]["body"]

    # Now X replies (resolves a different pending entry — supersede checks _RECENTLY_PRESUMED_DEAD)
    await api_chats._resolve_expected_reply("to-sid", ["other-sid"], chat_id="chat-abc")

    # Supersede notice fired
    assert len(notices) == 2
    assert "SUPERSEDE" in notices[1]["body"]
    assert "to-sid" in notices[1]["body"]
    # Entry cleared from supersede tracking
    assert ("to-sid", "from-sid") not in api_chats._RECENTLY_PRESUMED_DEAD


@pytest.mark.asyncio
async def test_sweep_drops_expired_supersede_entries(monkeypatch):
    """Entries older than _PRESUMED_DEAD_TTL_S are dropped by sweep."""
    ts_now = time.time()
    api_chats._RECENTLY_PRESUMED_DEAD[("old-sid", "from-sid")] = {
        "notice_ts": ts_now - 400,  # > 300s TTL
        "chat_id": "chat-abc",
        "from_id": "from-sid",
        "to_id": "old-sid",
        "elapsed_s": 200,
    }
    api_chats._RECENTLY_PRESUMED_DEAD[("recent-sid", "from-sid")] = {
        "notice_ts": ts_now - 100,  # < 300s TTL
        "chat_id": "chat-abc",
        "from_id": "from-sid",
        "to_id": "recent-sid",
        "elapsed_s": 200,
    }

    api_chats._sweep_presumed_dead(ts_now)

    assert ("old-sid", "from-sid") not in api_chats._RECENTLY_PRESUMED_DEAD
    assert ("recent-sid", "from-sid") in api_chats._RECENTLY_PRESUMED_DEAD


# ---------------------------------------------------------------------------
# Class-invariant: _threshold_for_session role lookup
# Per Cat 2 audit ctx-pattern5-architect-threshold-misfire.
# ---------------------------------------------------------------------------


def test_threshold_for_session_returns_role_specific_for_architect(monkeypatch):
    """After member_roles backfill, _threshold_for_session returns 180s for
    architect — not 90s default — regardless of whether session name is set.

    Canonical path (member_roles) checked BEFORE name-inference fallback.
    Guards the race that caused Pattern 5 to fire at T+92-116s: threshold
    cached at probe-registration before session name was set, locking in 90s.
    """
    import khimaira.monitor.chats as chats_mod

    monkeypatch.setattr(
        chats_mod,
        "load_room",
        lambda chat_id: {
            "meta": {
                "member_roles": {"arch-uuid": "architect"},
                "created_by": "master-uuid",
            },
            "members": {},
            "messages": [],
        },
    )

    result = api_chats._threshold_for_session("arch-uuid", "chat-any")
    assert result == 180.0, f"expected 180.0, got {result}"


def test_threshold_for_session_fallback_via_session_name(monkeypatch):
    """When member_roles absent, name-fallback returns role-specific threshold
    if session status.name contains the role segment.

    Covers v1-era chats before backfill: threshold correct only when name set
    before probe registration.
    """
    import khimaira.monitor.chats as chats_mod
    import khimaira.monitor.sessions as sessions_mod

    monkeypatch.setattr(
        chats_mod,
        "load_room",
        lambda chat_id: {"meta": {}, "members": {}, "messages": []},
    )
    monkeypatch.setattr(
        sessions_mod,
        "state",
        lambda sid: {"status": {"name": "architect-1"}, "name": None},
    )

    result = api_chats._threshold_for_session("arch-uuid", "chat-any")
    assert result == 180.0, f"expected 180.0 via name-fallback, got {result}"


def test_threshold_for_session_defaults_when_no_role_and_no_name(monkeypatch):
    """When member_roles absent AND session name unset, threshold falls back
    to 90s default — the broken state this fix closes.

    Documents the failure mode: if session_set_name not called before first
    probe registration, probe fires at T+90s instead of T+180s.
    """
    import khimaira.monitor.chats as chats_mod
    import khimaira.monitor.sessions as sessions_mod

    monkeypatch.setattr(
        chats_mod,
        "load_room",
        lambda chat_id: {"meta": {}, "members": {}, "messages": []},
    )
    monkeypatch.setattr(
        sessions_mod,
        "state",
        lambda sid: {"status": None, "name": None},
    )

    result = api_chats._threshold_for_session("arch-uuid", "chat-any")
    assert result == api_chats._REPLY_OVERDUE_DEFAULT_S, (
        f"expected {api_chats._REPLY_OVERDUE_DEFAULT_S} (default), got {result}"
    )
