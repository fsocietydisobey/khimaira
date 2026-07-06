"""Standalone MCP server for the khimaira-notebook-readonly proxy.

A SEPARATE FastMCP HTTP server — deliberately not folded into
`khimaira.server.mcp` (the ~119-tool `khimaira` server). Remote engineers
who only have read-only notebook access don't run khimaira (Joseph's
personal tool isn't installed on their machines at all) and shouldn't see
the other ~116 tools in their session's tool list; giving them a dedicated
`mcpServers` entry — a pure URL, no local process — keeps this
integration's surface exactly as small as the proxy it talks to, with
nothing to install.

Talks to `khimaira.notebook_readonly.server`'s real wire format
(`GET /notes/search`, `GET /notes/{note_id}`, `POST /notes/ask`, bearer
auth) — NOT to `khimaira.server.notebook_tools`'s `/api/notes/...` daemon
routes, which that proxy doesn't expose at all (see the wire-compat
verdict posted in chat-102d8b5fd82f: path prefix AND missing auth header
were both real breaks).

TWO independent trust boundaries, two independent tokens — deliberately
not shared, so a leak of one doesn't compromise the other:

  1. This MCP server ↔ remote engineer's Claude Code session. Gated by
     `KHIMAIRA_NOTEBOOK_MCP_TOKEN` via FastMCP's `StaticTokenVerifier`
     (fixed shared-secret bearer check, `secrets.compare_digest` under
     the hood — not OAuth/JWT, that's overkill for this). This is the
     interface now reachable from the tailnet.
  2. This MCP server ↔ the REST proxy (`khimaira.notebook_readonly.server`).
     Gated by `KHIMAIRA_NOTEBOOK_RO_TOKEN`, exactly as before. Both
     services are now CO-LOCATED on the same box — the REST proxy is
     loopback-only (see its own docstring), reachable only from this
     process, so `KHIMAIRA_NOTEBOOK_RO_URL` should point at
     `http://127.0.0.1:8742`, not a Tailscale address.

Config (env-tunable):
  KHIMAIRA_NOTEBOOK_MCP_TOKEN required — bearer token for THIS server
      (issue a different secret than KHIMAIRA_NOTEBOOK_RO_TOKEN).
  KHIMAIRA_NOTEBOOK_RO_URL   required — the REST proxy's base URL
      (co-located ⇒ typically http://127.0.0.1:8742).
  KHIMAIRA_NOTEBOOK_RO_TOKEN required — bearer token for the REST proxy.
"""

from __future__ import annotations

import json
import os
import urllib.parse
from typing import Any

import httpx
from fastmcp import FastMCP
from fastmcp.server.auth import StaticTokenVerifier

_BASE_URL = os.environ.get("KHIMAIRA_NOTEBOOK_RO_URL", "").rstrip("/")
_TOKEN = os.environ.get("KHIMAIRA_NOTEBOOK_RO_TOKEN", "").strip()
_MCP_TOKEN = os.environ.get("KHIMAIRA_NOTEBOOK_MCP_TOKEN", "").strip()

_ASK_TIMEOUT_S = 180.0


def _require_mcp_token() -> str:
    """Fail loud at boot — mirrors `notebook_readonly.server._require_token`.

    This server terminates on a non-loopback interface (the Tailscale
    tailnet) — an unset token means NO auth at all, which is not a
    degrade-gracefully case.
    """
    if not _MCP_TOKEN:
        raise SystemExit(
            "khimaira notebook-readonly mcp: refusing to start — "
            "KHIMAIRA_NOTEBOOK_MCP_TOKEN is unset. This service terminates "
            "on a non-loopback interface; an unset token means NO auth at all."
        )
    return _MCP_TOKEN


def _make_auth(token: str) -> StaticTokenVerifier | None:
    """Build the StaticTokenVerifier for a given token, or None (no auth
    gate — only ever correct for tests that don't exercise the HTTP auth
    layer; production always goes through `_require_mcp_token()` first).
    """
    if not token:
        return None
    return StaticTokenVerifier(tokens={token: {"client_id": "khimaira-notebook-readonly", "scopes": []}})


mcp = FastMCP("khimaira-notebook-readonly", auth=_make_auth(_MCP_TOKEN))

_client: httpx.AsyncClient | None = None


def _get_client() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(base_url=_BASE_URL, timeout=httpx.Timeout(_ASK_TIMEOUT_S))
    return _client


def _config_error() -> str | None:
    if not _BASE_URL:
        return (
            "❌ KHIMAIRA_NOTEBOOK_RO_URL is unset — set it to the proxy's base "
            "URL (e.g. http://100.64.123.46:8742) in your .mcp.json's env block."
        )
    if not _TOKEN:
        return (
            "❌ KHIMAIRA_NOTEBOOK_RO_TOKEN is unset — get the bearer token from "
            "whoever runs the proxy and set it in your .mcp.json's env block."
        )
    return None


async def _request(method: str, path: str, **kwargs: Any) -> dict[str, Any] | str:
    """Request → parsed JSON, or a friendly error string on failure.

    Mirrors the error-mapping convention `khimaira.server.monitor_tools`
    uses for the daemon client (HTTPError → "HTTP <code>: <detail>",
    connection error → "unreachable"), adapted to httpx since this client
    needs to attach a bearer header — the daemon client's urllib helpers
    don't support that.
    """
    error = _config_error()
    if error:
        return error
    headers = {"Authorization": f"Bearer {_TOKEN}"}
    url = f"{_BASE_URL}{path}"
    try:
        resp = await _get_client().request(method, path, headers=headers, **kwargs)
        resp.raise_for_status()
        return resp.json()
    except httpx.HTTPStatusError as e:
        payload = e.response.text
        try:
            detail = json.loads(payload).get("detail", payload)
        except (json.JSONDecodeError, AttributeError):
            detail = payload
        return f"khimaira-notebook-readonly {url} → HTTP {e.response.status_code}: {str(detail)[:400]}"
    except httpx.RequestError as e:
        return f"khimaira-notebook-readonly {url} unreachable: {e}"
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"khimaira-notebook-readonly returned non-JSON from {url}: {exc}"


async def _notebook_search_impl(query: str, top_k: int) -> str:
    if not query.strip():
        return "❌ notebook_search needs a non-empty query."
    data = await _request("GET", "/notes/search", params={"q": query, "top_k": top_k})
    if isinstance(data, str):
        return data

    hits = data.get("hits", [])
    if not hits:
        return f"🔍 no notes match {query!r}."

    lines = [f"🔍 **{len(hits)} match(es) for {query!r}**:\n"]
    for h in hits:
        lines.append(f"  • `{h.get('note_id', '?')}`  score={h.get('score', '?')}")
    lines.append("\nUse `notebook_get(note_id)` to read one in full.")
    return "\n".join(lines)


async def _notebook_get_impl(note_id: str) -> str:
    if not note_id:
        return "❌ notebook_get requires a note_id — get one from notebook_search."
    data = await _request("GET", f"/notes/{urllib.parse.quote(note_id, safe='')}")
    if isinstance(data, str):
        return data

    lifecycle = data.get("lifecycle") or ("resolved" if data.get("resolution") else "captured")
    lines = [
        f"**{data.get('title', '?')}**  `{data.get('id', note_id)}`  [{lifecycle}]",
        f"repo={data.get('repo', '?')}  tab={data.get('tab_id', '?')}\n",
        f"**Raw paste:**\n{data.get('raw_text', '')}\n",
    ]
    pipeline = data.get("pipeline")
    if pipeline:
        lines.append(f"**Summary:** {pipeline.get('summary', '')}")
        lines.append(f"\n**Organized:**\n{pipeline.get('organized_md', '')}\n")
    resolution = data.get("resolution")
    if resolution:
        lines.append(f"**Resolution:** {resolution}")
    return "\n".join(lines)


async def _notebook_ask_impl(question: str) -> str:
    if not question.strip():
        return "❌ notebook_ask needs a non-empty question."
    data = await _request("POST", "/notes/ask", json={"question": question})
    if isinstance(data, str):
        return data

    answer = data.get("answer", "")
    sources = data.get("sources") or []
    lines = [f"💬 {answer}"]
    if sources:
        lines.append(f"\n**Sources:** {', '.join(f'`{s}`' for s in sources)}")
    return "\n".join(lines)


@mcp.tool()
async def notebook_search(query: str, top_k: int = 10) -> str:
    """Semantic search over Joseph's read-only notebook (jeevy_portal scope).

    Returns ranked note ids + scores; follow up with `notebook_get(note_id)`
    to read a hit in full.

    Args:
        query: what you're looking for.
        top_k: max hits to return (default 10).
    """
    return await _notebook_search_impl(query, top_k)


@mcp.tool()
async def notebook_get(note_id: str) -> str:
    """Read one note in full from the read-only notebook.

    Sensitive notes are already redacted server-side before they reach
    this client — you'll see a placeholder in place of any real secret.

    Args:
        note_id: the note id (from notebook_search).
    """
    return await _notebook_get_impl(note_id)


@mcp.tool()
async def notebook_ask(question: str) -> str:
    """Ask a code-grounded question against Joseph's read-only notebook.

    Prefer this over `notebook_search` + `notebook_get` when you want an
    ANSWER, not a list of notes to read yourself.

    Args:
        question: the question to ask.
    """
    return await _notebook_ask_impl(question)


def serve(*, host: str = "0.0.0.0", port: int = 8743) -> None:
    """Start this MCP server over HTTP (blocks until shutdown).

    Fails loud before binding if `KHIMAIRA_NOTEBOOK_MCP_TOKEN` is unset —
    same posture as `notebook_readonly.server.serve`'s own `_require_token`
    check, since this is the interface now reachable from the tailnet.
    """
    _require_mcp_token()
    print(f"khimaira notebook-readonly mcp: starting on http://{host}:{port}/mcp")
    print(f"  REST proxy: {_BASE_URL or '(KHIMAIRA_NOTEBOOK_RO_URL unset)'}")
    print("  auth: bearer token required (KHIMAIRA_NOTEBOOK_MCP_TOKEN)")
    mcp.run(transport="http", host=host, port=port)
