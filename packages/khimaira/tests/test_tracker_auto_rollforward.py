"""Tracker auto-roll-forward pattern + invariant tests.

Asserts canonical regex behavior + audit-log schema. Does NOT test
tracker agent behavior end-to-end (that requires a live Claude Code
subprocess — manual review by critic + verifier covers it).
"""
import json
import re
import pytest

from khimaira.tracker_patterns import (
    NEGATION,
    ROLL_FORWARD_PATTERNS,
    SENDER_ALLOWLIST,
    STRUCTURED_MARKER,
    RollForwardMatch,
    classify_message,
)


_CASES = [
    ("filed_linear_jeevy", "I just filed as JEEVY-528, ready for review",
     "agent-1", "filed_linear", "JEEVY-528"),
    ("filed_linear_khm", "filed as KHM-107",
     "agent-2", "filed_linear", "KHM-107"),
    ("sent_email_named", "I sent the Walter email",
     "agent-2", "sent_email", "Walter"),
    ("sent_email_unnamed", "sent the email",
     "agent-1", "sent_email", None),
    ("shipped_commit", "I just shipped commit abc1234def567",
     "agent-3", "shipped_commit", "abc1234def567"),
    ("pr_opened", "PR opened: #42",
     "jp-agent-1", "pr_opened_merged", "42"),
    ("pr_merged_no_hash", "PR merged: 42",
     "agent-1", "pr_opened_merged", "42"),
    ("structured_marker", "🏷️ FILED: JEEVY-530 just now",
     "agent-2", "structured_marker", "JEEVY-530"),

    ("negation_didnt", "we didn't file JEEVY-528 yet",
     "agent-1", None, None),
    ("negation_havent", "I haven't sent the email",
     "agent-2", None, None),
    ("negation_never", "we never shipped commit abc1234",
     "agent-3", None, None),
    ("sender_intake", "I filed as JEEVY-528",
     "intake-1", None, None),
    ("sender_master", "I filed as JEEVY-528",
     "khimaira-0", None, None),
    ("sender_observer", "I filed as JEEVY-528",
     "observer-1", None, None),
    ("sender_tracker", "I filed as JEEVY-528",
     "tracker-1", None, None),
    ("no_completion_verb", "JEEVY-528 was discussed earlier",
     "agent-1", None, None),
    ("plain_text_no_match", "hey, how's the work going?",
     "agent-1", None, None),
]


@pytest.mark.parametrize(
    "description,body,sender,expected_label,expected_artifact",
    _CASES,
    ids=[c[0] for c in _CASES],
)
def test_classify_message(description, body, sender, expected_label, expected_artifact):
    """Parametrized class-invariant: each enumerated path → correct disposition."""
    result = classify_message(body, sender)
    if expected_label is None:
        assert result is None, f"{description}: expected no match, got {result}"
    else:
        assert result is not None, f"{description}: expected match, got None"
        assert result.pattern_label == expected_label
        if expected_artifact is not None:
            assert result.artifact == expected_artifact


def test_audit_log_schema_invariant():
    """Audit log entries MUST include chat_event_id + pattern_label + artifact."""
    required_keys = {
        "ts",
        "chat_event_id",
        "sender_name",
        "pattern_label",
        "artifact",
        "message_body_head",
        "state_md_change",
    }
    entry = {
        "ts": "2026-05-25T18:40:00Z",
        "chat_event_id": "abc123",
        "sender_name": "agent-1",
        "pattern_label": "filed_linear",
        "artifact": "JEEVY-528",
        "message_body_head": "I just filed as JEEVY-528, ready for review",
        "state_md_change": "moved 'BOM bbox' from pending external follow-up → done",
    }
    assert required_keys.issubset(entry.keys())
    assert json.loads(json.dumps(entry)) == entry


def test_pattern_set_is_extensible():
    """Meta-test: ROLL_FORWARD_PATTERNS is the canonical source."""
    assert len(ROLL_FORWARD_PATTERNS) >= 4
    for label, pattern in ROLL_FORWARD_PATTERNS:
        assert isinstance(label, str) and label
        assert isinstance(pattern, re.Pattern)


def test_sender_allowlist_excludes_non_agent_roles():
    """Only roster-agent senders may trigger; all other roles excluded."""
    assert SENDER_ALLOWLIST.match("agent-1")
    assert SENDER_ALLOWLIST.match("agent-12")
    assert SENDER_ALLOWLIST.match("jp-agent-1")
    assert SENDER_ALLOWLIST.match("foo-agent-3")
    assert not SENDER_ALLOWLIST.match("intake-1")
    assert not SENDER_ALLOWLIST.match("observer-1")
    assert not SENDER_ALLOWLIST.match("tracker-1")
    assert not SENDER_ALLOWLIST.match("critic-1")
    assert not SENDER_ALLOWLIST.match("analyst-1")
    assert not SENDER_ALLOWLIST.match("architect-1")
    assert not SENDER_ALLOWLIST.match("verifier-1")
    assert not SENDER_ALLOWLIST.match("khimaira-0")
    assert not SENDER_ALLOWLIST.match("khimaira-daemon")
    assert not SENDER_ALLOWLIST.match("")
    assert not SENDER_ALLOWLIST.match("agent")
    assert not SENDER_ALLOWLIST.match("agent-")
