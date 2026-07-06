"""Read-only remote surface onto the khimaira notebook.

Exposes exactly three daemon capabilities — search, get, ask — to callers
reachable over the Tailscale tailnet, so teammates who don't run the full
khimaira daemon locally (e.g. remote engineers) can query Joseph's
accumulated notebook context from their own Claude Code sessions.

PLUS one narrow write path — `POST /notes/ask-joseph` — the "notify +
pull-to-check" async-question flow: an engineer's question gets appended
to one fixed, pre-created note (id from `KHIMAIRA_ENGINEER_QUESTIONS_NOTE_ID`,
never a caller-supplied note_id — this route can't touch any other note),
Joseph gets a desktop notification, and he answers later via the existing
per-record grimoire chat (which auto-applies the edit). Not a general
write surface: still read-only for every OTHER note.

SECURITY MODEL — two layers, both required:

  1. The real daemon (`khimaira.monitor.server`) asserts loopback-only
     binding as its ENTIRE auth layer (`_assert_loopback` — no bearer, no
     API key, nothing else). THIS proxy also binds loopback-only by
     default now (`serve(host="127.0.0.1", ...)`) — it is NOT the thing
     terminating on the tailnet anymore. That role moved to the co-located
     `khimaira.notebook_readonly.mcp_client` FastMCP HTTP server, which is
     the only process that talks to this proxy (over 127.0.0.1) and the
     only one bearing a token meant to cross the tailnet
     (`KHIMAIRA_NOTEBOOK_MCP_TOKEN`, deliberately distinct from this
     proxy's own `KHIMAIRA_NOTEBOOK_RO_TOKEN`). This proxy still enforces
     its own bearer auth regardless — defense in depth, not "loopback is
     enough" — required at startup (refuses to boot without one, same
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
from datetime import UTC, datetime
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
_ASK_JOSEPH_NOTE_ID = os.environ.get("KHIMAIRA_ENGINEER_QUESTIONS_NOTE_ID", "").strip()

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


class AskJosephReq(BaseModel):
    asker: str
    question: str


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


@app.post("/notes/ask-joseph", dependencies=[Depends(_check_auth)])
async def ask_joseph(req: AskJosephReq) -> dict:
    """The "notify + pull-to-check" async-question flow (Joseph's pick over
    full bidirectional real-time chat): append a structured question block
    to one FIXED, pre-created note, fire a desktop notification, return the
    note_id so the caller knows where to check back. Never touches any
    other note — `note_id` is never caller-supplied.

    Race note: this is read-then-write (GET the note, append, PATCH the
    whole raw_text back) with no optimistic-concurrency guard — the
    daemon's PATCH /api/notes/{id} takes no `expected_updated_at` or
    similar field. At this note's actual traffic (2 engineers, occasional
    questions) a lost-update race is unlikely and low-cost if it happens
    (a re-asked question); not worth solving until traffic changes.

    Timestamp is date+HH:MM UTC, not full microsecond-precision ISO —
    this heading becomes a `section_anchor` slug for the grimoire chat's
    auto-apply-edit (see `notebook_pipeline._slugify_heading`, which strips
    all punctuation), and an LLM scoping an edit to "the pending question
    from X" has to reproduce that slug byte-exact. A 20-digit microsecond
    timestamp is effectively unreproducible; date+HH:MM is short enough an
    LLM can realistically get it right, and still unique enough to
    disambiguate same-day questions in practice.
    """
    if not _ASK_JOSEPH_NOTE_ID:
        raise HTTPException(500, "KHIMAIRA_ENGINEER_QUESTIONS_NOTE_ID is unset on the server.")
    assert _client is not None

    resp = await _client.get(f"/api/notes/{_ASK_JOSEPH_NOTE_ID}")
    if resp.status_code == 404:
        raise HTTPException(404, f"KHIMAIRA_ENGINEER_QUESTIONS_NOTE_ID={_ASK_JOSEPH_NOTE_ID!r} not found.")
    resp.raise_for_status()
    note = resp.json()

    timestamp = datetime.now(UTC).strftime("%Y-%m-%d %H:%M UTC")
    block = f"\n\n## Question from {req.asker} — {timestamp} — STATUS: pending\n{req.question}\n"
    new_raw_text = note.get("raw_text", "") + block

    patch_resp = await _client.patch(f"/api/notes/{_ASK_JOSEPH_NOTE_ID}", json={"raw_text": new_raw_text})
    patch_resp.raise_for_status()

    from khimaira.monitor.desktop_notify import notify_engineer_question

    notify_engineer_question(req.asker, req.question)

    return {"posted": True, "note_id": _ASK_JOSEPH_NOTE_ID}


# ---------------------------------------------------------------------------
# Serve entry point
# ---------------------------------------------------------------------------


def serve(*, host: str = "127.0.0.1", port: int = 8742) -> None:
    """Start the read-only notebook proxy in the foreground (blocks until shutdown)."""
    import uvicorn

    _require_token()
    print(f"khimaira notebook-readonly: starting on http://{host}:{port}")
    print(f"  daemon: {_DAEMON_BASE}")
    print(f"  repo scope: {_REPO_SCOPE or '(none — all repos visible)'}")
    print("  auth: bearer token required on every request except /health")
    uvicorn.run(app, host=host, port=port, log_level="warning")
