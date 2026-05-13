"""HTTP client wrappers for the khimaira-monitor REST API, exposed as
MCP tools so Claude can query LangGraph runtime state directly from
chat.

Why these aren't just MCP tools defined in monitor/server.py: khimaira's
MCP server runs over stdio (one process per Claude Code session); the
monitor daemon runs HTTP (one daemon shared across all chat sessions).
They're separate processes by design — the MCP layer here calls into
the daemon's REST API.

Failure mode: if the daemon isn't running, every tool returns a clear
"daemon not started" message rather than crashing. The user gets
actionable feedback instead of a stack trace.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Default to the monitor daemon's bind address. Override with
# KHIMAIRA_MONITOR_URL if the daemon runs elsewhere (rare).
_DEFAULT_BASE = os.environ.get("KHIMAIRA_MONITOR_URL", "http://127.0.0.1:8740").rstrip(
    "/"
)

_DAEMON_DOWN_HINT = (
    "khimaira-monitor daemon is not running or unreachable at {base}.\n"
    "Run `khimaira monitor start` and try again. If the daemon binds a\n"
    "different port, set KHIMAIRA_MONITOR_URL=http://127.0.0.1:<port>."
)


def _get(
    path: str, *, base: str = _DEFAULT_BASE, timeout: float = 5.0
) -> dict[str, Any] | str:
    """GET request → parsed JSON, or a friendly error string on failure.

    Error mapping (matches _post's pattern — earlier this was a bug:
    HTTPError is a subclass of URLError, so catching URLError first
    swallowed 4xx/5xx responses as "daemon down". 2026-05-10 fix: catch
    HTTPError BEFORE URLError so the agent sees the real status):

      HTTPError (e.g. 404 unknown session, 500 server bug) →
        "HTTP <code>: <detail>"
      URLError with ConnectionRefusedError reason → daemon truly down
      URLError other (timeout, dns) → transient; suggest retry
    """
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode("utf-8")
            # FastAPI returns {"detail": "..."} for HTTPException; surface
            # just the detail when present rather than the full JSON envelope.
            try:
                detail = json.loads(payload).get("detail", payload)
            except (json.JSONDecodeError, AttributeError):
                detail = payload
            return f"khimaira-monitor {url} → HTTP {e.code}: {detail[:400]}"
        except Exception:
            return f"khimaira-monitor {url} → HTTP {e.code}"
    except urllib.error.URLError as e:
        # Differentiate truly-down from transient. ConnectionRefusedError
        # means the daemon socket isn't bound; timeouts/dns/etc are
        # usually transient and benefit from a retry suggestion.
        reason = getattr(e, "reason", None)
        if isinstance(reason, ConnectionRefusedError):
            return _DAEMON_DOWN_HINT.format(base=base)
        return (
            f"khimaira-monitor {url} → transient connection error: {reason or e}. "
            f"Retry once; if it persists, check `khimaira monitor status`."
        )
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"khimaira-monitor returned non-JSON from {url}: {exc}"
    except Exception as exc:
        return f"khimaira-monitor query failed ({url}): {exc}"


def _post(
    path: str,
    body: dict[str, Any],
    *,
    base: str = _DEFAULT_BASE,
    timeout: float = 5.0,
) -> dict[str, Any] | str:
    """POST request → parsed JSON, or friendly error string. Long-poll friendly.

    For `wait_for_process` the caller passes a timeout matching the daemon's
    server-side wait timeout plus a small buffer.
    """
    url = f"{base}{path}"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode("utf-8")
            return f"khimaira-monitor {url} → HTTP {e.code}: {payload[:300]}"
        except Exception:
            return f"khimaira-monitor {url} → HTTP {e.code}"
    except urllib.error.URLError:
        return _DAEMON_DOWN_HINT.format(base=base)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"khimaira-monitor returned non-JSON from {url}: {exc}"
    except Exception as exc:
        return f"khimaira-monitor query failed ({url}): {exc}"


# ---------------------------------------------------------------------------
# Tool implementations — kept thin so Claude reads the daemon's data
# directly. Output is markdown-formatted for chat readability.
# ---------------------------------------------------------------------------


async def list_projects() -> str:
    """All projects the monitor daemon has discovered."""
    data = _get("/api/projects")
    if isinstance(data, str):
        return data
    if not isinstance(data, list) or not data:
        return "No projects discovered. Add roots to ~/.config/khimaira/roots.yaml."
    lines = [f"**{len(data)} projects:**\n"]
    for p in data:
        conns = p.get("connections", [])
        conn_summary = (
            ", ".join(f"{c['kind']}:{c.get('label', '?')}" for c in conns)
            or "no checkpointer"
        )
        lines.append(f"- `{p['name']}` — {p['path']} ({conn_summary})")
    return "\n".join(lines)


async def list_active_runs(project: str) -> str:
    """Threads currently running, paused, or starting in a project."""
    data = _get(f"/api/threads/{urllib.parse.quote(project)}?limit=50")
    if isinstance(data, str):
        return data

    threads = data.get("threads", [])
    live = [t for t in threads if t["status"] in ("running", "paused", "starting")]
    if not live:
        return f"No active runs in `{project}` ({len(threads)} idle)."

    lines = [
        f"**{project}** — {len(live)} active / {len(threads)} total"
        f" (running threshold: {data.get('running_threshold_seconds', 300)}s)\n"
    ]
    for t in live:
        node = t.get("current_node") or "—"
        scope = (t.get("scope_id") or "")[:8]
        lines.append(
            f"- `{t['thread_id'][:50]}`  {t['status']}  @{node}"
            f"  step={t.get('step') or 0}  scope={scope}"
        )
    return "\n".join(lines)


async def thread_state(project: str, thread_id: str, recent: int = 5) -> str:
    """Full state + recent N checkpoints for a single thread.

    Args:
        project: Project name (e.g. "khimaira", "jeevy_portal")
        thread_id: Thread to inspect
        recent: How many checkpoints to include (default 5, max 50)
    """
    recent = max(1, min(50, recent))
    data = _get(
        f"/api/threads/{urllib.parse.quote(project)}/{urllib.parse.quote(thread_id, safe='')}?limit={recent}"
    )
    if isinstance(data, str):
        return data

    cps = data.get("checkpoints", [])
    if not cps:
        return f"No checkpoints found for thread `{thread_id}` in `{project}`."

    # Show newest-first as the API returns them, with chronological numbering
    chrono = list(reversed(cps))
    lines = [f"**Thread `{thread_id}`** — {len(cps)} checkpoint(s)\n"]
    for i, cp in enumerate(chrono):
        state = cp.get("state") or {}
        keys = list(state.keys()) if isinstance(state, dict) else []
        ts = (cp.get("created_at") or "")[:19]
        lines.append(
            f"step {i}: {ts}  node={cp.get('node') or '-'}  "
            f"keys={keys[:10]}{'...' if len(keys) > 10 else ''}"
        )
    return "\n".join(lines)


async def wait_for_run(
    project: str,
    thread_id: str,
    until_status: str | None = None,
    until_node: str | None = None,
    timeout_s: float = 300.0,
) -> str:
    """**Blocking call — wait for a LangGraph run to reach a target state.**

    Replaces the polling pattern `sleep(N); monitor_active_runs(); …` with
    ONE MCP roundtrip. The daemon polls the checkpointer on your behalf
    and returns when the run is terminal (default), reaches `until_node`,
    matches `until_status`, or `timeout_s` elapses.

    Default behavior: returns when status leaves the in-flight set
    (running, starting) — i.e. the run hit idle / paused / terminal.

    Args:
        project: project name (e.g. "jeevy_portal", "khimaira").
        thread_id: thread/run id to watch.
        until_status: target status ("idle", "paused", "running"). None
            (default) = wait for any non-in-flight status.
        until_node: optional. Return when current_node == this string.
            Useful for "wait until the run reaches `vectorize`".
        timeout_s: max wall time. Default 300s. Returns reason=timeout.
    """
    client_timeout = timeout_s + 30.0
    params = {"timeout_s": str(timeout_s)}
    if until_status:
        params["until_status"] = until_status
    if until_node:
        params["until_node"] = until_node
    qs = "&".join(f"{k}={urllib.parse.quote(v)}" for k, v in params.items())
    data = _get(
        f"/api/threads/{urllib.parse.quote(project)}/"
        f"{urllib.parse.quote(thread_id, safe='')}/wait?{qs}",
        timeout=client_timeout,
    )
    if isinstance(data, str):
        return data

    reason = data.get("reason", "?")
    elapsed = data.get("elapsed_s", 0.0)
    summary = data.get("summary") or {}
    status = summary.get("status", "?")
    current_node = summary.get("current_node", "-")
    step = summary.get("step", "?")

    parts = [
        f"**`{thread_id}`** in `{project}` — {reason} after {elapsed:.1f}s",
        f"status: **{status}**  node: `{current_node}`  step: {step}",
    ]
    if reason == "timeout":
        parts.append(
            f"⚠️ timed out after {timeout_s}s — run may still be in flight; "
            f"call again with a higher timeout_s, or pass until_node to wait "
            f"for a specific intermediate state."
        )
    elif reason == "not_found":
        parts.append(
            f"⚠️ thread {thread_id!r} not found in project. "
            f"Check `monitor_active_runs({project!r})` for live thread ids."
        )
    return "\n".join(parts)


async def find_stuck(project: str) -> str:
    """Find threads classified as stuck/stale by the monitor's heuristics.

    Checks running/paused threads against the per-project
    running_threshold_seconds. Threads beyond 1× the threshold are
    "stale"; beyond 3× are "stuck".
    """
    from datetime import datetime, timezone

    data = _get(f"/api/threads/{urllib.parse.quote(project)}?limit=100")
    if isinstance(data, str):
        return data

    threshold = data.get("running_threshold_seconds", 300)
    now = datetime.now(timezone.utc)

    stuck: list[tuple[float, dict]] = []
    stale: list[tuple[float, dict]] = []
    for t in data.get("threads", []):
        if t["status"] not in ("running", "paused", "starting"):
            continue
        if not t.get("last_updated"):
            continue
        try:
            updated = datetime.fromisoformat(t["last_updated"])
            age_s = (now - updated).total_seconds()
        except ValueError:
            continue
        if age_s >= threshold * 3:
            stuck.append((age_s, t))
        elif age_s >= threshold:
            stale.append((age_s, t))

    if not stuck and not stale:
        return f"No stuck or stale threads in `{project}`."

    lines = [
        f"**`{project}` — {len(stuck)} stuck, {len(stale)} stale** (threshold={threshold}s)\n"
    ]
    for age, t in sorted(stuck, key=lambda x: -x[0]):
        lines.append(
            f"🔴 STUCK  `{t['thread_id'][:50]}`  @{t.get('current_node') or '-'}"
            f"  {age:.0f}s ago"
        )
    for age, t in sorted(stale, key=lambda x: -x[0]):
        lines.append(
            f"🟡 stale  `{t['thread_id'][:50]}`  @{t.get('current_node') or '-'}"
            f"  {age:.0f}s ago"
        )
    return "\n".join(lines)


async def api_routes(project: str, graph_linked_only: bool = False) -> str:
    """FastAPI routes for a project + graph-invocation indicators.

    Args:
        project: Project name.
        graph_linked_only: If True, only show routes that appear to
            invoke a LangGraph (the typical use case — finding which
            HTTP endpoint kicks off a particular graph).
    """
    data = _get(f"/api/api_routes/{urllib.parse.quote(project)}")
    if isinstance(data, str):
        return data
    routes = data.get("routes", [])
    if graph_linked_only:
        routes = [r for r in routes if r.get("invokes_graph")]
    if not routes:
        return f"No routes{'with graph links' if graph_linked_only else ''} found in `{project}`."
    lines = [
        f"**`{project}` — {len(routes)} route(s)** "
        f"({data.get('graph_linked_count', 0)} graph-linked)\n"
    ]
    # Sort: graph-linked first, then by path
    routes.sort(key=lambda r: (not r.get("invokes_graph"), r.get("path", "")))
    for r in routes:
        marker = "→graph" if r.get("invokes_graph") else "      "
        method = r.get("method", "?")
        path = r.get("path", "?")
        handler = r.get("handler", "?")
        file_loc = f"{r.get('file', '?')}:{r.get('line', 0)}"
        line = f"{marker}  {method:6s} {path:40s}  {handler}  ({file_loc})"
        if r.get("invokes_graph") and r.get("graph_hints"):
            line += f"\n        hints: {', '.join(r['graph_hints'][:3])}"
        lines.append(line)
    return "\n".join(lines)


async def anomalies(limit: int = 20, only_failures: bool = True) -> str:
    """Self-watch findings: invariants the daemon checks against itself.

    The daemon runs periodic invariant checks (DB ↔ API consistency,
    observation collector freshness, topology agreement). Failures
    are logged. This tool surfaces the recent log so Claude can see
    what's been weird without reading the daemon's stderr.

    Args:
        limit: max entries to return (1-100, default 20)
        only_failures: if True, hide passing checks (the typical use)
    """
    qs = f"limit={max(1, min(100, limit))}"
    if only_failures:
        qs += "&only_failures=true"
    data = _get(f"/api/anomalies?{qs}")
    if isinstance(data, str):
        return data
    items = data.get("items", [])
    if not items:
        return "No anomalies in the recent log." + (
            " Pass `only_failures=False` to see passing checks too."
            if only_failures
            else ""
        )
    lines = [f"**{len(items)} anomaly entries** (most recent first):\n"]
    for item in reversed(items):
        sev = item.get("severity", "warn")
        icon = {"error": "🔴", "warn": "🟡", "info": "ℹ️"}.get(sev, "·")
        passed = "✓" if item.get("passed") else "✗"
        proj = item.get("project") or "*"
        ts = (item.get("timestamp") or "")[:19]
        lines.append(f"{icon} {passed} `{item.get('check')}`  proj={proj}  {ts}")
        if item.get("detail"):
            lines.append(f"    {item['detail']}")
        if item.get("evidence"):
            lines.append(f"    evidence: {item['evidence']}")
    return "\n".join(lines)


async def frontend_components(project: str, with_api_calls_only: bool = False) -> str:
    """React/Next components in a project + their API calls + state hooks.

    Args:
        project: Project name.
        with_api_calls_only: If True, hide components that don't call any API.
    """
    data = _get(f"/api/frontend_components/{urllib.parse.quote(project)}")
    if isinstance(data, str):
        return data
    comps = data.get("components", [])
    if with_api_calls_only:
        comps = [c for c in comps if c.get("api_calls")]
    if not comps:
        return f"No components{' with API calls' if with_api_calls_only else ''} in `{project}`."
    lines = [
        f"**`{project}` — {len(comps)} component(s)** "
        f"({data.get('with_api_calls', 0)} make API calls)\n"
    ]
    comps.sort(
        key=lambda c: (not c.get("api_calls"), c.get("file", ""), c.get("line", 0))
    )
    for c in comps[:60]:  # cap output for chat readability
        marker = "→api" if c.get("api_calls") else "    "
        loc = f"{c.get('file', '?')}:{c.get('line', 0)}"
        lines.append(f"{marker}  {c.get('name', '?'):30s}  ({loc})")
        if c.get("api_calls"):
            lines.append(f"        api: {', '.join(c['api_calls'][:3])}")
        if c.get("state_hooks"):
            lines.append(f"        hooks: {', '.join(c['state_hooks'])}")
    if len(comps) > 60:
        lines.append(f"\n... {len(comps) - 60} more not shown")
    return "\n".join(lines)


async def schema_drift(project: str) -> str:
    """Pydantic / SQLAlchemy models vs the project's Postgres schema."""
    data = _get(f"/api/schema_drift/{urllib.parse.quote(project)}")
    if isinstance(data, str):
        return data
    if data.get("note"):
        return f"`{project}`: {data['note']}"
    reports = data.get("reports", [])
    drifty = [r for r in reports if r.get("has_drift")]
    if not drifty:
        return (
            f"**`{project}`**: {data.get('model_count')} model(s), "
            f"no drift detected against Postgres schema."
        )
    lines = [
        f"**`{project}` — {data.get('with_drift')} model(s) drift** "
        f"out of {data.get('model_count')}\n"
    ]
    for r in drifty[:20]:
        loc = f"{r.get('file', '?')}:{r.get('line', 0)}"
        if not r.get("table_exists"):
            lines.append(
                f"❌ `{r.get('model')}` → table `{r.get('table')}` MISSING  ({loc})"
            )
            continue
        bits = []
        if r.get("only_in_model"):
            bits.append(f"in model only: {r['only_in_model']}")
        if r.get("only_in_db"):
            bits.append(f"in DB only: {r['only_in_db']}")
        if r.get("type_mismatches"):
            mm = [
                f"{m['field']}({m['model_type']}→{m['db_type']})"
                for m in r["type_mismatches"]
            ]
            bits.append(f"type mismatch: {', '.join(mm)}")
        lines.append(f"⚠️ `{r.get('model')}` ↔ `{r.get('table')}`  ({loc})")
        for b in bits:
            lines.append(f"    {b}")
    if len(drifty) > 20:
        lines.append(f"\n... {len(drifty) - 20} more not shown")
    return "\n".join(lines)


async def heartbeat() -> str:
    """Self-watch heartbeat — when did the daemon last complete its
    invariant checks? Use for liveness."""
    data = _get("/api/heartbeat")
    if isinstance(data, str):
        return data
    if not data:
        return "Self-watch has not run yet."
    icon = "🟢" if data.get("healthy") else "🔴"
    parts = [
        f"{icon} **self-watch** {'healthy' if data.get('healthy') else 'STALE'}",
        f"last run: {data.get('last_self_watch_at', 'never')}",
        f"age: {data.get('age_seconds', '?'):.0f}s",
        f"checks: {data.get('checks_total', '?')} ({data.get('checks_failed', 0)} failed)",
    ]
    return "\n".join(parts)


async def topology(project: str) -> str:
    """Compiled-graph topology for a project: graph names + node counts."""
    data = _get(f"/api/topology/{urllib.parse.quote(project)}")
    if isinstance(data, str):
        return data

    graphs = data.get("graphs", [])
    if not graphs:
        return f"No graphs discovered in `{project}`."

    lines = [f"**`{project}` — {len(graphs)} graph(s):**\n"]
    for g in graphs:
        name = g.get("label") or g.get("name") or "?"
        nodes = g.get("nodes", [])
        edges = g.get("edges", [])
        invokes = g.get("invokes", {})
        lines.append(
            f"- **{name}** ({g.get('name', '?')}) — "
            f"{len(nodes)} nodes, {len(edges)} edges"
            + (f", invokes: {list(invokes.values())}" if invokes else "")
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Process observability — replaces agent polling with single-call SSE-backed
# blocking primitives. See khimaira/monitor/processes.py and the
# /api/processes/* endpoints for the daemon-side implementation.
# ---------------------------------------------------------------------------


async def spawn_process(
    cmd: list[str],
    label: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    replace_existing: bool = False,
) -> str:
    """Start a tracked subprocess. Returns a handle dict; process runs in the
    daemon. Use `wait_for_process` to block until completion or `follow_process`
    to read recent output.
    """
    data = _post(
        "/api/processes/spawn",
        {
            "cmd": cmd,
            "label": label,
            "cwd": cwd,
            "env": env,
            "replace_existing": replace_existing,
        },
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return (
        f"✅ spawned `{data['label']}` (pid {data['pid']}) — "
        f"{' '.join(data['cmd'][:3])}\n"
        f"Use `wait_for_process('{label}')` or `follow_process('{label}')` to observe."
    )


async def wait_for_process(
    label: str,
    completion_signal: str | None = None,
    timeout_s: float = 300.0,
) -> str:
    """**Blocking call** — wait for a process to finish OR for a regex match
    in its output. Returns ONE response with full stdout/stderr + exit code.

    This replaces the polling pattern of repeated `cat <log>` calls. Single
    MCP roundtrip instead of dozens.

    Args:
        label: process label from `spawn_process`.
        completion_signal: optional regex; returns as soon as it matches output.
            Examples: r"\\d+ passed|\\d+ failed" for tests, r"Local: http" for dev server.
        timeout_s: max wall time to wait. Returns reason="timeout" if exceeded.
    """
    client_timeout = timeout_s + 30.0
    data = _post(
        f"/api/processes/{urllib.parse.quote(label)}/wait",
        {"completion_signal": completion_signal, "timeout_s": timeout_s},
        timeout=client_timeout,
    )
    if isinstance(data, str):
        return data

    reason = data.get("reason", "?")
    # Prefer process runtime over wait-elapsed time. They diverge when the
    # process exited before the wait was called (then wait_duration_s ≈ 0
    # but process_runtime_s reflects the actual work).
    duration = data.get("process_runtime_s", data.get("duration_s", 0))
    parts = [f"**`{label}`** — finished in {duration:.1f}s ({reason})"]
    if reason == "signal_match":
        parts.append(f"matched: {data.get('matched', '')!r}")
    elif reason == "exit":
        parts.append(f"exit code: {data.get('exit_code')}")
    elif reason == "timeout":
        parts.append(f"⚠️ timed out after {timeout_s}s — process still running")

    stdout = (data.get("stdout_text") or "").strip()
    stderr = (data.get("stderr_text") or "").strip()
    if stdout:
        parts.append(
            f"\n**stdout** ({len(stdout)} chars):\n```\n{_tail(stdout, 4000)}\n```"
        )
    if stderr:
        parts.append(
            f"\n**stderr** ({len(stderr)} chars):\n```\n{_tail(stderr, 2000)}\n```"
        )
    return "\n".join(parts)


async def follow_process(label: str, max_chunks: int = 100) -> str:
    """Snapshot of a tracked process's output so far. Non-blocking.

    For real-time streaming, the dashboard's SSE endpoint
    (`/api/processes/{label}/stream`) is better; this is the single-call
    snapshot variant for MCP usage.
    """
    data = _get(f"/api/processes/{urllib.parse.quote(label)}", timeout=10.0)
    if isinstance(data, str):
        return data

    parts = [
        f"**`{label}`** — pid={data['pid']} "
        f"{'running' if data.get('is_running') else 'finished'} "
        f"({data.get('duration_s', 0):.1f}s)",
    ]
    if data.get("exit_code") is not None:
        parts.append(f"exit code: {data['exit_code']}")
    stdout = (data.get("stdout_text") or "").strip()
    stderr = (data.get("stderr_text") or "").strip()
    if stdout:
        parts.append(
            f"\n**stdout** ({len(stdout)} chars):\n```\n{_tail(stdout, 4000)}\n```"
        )
    if stderr:
        parts.append(
            f"\n**stderr** ({len(stderr)} chars):\n```\n{_tail(stderr, 2000)}\n```"
        )
    return "\n".join(parts)


async def list_processes() -> str:
    """All tracked processes — running + recently-finished."""
    data = _get("/api/processes")
    if isinstance(data, str):
        return data
    procs = data.get("processes", [])
    if not procs:
        return "No tracked processes. Spawn one with `spawn_process`."

    lines = [f"**{len(procs)} tracked process(es):**\n"]
    for p in procs:
        status = "🟢 running" if p["is_running"] else f"⚪ exit={p.get('exit_code')}"
        lines.append(
            f"- `{p['label']}` (pid {p['pid']}) {status} "
            f"— {p['duration_s']:.1f}s — `{' '.join(p['cmd'][:3])}`"
        )
    return "\n".join(lines)


async def kill_process(label: str) -> str:
    """Send SIGTERM (then SIGKILL after 5s grace) to a tracked process."""
    data = _post(f"/api/processes/{urllib.parse.quote(label)}/kill", {}, timeout=15.0)
    if isinstance(data, str):
        return data
    if data.get("stopped"):
        return f"✅ killed `{label}`"
    return f"`{label}` was already finished — nothing to kill"


def _tail(text: str, max_chars: int) -> str:
    """Show the last `max_chars` of text — what an agent usually wants from logs."""
    if len(text) <= max_chars:
        return text
    return f"... [{len(text) - max_chars} chars truncated] ...\n" + text[-max_chars:]


# ---------------------------------------------------------------------------
# Phase 13 — MCP call telemetry. "Is khimaira being used effectively?"
# ---------------------------------------------------------------------------


async def usage_report(window_minutes: int = 1440) -> str:
    """Aggregate report: which khimaira MCP tools were called, how often, with
    what success rate, and how many polls `wait_for_process` replaced.

    Default window is 24h (1440 min). Pass smaller for "what just happened"
    or larger for weekly patterns.

    Read by sessions wanting to know if khimaira is providing value, and
    by humans investigating "is the agent using the right tools?"
    """
    data = _get(f"/api/mcp-calls/summary?window_minutes={window_minutes}")
    if isinstance(data, str):
        return data

    parts = []
    win = data.get("window_minutes", window_minutes)
    if win >= 1440:
        win_label = f"last {win / 1440:.0f}d"
    elif win >= 60:
        win_label = f"last {win / 60:.0f}h"
    else:
        win_label = f"last {win}m"

    total = data.get("total_calls", 0)
    failures = data.get("total_failures", 0)
    rate = data.get("failure_rate", 0.0)

    if total == 0:
        return (
            f"📭 No khimaira MCP tool calls in the {win_label}.\n\n"
            "Either the agent hasn't been using khimaira, or the khimaira MCP\n"
            "server in this Claude Code session hasn't been restarted since\n"
            "the telemetry decorator was added (Phase 13). "
            "**Restart Claude Code to pick up the logging.**"
        )

    parts.append(
        f"**khimaira usage — {win_label}:** {total} calls "
        f"({failures} failed, {rate:.0%} failure rate)\n"
    )

    # Polling-replacement metric — the headline value pitch
    pr = data.get("polling_replacement", {})
    wait_calls = pr.get("wait_calls", 0)
    blocked_s = pr.get("total_blocked_seconds", 0.0)
    polls_saved = pr.get("estimated_polls_saved", 0)
    if wait_calls:
        parts.append(
            f"⚡ **Polling replacement:** {wait_calls} `wait_for_process` "
            f"calls blocked for {blocked_s:.0f}s total → "
            f"~{polls_saved} polls saved (vs. agent polling every 5s).\n"
        )

    # By-tool breakdown
    parts.append("**By tool:**")
    for entry in data.get("by_tool", [])[:20]:
        tool = entry.get("tool", "?")
        calls = entry.get("calls", 0)
        f_count = entry.get("failures", 0)
        p50 = entry.get("p50_ms", 0)
        p95 = entry.get("p95_ms", 0)
        warn = " ⚠️" if f_count > 0 and f_count >= calls * 0.5 else ""
        parts.append(
            f"- `{tool}` — {calls} calls ({f_count} failed) · "
            f"p50={p50}ms p95={p95}ms{warn}"
        )
        if entry.get("errors_sampled"):
            for e in entry["errors_sampled"][:2]:
                parts.append(f"    error: {e[:140]}")

    return "\n".join(parts)


async def list_calls(
    window_minutes: int | None = None,
    tool: str | None = None,
    only_failures: bool = False,
    limit: int = 50,
) -> str:
    """Recent khimaira MCP tool calls (newest first). Filter by tool name,
    failure-only, time window."""
    params = []
    if window_minutes is not None:
        params.append(f"window_minutes={window_minutes}")
    if tool:
        params.append(f"tool={urllib.parse.quote(tool)}")
    if only_failures:
        params.append("only_failures=true")
    params.append(f"limit={limit}")
    data = _get("/api/mcp-calls?" + "&".join(params))
    if isinstance(data, str):
        return data

    calls = data.get("calls", [])
    if not calls:
        return "📭 No matching MCP calls."

    parts = [f"**{len(calls)} most-recent call(s):**\n"]
    for c in calls[:limit]:
        ok = "✅" if c.get("success") else "❌"
        ts = c.get("ts", "")
        latency = c.get("elapsed_ms", 0)
        tool = c.get("tool", "?")
        size = c.get("output_size", 0)
        line = f"{ok} `{tool}` ({latency}ms, {size}ch) — {ts}"
        if c.get("error"):
            line += f"\n    error: {c['error'][:160]}"
        parts.append(line)
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Multi-session shared state — solves the "two parallel Claude Code sessions
# can't see each other" problem. See khimaira/monitor/sessions.py.
# ---------------------------------------------------------------------------


async def session_log_decision(session_id: str, text: str, why: str = "") -> str:
    """Record a decision (for the working agent — session A's write)."""
    data = _post(
        f"/api/sessions/{urllib.parse.quote(session_id)}/decision",
        {"text": text, "why": why},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"📝 logged decision (id={data['id']}): {text[:120]}"


async def session_log_touch(
    session_id: str,
    file: str,
    summary: str = "",
    line_start: int | None = None,
    line_end: int | None = None,
) -> str:
    """Record a file modification. Typically called from a PostToolUse hook
    on Edit/Write/MultiEdit — agent doesn't have to remember manually."""
    data = _post(
        f"/api/sessions/{urllib.parse.quote(session_id)}/touch",
        {
            "file": file,
            "summary": summary,
            "line_start": line_start,
            "line_end": line_end,
        },
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"📂 touch logged: {file}"


async def session_log_question(
    session_id: str,
    text: str,
    target_session_id: str | None = None,
    cross_workspace: bool = False,
) -> str:
    """Open a question other sessions can answer. Returns the question id —
    that's the handle other sessions use in `session_post_answer`.

    If `target_session_id` is provided, the question is *targeted* at that
    session — the target's UserPromptSubmit hook will surface it as an
    incoming question on its next turn (via the /incoming endpoint),
    without requiring the target to poll session_state. Accepts a UUID
    or a friendly name.

    If `target_session_id` is None, the question is broadcast — visible
    only to sessions that explicitly read this session's session_state.
    Use targeted questions for direct asks; broadcast for "anyone with
    context can chime in."

    Workspace guard: targeted questions across workspaces are rejected
    by default (HTTP 422). Pass `cross_workspace=True` to explicitly
    allow — used when you intentionally want to reach a sister project's
    session.
    """
    body: dict[str, Any] = {"text": text}
    if target_session_id:
        body["target_session_id"] = target_session_id
    if cross_workspace:
        body["cross_workspace"] = True
    data = _post(
        f"/api/sessions/{urllib.parse.quote(session_id)}/question",
        body,
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    target_note = f" (targeted at {target_session_id})" if target_session_id else ""
    return (
        f"❓ question opened (id={data['id']}){target_note}: {text[:120]}\n"
        f"Other sessions can answer with `session_post_answer(target_session_id='{session_id}', "
        f"question_id='{data['id']}', answer='...')`"
    )


async def session_search_archive(
    session_id: str,
    query: str | None = None,
    limit: int = 50,
) -> str:
    """Search archived inbox notes by substring."""
    qstr = (
        f"?q={urllib.parse.quote(query)}&limit={limit}" if query else f"?limit={limit}"
    )
    data = _get(
        f"/api/sessions/{urllib.parse.quote(session_id)}/inbox/archive{qstr}",
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    results = data.get("results", [])
    if not results:
        return f"📚 archive empty for query={query!r}"
    lines = [f"📚 **{len(results)} archived note(s) matching {query!r}:**\n"]
    for n in results:
        kind = n.get("kind") or "note"
        from_sid = (n.get("from_session_id") or "")[:8]
        ts = n.get("ts", "")[:19]
        body = (n.get("answer") or n.get("text") or "").strip()
        if len(body) > 400:
            body = body[:400] + "…"
        lines.append(f"- [{ts} {kind} from {from_sid}] {body}")
    return "\n".join(lines)


async def session_ack_notes(
    session_id: str,
    note_ids: list[str] | None = None,
) -> str:
    """Mark inbox notes as read after surfacing their content to the user.

    The auto-inject hook re-surfaces unread notes every turn until acked
    or until 3 surfaces (auto-expire safety net). Call this after you've
    relayed the notice content in your response so the same note doesn't
    re-loop into context next turn.

    `note_ids=None` acks all currently-unread notes — fine when you've
    surfaced everything in this turn's `📬 khimaira inbox` block.
    """
    body: dict[str, Any] = {"note_ids": note_ids}
    data = _post(
        f"/api/sessions/{urllib.parse.quote(session_id)}/inbox/ack",
        body,
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"📭 acked {data.get('acked', 0)} note(s)"


async def session_query_transcript(
    session_id: str,
    query: str,
    context_lines: int = 1,
    max_matches: int = 20,
) -> str:
    """Grep a session's Claude Code transcript for `query`.

    Returns matched turns + surrounding context. Use to read what a
    now-stopped session discussed.
    """
    qstr = (
        f"?q={urllib.parse.quote(query)}"
        f"&context_lines={context_lines}&max_matches={max_matches}"
    )
    data = _get(
        f"/api/sessions/{urllib.parse.quote(session_id)}/transcript/query{qstr}",
        timeout=15.0,
    )
    if isinstance(data, str):
        return data
    if not data.get("matches"):
        if data.get("error"):
            return f"❌ {data['error']}"
        return f"📜 no matches for {query!r} in {session_id}'s transcript"
    lines = [
        f"📜 **{data['match_count']} match(es) for {query!r}** "
        f"(transcript: {data['total_turns']} turns):",
        "",
    ]
    for m in data["matches"]:
        lines.append(f"--- match at turn {m['match_at_turn']} ---")
        for ex in m["excerpt"]:
            marker = "▶" if ex.get("is_match") else " "
            role = ex.get("role") or ex.get("type") or "?"
            preview = ex["text_preview"]
            lines.append(f"  {marker} [{role}] {preview}")
        lines.append("")
    if data.get("truncated"):
        lines.append(
            f"(truncated at {max_matches} matches; refine query or raise max_matches)"
        )
    return "\n".join(lines)


async def session_summarize_transcript(
    session_id: str,
    focus: str | None = None,
) -> str:
    """Heuristic summary of a session's transcript (no LLM call)."""
    qstr = f"?focus={urllib.parse.quote(focus)}" if focus else ""
    data = _get(
        f"/api/sessions/{urllib.parse.quote(session_id)}/transcript/summary{qstr}",
        timeout=20.0,
    )
    if isinstance(data, str):
        return data
    if data.get("error"):
        return f"❌ {data['error']}"
    lines = [
        f"📜 **transcript summary for {session_id}**",
        f"  size: {data.get('transcript_size_kb', '?')} KB",
        f"  turns: {data.get('turns_by_role', {})}",
        "",
        f"  top tools used: {data.get('top_tools_used', {})}",
        "",
        f"  files touched ({data.get('files_touched_count', 0)}, sample):",
    ]
    for f in data.get("files_touched_sample", [])[:15]:
        lines.append(f"    - {f}")
    lines.append("")
    lines.append(f"  recent user prompts ({data.get('user_messages_count', 0)} total):")
    for u in data.get("user_messages_recent", []):
        lines.append(f"    > {u[:200]}")
    lines.append("")
    lines.append(
        f"  recent assistant message intros ({data.get('assistant_text_count', 0)} total):"
    )
    for a in data.get("assistant_text_recent_intros", []):
        lines.append(f"    · {a[:200]}")
    if data.get("focus_query"):
        lines.append("")
        lines.append(
            f"  focus={data['focus_query']!r}: {data.get('focus_match_count', 0)} matches"
        )
        for m in data.get("focus_matches", [])[:5]:
            for ex in m.get("excerpt", []):
                if ex.get("is_match"):
                    lines.append(f"    ▶ {ex['text_preview'][:300]}")
    return "\n".join(lines)


async def session_subscribe_handoff(handoff_id: str, session_id: str) -> str:
    """Subscribe to receive owner's decisions in your inbox automatically."""
    data = _post(
        f"/api/handoffs/{urllib.parse.quote(handoff_id)}/subscribe",
        {"session_id": session_id},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"👀 subscribed to handoff {handoff_id[:8]} — owner's decisions will land in your inbox"


async def session_unsubscribe_handoff(handoff_id: str, session_id: str) -> str:
    """Stop receiving owner's progress updates for this handoff."""
    data = _post(
        f"/api/handoffs/{urllib.parse.quote(handoff_id)}/unsubscribe",
        {"session_id": session_id},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"🔕 unsubscribed from handoff {handoff_id[:8]}"


async def session_release_handoff(handoff_id: str, session_id: str) -> str:
    """Owner steps aside; next session to consume becomes new owner."""
    data = _post(
        f"/api/handoffs/{urllib.parse.quote(handoff_id)}/release",
        {"session_id": session_id},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"✋ released ownership of handoff {handoff_id[:8]}; next session to consume becomes owner"


async def session_invite_handoff(
    parent_handoff_id: str,
    owner_session_id: str,
    invitee_session_id: str,
    text: str,
    expires_in_hours: float = 168.0,
) -> str:
    """Owner delegates a slice of a handoff to a specific other session.

    Creates a child handoff targeting `invitee_session_id`. Invitee gets
    an inbox notice immediately (if currently live) AND the handoff
    surfaces on their next SessionStart hook.
    """
    body: dict[str, Any] = {
        "owner_session_id": owner_session_id,
        "invitee_session_id": invitee_session_id,
        "text": text,
        "expires_in_hours": expires_in_hours,
    }
    data = _post(
        f"/api/handoffs/{urllib.parse.quote(parent_handoff_id)}/invite",
        body,
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return (
        f"🤝 invited {invitee_session_id} to handoff {data.get('id')} "
        f"(parent={parent_handoff_id}, expires_in_hours={expires_in_hours})"
    )


async def session_consume_handoffs(session_id: str, cwd: str) -> str:
    """Pull cwd-scoped handoffs into this session mid-flight.

    SessionStart's auto-surfacing only fires once per session boot.
    When a handoff is posted AFTER a session has started, that session
    can't see it unless someone calls this. Wraps the daemon's
    /api/handoffs/consume endpoint — same auto-claim semantics as
    SessionStart (first session to consume becomes owner; subsequent
    sessions are observers).

    Args:
        session_id: this session's id (the one consuming).
        cwd: working directory to match handoff scope against. Pass
            the project root you're working in, not the khimaira
            install dir — handoffs are scoped to where the work
            lives, and the MCP server's cwd is the wrong source.
    """
    qs = (
        f"session_id={urllib.parse.quote(session_id)}" f"&cwd={urllib.parse.quote(cwd)}"
    )
    data = _get(f"/api/handoffs/consume?{qs}", timeout=10.0)
    if isinstance(data, str):
        return data

    handoffs = data.get("handoffs", [])
    if not handoffs:
        return f"📭 no new handoffs in scope {cwd!r} for session {session_id[:8]}."

    # Mirror the SessionStart hook's framing so the agent gets the
    # same directive tone whether the handoff arrived at boot or
    # mid-session. Split by claim role (owner / observer) the same way.
    owned = [h for h in handoffs if h.get("_claim_role", "owner") == "owner"]
    observed = [h for h in handoffs if h.get("_claim_role") == "observer"]

    lines: list[str] = []
    if owned:
        lines.append(
            f"📦 khimaira handoffs — {len(owned)} directive(s) you now OWN in {cwd}:"
        )
        lines.append("")
        for h in owned:
            from_id = (h.get("from_session_id") or "?")[:8]
            ts = (h.get("ts") or "")[:19]
            text = (h.get("text") or "").strip()
            parent = h.get("parent_id")
            target = h.get("target_session_id")
            if parent and target:
                lines.append(
                    f"- 🤝 [INVITE handoff {h['id'][:8]} · {ts} · from {from_id}]"
                )
            else:
                lines.append(f"- [handoff {h['id'][:8]} · {ts} · from {from_id}]")
            lines.append(f"  {text}")
            lines.append("")
        lines.append(
            "Treat these as directives, not FYIs. Read referenced files, "
            "pick the highest-priority item, propose a first action, and START. "
            "Use `session_release_handoff(id, me)` if you finish or this isn't your lane."
        )

    if observed:
        if owned:
            lines.append("")
            lines.append("---")
            lines.append("")
        lines.append(
            f"👀 khimaira handoffs — {len(observed)} already-claimed handoff(s) in {cwd}:"
        )
        for h in observed:
            from_id = (h.get("from_session_id") or "?")[:8]
            owner = (h.get("_owner_session_id") or "?")[:8]
            text = (h.get("text") or "").strip()
            lines.append(
                f"- [handoff {h['id'][:8]} · from {from_id} · OWNED BY {owner}]"
            )
            lines.append(f"  {text[:400]}{'…' if len(text) > 400 else ''}")

    return "\n".join(lines)


async def session_post_handoff(
    from_session_id: str,
    text: str,
    scope_cwd: str | None = None,
    expires_in_hours: float = 168.0,
) -> str:
    """Drop a handoff note any future session whose cwd matches will read.

    For cross-session handoffs to sessions that DON'T EXIST YET. Use this
    instead of session_post_notice when you don't know the target session
    id (because the handoff is for whoever picks up this work next).
    """
    body: dict[str, Any] = {
        "from_session_id": from_session_id,
        "text": text,
        "expires_in_hours": expires_in_hours,
    }
    if scope_cwd:
        body["scope_cwd"] = scope_cwd
    data = _post("/api/handoffs", body, timeout=10.0)
    if isinstance(data, str):
        return data
    return (
        f"📦 handoff posted (id={data.get('id')}, scope_cwd={data.get('scope_cwd')}, "
        f"expires_in_hours={expires_in_hours})"
    )


async def session_post_notice(
    target_session_id: str,
    text: str,
    from_session_id: str = "external",
) -> str:
    """Drop a FYI/ack note in another session's inbox — no answer expected."""
    data = _post(
        f"/api/sessions/{urllib.parse.quote(target_session_id)}/notice",
        {"text": text, "from_session_id": from_session_id},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"📨 notice posted to {target_session_id}"


async def session_wait_for_answer(
    session_id: str,
    question_id: str,
    timeout: float = 300.0,
) -> str:
    """Block until question_id is answered, or timeout.

    Real-time coordination primitive. After logging a TARGETED question
    on another session, call this to await the answer in the SAME TURN
    instead of ending the turn and reading the answer next time. Saves
    a turn-cycle of "wake the asking agent again to read the answer."

    `session_id` is the asking session (where the question was logged).
    `question_id` is the 12-char hex id returned by session_log_question.

    Returns the answer text on success. Raises if the request errors,
    times out, or the question was withdrawn.
    """
    # HTTP client timeout MUST exceed server-side wait timeout to give
    # the long-poll a chance to return.
    http_timeout = timeout + 30.0
    url = (
        f"/api/sessions/{urllib.parse.quote(session_id)}"
        f"/questions/{urllib.parse.quote(question_id)}/wait"
        f"?timeout={timeout}"
    )
    data = _get(url, timeout=http_timeout)
    if isinstance(data, str):
        return data
    if not data.get("answered"):
        return f"⏱️ no answer within {timeout:.0f}s for q={question_id}"
    q = data.get("question", {})
    answer = q.get("answer", "")
    answered_by = (q.get("answered_by") or "")[:8]
    return (
        f"✅ answer received for q={question_id} "
        f"(answered by {answered_by}):\n\n{answer}"
    )


async def session_incoming_questions(session_id: str) -> str:
    """Open questions from OTHER sessions targeted at this one.

    Symmetric counterpart to `session_pending_notes`: pending shows
    answers to questions THIS session asked; incoming shows questions
    OTHER sessions asked specifically targeting THIS session. Closes the
    "their inbox is empty" gap from before targeted questions existed.
    """
    data = _get(
        f"/api/sessions/{urllib.parse.quote(session_id)}/incoming",
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    questions = data.get("questions", [])
    if not questions:
        return "📨 no incoming questions"
    lines = [f"📨 **{len(questions)} incoming question(s) from other sessions:**\n"]
    for q in questions:
        from_sid = (q.get("from_session_id") or "")[:8] or "external"
        lines.append(f"- (q={q['id']}) from {from_sid}: {q.get('text', '')[:300]}")
        lines.append(
            f"  ➜ answer with `session_post_answer(target_session_id='{q.get('from_session_id')}', "
            f"question_id='{q['id']}', answer='...')`"
        )
    return "\n".join(lines)


async def session_set_status(session_id: str, status: str, detail: str = "") -> str:
    """Update agent's high-level state. Conventional values:
    'researching', 'implementing', 'blocked', 'awaiting-review', 'idle'."""
    data = _post(
        f"/api/sessions/{urllib.parse.quote(session_id)}/status",
        {"status": status, "detail": detail},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"🟢 status updated: {status}{(' — ' + detail) if detail else ''}"


async def session_set_name(session_id: str, name: str) -> str:
    """Set a friendly name for the session. After this, other sessions can
    refer to it by name in `session_state(name)`, `session_post_answer(name, ...)`,
    etc. — no need to remember the UUID.

    Names should be slug-shaped: lowercase, dashes ('khimaira-monitor',
    'jeevy-auth-fix'). Two sessions can share a name; lookup prefers
    most-recently-active.
    """
    data = _post(
        f"/api/sessions/{urllib.parse.quote(session_id)}/name",
        {"name": name},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"🏷️ session named `{name}` (id: {session_id})"


async def session_set_workspace(session_id: str, workspace: str) -> str:
    """Place this session in a named workspace (privacy/noise boundary).

    Sessions in the same workspace see each other normally; cross-
    workspace reads and targeted writes require explicit overrides
    (`workspace=` arg on reads, `cross_workspace=True` on
    `session_log_question`). Default workspace is `"default"` so
    pre-existing sessions are unaffected.
    """
    data = _post(
        f"/api/sessions/{urllib.parse.quote(session_id)}/workspace",
        {"workspace": workspace},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"🗂️ session workspace set to `{workspace}` (id: {session_id})"


async def session_post_answer(
    target_session_id: str,
    question_id: str,
    answer: str,
    from_session_id: str = "external",
) -> str:
    """Session B answers session A's open question.

    Updates the question's status + drops a note in A's inbox. A's
    SessionStart hook (which calls `session_pending_notes`) surfaces the
    answer next time A wakes up.
    """
    data = _post(
        f"/api/sessions/{urllib.parse.quote(target_session_id)}/answer",
        {
            "question_id": question_id,
            "answer": answer,
            "from_session_id": from_session_id,
        },
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return (
        f"📨 answer posted to session {target_session_id} for question {question_id}\n"
        f"Q: {data.get('text', '')[:120]}\nA: {answer[:200]}"
    )


async def session_state(session_id: str, recent: int = 10) -> str:
    """Full digest of a session — what is session A currently working on?

    The 'side conversation' query: B calls this to see A's status,
    decisions, file touches, open questions WITHOUT interrupting A.
    """
    data = _get(
        f"/api/sessions/{urllib.parse.quote(session_id)}?recent={recent}",
        timeout=10.0,
    )
    if isinstance(data, str):
        return data

    parts = [f"**session `{session_id}`**"]

    status = data.get("status")
    if status:
        parts.append(
            f"status: **{status['status']}**"
            + (f" — {status['detail']}" if status.get("detail") else "")
            + f" (updated {status.get('updated_at', '?')})"
        )

    parts.append(
        f"decisions: {data.get('decision_count', 0)} total, "
        f"files touched: {data.get('file_touch_count', 0)}, "
        f"open questions: {len(data.get('open_questions', []))}"
    )

    if data.get("recent_decisions"):
        parts.append("\n**Recent decisions:**")
        for d in data["recent_decisions"][-5:]:
            why = f" — {d.get('why')}" if d.get("why") else ""
            parts.append(f"- {d.get('text', '')[:160]}{why}")

    if data.get("open_questions"):
        parts.append("\n**Open questions:**")
        for q in data["open_questions"]:
            parts.append(f"- (id={q['id']}) {q.get('text', '')[:160]}")

    if data.get("recent_files"):
        parts.append("\n**Recent file touches:**")
        for f in data["recent_files"][-5:]:
            range_str = (
                f":{f['line_start']}-{f['line_end']}"
                if f.get("line_start") and f.get("line_end")
                else ""
            )
            parts.append(
                f"- {f.get('file', '')}{range_str} — {f.get('summary', '')[:100]}"
            )

    return "\n".join(parts)


async def session_summary(session_id: str) -> str:
    """Lightweight session digest — status + counts + last-active, no bodies.

    Use to poll "is session X done yet?" or render an overview. Cheaper
    than session_state() because it does NOT load decision/file bodies.
    """
    data = _get(
        f"/api/sessions/{urllib.parse.quote(session_id)}/summary",
        timeout=10.0,
    )
    if isinstance(data, str):
        return data

    parts = [f"**session `{session_id}`** (summary)"]
    status = data.get("status")
    if status:
        parts.append(
            f"status: **{status.get('status', '?')}**"
            + (f" — {status['detail']}" if status.get("detail") else "")
            + f" (updated {status.get('updated_at', '?')})"
        )
    age = data.get("last_active_age_s")
    age_str = f"{age:.0f}s ago" if isinstance(age, (int, float)) else "?"
    parts.append(
        f"decisions: {data.get('decision_count', 0)} · "
        f"files: {data.get('file_touch_count', 0)} · "
        f"open Qs: {data.get('open_question_count', 0)} · "
        f"last active {age_str}"
    )
    return "\n".join(parts)


async def session_pending_notes(session_id: str, mark_read: bool = True) -> str:
    """**A's inbox read.** Fetch unread answers other sessions have posted
    to this session's questions.

    Call automatically at SessionStart so the working agent sees "session B
    answered Q3 while you were running" without the user having to know to ask.

    Args:
        session_id: this session's id (the one reading its inbox).
        mark_read: if True (default), mark notes as read after returning them.
            Pass False to peek without consuming.
    """
    data = _get(
        f"/api/sessions/{urllib.parse.quote(session_id)}/pending"
        f"?mark_read={'true' if mark_read else 'false'}",
        timeout=10.0,
    )
    if isinstance(data, str):
        return data

    notes = data.get("notes", [])
    if not notes:
        return "📭 No pending notes — your inbox is empty."

    parts = [f"📬 **{len(notes)} pending note(s):**\n"]
    for n in notes:
        kind = n.get("kind") or "note"
        from_sid = (n.get("from_session_id") or "")[:8] or "external"
        ts = (n.get("ts") or "")[:19]
        # Render body based on kind:
        #   answer  → has answer + question_text (re Q: ... ➜ ...)
        #   notice  → has text only, no question coupling
        # Previous version assumed all notes were answers and rendered
        # notices with empty Q + empty ➜ fields. Body was in `text`
        # but never accessed.
        if kind == "answer":
            q_text = (n.get("question_text") or "").strip()
            a_text = (n.get("answer") or n.get("text") or "").strip()
            if q_text:
                parts.append(
                    f"- [answer from {from_sid} · {ts}]\n"
                    f"  re Q: {q_text[:200]}\n"
                    f"  ➜ {a_text[:600]}"
                )
            else:
                parts.append(f"- [answer from {from_sid} · {ts}]\n  {a_text[:600]}")
        else:
            # notice (or any future non-answer kind) — body lives in `text`
            body = (n.get("text") or n.get("answer") or "").strip()
            parts.append(f"- [{kind} from {from_sid} · {ts}]\n  {body[:600]}")
    return "\n".join(parts)


async def session_recent_decisions(recent_per_session: int = 5) -> str:
    """Recent decisions across ALL active sessions. Cross-session view."""
    data = _get(
        f"/api/sessions/recent_decisions?recent_per_session={recent_per_session}",
        timeout=10.0,
    )
    if isinstance(data, str):
        return data

    decisions = data.get("decisions", [])
    if not decisions:
        return "No recorded decisions yet across any session."

    parts = [f"**{len(decisions)} recent decision(s):**\n"]
    for d in decisions[:30]:
        parts.append(
            f"- ({d.get('session_id')}, {d.get('ts')}): " f"{d.get('text', '')[:180]}"
        )
    return "\n".join(parts)


async def session_list() -> str:
    """All known sessions + their freshness, status, counts."""
    data = _get("/api/sessions", timeout=10.0)
    if isinstance(data, str):
        return data

    sessions = data.get("sessions", [])
    if not sessions:
        return "No sessions tracked yet. Sessions are auto-created on first log call."

    parts = [f"**{len(sessions)} session(s):**\n"]
    for s in sessions:
        status = s.get("status", {}) or {}
        age_s = s.get("last_active_age_s") or 0
        age_str = f"{age_s/60:.0f}m ago" if age_s < 3600 else f"{age_s/3600:.1f}h ago"
        name = s.get("name")
        ident = f"`{name}` (id: {s['session_id']})" if name else f"`{s['session_id']}`"
        parts.append(
            f"- {ident} "
            f"({status.get('status', '?')}) — "
            f"last active {age_str}, "
            f"decisions={s.get('decision_count', 0)}, "
            f"open_q={s.get('open_question_count', 0)}"
        )
    parts.append(
        "\n_💡 Pass any name/id above directly to `session_state`, "
        "`session_summary`, `session_post_notice`, etc. — they resolve "
        "names internally, no need to list-then-filter._"
    )
    return "\n".join(parts)
