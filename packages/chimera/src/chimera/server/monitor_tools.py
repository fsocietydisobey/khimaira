"""HTTP client wrappers for the chimera-monitor REST API, exposed as
MCP tools so Claude can query LangGraph runtime state directly from
chat.

Why these aren't just MCP tools defined in monitor/server.py: chimera's
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
# CHIMERA_MONITOR_URL if the daemon runs elsewhere (rare).
_DEFAULT_BASE = os.environ.get(
    "CHIMERA_MONITOR_URL", "http://127.0.0.1:8740"
).rstrip("/")

_DAEMON_DOWN_HINT = (
    "chimera-monitor daemon is not running or unreachable at {base}.\n"
    "Run `chimera monitor start` and try again. If the daemon binds a\n"
    "different port, set CHIMERA_MONITOR_URL=http://127.0.0.1:<port>."
)


def _get(path: str, *, base: str = _DEFAULT_BASE, timeout: float = 5.0) -> dict[str, Any] | str:
    """GET request → parsed JSON, or a friendly error string on failure."""
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError:
        return _DAEMON_DOWN_HINT.format(base=base)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"chimera-monitor returned non-JSON from {url}: {exc}"
    except Exception as exc:
        return f"chimera-monitor query failed ({url}): {exc}"


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
        url, data=data, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            payload = e.read().decode("utf-8")
            return f"chimera-monitor {url} → HTTP {e.code}: {payload[:300]}"
        except Exception:
            return f"chimera-monitor {url} → HTTP {e.code}"
    except urllib.error.URLError:
        return _DAEMON_DOWN_HINT.format(base=base)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        return f"chimera-monitor returned non-JSON from {url}: {exc}"
    except Exception as exc:
        return f"chimera-monitor query failed ({url}): {exc}"


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
        return "No projects discovered. Add roots to ~/.config/chimera/roots.yaml."
    lines = [f"**{len(data)} projects:**\n"]
    for p in data:
        conns = p.get("connections", [])
        conn_summary = ", ".join(f"{c['kind']}:{c.get('label', '?')}" for c in conns) or "no checkpointer"
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
        project: Project name (e.g. "chimera", "jeevy_portal")
        thread_id: Thread to inspect
        recent: How many checkpoints to include (default 5, max 50)
    """
    recent = max(1, min(50, recent))
    data = _get(f"/api/threads/{urllib.parse.quote(project)}/{urllib.parse.quote(thread_id, safe='')}?limit={recent}")
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

    lines = [f"**`{project}` — {len(stuck)} stuck, {len(stale)} stale** (threshold={threshold}s)\n"]
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
            if only_failures else ""
        )
    lines = [f"**{len(items)} anomaly entries** (most recent first):\n"]
    for item in reversed(items):
        sev = item.get("severity", "warn")
        icon = {"error": "🔴", "warn": "🟡", "info": "ℹ️"}.get(sev, "·")
        passed = "✓" if item.get("passed") else "✗"
        proj = item.get("project") or "*"
        ts = (item.get("timestamp") or "")[:19]
        lines.append(
            f"{icon} {passed} `{item.get('check')}`  proj={proj}  {ts}"
        )
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
    comps.sort(key=lambda c: (not c.get("api_calls"), c.get("file", ""), c.get("line", 0)))
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
            lines.append(f"❌ `{r.get('model')}` → table `{r.get('table')}` MISSING  ({loc})")
            continue
        bits = []
        if r.get("only_in_model"):
            bits.append(f"in model only: {r['only_in_model']}")
        if r.get("only_in_db"):
            bits.append(f"in DB only: {r['only_in_db']}")
        if r.get("type_mismatches"):
            mm = [f"{m['field']}({m['model_type']}→{m['db_type']})"
                  for m in r['type_mismatches']]
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
# blocking primitives. See chimera/monitor/processes.py and the
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
            "cmd": cmd, "label": label, "cwd": cwd,
            "env": env, "replace_existing": replace_existing,
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
        parts.append(f"\n**stdout** ({len(stdout)} chars):\n```\n{_tail(stdout, 4000)}\n```")
    if stderr:
        parts.append(f"\n**stderr** ({len(stderr)} chars):\n```\n{_tail(stderr, 2000)}\n```")
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
        parts.append(f"\n**stdout** ({len(stdout)} chars):\n```\n{_tail(stdout, 4000)}\n```")
    if stderr:
        parts.append(f"\n**stderr** ({len(stderr)} chars):\n```\n{_tail(stderr, 2000)}\n```")
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
# Multi-session shared state — solves the "two parallel Claude Code sessions
# can't see each other" problem. See chimera/monitor/sessions.py.
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
        {"file": file, "summary": summary, "line_start": line_start, "line_end": line_end},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return f"📂 touch logged: {file}"


async def session_log_question(session_id: str, text: str) -> str:
    """Open a question other sessions can answer. Returns the question id —
    that's the handle other sessions use in `session_post_answer`."""
    data = _post(
        f"/api/sessions/{urllib.parse.quote(session_id)}/question",
        {"text": text},
        timeout=10.0,
    )
    if isinstance(data, str):
        return data
    return (
        f"❓ question opened (id={data['id']}): {text[:120]}\n"
        f"Other sessions can answer with `session_post_answer(target_session_id='{session_id}', "
        f"question_id='{data['id']}', answer='...')`"
    )


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
        {"question_id": question_id, "answer": answer, "from_session_id": from_session_id},
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
            parts.append(f"- {f.get('file', '')}{range_str} — {f.get('summary', '')[:100]}")

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
        parts.append(
            f"- ({n.get('kind')}) Q={n.get('question_text', '')[:120]}\n"
            f"  ➜ {n.get('answer', '')[:300]}\n"
            f"  from: {n.get('from_session_id')}, ts: {n.get('ts')}"
        )
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
            f"- ({d.get('session_id')}, {d.get('ts')}): "
            f"{d.get('text', '')[:180]}"
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
        parts.append(
            f"- `{s['session_id']}` "
            f"({status.get('status', '?')}) — "
            f"last active {age_str}, "
            f"decisions={s.get('decision_count', 0)}, "
            f"open_q={s.get('open_question_count', 0)}"
        )
    return "\n".join(parts)
