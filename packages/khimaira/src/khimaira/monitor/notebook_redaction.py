"""Grimoire — deterministic secret detection for sensitive notes.

DETERMINISTIC, never an LLM: the whole point of a sensitive note is that the
model never sees the secret, so LLM-based detection would be self-defeating
— and a security boundary must not be probabilistic. Regex + Shannon-entropy
heuristics only, per the ai-engineering rule (perception/decision boundary:
an LLM is never the right tool for a gate that must be structurally sound).

Fail-safe DIRECTION (load-bearing): on uncertainty, OVER-redact. Masking a
non-secret is recoverable (the human still has the note's real `raw_text`);
under-masking leaks irreversibly into every LLM call the note's `llm_text`
feeds (structuring, embedding, chat, cross-note context). The default points
at the recoverable failure.

`redact_secrets` is the single entry point: called once at write time
(notes.add_note/add_study_guide/update_note, when `sensitive=True`), never
per-egress — every downstream consumer reads the already-redacted
`llm_text` via `notes.llm_view(record)` instead of re-deriving it.
"""

from __future__ import annotations

import math
import re
from collections import Counter

# ---------------------------------------------------------------------------
# Pattern catalog. Each pattern is tried independently; overlaps are
# resolved by priority (lower number wins) then by longer span, so a
# specific pattern (PEM block, assignment) always beats the generic
# high-entropy catch-all when both would match the same text.
# ---------------------------------------------------------------------------

_PEM_BLOCK_PRIORITY = 0
_ASSIGNMENT_PRIORITY = 1
_CONNECTION_STRING_PRIORITY = 2
_JWT_PRIORITY = 3
_PREFIXED_TOKEN_PRIORITY = 4
_HIGH_ENTROPY_PRIORITY = 5

_PEM_BLOCK_PATTERN = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL
)

_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)^([ \t]*(?:export\s+)?[A-Za-z0-9_]*"
    r"(?:API_?KEY|SECRET|TOKEN|PASSWORD|PASSWD|PRIVATE_KEY|ACCESS_KEY|CLIENT_SECRET)"
    r"[A-Za-z0-9_]*\s*[:=]\s*)(\"?)([^\s\"'#]+)"
)

_CONNECTION_STRING_PATTERN = re.compile(r"\w+://[^:/\s]+:([^@/\s]+)@")

_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")

_PREFIXED_TOKEN_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("anthropic_key", re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}")),
    ("openai_key", re.compile(r"sk-(?!ant-)[A-Za-z0-9_-]{20,}")),
    ("aws_key_id", re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b")),
    ("google_api_key", re.compile(r"AIza[A-Za-z0-9_-]{35}")),
    ("github_token", re.compile(r"gh[posru]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}")),
    ("gitlab_token", re.compile(r"glpat-[A-Za-z0-9_-]{20,}")),
    ("bearer_token", re.compile(r"(?i)\bbearer\s+([A-Za-z0-9._~+/=-]{20,})")),
]

_HIGH_ENTROPY_TOKEN_PATTERN = re.compile(r"\b[A-Za-z0-9+/_=-]{20,}\b")
_MIN_HIGH_ENTROPY_LEN = 20
_ENTROPY_THRESHOLD = 3.5  # bits/char — a real random-looking secret sits well above this


def _shannon_entropy(s: str) -> float:
    if not s:
        return 0.0
    counts = Counter(s)
    length = len(s)
    return -sum((n / length) * math.log2(n / length) for n in counts.values())


def _looks_high_entropy(token: str) -> bool:
    return len(token) >= _MIN_HIGH_ENTROPY_LEN and _shannon_entropy(token) >= _ENTROPY_THRESHOLD


def _collect_matches(text: str) -> list[tuple[int, int, str]]:
    """Every detected secret span as (start, end, kind), overlaps resolved.

    Candidates are sorted by (priority, -length) so a higher-priority (more
    specific) pattern always wins an overlap against a lower-priority one,
    and a longer match wins a tie within the same priority — then accepted
    greedily, skipping anything that overlaps an already-accepted span.
    O(n^2) in match count, which is irrelevant at note-paste scale.
    """
    candidates: list[tuple[int, int, str, int]] = []

    for m in _PEM_BLOCK_PATTERN.finditer(text):
        candidates.append((m.start(), m.end(), "pem_private_key", _PEM_BLOCK_PRIORITY))

    for m in _ASSIGNMENT_PATTERN.finditer(text):
        candidates.append((m.start(3), m.end(3), "assignment_secret", _ASSIGNMENT_PRIORITY))

    for m in _CONNECTION_STRING_PATTERN.finditer(text):
        candidates.append(
            (m.start(1), m.end(1), "connection_string_password", _CONNECTION_STRING_PRIORITY)
        )

    for m in _JWT_PATTERN.finditer(text):
        candidates.append((m.start(), m.end(), "jwt", _JWT_PRIORITY))

    for kind, pattern in _PREFIXED_TOKEN_PATTERNS:
        for m in pattern.finditer(text):
            group = 1 if pattern.groups else 0
            candidates.append((m.start(group), m.end(group), kind, _PREFIXED_TOKEN_PRIORITY))

    for m in _HIGH_ENTROPY_TOKEN_PATTERN.finditer(text):
        token = m.group(0)
        if _looks_high_entropy(token):
            candidates.append((m.start(), m.end(), "high_entropy_token", _HIGH_ENTROPY_PRIORITY))

    candidates.sort(key=lambda c: (c[3], -(c[1] - c[0]), c[0]))
    accepted: list[tuple[int, int, str]] = []
    for start, end, kind, _priority in candidates:
        if any(start < a_end and end > a_start for a_start, a_end, _ in accepted):
            continue
        accepted.append((start, end, kind))
    accepted.sort(key=lambda c: c[0])
    return accepted


def redact_secrets(raw_text: str) -> tuple[str, list[dict[str, str]]]:
    """Deterministically redact secret-shaped substrings from `raw_text`.

    Returns (llm_text, redactions):
      llm_text:    raw_text with each detected secret replaced by a stable
                   placeholder like "‹SECRET:openai_key#1›".
      redactions:  [{placeholder, kind}, ...] — what was masked, for the UI's
                   "what got hidden" panel. NEVER contains the masked value
                   itself — that's the entire point of this module.

    Returns (raw_text, []) unchanged when nothing is detected.
    """
    matches = _collect_matches(raw_text)
    if not matches:
        return raw_text, []

    redactions: list[dict[str, str]] = []
    counters: dict[str, int] = {}
    pieces: list[str] = []
    cursor = 0
    for start, end, kind in matches:
        pieces.append(raw_text[cursor:start])
        counters[kind] = counters.get(kind, 0) + 1
        placeholder = f"‹SECRET:{kind}#{counters[kind]}›"
        pieces.append(placeholder)
        redactions.append({"placeholder": placeholder, "kind": kind})
        cursor = end
    pieces.append(raw_text[cursor:])
    return "".join(pieces), redactions
