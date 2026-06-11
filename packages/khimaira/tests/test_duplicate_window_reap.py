"""Duplicate-window reap (2026-06-11) — title-match substrate fix.

A restart/resume can leave two kitty windows with the identical title (live
agent + stale shell). The anchored ^...$ regex matches BOTH, breaking every
title-anchored op. Reap the stale one (foreground has no `claude`) so each
title resolves to exactly one window. Ambiguous → reap nothing, loud-log.
"""

from __future__ import annotations

import json
import importlib
import pytest


@pytest.fixture
def rr():
    from khimaira.monitor import roster_recovery as mod
    importlib.reload(mod)
    return mod


def _win(wid, title, *, live):
    fg = [{"cmdline": ["claude", "--dangerously-load-development-channels"]}] if live \
        else [{"cmdline": ["/usr/bin/bash"]}]
    return {"id": wid, "title": title, "foreground_processes": fg}


def _ls(*wins):
    return json.dumps([{"tabs": [{"windows": list(wins)}]}])


def test_window_is_live(rr):
    assert rr._window_is_live(_win(1, "agent-1", live=True))
    assert not rr._window_is_live(_win(2, "agent-1", live=False))


def test_reaps_stale_duplicate(rr, monkeypatch):
    calls = []
    def fake_kitty(*args):
        calls.append(args)
        if args[0] == "ls":
            return _ls(_win(10, "muther-critic-1", live=True),
                       _win(11, "muther-critic-1", live=False))
        return ""  # close-window succeeds
    monkeypatch.setattr(rr, "_kitty", fake_kitty)
    reaped = rr._reap_duplicate_windows()
    assert reaped == 1
    # the STALE one (id 11) was closed, not the live one
    close_calls = [c for c in calls if c[0] == "close-window"]
    assert close_calls == [("close-window", "--match=id:11")]


def test_no_reap_when_no_duplicates(rr, monkeypatch):
    monkeypatch.setattr(rr, "_kitty",
                        lambda *a: _ls(_win(1, "a", live=True), _win(2, "b", live=True)) if a[0]=="ls" else "")
    assert rr._reap_duplicate_windows() == 0


def test_ambiguous_two_live_not_reaped(rr, monkeypatch):
    closed = []
    def fk(*a):
        if a[0] == "ls":
            return _ls(_win(1, "dup", live=True), _win(2, "dup", live=True))
        closed.append(a); return ""
    monkeypatch.setattr(rr, "_kitty", fk)
    assert rr._reap_duplicate_windows() == 0
    assert closed == [], "two live windows → ambiguous → reap nothing"


def test_ambiguous_zero_live_not_reaped(rr, monkeypatch):
    closed = []
    def fk(*a):
        if a[0] == "ls":
            return _ls(_win(1, "dup", live=False), _win(2, "dup", live=False))
        closed.append(a); return ""
    monkeypatch.setattr(rr, "_kitty", fk)
    assert rr._reap_duplicate_windows() == 0
    assert closed == []


def test_inject_refuses_ambiguous_title(rr, monkeypatch):
    # _count_title_windows returns 2 even after a reap attempt → inject aborts
    monkeypatch.setattr(rr, "_count_title_windows", lambda t: 2)
    monkeypatch.setattr(rr, "_reap_duplicate_windows", lambda: 0)
    sent = []
    monkeypatch.setattr(rr, "_kitty", lambda *a, **k: sent.append(a) or "")
    ok = rr._inject_text_and_submit(5, "hi", "muther-critic-1")
    assert ok is False
    assert sent == [], "must not inject when title is ambiguous"


def test_inject_proceeds_after_reap_resolves(rr, monkeypatch):
    counts = iter([2, 1])  # ambiguous → reap → now unique
    monkeypatch.setattr(rr, "_count_title_windows", lambda t: next(counts))
    monkeypatch.setattr(rr, "_reap_duplicate_windows", lambda: 1)
    # TOCTOU reads buffer via _get_screen; last non-empty line must == our text
    monkeypatch.setattr(rr, "_get_screen", lambda wid: "previous\n/compact")
    monkeypatch.setattr(rr, "_kitty", lambda *a, **k: "")  # all kitty ops succeed
    ok = rr._inject_text_and_submit(5, "/compact", "muther-critic-1")
    assert ok is True, "after reap resolves the duplicate, inject proceeds"
