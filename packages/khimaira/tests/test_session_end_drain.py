"""Tests for the drain-before-idle keystone in the Stop hook (session_end.py).

The keystone blocks a seat from idling while it still owes a verdict or has an
unanswered directed message — emitting the verified CC Stop-hook control
(exit 0 + `{"decision":"block", …, hookSpecificOutput.additionalContext}`) so the
seat drains IN-SESSION. The load-bearing invariant (master's explicit requirement):
**fail-open-on-ERROR** — a hook exception must STILL exit 0, never wedge a seat.

Covers:
- exception in the drain path → main() still exits 0 (the fail-open invariant)
- owed work → emits decision=block JSON on stdout + returns 0 (not 2 — exit 2 would
  discard the JSON per the CC docs)
- not-owed → no block, returns None + resets the cross-chain counter
- throttled seat → never blocked (can't drain via re-engage)
- cross-chain block cap → fail-opens (no block) once exceeded
"""

from __future__ import annotations

import json

import pytest
from khimaira.hooks import session_end as se


@pytest.fixture(autouse=True)
def _disable_memory_refresh(monkeypatch):
    """Keep Stop-hook tests away from the developer's live memory files."""
    monkeypatch.setattr(se, "_refresh_claude_memory", lambda cwd: None)


@pytest.fixture
def isolated_counter(tmp_path, monkeypatch):
    """Point the drain counter at a tmp file so tests don't touch /tmp state."""
    counter = tmp_path / "drain.count"
    monkeypatch.setattr(se, "_drain_counter_path", lambda _sid: counter)
    return counter


# ---------------------------------------------------------------------------
# Fail-open invariant (the one master called out explicitly)
# ---------------------------------------------------------------------------


def test_main_drain_exception_still_exits_0(monkeypatch):
    """If the drain path raises, main() must STILL return 0 — a hook error can
    never block CC from exiting (never wedge a seat)."""

    def _boom(*_a, **_k):
        raise RuntimeError("drain blew up")

    monkeypatch.setattr(se, "_drain_before_idle", _boom)
    # Neutralize the lead-distill side so the test is hermetic.
    monkeypatch.setattr(se, "detect_domain", lambda _n: "general")
    monkeypatch.setattr(se, "_get_session_name", lambda _sid: "agent-1")

    payload = json.dumps({"session_id": "s-1", "transcript_path": None})
    monkeypatch.setattr("sys.stdin", _Stdin(payload))

    assert se.main() == 0


def test_owed_work_probe_errors_are_swallowed(monkeypatch):
    """_owed_work swallows a probe error → returns None (fail-open: not-owed)."""
    import khimaira.monitor.api.chats as apichats

    def _boom(_sid):
        raise RuntimeError("daemon read failed")

    monkeypatch.setattr(apichats, "_get_session_obligations", _boom)
    # directed probe also raises
    import khimaira.monitor.roster_recovery as rr

    monkeypatch.setattr(rr, "_session_has_directed_unanswered", _boom)

    assert se._owed_work("s-1") is None


# ---------------------------------------------------------------------------
# Block emission (verified mechanism: exit 0 + JSON decision=block on stdout)
# ---------------------------------------------------------------------------


def test_owed_emits_block_json_and_exits_0(isolated_counter, monkeypatch, capsys):
    monkeypatch.setattr(
        se,
        "_owed_work",
        lambda _sid: {"summary": "1 owed verdict(s): task-x", "drain_steps": "Post it."},
    )
    rc = se._drain_before_idle({"session_id": "s-1"}, throttled=False)
    assert rc == 0  # exit 0 (NOT 2 — exit 2 would discard the JSON)

    out = json.loads(capsys.readouterr().out)
    assert out["decision"] == "block"
    assert "owed verdict" in out["reason"]
    assert out["hookSpecificOutput"]["additionalContext"] == "Post it."
    assert out["hookSpecificOutput"]["hookEventName"] == "Stop"
    assert isolated_counter.read_text().strip() == "1"  # attempt bumped


def test_not_owed_no_block_and_resets_counter(isolated_counter, monkeypatch, capsys):
    isolated_counter.write_text("3")  # stale attempts from a prior owed streak
    monkeypatch.setattr(se, "_owed_work", lambda _sid: None)

    rc = se._drain_before_idle({"session_id": "s-1"}, throttled=False)
    assert rc is None
    assert capsys.readouterr().out == ""  # no block emitted
    assert not isolated_counter.exists()  # counter reset on clear


def test_throttled_seat_never_blocked(isolated_counter, monkeypatch, capsys):
    """A rate-limited seat can't act on a re-engage → never block it."""
    monkeypatch.setattr(se, "_owed_work", lambda _sid: {"summary": "owed", "drain_steps": "x"})
    rc = se._drain_before_idle({"session_id": "s-1"}, throttled=True)
    assert rc is None
    assert capsys.readouterr().out == ""


def test_cross_chain_cap_fail_opens(isolated_counter, monkeypatch, capsys):
    """Once the cross-chain attempt counter hits the cap, a still-owed seat
    fail-opens (no block) so it can't be perpetually re-blocked."""
    monkeypatch.setattr(se, "_DRAIN_BLOCK_CAP", 2)
    monkeypatch.setattr(se, "_owed_work", lambda _sid: {"summary": "owed", "drain_steps": "x"})
    isolated_counter.write_text("2")  # already at cap

    rc = se._drain_before_idle({"session_id": "s-1"}, throttled=False)
    assert rc is None
    assert capsys.readouterr().out == ""


def test_disabled_via_env(monkeypatch, capsys):
    monkeypatch.setattr(se, "_DRAIN_ENABLED", False)
    monkeypatch.setattr(se, "_owed_work", lambda _sid: {"summary": "owed", "drain_steps": "x"})
    assert se._drain_before_idle({"session_id": "s-1"}, throttled=False) is None
    assert capsys.readouterr().out == ""


class _Stdin:
    """Minimal stdin stand-in carrying a fixed payload for main()."""

    def __init__(self, data: str):
        self._data = data

    def read(self) -> str:
        return self._data
