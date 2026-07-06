"""Read-only remote surface onto the khimaira notebook.

Exposes exactly three daemon capabilities — search, get, ask — to callers
reachable over the Tailscale tailnet, so teammates who don't run the full
khimaira daemon locally (e.g. remote engineers) can query Joseph's
accumulated notebook context from their own Claude Code sessions.

SECURITY MODEL — two layers, both required:

  1. The real daemon (`khimaira.monitor.server`) asserts loopback-only
     binding as its ENTIRE auth layer (`_assert_loopback` — no bearer, no
     API key, nothing else). This proxy is what makes the notebook
     reachable off-box AT ALL, so it cannot inherit that same "loopback is
     enough" assumption — it terminates on a real interface (Tailscale, or
     whatever `--host` is passed), so it enforces its OWN auth: a bearer
     token, required at startup (refuses to boot without one, same
     fail-loud posture as `_assert_loopback`).

  2. Notebook records are NOT public even to an authenticated caller —
     records marked `sensitive` carry a redacted `llm_text` twin
     specifically because `raw_text` may contain real credentials
     (packages/khimaira/src/khimaira/monitor/notes.py's `llm_view()` is
     the codebase's own established choke point for this: "every LLM
     egress site reads llm_view(record), never raw_text directly"). A
     remote, non-Joseph caller is exactly that kind of egress site, so
     `get_note` here re-applies the same substitution before returning —
     the daemon's own `GET /notes/{id}` does NOT do this (it's Joseph's
     own loopback-only UI, trusted to show him his own real secrets back).

  Repo scoping (`KHIMAIRA_NOTEBOOK_RO_REPO`) is a third, optional layer:
  when set, every search/ask is forced to that repo (client-supplied
  `repo` is ignored, not merely defaulted) and `get_note` 404s on any note
  outside that repo + the cross-cutting "General" bucket. Unset = no repo
  restriction (documented, not the default anyone should ship with).
"""

from __future__ import annotations

import logging
import os
import secrets
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import BaseModel

from khimaira.monitor.notes import GENERAL_REPO as _GENERAL_REPO

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Config (all env-tunable, never hardcoded)
# ---------------------------------------------------------------------------

_DAEMON_BASE = os.environ.get("KHIMAIRA_MONITOR_URL", "http://127.0.0.1:8740").rstrip("/")
_AUTH_TOKEN = os.environ.get("KHIMAIRA_NOTEBOOK_RO_TOKEN", "").strip()
_REPO_SCOPE = os.environ.get("KHIMAIRA_NOTEBOOK_RO_REPO", "").strip() or None

_DEFAULT_TOP_K = 10
_ASK_TIMEOUT_S = 180.0


def _require_token() -> str:
    """Fail loud at import/boot time — never serve unauthenticated.

    Mirrors `khimaira.monitor.server._assert_loopback`'s posture: this is
    the ONE thing standing between "notebook reachable on the tailnet" and
    "notebook reachable on the tailnet with no auth at all". A missing
    token is a config bug, not a degrade-gracefully case.
    """
    if not _AUTH_TOKEN:
        raise SystemExit(
            "khimaira notebook-readonly: refusing to start — "
            "KHIMAIRA_NOTEBOOK_RO_TOKEN is unset. This service terminates "
            "on a non-loopback interface; an unset token means NO auth at all."
        )
    return _AUTH_TOKEN


_bearer_scheme = HTTPBearer(auto_error=False)


def _check_auth(creds: HTTPAuthorizationCredentials | None = Depends(_bearer_scheme)) -> None:
    token = _require_token()
    if creds is None or not secrets.compare_digest(creds.credentials, token):
        raise HTTPException(401, "invalid or missing bearer token")


# ---------------------------------------------------------------------------
# Sensitive-note redaction — the same boundary notes.llm_view() enforces
# in-process, re-applied here for an out-of-process, non-Joseph caller.
# ---------------------------------------------------------------------------


def _redact_for_remote(note: dict[str, Any]) -> dict[str, Any]:
    """Strip real secrets out of a note record before it leaves this box.

    Non-sensitive notes pass through unchanged. Sensitive notes: `raw_text`
    is replaced by the already-computed `llm_text` redacted twin (falls
    back to a placeholder if somehow absent — never fall through to the
    real text), and `history` (prior pipeline snapshots) is dropped
    entirely rather than audited entry-by-entry, since a remote read-only
    caller has no use for note history and it's cheaper to omit than to
    prove safe.
    """
    if not note.get("sensitive"):
        return note
    out = dict(note)
    out["raw_text"] = note.get("llm_text") or "[redacted — sensitive note]"
    out.pop("history", None)
    return out


def _check_repo_scope(note: dict[str, Any]) -> None:
    if _REPO_SCOPE is None:
        return
    repo = note.get("repo")
    if repo not in (_REPO_SCOPE, _GENERAL_REPO):
        raise HTTPException(404, f"No note with id={note.get('id')!r} in scope.")


def _scoped_repo_param(client_repo: str | None) -> str | None:
    """Repo scoping is enforced server-side, not merely defaulted — a
    client-supplied `repo` is IGNORED (not honored) when scoping is on."""
    return _REPO_SCOPE if _REPO_SCOPE is not None else client_repo


# ---------------------------------------------------------------------------
# Daemon HTTP client
# ---------------------------------------------------------------------------

_client: httpx.AsyncClient | None = None


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    global _client
    _require_token()
    _client = httpx.AsyncClient(base_url=_DAEMON_BASE, timeout=httpx.Timeout(_ASK_TIMEOUT_S))
    logger.info(
        "notebook-readonly: started — daemon=%s repo_scope=%s",
        _DAEMON_BASE,
        _REPO_SCOPE or "(none)",
    )
    try:
        yield
    finally:
        if _client is not None:
            await _client.aclose()


app = FastAPI(title="khimaira-notebook-readonly", lifespan=_lifespan)


class AskReq(BaseModel):
    question: str
    repo: str | None = None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "repo_scope": _REPO_SCOPE}


@app.get("/notes/search", dependencies=[Depends(_check_auth)])
async def search(q: str, top_k: int = Query(_DEFAULT_TOP_K, le=50), repo: str | None = None) -> dict:
    assert _client is not None
    resp = await _client.get(
        "/api/notes/search",
        params={"q": q, "top_k": top_k, "repo": _scoped_repo_param(repo)},
    )
    resp.raise_for_status()
    return resp.json()


@app.get("/notes/{note_id}", dependencies=[Depends(_check_auth)])
async def get_note(note_id: str) -> dict:
    assert _client is not None
    resp = await _client.get(f"/api/notes/{note_id}")
    if resp.status_code == 404:
        raise HTTPException(404, f"No note with id={note_id!r}.")
    resp.raise_for_status()
    note = resp.json()
    _check_repo_scope(note)
    return _redact_for_remote(note)


@app.post("/notes/ask", dependencies=[Depends(_check_auth)])
async def ask(req: AskReq) -> dict:
    assert _client is not None
    resp = await _client.post(
        "/api/notes/ask",
        json={"question": req.question, "repo": _scoped_repo_param(req.repo)},
        timeout=_ASK_TIMEOUT_S,
    )
    resp.raise_for_status()
    return resp.json()


# ---------------------------------------------------------------------------
# Serve entry point
# ---------------------------------------------------------------------------


def serve(*, host: str = "0.0.0.0", port: int = 8742) -> None:
    """Start the read-only notebook proxy in the foreground (blocks until shutdown)."""
    import uvicorn

    _require_token()
    print(f"khimaira notebook-readonly: starting on http://{host}:{port}")
    print(f"  daemon: {_DAEMON_BASE}")
    print(f"  repo scope: {_REPO_SCOPE or '(none — all repos visible)'}")
    print("  auth: bearer token required on every request except /health")
    uvicorn.run(app, host=host, port=port, log_level="warning")
