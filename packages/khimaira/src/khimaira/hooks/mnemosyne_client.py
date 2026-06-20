"""Shared mnemosyne HTTP client — stdlib only, fail-open.

Both session_end.py (distill) and session_start.py (query) import from here
so urllib call logic isn't duplicated. No third-party deps; must stay stdlib-only
so it can run in any subprocess that has the khimaira package importable.
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

_MNEMOSYNE_BASE = "http://127.0.0.1:8766"
# The trained codebase-oracle MODELS (vLLM, OpenAI-compatible) — distinct from the
# knowledge STORE on 8766. One vLLM instance per codebase; each serves a different
# served-model-name on its own port. Defaults assume spark ports forwarded locally.
_ORACLE_BASE = os.environ.get("MNEMOSYNE_ORACLE_URL", "http://127.0.0.1:18000")
_JEEVY_BASE = os.environ.get("MNEMOSYNE_JEEVY_URL", "http://127.0.0.1:18001")

# project → (base_url, served_model_name). ask_oracle(project=...) routes here.
_ORACLES: dict[str, tuple[str, str]] = {
    "khimaira": (_ORACLE_BASE, "khimaira"),
    "jeevy": (_JEEVY_BASE, "jeevy"),
}
_DEFAULT_PROJECT = "khimaira"

_log = logging.getLogger(__name__)


def distill(
    domain: str,
    transcript: str,
    session_slug: str,
    *,
    timeout: float = 240.0,
) -> dict | None:
    """POST /distill; return parsed response dict or None on any failure.

    Fail-open: URLError, OSError, TimeoutError → None (never raises).

    Timeout is 240s: distillation runs an LLM pass over the full transcript
    server-side. Post-truncation-fix the window is ~600k chars (~150k tok), so a
    Haiku distill runs ~20-90s — the old 30s cap silently None'd EVERY large
    transcript (backfill, the Stop hook, AND the weekly active-distill all send
    600k windows). Downstream is uncapped below this: the anthropic SDK default is
    600s and uvicorn sets no request-duration timeout, so 240s is the binding cap
    with safe headroom. The failure path logs a warning so a future divergence is
    audible instead of silently swallowed.
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
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        _log.warning(
            "mnemosyne distill failed (domain=%r, slug=%r, timeout=%.1fs): %s",
            domain,
            session_slug,
            timeout,
            exc,
        )
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
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        _log.warning("mnemosyne query failed (domain=%r): %s", domain, exc)
        return None


def ask_oracle(
    question: str,
    *,
    project: str = _DEFAULT_PROJECT,
    max_tokens: int = 256,
    temperature: float = 0.3,
    timeout: float = 60.0,
) -> dict | None:
    """Ask a local codebase-oracle MODEL (vLLM) a question.

    Distinct from query(): query() reads the distilled-knowledge STORE (8766);
    this generates an answer from a TRAINED MODEL via its OpenAI-compatible
    /v1/chat/completions endpoint. One vLLM per codebase — `project` selects
    which (see _ORACLES). khimaira -> :18000, jeevy -> :18001 by default;
    override the base URLs with MNEMOSYNE_ORACLE_URL / MNEMOSYNE_JEEVY_URL.

    Returns {"answer", "model", "usage"} or None on any failure.
    Fail-open: URLError/OSError/TimeoutError/ValueError → None (never raises).
    An unknown `project` falls back to the default oracle (logged).

    The answer is a FAST, FALLIBLE reference — verify facts against live source.
    """
    base, model = _ORACLES.get(project, (None, None))
    if base is None:
        _log.warning(
            "mnemosyne: unknown project %r (known: %s) — using %r",
            project,
            ", ".join(_ORACLES),
            _DEFAULT_PROJECT,
        )
        base, model = _ORACLES[_DEFAULT_PROJECT]

    payload = json.dumps(
        {
            "model": model,
            "messages": [{"role": "user", "content": question}],
            "max_tokens": max_tokens,
            "temperature": temperature,
        },
        separators=(",", ":"),
    ).encode("utf-8")
    try:
        req = urllib.request.Request(
            f"{base}/v1/chat/completions",
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        answer = (
            ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
        ).strip()
        if not answer:
            return None
        return {
            "answer": answer,
            "model": data.get("model", model),
            "usage": data.get("usage", {}),
        }
    except (urllib.error.URLError, OSError, TimeoutError, ValueError) as exc:
        _log.warning("mnemosyne oracle ask failed (project=%r): %s", project, exc)
        return None
