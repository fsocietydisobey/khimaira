"""Tracker auto-roll-forward + auto-distill pattern set.

Canonical source for the regex patterns and filters that tracker.md
auto-roll-forward and auto-distill specs reference. Extracting this to
Python lets tests verify pattern behavior without spawning a tracker
subprocess.

tracker.md's `## Auto-roll-forward` and `## Auto-distill` sections are the
human-readable specs; this module is the machine-readable single source of
truth. Add new patterns HERE first, then document them in tracker.md.
"""

from __future__ import annotations

import re
from typing import NamedTuple, Optional


# ---------------------------------------------------------------------------
# Auto-roll-forward constants (unchanged)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Auto-distill filter constants
# ---------------------------------------------------------------------------

# Roles whose decisions are always distilled (design/judgment-heavy).
DISTILL_ROLES: frozenset[str] = frozenset(
    {"architect", "analyst", "critic", "master"}
)

# Keywords indicating a high-signal engineering decision.
DISTILL_KEYWORDS: re.Pattern = re.compile(
    r"\b(class|pattern|architecture|invariant|rule|fix|bug|regression|"
    r"refactor|security|breach|gate|enforcement|design)\b",
    re.IGNORECASE,
)

# Minimum `why` field length to qualify as high-signal.
DISTILL_MIN_WHY_LEN: int = 80


def should_distill_decision(
    decision: dict,
    role: str | None,
    *,
    all_decisions: list[dict] | None = None,
) -> bool:
    """Return True if this decision should be distilled into mnemosyne.

    Criteria (ANY → distill):
    1. Deciding session holds a high-signal role (architect/analyst/critic/master).
    2. `why` field is long (>80 chars) — well-documented decisions are load-bearing.
    3. Decision text or `why` contains engineering-significance keywords.
    4. Multi-agent convergence: another session logged a decision with
       overlapping keywords within the same session batch (all_decisions).
    """
    text = (decision.get("text") or "").strip()
    why = (decision.get("why") or "").strip()

    # Criterion 1: high-signal role
    if role and role.split("-")[0] in DISTILL_ROLES:
        return True
    # Also match prefixed roles like "jp-architect"
    if role:
        for r in DISTILL_ROLES:
            if role.endswith(r):
                return True

    # Criterion 2: long why field
    if len(why) > DISTILL_MIN_WHY_LEN:
        return True

    # Criterion 3: keywords in text or why
    combined = f"{text} {why}"
    if DISTILL_KEYWORDS.search(combined):
        return True

    # Criterion 4: multi-agent convergence (another session has overlapping keywords)
    if all_decisions:
        my_sid = decision.get("session_id", "")
        my_keywords = set(
            m.group(0).lower()
            for m in DISTILL_KEYWORDS.finditer(combined)
        )
        if my_keywords:
            for other in all_decisions:
                if other.get("session_id", "") == my_sid:
                    continue
                other_text = (other.get("text") or "") + " " + (other.get("why") or "")
                other_keywords = set(
                    m.group(0).lower()
                    for m in DISTILL_KEYWORDS.finditer(other_text)
                )
                if my_keywords & other_keywords:
                    return True

    return False


def filter_decisions_for_distill(
    decisions: list[dict],
    role_by_session: dict[str, str | None] | None = None,
    *,
    watermark_by_session: dict[str, str] | None = None,
) -> list[dict]:
    """Filter a list of session decisions to those that should be distilled.

    Args:
        decisions: flat list of decision records (each has `session_id`, `text`,
            `why`, `ts` fields as written by session_log_decision).
        role_by_session: optional {session_id → role-string}; used for criterion 1.
        watermark_by_session: optional {session_id → last_distilled_ts}; decisions
            at-or-before the watermark are deduped out.

    Returns the subset of decisions that pass the filter, newest-first within
    each session, deduplicated against the watermark.
    """
    role_by_session = role_by_session or {}
    watermark_by_session = watermark_by_session or {}

    # Deduplicate against watermarks
    candidates = []
    for d in decisions:
        sid = d.get("session_id", "")
        wm_ts = watermark_by_session.get(sid)
        if wm_ts and d.get("ts", "") <= wm_ts:
            continue
        candidates.append(d)

    # Apply per-decision filter using all candidates for convergence check
    return [
        d
        for d in candidates
        if should_distill_decision(
            d,
            role_by_session.get(d.get("session_id", "")),
            all_decisions=candidates,
        )
    ]


def format_decisions_for_distill(decisions: list[dict]) -> str:
    """Format a list of filtered decisions as a distill transcript.

    Produces a compact markdown-ish text suitable for POST /distill.
    One entry per decision: session_id / role / text / why.
    """
    lines = []
    for d in decisions:
        sid = (d.get("session_id") or "")[:8]
        role = d.get("role") or "?"
        text = (d.get("text") or "").strip()
        why = (d.get("why") or "").strip()
        why_part = f" | why: {why}" if why else ""
        lines.append(f"[{sid}/{role}] {text}{why_part}")
    return "\n".join(lines)
