"""Shared mnemosyne HTTP client — stdlib only, fail-open.

Both session_end.py (distill) and session_start.py (query) import from here
so urllib call logic isn't duplicated. No third-party deps; must stay stdlib-only
so it can run in any subprocess that has the khimaira package importable.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

_MNEMOSYNE_BASE = "http://127.0.0.1:8766"


def distill(
    domain: str,
    transcript: str,
    session_slug: str,
    *,
    timeout: float = 2.0,
) -> dict | None:
    """POST /distill; return parsed response dict or None on any failure.

    Fail-open: URLError, OSError, TimeoutError → None (never raises).
    """
    payload = json.dumps(
        {"domain": domain, "transcript": transcript, "session_slug": session_slug},
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{_MNEMOSYNE_BASE}/distill",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None


def query(
    domain: str,
    *,
    limit: int = 20,
    timeout: float = 2.0,
) -> dict | None:
    """POST /query for domain; return response dict or None on any failure.

    The mnemosyne /query endpoint applies its own _TOP_K=20 cap server-side;
    the `limit` param is accepted for forward-compatibility but not forwarded.

    Returns dict with keys: domain, question, answer (formatted pairs block),
    training_pairs_available. Callers use result["answer"] for the pairs text
    and result["training_pairs_available"] for count display.

    Fail-open: URLError, OSError, TimeoutError → None (never raises).
    """
    _ = limit  # server-side cap enforced; kept for API symmetry
    payload = json.dumps(
        {"domain": domain, "question": f"recent context for {domain}"},
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{_MNEMOSYNE_BASE}/query",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = data.get("answer") or ""
        if not answer or "No accumulated memory" in answer:
            return None
        return data
    except (urllib.error.URLError, OSError, TimeoutError, ValueError):
        return None
