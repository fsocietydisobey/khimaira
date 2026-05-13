"""LLM-driven enrichment scan.

Reads a project's AST topology + README + graph source, sends them to
Gemini, parses the yaml response, validates it against the schema, and
writes the cache file. The user never invokes this directly — the
background worker (`scanner.py`) calls it whenever a project's cache is
missing or stale.

Failures are non-fatal. If Gemini returns garbage or the prompt fails,
we log a warning and leave the cache empty; the UI keeps rendering the
AST-only fallback.
"""

from __future__ import annotations

import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from khimaira.dispatch.runners import run_claude, run_gemini
from khimaira.log import get_logger

from ..discovery import ast_walker
from ..discovery.connections import discover_all
from ..discovery.introspector import TopologyResult
from .cache import newest_source_mtime, save
from .schema import ProjectMetadata

log = get_logger("monitor.metadata.scan")

# Cap how much source we shovel into the prompt — Gemini's context is
# generous but we still want predictable latency. The graph source files
# are the load-bearing context; README + others are nice-to-have.
_MAX_README_CHARS = 30_000
_MAX_GRAPH_SOURCE_CHARS = 60_000

_PROMPT_TEMPLATE = """\
You are documenting the architecture of a LangGraph project so an
observability dashboard can render polished diagrams of it AND group
its run history sensibly.

Below is what an AST scan extracted from the project's source: a list of
compiled graphs with their nodes and edges, PLUS sample thread_ids the
project actually writes to its checkpointer. Your job is to enrich this
with semantic information that the AST cannot infer:

  - For each graph: assign a `role` (orchestrator / subgraph / leaf), a
    short human label, a one-line summary, and a `layout` direction
    (TB / LR — pick what reads better).
  - For each graph, record `invokes`: a mapping from node-name → name of
    the OTHER graph that node delegates to (when one graph composes
    another via add_node). Empty map if it doesn't invoke anything.
  - For each node, set a `role` from this list (skip if not confident):
        entry      — start of the graph
        exit       — terminal node
        router     — decision / dispatch / branching
        gate       — human-in-the-loop / approval / validation gate
        critic     — quality check / adversarial review
        synthesis  — aggregation / merge / commit
        executor   — main work (default — only set if obvious)
  - For each node, write a `summary`: one short sentence (max ~120 chars)
    describing what the node DOES — its purpose, the work it performs,
    not just its name restated. This is what a developer would read in a
    side panel to understand the node's intent. Read the actual node
    function body to derive this; don't guess from the name alone.
    Skip the summary only if the node is purely structural (`__start__`,
    `__end__`).
  - Determine `thread_grouping`: how this project's thread_ids should be
    parsed into UI grouping fields. Inspect the SAMPLE THREAD IDS section
    below and the code that constructs them (often in worker / dispatcher
    files). Produce one or more Python regex patterns with NAMED GROUPS
    `scope_id` (required), `stage` (optional), `stage_detail` (optional).
    Examples:
      - jeevy `deliverable:<uuid>:digestion:<run-uuid>`:
          pattern: `^deliverable:(?P<scope_id>[0-9a-f-]{{36}}):(?P<stage>\\w+)(?::(?P<stage_detail>.+))?$`
          scope_kind: "deliverable"
      - khimaira bare uuid: pattern: `^(?P<scope_id>[0-9a-f-]{{36}})$`, scope_kind: "thread"
      - generic: pattern: `^(?P<scope_id>[^:]+)$`, scope_kind: "thread"
    Set `scope_label` to the natural human label for the scope (e.g.
    "Deliverable", "Run", "Chain"). When uncertain or no thread_ids exist
    yet, omit `thread_grouping` entirely — the dashboard will use a
    generic heuristic until the next scan.
  - Determine `run_clustering`: how sister threads are grouped into
    a "run" — a single logical execution pass. A run may span multiple
    threads (e.g. ingest → digestion → output), each appearing as a
    separate thread_id but sharing a common identifier somewhere in
    their parsed fields. The dashboard uses this to nest threads under
    a single "Run abc..." card so users see one work unit, not three
    scattered rows.

    The rule has two parts:
      `pattern`     — a JAVASCRIPT-COMPATIBLE regex (NOT Python's
                      `(?P<...>)` syntax). The FIRST CAPTURE GROUP
                      becomes the cluster key. Threads where this
                      pattern matches and produces the same key are
                      clustered together.
      `source_field` — which field to apply the pattern to. One of:
                      "thread_id" (the full id), "scope_id", "stage",
                      or "stage_detail" (the parsed grouping fields).

    Plus a fallback: `time_window_seconds` (default 300) groups
    pattern-misses by time proximity. Use 0 to disable.

    Examples:
      - jeevy: trailing UUID in stage_detail (`digestion:<run-uuid>`):
          source_field: stage_detail
          pattern: "([0-9a-f-]{{36}})$"
          run_label: "Run"
      - apps with serial run numbers (`pipeline:run-42`):
          source_field: stage_detail
          pattern: "run-(\\d+)"
      - khimaira: bare-uuid threads have no shared run id; rely on time
        proximity only:
          source_field: thread_id
          pattern: null
          time_window_seconds: 300
    When uncertain, omit `run_clustering` entirely — the dashboard
    will use a trailing-UUID + 5-min heuristic.
  - Determine `running_threshold_seconds`: how long can a node
    legitimately run before writing a new checkpoint? This drives
    the dashboard's "is this thread still executing?" classifier.
    Inspect node bodies for the slowest possible operation:
      - Pure data transformations / DB writes        → 60-120s
      - Single LLM call (Gemini Flash, GPT-4o-mini)  → 180-300s
      - Heavy LLM call (Claude Sonnet, GPT-4o)       → 300-600s
      - Multi-step LLM chain in one node body
        (parallel sub-runnables, e.g. drawing_extract
         doing 30 parallel Gemini calls)             → 600-900s
      - Pipeline-style nodes that themselves invoke
        sub-graphs synchronously                     → 900-1800s
    Pick the maximum across the project's nodes — the dashboard
    needs to tolerate the slowest legitimate node without
    false-flagging it as idle. When uncertain, omit and the
    dashboard uses 300s. Range: 60-1800.

Respond with valid YAML matching this schema EXACTLY. No commentary, no
markdown fences, no leading text:

```
schema_version: 1
project_name: <string>
project_path: <string>
generated_at: <ISO-8601 UTC>
source_mtime_max: <float>
summary: <one-paragraph description of what the project does>
graphs:
  <graph_name>:
    role: <orchestrator|subgraph|leaf>
    label: <short display name>
    summary: <one-line description>
    layout: <TB|LR>
    invokes:
      <node_name>: <other_graph_name>
    nodes:
      <node_name>:
        role: <entry|exit|router|gate|critic|synthesis|executor>
        label: <optional display name>
        summary: <one short sentence, what the node does>
thread_grouping:                # OMIT entirely if uncertain
  scope_label: <Deliverable|Run|Chain|...>
  examples:                     # the thread_ids you inspected
    - <thread_id>
  patterns:
    - pattern: <python regex with named groups scope_id, [stage], [stage_detail]>
      scope_kind: <static label, e.g. "deliverable">
      stage: <static stage label, OMIT if regex captures it>
run_clustering:                 # OMIT entirely if uncertain
  source_field: <thread_id|scope_id|stage|stage_detail>
  pattern: <JAVASCRIPT regex; first capture group = cluster key; or null>
  time_window_seconds: <int, default 300>
  run_label: <Run|Pipeline|Cycle|...>
running_threshold_seconds: <int, 60-1800, OMIT for default 300>
```

# Project metadata
project_name: {project_name}
project_path: {project_path}
generated_at: {generated_at}
source_mtime_max: {source_mtime_max}

# AST scan (authoritative — these are the graphs that exist)
{ast_block}

# Sample thread_ids from this project's checkpointer (use these to derive thread_grouping)
{thread_id_samples}

# Previous scan's output (when present — refine rather than recompute)
# If this section has content, you've analyzed this project before. Compare your
# previous decisions against the new observations below. Where observations
# CONTRADICT prior reasoning (e.g. you guessed running_threshold_seconds=300 but
# p95 of correspondence_phase1 is 3000s), update. Where observations CONFIRM
# prior reasoning, keep. The system gets sharper across rescans.
{previous_block}

# Runtime observations (when present — empirical p50/p95/max latencies + end-node frequencies)
# Use these to sharpen `running_threshold_seconds` toward the slowest-actually-observed node.
# If a node's p95 is dramatically higher than what its body would suggest, the project is
# probably hitting that pathology in production — the threshold should accommodate it.
# End-node frequencies cross-check the topology-derived terminals.
{observations_block}

# README (if present, may be truncated)
{readme_block}

# Graph source files (truncated)
{source_block}

Respond with only the YAML document. Begin immediately with `schema_version:`.
"""


async def scan_project(project_name: str, project_path: Path) -> ProjectMetadata | None:
    """Run a single project scan. Writes the cache on success.

    Returns the saved metadata, or None if the scan failed.
    """
    log.info("scan: starting %s at %s", project_name, project_path)

    ast_results = ast_walker.extract_from_path(project_path)
    if not ast_results:
        log.warning("scan: no AST results for %s — nothing to enrich", project_name)
        return None

    ast_block = _format_ast(ast_results)
    readme_block = _read_readme(project_path)
    source_block = _read_graph_sources(project_path)
    thread_id_samples = _sample_thread_ids(project_path)
    observations_block = _format_observations(project_path)
    previous_block = _format_previous(project_path)
    mtime_max = newest_source_mtime(project_path)
    now_iso = datetime.now(timezone.utc).isoformat()

    prompt = _PROMPT_TEMPLATE.format(
        project_name=project_name,
        project_path=str(project_path),
        generated_at=now_iso,
        source_mtime_max=mtime_max,
        ast_block=ast_block,
        readme_block=readme_block or "(no README found)",
        source_block=source_block or "(no graph source files captured)",
        thread_id_samples=thread_id_samples or "(no thread_ids in checkpointer yet — omit thread_grouping)",
        observations_block=observations_block or "(no runtime observations collected yet — first scan)",
        previous_block=previous_block or "(no previous scan — this is a fresh enrichment)",
    )

    raw = await _run_model(prompt, project_path, project_name)
    if raw is None:
        return None

    metadata = _parse_response(
        raw,
        project_name=project_name,
        project_path=project_path,
        generated_at=now_iso,
        source_mtime_max=mtime_max,
    )
    if metadata is None:
        return None

    save(metadata)
    log.info(
        "scan: saved metadata for %s — %d graphs enriched",
        project_name,
        len(metadata.graphs),
    )
    return metadata


async def _run_model(prompt: str, project_path: Path, project_name: str) -> str | None:
    """Dispatch to the configured scan model. Defaults to Claude (Opus high)
    because metadata enrichment is classification-heavy work where extended
    thinking pays off. Override via env: KHIMAIRA_MONITOR_SCAN_MODEL.

    Falls back to the alternate model on first-attempt failure so a
    transient outage on one provider doesn't block the scan.
    """
    # Default to gemini (subscription-billed) over claude (per-call API billing).
    # Reasoning: a fresh `khimaira monitor start` against N projects spawns N
    # Opus-on-94k-char-prompt scans = ~$1.40/project = surprise spend the user
    # didn't authorize. Gemini CLI hits the user's Gemini subscription instead.
    # Override via KHIMAIRA_MONITOR_SCAN_MODEL=claude when API billing is OK.
    primary = (os.environ.get("KHIMAIRA_MONITOR_SCAN_MODEL") or "gemini").lower()
    fallback = "claude" if primary == "gemini" else "gemini"
    # Pin Opus 4.7 for scans regardless of khimaira's CLAUDE_MODEL default —
    # metadata classification benefits from the largest reasoning model.
    # Override via KHIMAIRA_MONITOR_SCAN_CLAUDE_MODEL.
    claude_model = os.environ.get("KHIMAIRA_MONITOR_SCAN_CLAUDE_MODEL", "claude-opus-4-7")

    for attempt, model in enumerate((primary, fallback), start=1):
        log.info("scan: %s attempt %d using %s", project_name, attempt, model)
        try:
            if model == "claude":
                # permission_mode=None: no file edits should occur — the prompt
                # is self-contained, we don't want claude touching anything.
                # effort=high: extended thinking for nuanced classification.
                raw = await run_claude(
                    prompt,
                    cwd=str(project_path),
                    timeout=600,
                    permission_mode=None,
                    effort="high",
                    model=claude_model,
                )
            else:
                raw = await run_gemini(prompt, cwd=str(project_path), timeout=180)
        except Exception as exc:
            log.warning("scan: %s call failed for %s: %s", model, project_name, exc)
            continue

        if not raw or raw.startswith("Error:"):
            log.warning("scan: %s reported error for %s: %s", model, project_name, (raw or "")[:200])
            continue

        return raw

    log.warning("scan: all model attempts failed for %s", project_name)
    return None


def _parse_response(
    raw: str,
    *,
    project_name: str,
    project_path: Path,
    generated_at: str,
    source_mtime_max: float,
) -> ProjectMetadata | None:
    """Strip code fences, parse yaml, validate against the schema."""
    cleaned = _strip_code_fences(raw).strip()
    if not cleaned:
        log.warning("scan: empty response for %s", project_name)
        return None
    try:
        data = yaml.safe_load(cleaned)
    except yaml.YAMLError as exc:
        log.warning("scan: invalid yaml for %s: %s", project_name, exc)
        log.debug("scan: raw response:\n%s", cleaned[:1000])
        return None
    if not isinstance(data, dict):
        log.warning("scan: response wasn't a mapping for %s (got %s)", project_name, type(data).__name__)
        return None

    # Fill in fields the model might have omitted or hallucinated wrong values for
    data["project_name"] = project_name
    data["project_path"] = str(project_path)
    data["generated_at"] = generated_at
    data["source_mtime_max"] = source_mtime_max
    data.setdefault("schema_version", 1)

    try:
        return ProjectMetadata.model_validate(data)
    except Exception as exc:
        log.warning("scan: response failed schema validation for %s: %s", project_name, exc)
        return None


_FENCE_RE = re.compile(r"```(?:yaml|yml)?\s*\n(.*?)\n```", re.DOTALL)
_SCHEMA_LINE_RE = re.compile(r"^schema_version:\s*\d+\s*$", re.MULTILINE)


def _strip_code_fences(text: str) -> str:
    """Extract the yaml document from a model response.

    Order of attempts:
      1. Markdown fence containing yaml — return its inside
      2. Raw text starting with `schema_version: <int>` — return text from
         that line to end (drops any preamble like "Here's the metadata:")
      3. The text as-is — let the yaml parser surface the error
    """
    match = _FENCE_RE.search(text)
    if match:
        return match.group(1)
    schema_match = _SCHEMA_LINE_RE.search(text)
    if schema_match:
        return text[schema_match.start():]
    return text


def _format_previous(project_path: Path) -> str:
    """Render the project's previously-saved metadata (if any) for the
    refinement scan. Lets the LLM compare its own prior reasoning
    against new observations and adjust rather than recompute. Empty
    string on first scan."""
    from . import cache as meta_cache

    prev = meta_cache.load(project_path)
    if prev is None:
        return ""
    # Use yaml.safe_dump for a clean, readable rendering. Strip the
    # auto-managed timestamp/mtime fields — those are about cache
    # invalidation, not about decisions.
    data = prev.model_dump(exclude={"generated_at", "source_mtime_max", "schema_version"})
    return yaml.safe_dump(data, sort_keys=True, default_flow_style=False, allow_unicode=True)


def _format_observations(project_path: Path) -> str:
    """Render the project's accumulated runtime observations as a
    human-readable block for the scan prompt. Empty string when no
    observation file exists yet (first scan, no runs ever)."""
    from . import observations as obs_mod

    obs = obs_mod.load(project_path)
    if obs is None or obs.samples_seen == 0:
        return ""
    lines = [
        f"# Last collected: {obs.last_collected_at}",
        f"# Threads observed: {obs.samples_seen}",
    ]
    for graph_name, graph_obs in obs.graphs.items():
        if not graph_obs.nodes:
            continue
        lines.append(f"# {graph_name}:")
        # Sort nodes by p95 descending so the slowest are most prominent
        sorted_nodes = sorted(
            graph_obs.nodes.items(),
            key=lambda kv: kv[1].duration_p95,
            reverse=True,
        )
        for node_name, stats in sorted_nodes:
            lines.append(
                f"#   {node_name}: visits={stats.visits}  "
                f"p50={stats.duration_p50:.1f}s  p95={stats.duration_p95:.1f}s  "
                f"max={stats.duration_max:.1f}s"
            )
        if graph_obs.end_node_counts:
            ends = sorted(graph_obs.end_node_counts.items(), key=lambda kv: kv[1], reverse=True)
            lines.append(
                "#   empirical end-nodes: "
                + ", ".join(f"{n} ({c}x)" for n, c in ends[:6])
            )
    return "\n".join(lines)


def _format_ast(results: list[TopologyResult]) -> str:
    lines: list[str] = []
    for r in results:
        if r.error and not r.nodes:
            lines.append(f"- {r.graph_name}: ERROR — {r.error}")
            continue
        lines.append(f"- {r.graph_name}:")
        lines.append(f"    nodes: {r.nodes}")
        if r.edges:
            lines.append("    edges:")
            for src, dst in r.edges:
                lines.append(f"      - {src} -> {dst}")
    return "\n".join(lines)


def _read_readme(project_path: Path) -> str:
    for name in ("README.md", "README.rst", "README.txt", "README"):
        path = project_path / name
        if path.is_file():
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if len(text) > _MAX_README_CHARS:
                text = text[:_MAX_README_CHARS] + "\n... [truncated]"
            return text
    return ""


def _sample_thread_ids(project_path: Path, limit: int = 30) -> str:
    """Pull a handful of thread_ids from whatever checkpointer the project
    uses so Claude has real data to derive grouping rules from.

    Best-effort: returns an empty string if the project has no
    checkpointer connection, or if querying fails for any reason. Order
    is most-recent-first so Claude sees the current shape, not stale
    legacy ids.
    """
    try:
        conns = discover_all(project_path)
    except Exception:
        return ""

    rows: list[str] = []
    seen: set[str] = set()

    for sqlite_conn in conns.sqlite:
        if len(rows) >= limit:
            break
        try:
            import sqlite3

            with sqlite3.connect(f"file:{sqlite_conn.path}?mode=ro", uri=True, timeout=2.0) as db:
                cur = db.execute(
                    "SELECT DISTINCT thread_id FROM checkpoints "
                    "ORDER BY checkpoint_id DESC LIMIT ?",
                    (limit,),
                )
                for (tid,) in cur.fetchall():
                    if tid in seen:
                        continue
                    seen.add(tid)
                    rows.append(tid)
                    if len(rows) >= limit:
                        break
        except Exception:
            continue

    for pg_conn in conns.postgres:
        if len(rows) >= limit:
            break
        try:
            import psycopg

            with psycopg.connect(pg_conn.url, connect_timeout=3) as db:
                with db.cursor() as cur:
                    # GROUP BY (not DISTINCT) so ORDER BY MAX(checkpoint_id)
                    # is legal. We want most-recent threads, not arbitrary
                    # samples — older thread_id shapes may be stale.
                    cur.execute(
                        "SELECT thread_id, MAX(checkpoint_id) AS latest "
                        "FROM checkpoints GROUP BY thread_id "
                        "ORDER BY latest DESC LIMIT %s",
                        (limit,),
                    )
                    for (tid, _latest) in cur.fetchall():
                        if tid in seen:
                            continue
                        seen.add(tid)
                        rows.append(tid)
                        if len(rows) >= limit:
                            break
        except Exception:
            continue

    if not rows:
        return ""
    return "\n".join(f"  - {r}" for r in rows[:limit])


def _read_graph_sources(project_path: Path) -> str:
    """Concatenate every .py file that contains `StateGraph`, capped at the
    overall char budget."""
    chunks: list[str] = []
    total = 0
    skip = {".venv", "venv", "node_modules", ".git", "__pycache__", "site-packages"}
    for py_file in project_path.rglob("*.py"):
        if any(part in skip for part in py_file.parts):
            continue
        try:
            text = py_file.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if "StateGraph" not in text:
            continue
        rel = py_file.relative_to(project_path)
        chunks.append(f"# === {rel} ===\n{text}")
        total += len(text)
        if total >= _MAX_GRAPH_SOURCE_CHARS:
            chunks.append("# ... [remaining graph files truncated]")
            break
    return "\n\n".join(chunks)
