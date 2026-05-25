"""Tracker auto-roll-forward pattern set.

Canonical source for the regex patterns and sender allowlist that
tracker.md's auto-roll-forward spec references. Extracting this to
Python lets tests verify pattern behavior without spawning a tracker
subprocess.

tracker.md's `## Auto-roll-forward` section is the human-readable spec;
this module is the machine-readable single source of truth. Add a new
pattern HERE first, then document it in tracker.md.
"""
import re
from typing import NamedTuple, Optional


ROLL_FORWARD_PATTERNS: list[tuple[str, re.Pattern]] = [
    (
        "filed_linear",
        re.compile(
            r"^(?:(?:I|we) (?:just )?|just )?filed as ((?:JEEVY|KHM|JP)-\d+)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "sent_email",
        re.compile(
            r"^(?:I |just )?sent (?:the |my |)(?:(\w+) )?email\b",
            re.IGNORECASE,
        ),
    ),
    (
        "shipped_commit",
        re.compile(
            r"^(?:I (?:just )?|just )?shipped commit ([a-f0-9]{7,40})\b",
            re.IGNORECASE,
        ),
    ),
    (
        "pr_opened_merged",
        re.compile(
            r"^PR (?:opened|merged):?\s*#?(\d+)\b",
            re.IGNORECASE,
        ),
    ),
]

STRUCTURED_MARKER = re.compile(
    r"^🏷️ FILED:\s*(\S+)",
    re.UNICODE,
)

SENDER_ALLOWLIST = re.compile(r"^(?:[a-z]+-)?agent-\d+$")

NEGATION = re.compile(
    r"\b(?:not|didn'?t|haven'?t|won'?t|never|nope|hasn'?t)\b",
    re.IGNORECASE,
)


class RollForwardMatch(NamedTuple):
    """One auto-rollforward trigger match."""

    pattern_label: str
    artifact: str


def classify_message(body: str, sender_name: str) -> Optional[RollForwardMatch]:
    """Return a RollForwardMatch if body should trigger roll-forward, else None.

    Decision order:
    1. Sender must match SENDER_ALLOWLIST (roster agent only).
    2. First 200 chars of body checked against STRUCTURED_MARKER first
       (opt-in always wins).
    3. Otherwise, first sentence checked for NEGATION — if negated, skip.
    4. Else: try each ROLL_FORWARD_PATTERNS regex in order; return first match.
    """
    if not SENDER_ALLOWLIST.match(sender_name or ""):
        return None
    head = (body or "")[:200]
    marker_match = STRUCTURED_MARKER.search(head)
    if marker_match:
        return RollForwardMatch("structured_marker", marker_match.group(1))
    first_sentence = head.split(".", 1)[0]
    if NEGATION.search(first_sentence):
        return None
    for label, pattern in ROLL_FORWARD_PATTERNS:
        m = pattern.search(head)
        if m:
            artifact = m.group(1) if m.groups() else m.group(0)
            return RollForwardMatch(label, artifact)
    return None
