"""Themis MCP tool surface — 5 tools for role-invariant enforcement.

Tools:
  - themis_my_rules: invariants bound to the calling session's role
  - themis_list_rules: survey the full rule set (one or all roles)
  - themis_check: check a tool call against role invariants (thin daemon wrapper)
  - themis_record_violation: record a violation to the log (thin daemon wrapper)
  - themis_violations_for: query the violation log (thin daemon wrapper)

All daemon HTTP calls use urllib.request (stdlib, same pattern as
khimaira.server.monitor_tools). Daemon errors are surfaced explicitly;
this layer never silently swallows them.

Re-registered on khimaira's MCP server at boot under the `themis_`
prefix by khimaira.server.sibling_tools (NORTH_STAR Phase 0 pattern).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from mcp.server.fastmcp import FastMCP

logger = logging.getLogger(__name__)

# Caller identity for X-Session-ID header. Claude Code sets CLAUDE_CODE_SESSION_ID
# on every MCP subprocess at launch time (verified via probe v2 2026-05-21).
_CALLER_SESSION_ID: str = os.environ.get("CLAUDE_CODE_SESSION_ID", "")

_DAEMON_BASE = "http://127.0.0.1:8740"
_TIMEOUT = 5.0

mcp = FastMCP(
    "themis",
    instructions=(
        "Themis enforces role-invariant boundaries for khimaira sessions.\n\n"
        "## Tools\n\n"
        "- `themis_my_rules(session_id)` — what rules am I bound by?\n"
        "- `themis_list_rules(role?)` — survey all rules or one role's rules.\n"
        "- `themis_check(session_id, tool_name, tool_input, cwd?)` — would this "
        "tool call violate my invariants? Use in observer scan-loop for post-hoc "
        "detection. Phase 2 hook uses the daemon endpoint directly.\n"
        "- `themis_record_violation(...)` — record a violation to the log.\n"
        "- `themis_violations_for(session_id?, role?, since?, limit=50)` — "
        "query the violation log. Read-auth enforced by daemon (D12).\n\n"
        "## When to use\n\n"
        "Observer: call `themis_check` once per scan-loop iteration on each "
        "agent's most-recent tool call to surface violations without Phase 2 "
        "hook integration.\n\n"
        "Agent self-inspection: `themis_my_rules(session_id)` before starting "
        "a task to confirm which tools are blocked for your role.\n\n"
        "Master postmortem: `themis_violations_for(role='intake')` to audit "
        "intake boundary violations over a session."
    ),
)


# ---------------------------------------------------------------------------
# HTTP helpers (stdlib only — matches monitor_tools.py pattern)
# ---------------------------------------------------------------------------


def _caller_headers() -> dict[str, str]:
    """Return X-Session-ID header for daemon auth if caller ID is known."""
    if _CALLER_SESSION_ID:
        return {"X-Session-ID": _CALLER_SESSION_ID}
    return {}


def _daemon_get(
    path: str,
    params: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> Any:
    """GET request to the khimaira daemon. Returns parsed JSON or raises."""
    url = f"{_DAEMON_BASE}{path}"
    if params:
        filtered = {k: v for k, v in params.items() if v is not None}
        if filtered:
            url += "?" + urllib.parse.urlencode(filtered)
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8")
            detail = json.loads(payload).get("detail", payload)
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"daemon HTTP {exc.code}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"daemon unreachable: {exc.reason}") from exc


def _daemon_post(
    path: str,
    body: dict[str, Any],
    headers: dict[str, str] | None = None,
) -> Any:
    """POST JSON to the daemon. Returns parsed JSON or raises."""
    data = json.dumps(body).encode("utf-8")
    all_headers: dict[str, str] = {"Content-Type": "application/json"}
    if headers:
        all_headers.update(headers)
    req = urllib.request.Request(
        f"{_DAEMON_BASE}{path}",
        data=data,
        headers=all_headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            payload = exc.read().decode("utf-8")
            detail = json.loads(payload).get("detail", payload)
        except Exception:
            detail = str(exc)
        raise RuntimeError(f"daemon HTTP {exc.code}: {detail[:400]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"daemon unreachable: {exc.reason}") from exc


def _resolve_role(session_id: str) -> str | None:
    """Resolve the role for session_id via daemon chat membership lookup."""
    try:
        result = _daemon_get(f"/api/sessions/{session_id}/role")
        return result.get("role")
    except Exception as exc:
        logger.warning("themis: role resolution failed for %s: %s", session_id, exc)
        return None


def _invariant_to_dict(inv: Any) -> dict[str, Any]:
    """Serialize an Invariant to the MCP response shape."""
    matcher_parts: list[str] = []
    for m in inv.matchers:
        summary = m.tool
        if m.tool_input_field is not None:
            summary += f"[{m.tool_input_field.field}~/{m.tool_input_field.pattern}/]"
        matcher_parts.append(summary)
    return {
        "id": inv.id,
        "name": inv.name,
        "severity": inv.severity.value if hasattr(inv.severity, "value") else str(inv.severity),
        "message_template": inv.message,
        "matcher_summary": " | ".join(matcher_parts),
    }


# ---------------------------------------------------------------------------
# MCP tools
# ---------------------------------------------------------------------------


@mcp.tool()
def my_rules(session_id: str, cwd: str | None = None) -> list[dict[str, Any]]:
    """Return the invariants bound to the calling session's role.

    Resolves the session's role via the daemon (live chat membership
    lookup — not cached, so deputize/resume role changes are reflected
    immediately). Returns [] if the session has no role assigned or if
    the role has no rule file.

    If `cwd` is provided, app-scoped rules from `<project_root>/.claude/themis/`
    are merged into the result (additive only — core rules always win on collision).

    Args:
        session_id: The calling session's khimaira session ID.
        cwd: Optional working directory of the calling session. When given,
            app-scoped rules for the session's git repo are included.

    Returns:
        List of [{id, name, severity, message_template, matcher_summary}]
    """
    role = _resolve_role(session_id)
    if not role:
        return []

    try:
        from themis.data import find_app_rules_dir, load_rules

        app_rules_dir = find_app_rules_dir(cwd) if cwd else None
        rule_set = load_rules(role, app_rules_dir=app_rules_dir)
        return [_invariant_to_dict(inv) for inv in rule_set.invariants]
    except Exception as exc:
        logger.warning("themis my_rules: failed to load rules for role %s: %s", role, exc)
        return []


@mcp.tool()
def list_rules(role: str | None = None) -> list[dict[str, Any]]:
    """Survey the Themis rule set — one role or all 8.

    Args:
        role: Optional role name (e.g. "intake", "master"). Omit to return all roles.

    Returns:
        List of [{role, invariants: [{id, name, severity, message_template, matcher_summary}]}]
    """
    from themis.data import VALID_ROLES, load_rules

    targets = [role] if role else sorted(VALID_ROLES)
    result: list[dict[str, Any]] = []

    for r in targets:
        try:
            rule_set = load_rules(r)
            result.append(
                {
                    "role": r,
                    "invariants": [_invariant_to_dict(inv) for inv in rule_set.invariants],
                }
            )
        except Exception as exc:
            logger.warning("themis list_rules: failed to load rules for role %s: %s", r, exc)

    return result


@mcp.tool()
def check(
    session_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    cwd: str | None = None,
) -> dict[str, Any]:
    """Check whether a tool call violates the session's role invariants.

    Thin wrapper over POST /api/themis/check. The daemon resolves the
    session's role from chat membership and runs the engine. Single HTTP
    round-trip; p99 target <20ms local, <35ms with cross-daemon role
    resolution.

    Fail-open: if the daemon is unreachable, returns {ok: true, error: "..."}.
    Themis is a guardrail, not a security gate.

    Args:
        session_id: The session making the tool call.
        tool_name: The tool being called (e.g. "Edit", "mcp__khimaira__auto").
        tool_input: The tool's input parameters.
        cwd: Optional working directory context.

    Returns:
        {ok: bool, violation?: {rule_id, name, message, severity}, role?: str}
    """
    body: dict[str, Any] = {
        "session_id": session_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
    }
    if cwd is not None:
        body["cwd"] = cwd

    try:
        return _daemon_post("/api/themis/check", body, headers=_caller_headers())
    except RuntimeError as exc:
        msg = str(exc)
        logger.warning("themis check: %s", msg)
        if msg.startswith("daemon unreachable"):
            # Fail-open: daemon down should never hard-block agents.
            # Themis is a guardrail, not a security gate (D7).
            return {"ok": True, "error": msg}
        return {"ok": False, "error": msg}
    except Exception as exc:
        logger.warning("themis check: unexpected error (fail-open): %s", exc)
        return {"ok": True, "error": f"fail-open: {exc}"}


@mcp.tool()
def record_violation(
    session_id: str,
    rule_id: str,
    tool_name: str,
    tool_input: dict[str, Any],
    tool_use_id: str,
    cwd: str,
) -> dict[str, Any]:
    """Record a rule violation to the violations log.

    Called by the PreToolUse hook after blocking (Phase 2), and by the
    observer after detecting a warn-severity hit. The daemon appends to
    ~/.local/state/khimaira/themis_violations.jsonl and handles
    tool_input truncation to 500 chars.

    Args:
        session_id: Session that triggered the violation.
        rule_id: The invariant ID that fired (e.g. "IN-INTAKE-1").
        tool_name: The tool that was blocked or warned.
        tool_input: The tool's input at violation time.
        tool_use_id: Claude Code's tool_use_id from the PreToolUse envelope.
        cwd: Working directory at time of violation.

    Returns:
        {logged: true, id: <record_id>} or {logged: false, error: str}
    """
    body: dict[str, Any] = {
        "session_id": session_id,
        "rule_id": rule_id,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "tool_use_id": tool_use_id,
        "cwd": cwd,
    }
    try:
        return _daemon_post("/api/themis/violations", body, headers=_caller_headers())
    except Exception as exc:
        logger.warning("themis record_violation: daemon call failed: %s", exc)
        return {"logged": False, "error": str(exc)}


@mcp.tool()
def violations_for(
    session_id: str | None = None,
    role: str | None = None,
    since: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Query the violation log.

    Read auth (D12) enforced by daemon: callers may read their own
    session's violations; only sessions with role in {master, observer,
    critic} may read cross-session violations. Pass your session_id so
    the daemon can resolve your role for the auth check.

    Args:
        session_id: Filter by session ID (also used for caller auth).
        role: Filter by role name (e.g. "intake").
        since: ISO 8601 timestamp — return violations after this time.
        limit: Max records (default 50).

    Returns:
        List of violation records: [{ts, session_id, role, rule_id,
        tool_name, tool_use_id, tool_input_summary, decision, cwd}]
    """
    params: dict[str, Any] = {"limit": limit}
    if session_id is not None:
        params["session_id"] = session_id
    if role is not None:
        params["role"] = role
    if since is not None:
        params["since"] = since

    try:
        result = _daemon_get("/api/themis/violations", params, headers=_caller_headers())
        # Daemon returns {violations: [...]}; unwrap the envelope.
        if isinstance(result, dict):
            return result.get("violations", [])
        return result if isinstance(result, list) else []
    except Exception as exc:
        logger.warning("themis violations_for: daemon call failed: %s", exc)
        return []
