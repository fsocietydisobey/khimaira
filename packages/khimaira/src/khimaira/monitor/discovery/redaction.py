"""Server-side redaction of PII fields in deserialized state.

A column-name allowlist (default: `*email*`, `*password*`, `*token*`,
`*secret*`, `*ssn*`, `*pii*`) is matched against dict keys, recursively.
Matching values are replaced with the literal string `<redacted>` before
the payload is JSON-serialized to the client. Defense-in-depth alongside
the `127.0.0.1` binding — never trust the network layer alone with PII.
"""

from __future__ import annotations

import re
from typing import Any

DEFAULT_PATTERNS: tuple[str, ...] = (
    "email",
    "password",
    "token",
    "secret",
    "ssn",
    "pii",
)
REDACTED_MARKER = "<redacted>"


def _build_regex(patterns: tuple[str, ...]) -> re.Pattern[str]:
    """Return a compiled case-insensitive pattern matching any substring."""
    escaped = [re.escape(p) for p in patterns]
    return re.compile("|".join(escaped), re.IGNORECASE)


_DEFAULT_RE = _build_regex(DEFAULT_PATTERNS)


def redact(value: Any, patterns: tuple[str, ...] | None = None) -> Any:
    """Recursively redact dict values whose key matches any pattern.

    - dict: each value is replaced with `<redacted>` when its key matches;
      non-matching values are recursed into.
    - list / tuple: each element is recursed.
    - other types: returned unchanged.
    """
    pattern = _DEFAULT_RE if patterns is None else _build_regex(patterns)
    return _walk(value, pattern)


def _walk(value: Any, pattern: re.Pattern[str]) -> Any:
    if isinstance(value, dict):
        out: dict[Any, Any] = {}
        for key, item in value.items():
            if isinstance(key, str) and pattern.search(key):
                out[key] = REDACTED_MARKER
            else:
                out[key] = _walk(item, pattern)
        return out
    if isinstance(value, list):
        return [_walk(item, pattern) for item in value]
    if isinstance(value, tuple):
        return tuple(_walk(item, pattern) for item in value)
    return value
