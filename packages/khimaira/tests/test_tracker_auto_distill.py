"""Tests for tracker auto-distill filter logic (tracker_patterns.py).

Unit coverage per filter branch; integration test for the end-to-end flow
(filter → format → mnemosyne call shape). No live mnemosyne needed.
"""

from __future__ import annotations

import pytest

from khimaira.tracker_patterns import (
    DISTILL_ROLES,
    filter_decisions_for_distill,
    format_decisions_for_distill,
    should_distill_decision,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _dec(
    text: str = "made a decision",
    why: str = "",
    session_id: str = "sid-1",
    ts: str = "2026-06-01T10:00:00",
) -> dict:
    return {"text": text, "why": why, "session_id": session_id, "ts": ts}


# ---------------------------------------------------------------------------
# Criterion 1 — high-signal role
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("role", list(DISTILL_ROLES))
def test_distill_roles_always_qualify(role):
    """Criterion 1: decisions from architect/analyst/critic/master always distill."""
    d = _dec("ordinary decision", "short why")
    assert should_distill_decision(d, role) is True


def test_prefixed_role_also_qualifies():
    """Prefixed role (e.g. jp-architect) matches via suffix check."""
    d = _dec("ordinary decision", "short why")
    assert should_distill_decision(d, "jp-architect") is True


def test_agent_role_does_not_auto_qualify():
    """agent role does NOT auto-qualify — must pass criteria 2/3/4."""
    d = _dec("trivial note", "short")
    assert should_distill_decision(d, "agent") is False


# ---------------------------------------------------------------------------
# Criterion 2 — long `why`
# ---------------------------------------------------------------------------


def test_long_why_qualifies():
    """Criterion 2: why > 80 chars qualifies regardless of role."""
    long_why = "a" * 81
    d = _dec("something happened", long_why)
    assert should_distill_decision(d, "agent") is True


def test_short_why_does_not_qualify_alone():
    """why ≤ 80 chars with no keywords → no distill."""
    d = _dec("something happened", "brief note")
    assert should_distill_decision(d, "agent") is False


# ---------------------------------------------------------------------------
# Criterion 3 — keywords
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "keyword",
    ["class", "pattern", "architecture", "invariant", "rule", "fix", "bug",
     "regression", "refactor", "security", "breach", "gate", "enforcement",
     "design"],
)
def test_keyword_in_text_qualifies(keyword):
    """Criterion 3: each keyword in text qualifies."""
    d = _dec(f"we closed the {keyword} for this pattern", "short why")
    assert should_distill_decision(d, "agent") is True


def test_keyword_in_why_qualifies():
    """Criterion 3: keyword in `why` also qualifies."""
    d = _dec("changed something", "this addresses the bug in the guard path")
    assert should_distill_decision(d, "agent") is True


def test_no_keyword_no_role_no_qualify():
    """No keyword, no special role → no distill."""
    d = _dec("sent the PR", "routine update")
    assert should_distill_decision(d, "agent") is False


# ---------------------------------------------------------------------------
# Criterion 4 — multi-agent convergence
# ---------------------------------------------------------------------------


def test_convergence_on_keyword_qualifies():
    """Criterion 4: two sessions with overlapping keywords → both qualify."""
    d1 = _dec("we fixed the design", "short", session_id="s1")
    d2 = _dec("design looks good", "short", session_id="s2")
    all_decisions = [d1, d2]

    # Both share "design" keyword — convergence → both distill
    assert should_distill_decision(d1, "agent", all_decisions=all_decisions) is True
    assert should_distill_decision(d2, "agent", all_decisions=all_decisions) is True


def test_no_convergence_no_keywords_no_qualify():
    """No keyword overlap between sessions → no convergence."""
    d1 = _dec("sent the email", "short", session_id="s1")
    d2 = _dec("updated the docs", "short", session_id="s2")
    assert should_distill_decision(d1, "agent", all_decisions=[d1, d2]) is False


def test_convergence_same_session_does_not_count():
    """Same-session decisions do not count as convergence."""
    # Use text without any DISTILL_KEYWORDS so criteria 1/2/3 don't fire
    d1 = _dec("the issue is resolved", "short note", session_id="s1")
    d2 = _dec("the issue was found earlier", "short note", session_id="s1")  # same sid
    # No other session → no convergence (and no keywords like "fix"/"bug"/etc.)
    assert should_distill_decision(d1, "agent", all_decisions=[d1, d2]) is False


# ---------------------------------------------------------------------------
# filter_decisions_for_distill
# ---------------------------------------------------------------------------


def test_filter_respects_watermark():
    """Decisions at-or-before the watermark ts are excluded (dedup)."""
    decisions = [
        _dec("old decision", "old", ts="2026-06-01T09:00:00", session_id="s1"),
        _dec("new decision", "fix pattern", ts="2026-06-01T11:00:00", session_id="s1"),
    ]
    watermark = {"s1": "2026-06-01T10:00:00"}
    result = filter_decisions_for_distill(decisions, watermark_by_session=watermark)
    # Only "new decision" is after the watermark
    assert len(result) == 1
    assert result[0]["text"] == "new decision"


def test_filter_applies_role_map():
    """Role map is used in criterion-1 check."""
    decisions = [
        _dec("ordinary note", "short", session_id="s-arch"),
    ]
    role_map = {"s-arch": "architect"}
    result = filter_decisions_for_distill(decisions, role_by_session=role_map)
    assert len(result) == 1


def test_filter_excludes_non_qualifying():
    """Decisions that fail all criteria are excluded."""
    decisions = [
        _dec("routine update", "no keywords short", session_id="s1"),
    ]
    result = filter_decisions_for_distill(decisions)
    assert len(result) == 0


# ---------------------------------------------------------------------------
# format_decisions_for_distill
# ---------------------------------------------------------------------------


def test_format_produces_readable_lines():
    """format_decisions_for_distill produces one line per decision."""
    decisions = [
        {"session_id": "abcd1234", "role": "analyst", "text": "closed the class",
         "why": "invariant now uniform"},
        {"session_id": "efgh5678", "role": "agent", "text": "shipped commit abc1234",
         "why": ""},
    ]
    output = format_decisions_for_distill(decisions)
    lines = output.strip().splitlines()
    assert len(lines) == 2
    assert "abcd1234/analyst" in lines[0]
    assert "closed the class" in lines[0]
    assert "invariant now uniform" in lines[0]
    assert "efgh5678/agent" in lines[1]
    # No why → no "| why:" suffix
    assert "| why:" not in lines[1]


# ---------------------------------------------------------------------------
# Integration: filter → format → distill call shape
# ---------------------------------------------------------------------------


def test_integration_filter_format_distill_payload(monkeypatch):
    """End-to-end: qualifying decisions are filtered, formatted, and sent to distill.

    Verifies the distill() call receives the expected domain + slug shape.
    Uses a monkeypatched distill() — no live mnemosyne needed.
    """
    calls = []

    def fake_distill(domain, transcript, session_slug, *, timeout=30.0):
        calls.append({"domain": domain, "transcript": transcript, "slug": session_slug})
        return {"pairs_extracted": 1}

    import khimaira.hooks.mnemosyne_client as mc
    monkeypatch.setattr(mc, "distill", fake_distill)

    decisions = [
        {"session_id": "s1", "role": "architect", "text": "closed the bug class",
         "why": "invariant now uniform across all paths", "ts": "2026-06-01T12:00:00"},
    ]
    filtered = filter_decisions_for_distill(
        decisions, role_by_session={"s1": "architect"}
    )
    assert len(filtered) == 1

    transcript = format_decisions_for_distill(filtered)
    result = mc.distill(
        domain="khimaira",
        transcript=transcript,
        session_slug="tracker-2026-06-01",
    )

    assert len(calls) == 1
    assert calls[0]["domain"] == "khimaira"
    assert "bug class" in calls[0]["transcript"]
    assert result is not None
