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


async def test_reaps_stale_duplicate(rr, monkeypatch):
    calls = []
    async def fake_kitty(*args):
        calls.append(args)
        if args[0] == "ls":
            return _ls(_win(10, "muther-critic-1", live=True),
                       _win(11, "muther-critic-1", live=False))
        return ""  # close-window succeeds
    monkeypatch.setattr(rr, "_kitty", fake_kitty)
    reaped = await rr._reap_duplicate_windows()
    assert reaped == 1
    # the STALE one (id 11) was closed, not the live one
    close_calls = [c for c in calls if c[0] == "close-window"]
    assert close_calls == [("close-window", "--match=id:11")]


async def test_no_reap_when_no_duplicates(rr, monkeypatch):
    async def fake_kitty(*a):
        return _ls(_win(1, "a", live=True), _win(2, "b", live=True)) if a[0] == "ls" else ""
    monkeypatch.setattr(rr, "_kitty", fake_kitty)
    assert await rr._reap_duplicate_windows() == 0


async def test_ambiguous_two_live_not_reaped(rr, monkeypatch):
    closed = []
    async def fk(*a):
        if a[0] == "ls":
            return _ls(_win(1, "dup", live=True), _win(2, "dup", live=True))
        closed.append(a); return ""
    monkeypatch.setattr(rr, "_kitty", fk)
    assert await rr._reap_duplicate_windows() == 0
    assert closed == [], "two live windows → ambiguous → reap nothing"


async def test_ambiguous_zero_live_not_reaped(rr, monkeypatch):
    closed = []
    async def fk(*a):
        if a[0] == "ls":
            return _ls(_win(1, "dup", live=False), _win(2, "dup", live=False))
        closed.append(a); return ""
    monkeypatch.setattr(rr, "_kitty", fk)
    assert await rr._reap_duplicate_windows() == 0
    assert closed == []


async def test_inject_refuses_ambiguous_title(rr, monkeypatch):
    # _count_title_windows returns 2 even after a reap attempt → inject aborts
    async def _count(t):
        return 2
    async def _reap():
        return 0
    sent = []
    async def _kitty(*a, **k):
        sent.append(a)
        return ""
    monkeypatch.setattr(rr, "_count_title_windows", _count)
    monkeypatch.setattr(rr, "_reap_duplicate_windows", _reap)
    monkeypatch.setattr(rr, "_kitty", _kitty)
    ok = await rr._inject_text_and_submit(5, "hi", "muther-critic-1")
    assert ok is False
    assert sent == [], "must not inject when title is ambiguous"


async def test_inject_proceeds_after_reap_resolves(rr, monkeypatch):
    counts = iter([2, 1])  # ambiguous → reap → now unique
    async def _count(t):
        return next(counts)
    async def _reap():
        return 1

    async def _no_sleep(_s):
        return None

    monkeypatch.setattr(rr.asyncio, "sleep", _no_sleep)  # skip real poll waits

    # TOCTOU reads buffer via _get_screen and checks for the nonce _kitty's
    # send-text call was given — a static screen (no nonce) always aborts,
    # regardless of timing. Capture+echo it, same pattern as
    # TestInjectTextAndSubmit._drive.
    state = {"inject": None, "enters": 0}

    async def _kitty(*args, **kw):
        op = args[0] if args else ""
        if op == "send-text":
            state["inject"] = args[-1]
        elif op == "send-key" and "enter" in args:
            state["enters"] += 1
        return ""  # all kitty ops succeed

    async def _get_screen(wid):
        if state["enters"] == 0:
            return f"previous\n{state['inject'] or ''}"  # nonce present, not yet submitted
        return "✶ Working…\n❯ "  # input cleared → submitted

    monkeypatch.setattr(rr, "_count_title_windows", _count)
    monkeypatch.setattr(rr, "_reap_duplicate_windows", _reap)
    monkeypatch.setattr(rr, "_get_screen", _get_screen)
    monkeypatch.setattr(rr, "_kitty", _kitty)
    ok = await rr._inject_text_and_submit(5, "/compact", "muther-critic-1")
    assert ok is True, "after reap resolves the duplicate, inject proceeds"


# --- _window_for_session_name: unscoped targeted lookup (muther note-2 fix) ----

async def test_window_for_session_name_matches_title(rr, monkeypatch):
    ls = [{"tabs": [{"windows": [
        {"id": 1, "title": "muther-agent-1", "cmdline": ["bash", "-ic", "claude-chat -n muther-agent-1"]},
        {"id": 2, "title": "other", "cmdline": ["bash"]},
    ]}]}]
    async def _kitty(*a):
        return json.dumps(ls)
    monkeypatch.setattr(rr, "_kitty", _kitty)
    w = await rr._window_for_session_name("muther-agent-1")
    assert w and w["window_id"] == 1


async def test_window_for_session_name_strips_activity_marker(rr, monkeypatch):
    # kitty prepends "✳ " to active windows — must still match (the muther agent-3 case)
    ls = [{"tabs": [{"windows": [
        {"id": 5, "title": "✳ muther-agent-3", "cmdline": ["bash", "--posix"]},
    ]}]}]
    async def _kitty(*a):
        return json.dumps(ls)
    monkeypatch.setattr(rr, "_kitty", _kitty)
    w = await rr._window_for_session_name("muther-agent-3")
    assert w and w["window_id"] == 5


async def test_window_for_session_name_matches_cmdline_when_title_drifts(rr, monkeypatch):
    ls = [{"tabs": [{"windows": [
        {"id": 7, "title": "totally-drifted", "cmdline": ["bash", "-ic", "cd x && claude-chat -n muther"]},
    ]}]}]
    async def _kitty(*a):
        return json.dumps(ls)
    monkeypatch.setattr(rr, "_kitty", _kitty)
    w = await rr._window_for_session_name("muther")
    assert w and w["window_id"] == 7


async def test_window_for_session_name_no_match(rr, monkeypatch):
    ls = [{"tabs": [{"windows": [{"id": 1, "title": "a", "cmdline": ["bash"]}]}]}]
    async def _kitty(*a):
        return json.dumps(ls)
    monkeypatch.setattr(rr, "_kitty", _kitty)
    assert await rr._window_for_session_name("nonexistent") is None
