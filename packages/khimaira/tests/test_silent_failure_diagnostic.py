"""Silent-failure diagnostic invariant tests.

Asserts that every documented path in the agent-silent-failure bug class
produces a deterministic disposition (either reschedule or presumed-dead
notice). Adding a new path means adding a new parametrize case.

See ~/.claude/rules/personal/bug-class-enumeration.md for the discipline.
"""

import asyncio
import time

import pytest

from khimaira.monitor.api import chats as api_chats


_CAUSES = [
    ("alive_recent_activity", 30, "reschedule"),
    ("alive_silent_chat_dropped", 5, "reschedule"),
    ("presumed_dead_no_activity", 999, "presumed_dead"),
    ("rate_limit_simulation", 999, "presumed_dead"),
    ("subprocess_crashed", 999, "presumed_dead"),
]


@pytest.mark.parametrize(
    "cause_id,last_activity_s_ago,expected",
    _CAUSES,
    ids=[c[0] for c in _CAUSES],
)
@pytest.mark.asyncio
async def test_silent_failure_disposition(monkeypatch, cause_id, last_activity_s_ago, expected):
    """For each documented cause: assert correct disposition.

    presumed_dead cases require two _diagnose_and_dispose ticks: first tick
    sends probe (Phase 2), second tick fires presumed-dead notice (Phase 3).
    """
    monkeypatch.setattr(
        api_chats,
        "_session_active_within",
        lambda sid, window: last_activity_s_ago < window,
    )
    monkeypatch.setattr(
        api_chats,
        "_resolve_master_session_id",
        lambda chat_id: "master-sid",
    )
    notices = []
    monkeypatch.setattr(
        "khimaira.monitor.sessions.post_notice",
        lambda target_session_id, text, from_session_id="external", **kw: (
            notices.append({"target": target_session_id, "from": from_session_id, "body": text})
            or {}
        ),
    )

    # Stub probe so it doesn't try to call the real _post_synthetic_message.
    async def _fake_probe(chat_id, to_id, from_id, elapsed_s):
        return True

    monkeypatch.setattr(api_chats, "_send_diagnostic_probe", _fake_probe)

    ts_now = time.time()
    entry = {
        "ts": ts_now - 200,
        "from": "from-sid",
        "to": "to-sid",
        "chat_id": "chat-abc",
        "threshold_s": 90.0,
    }
    key = ("to-sid", "from-sid")
    async with api_chats._REGISTRY_LOCK:
        api_chats._EXPECTED_REPLIES[key] = entry

    # Tick 1
    await api_chats._diagnose_and_dispose(key, entry, ts_now)

    if expected == "reschedule":
        assert key in api_chats._EXPECTED_REPLIES
        assert api_chats._EXPECTED_REPLIES[key]["ts"] == ts_now
        assert len(notices) == 0
    elif expected == "presumed_dead":
        # After tick 1: probe sent, entry still in registry with probe_sent_at set.
        assert key in api_chats._EXPECTED_REPLIES
        assert api_chats._EXPECTED_REPLIES[key].get("probe_sent_at") is not None
        assert len(notices) == 0

        # Tick 2: probe already sent, X still silent → presumed-dead.
        entry2 = api_chats._EXPECTED_REPLIES[key]
        ts_next = ts_now + 30
        await api_chats._diagnose_and_dispose(key, entry2, ts_next)

        assert key not in api_chats._EXPECTED_REPLIES
        assert len(notices) == 1
        assert notices[0]["target"] == "master-sid"
        assert "PRESUMED-DEAD" in notices[0]["body"]
        assert "to-sid" in notices[0]["body"]
        assert "from-sid" in notices[0]["body"]

    async with api_chats._REGISTRY_LOCK:
        api_chats._EXPECTED_REPLIES.pop(key, None)
    api_chats._RECENTLY_PRESUMED_DEAD.clear()


_ROLE_THRESHOLDS = [
    ("architect", 180.0),
    ("analyst", 180.0),
    ("verifier", 300.0),
    ("critic", 120.0),
    ("agent", 90.0),
    ("intake", 90.0),
    ("observer", 90.0),
    ("tracker", 90.0),
    ("master", 90.0),
    ("unknown-role", 90.0),
]


@pytest.mark.parametrize("role,expected_threshold", _ROLE_THRESHOLDS)
def test_threshold_for_role(monkeypatch, role, expected_threshold):
    """Per-role threshold returns expected value; falls back to default for unknown roles."""
    from khimaira.monitor import chats as chats_mod

    fake_room = {"meta": {"member_roles": {"to-sid": role}}}
    monkeypatch.setattr(chats_mod, "load_room", lambda chat_id: fake_room)

    threshold = api_chats._threshold_for_session("to-sid", "chat-abc")
    assert threshold == expected_threshold


def test_threshold_falls_back_when_room_lookup_fails(monkeypatch):
    """If load_room raises, fall back to default (90s)."""
    from khimaira.monitor import chats as chats_mod

    def _raise(chat_id):
        raise Exception("boom")

    monkeypatch.setattr(chats_mod, "load_room", _raise)
    threshold = api_chats._threshold_for_session("to-sid", "chat-abc")
    assert threshold == 90.0


def test_threshold_falls_back_to_name_inference(monkeypatch):
    """When chat.member_roles is empty (pre-v1.9.6 chat), threshold inferred from session name."""
    from khimaira.monitor import chats as chats_mod

    monkeypatch.setattr(chats_mod, "load_room", lambda chat_id: {"meta": {}})
    fake_state = {"status": {"name": "architect-1"}}
    monkeypatch.setattr("khimaira.monitor.sessions.state", lambda sid: fake_state)
    threshold = api_chats._threshold_for_session("some-uuid", "chat-abc")
    assert threshold == 180.0, f"expected 180s for architect, got {threshold}"


def test_threshold_inference_works_for_prefixed_names(monkeypatch):
    """Name inference handles jp-architect-1 / khimaira-architect-1 style prefixes."""
    from khimaira.monitor import chats as chats_mod

    monkeypatch.setattr(chats_mod, "load_room", lambda cid: {"meta": {}})
    fake_state = {"status": {"name": "jp-architect-1"}}
    monkeypatch.setattr("khimaira.monitor.sessions.state", lambda sid: fake_state)
    assert api_chats._threshold_for_session("uuid", "chat-abc") == 180.0


# ---------------------------------------------------------------------------
# Class B: /proc fallback for kitty-"unknown" (B2 invariant)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b2_proc_fallback_downgrades_unknown_to_deaf(monkeypatch):
    """Class B invariant (B2): when kitty returns 'unknown' AND /proc confirms
    the process is alive, _diagnose_and_dispose must send a DEAF nudge notice
    (body contains 'IDLE/DEAF'), NOT a PRESUMED-DEAD alarm. The fallback honors
    the involuntary-signal invariant — a process we can verify alive is not dead.
    """
    from khimaira.monitor import chats as chats_mod

    # Monkeypatch the kitty classifier to return "unknown".
    async def _classify_unknown(sid):
        return "unknown"

    monkeypatch.setattr(api_chats, "_classify_unresponsive", _classify_unknown)
    # /proc confirms the process is alive.
    monkeypatch.setattr(api_chats, "_is_process_alive_for_session", lambda sid: True)
    # Phase-1 liveness check must NOT short-circuit (we want to reach Phase 3).
    # B1 uses _classify_unresponsive for Phase 1 now; "unknown" is not in ("active","busy"),
    # so Phase 1 falls through correctly — no additional monkeypatch needed.

    monkeypatch.setattr(
        api_chats,
        "_session_active_within",
        lambda sid, window: False,
    )

    notices: list[str] = []

    async def fake_post_notice(target_session_id, text, from_session_id=None, **kw):
        notices.append(text)

    monkeypatch.setattr(
        api_chats,
        "_resolve_master_session_id",
        lambda chat_id: "master-sid",
    )

    import khimaira.monitor.sessions as sessions_mod

    monkeypatch.setattr(
        sessions_mod,
        "post_notice",
        lambda target_session_id, text, from_session_id=None, **kw: notices.append(text),
    )

    # Synthesize an entry that has already been probed (probe_sent_at set) so
    # _diagnose_and_dispose reaches Phase 3 directly.
    now = time.time()
    key = ("to-session", "from-session")
    entry = {
        "to": "to-session",
        "from": "from-session",
        "chat_id": "chat-test",
        "ts": now - 120,
        "probe_sent_at": now - 60,
        "threshold_s": 90.0,
    }

    # Seed _EXPECTED_REPLIES so pop() in Phase 3 finds the entry.
    api_chats._EXPECTED_REPLIES[key] = entry

    await api_chats._diagnose_and_dispose(key, entry, now)

    assert notices, "No notice was sent — _diagnose_and_dispose silently did nothing."
    combined = " ".join(notices)
    assert "IDLE/DEAF" in combined, (
        f"Expected IDLE/DEAF nudge notice (B2 downgrade), got: {combined!r}"
    )
    assert "PRESUMED-DEAD" not in combined, (
        f"Got PRESUMED-DEAD alarm despite /proc confirming process alive: {combined!r}"
    )
