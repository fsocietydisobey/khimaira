"""CHIMERA MCP server — autonomous multi-model orchestration.

Loads .env automatically so API keys don't need to be in MCP config.

19 MCP tools:
  Orchestration: health, research, architect, brainstorm, classify, chain,
                 chain_pipeline, chain_refiner, swarm, chain_hypervisor,
                 status, approve, history, rewind
  Monitor:       monitor_projects, monitor_active_runs, monitor_thread_state,
                 monitor_find_stuck, monitor_topology
"""

import asyncio
import time
from pathlib import Path

from dotenv import load_dotenv
from langgraph.types import Command
from mcp.server.fastmcp import FastMCP

# Load .env from the project root
_project_root = Path(__file__).parent.parent.parent.parent
load_dotenv(_project_root / ".env")

from chimera.prompts.builder import build_prompt
from chimera.dispatch.runners import run_claude, run_gemini
from chimera.config import load_config
from chimera.config.router import Router
from chimera.graphs.components import build_components_graph
from chimera.graphs.deadcode import build_deadcode_graph
from chimera.graphs.hypervisor import build_hypervisor_graph
from chimera.graphs.pipeline import build_pipeline_graph
from chimera.graphs.refiner import build_refiner_graph
from chimera.graphs.supervisor import build_orchestrator_graph
from chimera.graphs.swarm import build_swarm_graph
from chimera.graphs.toolbuilder import build_toolbuilder_graph
from chimera.log import get_logger, setup_logging
from chimera.prompts import (
    ARCHITECT_SYSTEM_PROMPT,
    BRAINSTORM_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
)
from chimera.server.jobs import create_job, format_job_status, get_job, list_jobs, notify_job_update

log = get_logger("graph-server")

# Load config (still needed for classify model via API)
config = load_config()
router = Router(config)

# Graph and checkpointer are built lazily on first use
_server_start_time = time.time()

_orchestrator_graph = None
_checkpointer = None
_pipeline_graph = None
_pipeline_checkpointer = None
_refiner_graph = None
_refiner_checkpointer = None
_swarm_graph = None
_swarm_checkpointer = None
_hypervisor_graph = None
_hypervisor_checkpointer = None
_components_graph = None
_components_checkpointer = None
_deadcode_graph = None
_deadcode_checkpointer = None
_toolbuilder_graph = None
_toolbuilder_checkpointer = None


async def _get_graph():
    """Get or build the orchestrator graph (lazy async singleton)."""
    global _orchestrator_graph, _checkpointer
    if _orchestrator_graph is None:
        _orchestrator_graph, _checkpointer = await build_orchestrator_graph(config)
    return _orchestrator_graph


async def _get_pipeline_graph():
    """Get or build the SPR-4 graph (lazy async singleton)."""
    global _pipeline_graph, _pipeline_checkpointer
    if _pipeline_graph is None:
        _pipeline_graph, _pipeline_checkpointer = await build_pipeline_graph(config)
    return _pipeline_graph


async def _get_refiner_graph():
    """Get or build the CLR graph (lazy async singleton)."""
    global _refiner_graph, _refiner_checkpointer
    if _refiner_graph is None:
        _refiner_graph, _refiner_checkpointer = await build_refiner_graph(config)
    return _refiner_graph


async def _get_swarm_graph():
    """Get or build the PDE graph (lazy async singleton)."""
    global _swarm_graph, _swarm_checkpointer
    if _swarm_graph is None:
        _swarm_graph, _swarm_checkpointer = await build_swarm_graph(config)
    return _swarm_graph


async def _get_hypervisor_graph():
    """Get or build the HVD graph (lazy async singleton)."""
    global _hypervisor_graph, _hypervisor_checkpointer
    if _hypervisor_graph is None:
        _hypervisor_graph, _hypervisor_checkpointer = await build_hypervisor_graph(config)
    return _hypervisor_graph


async def _get_components_graph():
    """Get or build the ACL graph (lazy async singleton)."""
    global _components_graph, _components_checkpointer
    if _components_graph is None:
        _components_graph, _components_checkpointer = await build_components_graph(config)
    return _components_graph


async def _get_deadcode_graph():
    """Get or build the DCE graph (lazy async singleton)."""
    global _deadcode_graph, _deadcode_checkpointer
    if _deadcode_graph is None:
        _deadcode_graph, _deadcode_checkpointer = await build_deadcode_graph(config)
    return _deadcode_graph


async def _get_toolbuilder_graph():
    """Get or build the POB graph (lazy async singleton)."""
    global _toolbuilder_graph, _toolbuilder_checkpointer
    if _toolbuilder_graph is None:
        _toolbuilder_graph, _toolbuilder_checkpointer = await build_toolbuilder_graph(config)
    return _toolbuilder_graph


# Create the MCP server
mcp = FastMCP("chimera")


@mcp.tool()
async def health() -> str:
    """Quick health check — returns server status and uptime.

    Use to verify the MCP server is running and responsive.
    """
    uptime_s = time.time() - _server_start_time
    hours, remainder = divmod(int(uptime_s), 3600)
    minutes, seconds = divmod(remainder, 60)

    jobs = list_jobs()
    running = sum(1 for j in jobs if j.status == "running")
    paused = sum(1 for j in jobs if j.status == "paused")

    return (
        f"**Status:** healthy\n"
        f"**Server:** CHIMERA\n"
        f"**Uptime:** {hours}h {minutes}m {seconds}s\n"
        f"**Jobs running:** {running}\n"
        f"**Jobs paused:** {paused}"
    )


@mcp.tool()
async def research(question: str, context: str = "", cwd: str = "") -> str:
    """Deep research using Gemini CLI. Use for domain exploration, technology
    investigation, or understanding unknowns before planning.

    Args:
        question: What you want to research.
        context: Optional context — file contents, prior findings, etc.
        cwd: Optional project directory the spawned Gemini should run from.
            Defaults to chimera's resolved PROJECT_ROOT (which prefers the
            calling shell's PWD).
    """
    prompt = build_prompt(
        RESEARCH_SYSTEM_PROMPT,
        question,
        f"## Context\n\n{context}" if context else "",
    )
    return await run_gemini(prompt, cwd=cwd or None)


@mcp.tool()
async def architect(goal: str, context: str = "", constraints: str = "", cwd: str = "") -> str:
    """Design an implementation plan using Claude Code CLI.

    Writes `tasks/<slug>/IMPLEMENTATION.md` and `tasks/<slug>/TODO.md` into
    the target project directory and returns a short pointer.

    Args:
        goal: What you want to build or change.
        context: Optional context — relevant code, file contents, prior research.
        constraints: Optional constraints — tech stack, patterns to follow, etc.
        cwd: Optional project directory the spawned Claude should run from.
            Defaults to chimera's resolved PROJECT_ROOT (which prefers the
            calling shell's PWD).
    """
    prompt = build_prompt(
        ARCHITECT_SYSTEM_PROMPT,
        goal,
        f"## Context\n\n{context}" if context else "",
        f"## Constraints\n\n{constraints}" if constraints else "",
    )
    return await run_claude(prompt, cwd=cwd or None)


def _slugify(text: str, max_len: int = 60) -> str:
    """Lowercase kebab-case slug from arbitrary text."""
    import re

    cleaned = re.sub(r"[^a-z0-9\s-]", "", text.lower())
    slug = re.sub(r"[\s-]+", "-", cleaned).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def _save_brainstorm(topic: str, claude_out: str, base_dir: str) -> Path:
    """Write a brainstorm file under <base_dir>/brainstorms/.

    File name: <slug>-<YYYY-MM-DD>.md, with -2/-3 suffix on collision.
    Returns the absolute path written.

    Claude's output already contains both `## Divergent ideas` and
    `## Critique` sections (per the brainstorm system prompt); we just
    wrap it with a title + date header.
    """
    from datetime import date

    folder = Path(base_dir) / "brainstorms"
    folder.mkdir(parents=True, exist_ok=True)

    slug = _slugify(topic)
    today = date.today().isoformat()
    candidate = folder / f"{slug}-{today}.md"
    suffix = 2
    while candidate.exists():
        candidate = folder / f"{slug}-{today}-{suffix}.md"
        suffix += 1

    body = f"# Brainstorm — {topic}\n\n**Date:** {today}\n\n---\n\n{claude_out.strip()}\n"
    candidate.write_text(body, encoding="utf-8")
    _open_in_default_app(candidate)
    return candidate


def _open_in_default_app(path: Path) -> None:
    """Open `path` in the user's default app.

    Resolution order:
      1. CHIMERA_OPEN_CMD env var (if set) — used verbatim, path appended.
         Examples: "code", "code -r", "firefox", "xdg-open".
      2. macOS: `open <path>`.
      3. Linux: `xdg-open <path>` (requires an `xdg-mime` handler for the
         file's MIME type — markdown registration is NOT default on most
         distros; surface a clear log if it fails).

    Best-effort. Exit code is checked for ~2s after spawn; if the dispatcher
    exits non-zero in that window, log a clear warning so the user knows why
    auto-open didn't work. If the dispatcher is still running after 2s, the
    GUI app is what's running — assume success and return.
    """
    import os
    import subprocess
    import sys
    import time

    custom = os.environ.get("CHIMERA_OPEN_CMD")
    if custom:
        cmd = [*custom.split(), str(path)]
    elif sys.platform == "darwin":
        cmd = ["open", str(path)]
    else:
        cmd = ["xdg-open", str(path)]

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except (OSError, FileNotFoundError) as exc:
        log.warning("could not auto-open %s: %s", path, exc)
        return

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline:
        rc = proc.poll()
        if rc is None:
            time.sleep(0.05)
            continue
        if rc != 0:
            stderr_bytes = proc.stderr.read() if proc.stderr else b""
            stderr_text = stderr_bytes.decode(errors="replace").strip()[:200]
            log.warning(
                "auto-open failed (exit=%d): %s — %s. "
                "On Linux, register a handler with `xdg-mime default <app>.desktop text/markdown` "
                "or set CHIMERA_OPEN_CMD env var (e.g. CHIMERA_OPEN_CMD=code).",
                rc,
                " ".join(cmd),
                stderr_text or "(no stderr)",
            )
        return


@mcp.tool()
async def brainstorm(topic: str, context: str = "", cwd: str = "") -> str:
    """Single-call Claude brainstorm: divergent generation + self-critique.

    Claude produces 8–12 divergent ideas with tradeoffs, then in the same
    response runs an adversarial pass naming the weakest ideas, what's
    missing from the space, and the quietest assumption. Output is saved
    to `<cwd>/brainstorms/<slug>-<date>.md` and auto-opened.

    Used to also run a parallel Gemini prior-art survey; dropped 2026-05-06
    because Gemini was unreliable (intermittent multi-minute hangs, empty
    outputs) and the prior-art half rarely was the load-bearing piece for
    design-space brainstorms. /chimera-research stays Gemini-driven for
    genuine prior-art questions.

    Args:
        topic: What to brainstorm. Be specific — "ideas for a langgraph
            monitoring dashboard" beats "monitoring".
        context: Optional context — relevant code, constraints, prior
            findings, conversation history.
        cwd: Optional project directory. Spawned subprocess runs from
            here AND the output file lands here. Defaults to chimera's
            resolved PROJECT_ROOT.
    """
    target_cwd = cwd or config.PROJECT_ROOT

    prompt = build_prompt(
        BRAINSTORM_SYSTEM_PROMPT,
        f"## Topic\n\n{topic}",
        f"## Context\n\n{context}" if context else "",
    )

    log.info("brainstorm: topic=%s, cwd=%s", topic[:80], target_cwd)
    claude_out = await run_claude(prompt, cwd=target_cwd)

    saved = _save_brainstorm(topic, claude_out, target_cwd)
    rel = saved.relative_to(target_cwd) if str(saved).startswith(str(target_cwd)) else saved
    return f"Saved to `{rel}` ({len(claude_out)} chars)."


@mcp.tool()
async def classify(task_description: str) -> str:
    """Classify a task into a tier (research / architect / implement) and
    recommend the right pipeline. Uses a fast, cheap API model.

    Args:
        task_description: Description of the task to classify.
    """
    result = await router.classify(task_description)
    tier = result.get("tier", "unknown")
    confidence = result.get("confidence", 0)
    reasoning = result.get("reasoning", "")
    pipeline = " → ".join(result.get("pipeline", []))

    return f"**Tier:** {tier} (confidence: {confidence:.0%})\n**Pipeline:** {pipeline}\n**Reasoning:** {reasoning}"


@mcp.tool()
async def chain(task_description: str, context: str = "", thread_id: str = "") -> str:
    """Start a LangGraph pipeline in the background and return immediately.

    The pipeline runs asynchronously — use status(job_id) to check progress.
    A supervisor decides the next step at each point: research, architect,
    validate, human review, or implement.

    The pipeline PAUSES for human approval before implementation.
    When paused, use approve(job_id) to continue.

    Args:
        task_description: What you want to accomplish.
        context: Optional context — relevant code, file contents, etc.
        thread_id: Optional thread ID to continue a previous chain.
    """
    t_entry = time.time()
    log.info("chain() called — task: %s", task_description[:80])
    graph = await _get_graph()
    log.info("graph ready (%.1fs)", time.time() - t_entry)

    job = create_job(thread_id=thread_id if thread_id else None)
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state = {"task": task_description}
    if context:
        initial_state["context"] = context

    async def _run():
        try:
            node_start = time.time()
            async for update in graph.astream(initial_state, config=graph_config, stream_mode="updates"):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    node_elapsed = time.time() - node_start
                    # Skip interrupt markers — they're not state dicts
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    message = _build_progress_message(node_name, state_update)
                    job.progress.append(f"[{node_elapsed:.1f}s] {message}")
                    log.info("job %s [%.1fs]: %s", job.job_id, node_elapsed, message)
                    node_start = time.time()  # Reset for next node

            # Check if paused at human review
            state = await graph.aget_state(graph_config)
            if state and state.next and "human_review" in state.next:
                job.status = "paused"
                job.progress.append("Paused — waiting for human approval")
                log.info("job %s: paused at human_review", job.job_id)
                notify_job_update(job)
            else:
                job.status = "completed"
                job.finished_at = time.time()
                log.info("job %s: completed", job.job_id)
                notify_job_update(job)

        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            log.error("job %s: failed — %s", job.job_id, e)
            notify_job_update(job)

    job._task = asyncio.create_task(_run())

    return (
        f"**Job started:** `{job.job_id}`\n"
        f"**Thread:** `{job.thread_id}`\n\n"
        f'Use `status(job_id="{job.job_id}")` to check progress.\n\n'
        f"The pipeline will pause for your approval before implementing."
    )


@mcp.tool()
async def chain_pipeline(task_description: str, context: str = "", thread_id: str = "") -> str:
    """Start the SPR-4 pipeline (CHIMERA) in the background.

    SPR-4 runs a phased pipeline: research → planning → implementation → review.
    Each phase has a critic that loops until quality passes or max steps reached.
    The pipeline PAUSES for human approval in the review phase.

    More structured than chain() — uses phase subgraphs with critic loops,
    bounded steps, and persistent memory across runs.

    Use status(job_id) to check progress. Progress messages include [phase] tags.

    Args:
        task_description: What you want to accomplish.
        context: Optional context — relevant code, file contents, etc.
        thread_id: Optional thread ID to continue a previous chain.
    """
    t_entry = time.time()
    log.info("chain_pipeline() called — task: %s", task_description[:80])
    graph = await _get_pipeline_graph()
    log.info("SPR-4 graph ready (%.1fs)", time.time() - t_entry)

    job = create_job(thread_id=thread_id if thread_id else None)
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state: dict = {"task": task_description}
    if context:
        initial_state["context"] = context

    async def _run():
        try:
            node_start = time.time()
            async for update in graph.astream(initial_state, config=graph_config, stream_mode="updates"):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    node_elapsed = time.time() - node_start
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    message = _build_pipeline_progress_message(node_name, state_update)
                    job.progress.append(f"[{node_elapsed:.1f}s] {message}")
                    log.info("job %s [%.1fs]: %s", job.job_id, node_elapsed, message)
                    node_start = time.time()

            # Check if paused at human review
            state = await graph.aget_state(graph_config)
            if state and state.next:
                # Check for human_review pause in nested subgraph
                next_nodes = state.next
                is_paused = any("human_review" in str(n) for n in next_nodes)
                if is_paused:
                    job.status = "paused"
                    job.progress.append("Paused — waiting for human approval (SPR-4 review phase)")
                    log.info("job %s: paused at review phase", job.job_id)
                    notify_job_update(job)
                    return

            job.status = "completed"
            job.finished_at = time.time()
            log.info("job %s: SPR-4 completed", job.job_id)
            notify_job_update(job)

        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            log.error("job %s: SPR-4 failed — %s", job.job_id, e)
            notify_job_update(job)

    job._task = asyncio.create_task(_run())

    return (
        f"**SPR-4 Job started:** `{job.job_id}`\n"
        f"**Thread:** `{job.thread_id}`\n\n"
        f"Phases: research → planning → implementation → review\n"
        f'Use `status(job_id="{job.job_id}")` to check progress.\n\n'
        f"The pipeline will pause for your approval in the review phase."
    )


@mcp.tool()
async def chain_refiner(max_cycles: int = 50, budget: float = 5.0) -> str:
    """Start the CLR continuous refinement loop.

    Autonomously improves the codebase: assess health → triage → execute → validate
    → commit or revert → loop. Reads SPEC.md for feature goals. Stops on convergence,
    budget exhaustion, or spec completion.

    Args:
        max_cycles: Maximum refinement cycles before stopping (default 50).
        budget: Maximum estimated cost in USD (default 5.0).
    """
    log.info("chain_refiner() called — max_cycles=%d, budget=$%.2f", max_cycles, budget)
    graph = await _get_refiner_graph()

    job = create_job()
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state: dict = {"max_cycles": max_cycles, "task": ""}

    async def _run():
        try:
            async for update in graph.astream(initial_state, config=graph_config, stream_mode="updates"):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    cycle = state_update.get("cycle_count", "") if isinstance(state_update, dict) else ""
                    message = f"[cycle {cycle}] {node_name}: completed" if cycle else f"{node_name}: completed"
                    job.progress.append(message)

            job.status = "completed"
            job.finished_at = time.time()
            notify_job_update(job)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            notify_job_update(job)

    job._task = asyncio.create_task(_run())

    return (
        f"**CLR (refinement loop) started:** `{job.job_id}`\n"
        f"**Max cycles:** {max_cycles} | **Budget:** ${budget:.2f}\n\n"
        f'Use `status(job_id="{job.job_id}")` to check progress.'
    )


@mcp.tool()
async def swarm(goal: str, budget: float = 2.0, max_agents: int = 10) -> str:
    """Start a PDE parallel dispatch.

    The task decomposer decomposes the goal into N independent tasks and dispatches
    them concurrently. Results are merged and validated atomically.

    Best for batch operations: fix all pyright errors, add tests for 10 modules, etc.

    Args:
        goal: What to accomplish (e.g. "fix all pyright errors").
        budget: Maximum estimated cost in USD (default 2.0).
        max_agents: Maximum parallel workers (default 10).
    """
    log.info("swarm() called — goal: %s, budget=$%.2f, max_agents=%d", goal[:80], budget, max_agents)
    graph = await _get_swarm_graph()

    job = create_job()
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state: dict = {
        "task": goal,
        "swarm_budget": {"max_agents": max_agents, "max_cost_usd": budget},
    }

    async def _run():
        try:
            async for update in graph.astream(initial_state, config=graph_config, stream_mode="updates"):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    message = f"{node_name}: completed"
                    if node_name == "decomposer":
                        manifest = state_update.get("swarm_manifest", {}) if isinstance(state_update, dict) else {}
                        tasks = manifest.get("tasks", [])
                        message = f"Decomposer: decomposed into {len(tasks)} tasks"
                    elif node_name == "merge":
                        outcome = state_update.get("swarm_outcome", "?") if isinstance(state_update, dict) else "?"
                        message = f"Merge: {outcome}"
                    job.progress.append(message)

            job.status = "completed"
            job.finished_at = time.time()
            notify_job_update(job)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            notify_job_update(job)

    job._task = asyncio.create_task(_run())

    return (
        f"**PDE (parallel dispatch) started:** `{job.job_id}`\n"
        f"**Goal:** {goal}\n"
        f"**Max agents:** {max_agents} | **Budget:** ${budget:.2f}\n\n"
        f'Use `status(job_id="{job.job_id}")` to check progress.'
    )


@mcp.tool()
async def chain_hypervisor(budget: float = 10.0) -> str:
    """Start HVD — the autonomous meta-orchestrator.

    Monitors repository health, decides which pattern to spawn (CLR for refinement,
    PDE for batch fixes, SPR-4 for single tasks), enforces directives, and
    controls compute budget. The "leave it running" system.

    Args:
        budget: Maximum daily cost in USD (default 10.0).
    """
    log.info("chain_hypervisor() called — budget=$%.2f", budget)
    graph = await _get_hypervisor_graph()

    job = create_job()
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state: dict = {
        "global_budget": {"max_daily_cost_usd": budget},
        "task": "",
    }

    async def _run():
        try:
            async for update in graph.astream(initial_state, config=graph_config, stream_mode="updates"):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    cycle = state_update.get("hypervisor_cycle", "") if isinstance(state_update, dict) else ""
                    message = f"HVD [{node_name}]"
                    if cycle:
                        message += f" cycle {cycle}"
                    job.progress.append(message)

            job.status = "completed"
            job.finished_at = time.time()
            notify_job_update(job)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            notify_job_update(job)

    job._task = asyncio.create_task(_run())

    return (
        f"**HVD (meta-orchestrator) started:** `{job.job_id}`\n"
        f"**Daily budget:** ${budget:.2f}\n\n"
        f"HVD will assess, dispatch, and manage patterns autonomously.\n"
        f'Use `status(job_id="{job.job_id}")` to check progress.'
    )


@mcp.tool()
async def chain_components() -> str:
    """Run the ACL (Atomic Component Library) validation pipeline.

    Scans the codebase for component usage and violations, runs combinatorial
    validation on registered components, and enforces compliance on recent changes.

    Flow: init → scan → validate → enforce → report
    """
    log.info("chain_components() called")
    graph = await _get_components_graph()

    job = create_job()
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state: dict = {"task": "ACL validation"}

    async def _run():
        try:
            async for update in graph.astream(initial_state, config=graph_config, stream_mode="updates"):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    job.progress.append(f"ACL [{node_name}]: completed")

            job.status = "completed"
            job.finished_at = time.time()
            notify_job_update(job)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            notify_job_update(job)

    job._task = asyncio.create_task(_run())

    return (
        f"**ACL (component library) started:** `{job.job_id}`\n\n"
        f"Validates atomic components: scan → validate → enforce.\n"
        f'Use `status(job_id="{job.job_id}")` to check progress.'
    )


@mcp.tool()
async def chain_deadcode() -> str:
    """Run the DCE (Dead Code Eliminator) pipeline.

    Operates in a shadow worktree for safety. Finds dead code via static
    analysis, proposes file splits for monoliths, and deletes dead code
    in risk-ordered batches with test validation.

    Flow: create shadow → seek → shatter → reap → merge → cleanup
    """
    log.info("chain_deadcode() called")
    graph = await _get_deadcode_graph()

    job = create_job()
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state: dict = {"task": "DCE dead code elimination"}

    async def _run():
        try:
            async for update in graph.astream(initial_state, config=graph_config, stream_mode="updates"):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    job.progress.append(f"DCE [{node_name}]: completed")

            job.status = "completed"
            job.finished_at = time.time()
            notify_job_update(job)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            notify_job_update(job)

    job._task = asyncio.create_task(_run())

    return (
        f"**DCE (dead code eliminator) started:** `{job.job_id}`\n\n"
        f"Operates in shadow worktree: seek → shatter → reap → merge.\n"
        f'Use `status(job_id="{job.job_id}")` to check progress.'
    )


@mcp.tool()
async def chain_toolbuilder() -> str:
    """Run the POB (Proactive Observation Builder) pipeline.

    Observes developer behavior (shell history, git patterns, file metrics),
    identifies friction points, proposes a tool, builds it on a branch,
    and opens a PR. Never touches product code.

    Flow: watch → analyze → propose → forge → PR
    """
    log.info("chain_toolbuilder() called")
    graph = await _get_toolbuilder_graph()

    job = create_job()
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state: dict = {"task": "POB proactive tool building"}

    async def _run():
        try:
            async for update in graph.astream(initial_state, config=graph_config, stream_mode="updates"):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    job.progress.append(f"POB [{node_name}]: completed")

            job.status = "completed"
            job.finished_at = time.time()
            notify_job_update(job)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            notify_job_update(job)

    job._task = asyncio.create_task(_run())

    return (
        f"**POB (proactive tool-builder) started:** `{job.job_id}`\n\n"
        f"Observes behavior → identifies friction → builds tool → opens PR.\n"
        f'Use `status(job_id="{job.job_id}")` to check progress.'
    )


@mcp.tool()
async def status(job_id: str = "") -> str:
    """Check the status of a background pipeline job. Returns instantly.

    Without a job_id, shows all recent jobs.

    Args:
        job_id: The job ID from chain(). Empty to list all jobs.
    """
    log.info("status() called, job_id=%s", job_id or "(all)")

    if not job_id:
        jobs = list_jobs()
        if not jobs:
            return "No jobs found."
        parts = [format_job_status(j) for j in jobs]
        return "## Recent Jobs\n\n" + "\n\n---\n\n".join(parts)

    job = get_job(job_id)
    if not job:
        return f"No job found with ID `{job_id}`."

    return format_job_status(job)


@mcp.tool()
async def approve(job_id: str, feedback: str = "") -> str:
    """Approve or reject a paused pipeline job.

    Without feedback, the plan is approved and implementation proceeds.
    With feedback, the plan is rejected and the architect revises.

    Args:
        job_id: The job ID from a paused chain().
        feedback: Optional rejection feedback for the architect.
    """
    job = get_job(job_id)
    if not job:
        return f"No job found with ID `{job_id}`."
    if job.status != "paused":
        return f"Job `{job_id}` is not paused (status: {job.status})."

    log.info("approve: job %s, feedback=%s", job_id, "yes" if feedback else "none")

    graph = await _get_graph()
    graph_config = {"configurable": {"thread_id": job.thread_id}}

    if feedback:
        resume_value = {"decision": "rejected", "feedback": feedback}
    else:
        resume_value = {"decision": "approved", "feedback": ""}

    job.status = "running"
    job.progress.append(f"Resumed — {'rejected with feedback' if feedback else 'approved'}")

    async def _run_resume():
        try:
            async for update in graph.astream(
                Command(resume=resume_value),
                config=graph_config,
                stream_mode="updates",
            ):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    message = _build_progress_message(node_name, state_update)
                    job.progress.append(message)
                    log.info("job %s: %s", job.job_id, message)

            # Check if paused again
            state = await graph.aget_state(graph_config)
            if state and state.next and "human_review" in state.next:
                job.status = "paused"
                job.progress.append("Paused — waiting for human approval (again)")
                log.info("job %s: paused again at human_review", job.job_id)
                notify_job_update(job)
            else:
                job.status = "completed"
                job.finished_at = time.time()
                log.info("job %s: completed", job.job_id)
                notify_job_update(job)

        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            job.finished_at = time.time()
            log.error("job %s: failed — %s", job.job_id, e)
            notify_job_update(job)

    job._task = asyncio.create_task(_run_resume())

    decision = "rejected" if feedback else "approved"
    return f'**Job resumed:** `{job_id}` ({decision})\n\nUse `status(job_id="{job_id}")` to check progress.'


@mcp.tool()
async def history(thread_id: str, limit: int = 10) -> str:
    """Show the checkpoint history for a thread. Use this to see
    what happened at each step.

    Each checkpoint has an ID you can use with rewind() to go back.

    Args:
        thread_id: The thread ID from a previous chain() call.
        limit: Max number of checkpoints to show (default 10).
    """
    graph = await _get_graph()
    graph_config = {"configurable": {"thread_id": thread_id}}

    entries = []
    async for snapshot in graph.aget_state_history(graph_config, limit=limit):
        checkpoint_id = snapshot.config["configurable"].get("checkpoint_id", "?")
        metadata = snapshot.metadata or {}
        step = metadata.get("step", "?")
        source = metadata.get("source", "?")

        values = snapshot.values or {}
        has = [k for k in ["research_findings", "architecture_plan", "implementation_result"] if values.get(k)]

        entry = (
            f"### Step {step} (`{checkpoint_id[:12]}...`)\n"
            f"- **Source:** {source}\n"
            f"- **Has:** {', '.join(has) if has else 'empty'}\n"
            f"- **Next:** {', '.join(snapshot.next) if snapshot.next else 'END'}"
        )

        rationale = values.get("supervisor_rationale", "")
        next_node = values.get("next_node", "")
        if rationale:
            entry += f"\n- **Supervisor → {next_node}:** {rationale}"

        v_score = values.get("validation_score")
        if v_score is not None:
            entry += f"\n- **Validation score:** {v_score:.2f}"

        review_status = values.get("human_review_status", "")
        if review_status:
            entry += f"\n- **Human review:** {review_status}"

        versions = values.get("output_versions") or []
        if versions:
            version_summary: dict[str, int] = {}
            for v in versions:
                node = v.get("node", "?")
                version_summary[node] = version_summary.get(node, 0) + 1
            counts = ", ".join(f"{k}: {v}" for k, v in sorted(version_summary.items()))
            entry += f"\n- **Output versions:** {counts}"

        entries.append(entry)

    if not entries:
        return f"No history found for thread `{thread_id}`."

    return f"## History for thread `{thread_id}`\n\n" + "\n\n".join(entries)


@mcp.tool()
async def rewind(thread_id: str, checkpoint_id: str, new_task: str = "") -> str:
    """Rewind to a previous checkpoint and re-run from that point.

    Use history() to find checkpoint IDs.

    Args:
        thread_id: The thread ID.
        checkpoint_id: The checkpoint ID to rewind to.
        new_task: Optional new task description.
    """
    graph = await _get_graph()
    graph_config = {
        "configurable": {
            "thread_id": thread_id,
            "checkpoint_id": checkpoint_id,
        }
    }

    snapshot = await graph.aget_state(graph_config)
    if not snapshot or not snapshot.values:
        return f"No checkpoint found for `{checkpoint_id}` in thread `{thread_id}`."

    input_state = {}
    if new_task:
        input_state["task"] = new_task

    result = await graph.ainvoke(input_state or None, config=graph_config)

    formatted = _format_graph_result(result)
    next_nodes = ", ".join(snapshot.next) if snapshot.next else "END"
    return (
        f"## Rewound to checkpoint `{checkpoint_id[:12]}...`\n\n"
        f"**Resumed from:** {next_nodes}\n"
        f"**Thread:** `{thread_id}`\n\n"
        f"{formatted}"
    )


def _build_progress_message(node_name: str, state_update: dict) -> str:
    """Build a human-readable progress message from a node's state update."""
    if state_update is None:
        state_update = {}
    if node_name == "supervisor":
        next_node = state_update.get("next_node", "?")
        rationale = state_update.get("supervisor_rationale", "")
        parallel = state_update.get("parallel_tasks") or []
        msg = f"Supervisor → {next_node}"
        if rationale:
            msg += f": {rationale}"
        if parallel:
            topics = ", ".join(pt.get("topic", "?") for pt in parallel)
            msg += f" [fan-out: {topics}]"
        return msg

    if node_name == "validator":
        score = state_update.get("validation_score")
        feedback = state_update.get("validation_feedback", "")
        if score is not None:
            msg = f"Validator: score {score:.2f}"
            if feedback:
                msg += f" — {feedback}"
            return msg
        return "Validator: scoring output"

    if node_name == "research":
        topic = state_update.get("parallel_task_topic", "")
        if topic:
            return f"Research completed: {topic}"
        return "Research completed"

    if node_name == "architect":
        return "Architect: plan ready"

    if node_name == "implement":
        return "Implementation completed"

    if node_name == "merge_research":
        return "Merge: combining parallel research findings"

    if node_name == "human_review":
        status = state_update.get("human_review_status", "")
        if status:
            return f"Human review: {status}"
        return "Human review: waiting for approval"

    return f"{node_name}: completed"


def _build_pipeline_progress_message(node_name: str, state_update: dict) -> str:
    """Build a phase-aware progress message for SPR-4 graph updates."""
    if state_update is None:
        state_update = {}

    phase = state_update.get("phase", "")
    phase_tag = f"[{phase}] " if phase else ""

    # Phase router
    if node_name == "phase_router":
        new_phase = state_update.get("phase", "?")
        handoff = state_update.get("handoff_type", "")
        if new_phase == "done":
            return f"{phase_tag}Phase router: finishing"
        return f"Phase router → {new_phase}" + (f" (handoff={handoff})" if handoff else "")

    # Memory nodes
    if node_name == "load_memory":
        return "Loading past run context"
    if node_name == "save_memory":
        return "Saving run memory"

    # Subgraph nodes — add phase tag
    if node_name in ("research_phase", "planning_phase", "implementation_phase", "review_phase"):
        # Subgraph wrapper updates — extract inner details
        return f"{phase_tag}{node_name.replace('_', ' ').title()}: completed"

    # Inner subgraph nodes (critic, research, architect, etc.)
    if "critic" in node_name or node_name == "critic":
        score = state_update.get("validation_score")
        handoff = state_update.get("handoff_type", "")
        if score is not None:
            return f"{phase_tag}Critic: score {score:.2f} → {handoff}"
        return f"{phase_tag}Critic: evaluating"

    if node_name == "guard":
        handoff = state_update.get("handoff_type", "")
        if handoff == "plan_not_approved":
            return f"{phase_tag}Guard: blocked — plan not approved"
        return f"{phase_tag}Guard: passed"

    if node_name == "set_handoff":
        handoff = state_update.get("handoff_type", "")
        return f"{phase_tag}Review decision: {handoff}"

    # Fall back to the standard progress builder for domain nodes
    return f"{phase_tag}{_build_progress_message(node_name, state_update)}"


def _format_graph_result(state: dict) -> str:
    """Format the final graph state into a readable markdown response."""
    output_parts: list[str] = []

    history_list = state.get("history") or []
    node_calls = state.get("node_calls") or {}
    if history_list:
        journey = "\n".join(f"{i + 1}. {h}" for i, h in enumerate(history_list))
        calls = ", ".join(f"{k}: {v}" for k, v in sorted(node_calls.items()))
        output_parts.append(f"## Supervisor Journey\n\n{journey}\n\n**Node calls:** {calls}")

    review_status = state.get("human_review_status", "")
    if review_status:
        human_feedback = state.get("human_feedback", "")
        review_line = f"**Human review:** {review_status}"
        if human_feedback:
            review_line += f" — {human_feedback}"
        output_parts.append(f"## Human Review\n\n{review_line}")

    v_score = state.get("validation_score")
    if v_score is not None:
        v_feedback = state.get("validation_feedback", "")
        score_line = f"**Validation score:** {v_score:.2f}"
        if v_feedback:
            score_line += f" — {v_feedback}"
        output_parts.append(f"## Quality\n\n{score_line}")

    research_findings = state.get("research_findings", "")
    if research_findings:
        output_parts.append(f"## Research Findings\n\n{research_findings}")

    architecture_plan = state.get("architecture_plan", "")
    if architecture_plan:
        output_parts.append(f"## Architecture Plan\n\n{architecture_plan}")

    implementation_result = state.get("implementation_result", "")
    if implementation_result:
        output_parts.append(f"## Implementation\n\n{implementation_result}")

    if not output_parts:
        return "No output produced by the orchestrator graph."

    return "\n\n---\n\n".join(output_parts)


async def _cleanup():
    """Close checkpointer connections on shutdown."""
    global _checkpointer, _pipeline_checkpointer, _refiner_checkpointer, _swarm_checkpointer, _hypervisor_checkpointer
    global _components_checkpointer, _deadcode_checkpointer, _toolbuilder_checkpointer
    for name, cp in [
        ("supervisor", _checkpointer),
        ("spr4", _pipeline_checkpointer),
        ("clr", _refiner_checkpointer),
        ("pde", _swarm_checkpointer),
        ("hvd", _hypervisor_checkpointer),
        ("acl", _components_checkpointer),
        ("dce", _deadcode_checkpointer),
        ("pob", _toolbuilder_checkpointer),
    ]:
        if cp is not None:
            try:
                await cp.conn.close()
                log.info("%s checkpointer connection closed", name)
            except Exception as e:
                log.warning("%s checkpointer cleanup failed: %s", name, e)
    _checkpointer = None
    _pipeline_checkpointer = None
    _refiner_checkpointer = None
    _swarm_checkpointer = None
    _hypervisor_checkpointer = None
    _components_checkpointer = None
    _deadcode_checkpointer = None
    _toolbuilder_checkpointer = None


# ---------------------------------------------------------------------------
# Monitor tools — query the chimera-monitor daemon's REST API. Lets Claude
# inspect LangGraph runtime state directly from chat without curl
# gymnastics. Skills (debug-runtime-issue, full-stack-trace) compose
# these as their primary observability surface.
# ---------------------------------------------------------------------------

from chimera.server import monitor_tools as _monitor_tools


@mcp.tool()
async def monitor_projects() -> str:
    """List LangGraph projects the chimera-monitor daemon has discovered.

    Use to confirm what's available before asking about a specific
    project. Returns project names, paths, and detected checkpointer
    connections (Postgres URL or SQLite paths).
    """
    return await _monitor_tools.list_projects()


@mcp.tool()
async def monitor_active_runs(project: str) -> str:
    """Threads currently running, paused, or starting in a project.

    Excludes idle threads — use this to answer "what's happening
    right now?" Returns thread_id, status, current_node, step, and
    scope. Status classification accounts for HITL pauses, terminal
    nodes, per-project running thresholds, and per-node observed
    p95 latencies — trust it.

    Args:
        project: Project name as discovered by `monitor_projects()`.
    """
    return await _monitor_tools.list_active_runs(project)


@mcp.tool()
async def monitor_thread_state(project: str, thread_id: str, recent: int = 5) -> str:
    """Full state + recent N checkpoints for one thread.

    Use to investigate a specific run. Each checkpoint includes the
    full state at that step and which node fired. The trajectory
    (node-by-node sequence) is the single most informative artifact
    for debugging stuck or failing runs.

    Args:
        project: Project name.
        thread_id: Thread to inspect.
        recent: How many most-recent checkpoints to include (1-50, default 5).
    """
    return await _monitor_tools.thread_state(project, thread_id, recent)


@mcp.tool()
async def monitor_find_stuck(project: str) -> str:
    """Find threads classified as stuck (running >3× threshold) or
    stale (>1× threshold) by the monitor's heuristics.

    Use as the FIRST query when user asks "what's broken?" — surfaces
    anomalies before deep investigation of any single thread. Returns
    a prioritized list with age vs threshold for each.

    Args:
        project: Project name.
    """
    return await _monitor_tools.find_stuck(project)


@mcp.tool()
async def monitor_auto_fix(check: str, project: str = "") -> str:
    """Propose a fix for the most-recent anomaly matching `check`+`project`.

    Routes the anomaly through chimera's chain_pipeline (research →
    architect → implement → review). Saves the proposal as a markdown
    report PLUS a `gh pr create --draft` script you review and run
    manually.

    NEVER auto-merges. Two human-in-the-loop checkpoints: you decide
    whether to invoke this tool, and you review the draft PR before
    merging. If the proposal looks wrong, delete the script and
    investigate further.

    Args:
        check: The check name to find an anomaly for (e.g.
            "recent_thread_visibility", "topology_consistency").
        project: Optionally narrow to a specific project. Empty matches
            anomalies for any project.

    Returns:
        Path to the saved investigation + how to run the PR script.
    """
    from chimera.monitor import anomalies as anomalies_module
    from chimera.monitor import auto_fix

    # Find the most-recent matching anomaly
    items = anomalies_module.recent_anomalies(limit=100)
    matching = [
        it for it in items
        if it.get("check") == check
        and (not project or it.get("project") == project)
        and not it.get("passed", True)
    ]
    if not matching:
        return f"No recent failed anomaly matches check={check!r} project={project!r}."
    anomaly = matching[-1]  # most-recent in append order

    proposal = await auto_fix.propose_fix(anomaly)
    gh_status = "✓ available" if auto_fix.is_gh_available() else "✗ not installed/authenticated"

    return (
        f"**Auto-fix proposal saved.**\n\n"
        f"- Investigation: `{proposal.investigation_md}`\n"
        f"- PR script: `{proposal.pr_script}`\n"
        f"- Branch (when run): `{proposal.branch_name}`\n"
        f"- gh CLI: {gh_status}\n\n"
        f"**Next:** open the investigation, review the proposal,\n"
        f"then run the PR script if you agree:\n"
        f"```\nbash {proposal.pr_script}\n```\n"
        f"Or, if no code change was generated by the pipeline, just\n"
        f"read the investigation and decide manually."
    )


@mcp.tool()
async def monitor_anomalies(limit: int = 20, only_failures: bool = True) -> str:
    """Self-watch findings — invariant checks the chimera-monitor daemon
    runs against itself every 5 min.

    Surfaces any inconsistencies between what the dashboard shows and
    the underlying state (DB ↔ API mismatches, stale observation
    collector, topology drift). Use to verify the daemon's claims
    before trusting them, or when something on the dashboard "looks
    off" and you want to know if the daemon already noticed.

    Args:
        limit: Max entries (1-100, default 20).
        only_failures: True hides passing checks (typical use).
    """
    return await _monitor_tools.anomalies(limit, only_failures)


@mcp.tool()
async def monitor_api_routes(project: str, graph_linked_only: bool = False) -> str:
    """FastAPI routes for a project, with which routes invoke a LangGraph.

    Use to follow user actions back to their handler — the full-stack-trace
    skill calls this to bridge "user clicked X" → "this API route was hit"
    → "which fired graph Y". Output is markdown-formatted with graph-linked
    routes pinned to the top.

    Args:
        project: Project name.
        graph_linked_only: If True, hide routes that don't invoke a graph.
            Useful when the user is asking about LangGraph dispatch
            specifically.
    """
    return await _monitor_tools.api_routes(project, graph_linked_only)


@mcp.tool()
async def monitor_frontend_components(project: str, with_api_calls_only: bool = False) -> str:
    """React/Next components in a project + their API calls + state hooks.

    Companion to `monitor_api_routes` — together they let
    `full-stack-trace` walk UI click → component → API call → server
    handler → graph. Use `with_api_calls_only=True` to filter to
    components that actually hit the backend.
    """
    return await _monitor_tools.frontend_components(project, with_api_calls_only)


@mcp.tool()
async def monitor_schema_drift(project: str) -> str:
    """Compare a project's Pydantic/SQLAlchemy models against the
    actual Postgres schema. Returns drift reports — extra fields in
    code, extra columns in DB, type mismatches.

    Surfaces the class of bug where the model says X but the table has
    Y, leading to silent ValidationError or missing data. Run this
    after schema changes or when debugging "why is this field empty?"
    """
    return await _monitor_tools.schema_drift(project)


@mcp.tool()
async def monitor_heartbeat() -> str:
    """Liveness check for the chimera-monitor daemon's self-watch loop.

    Returns the timestamp of the last self-watch pass + healthy/stale.
    Stale (>10min) means the daemon is up but the self-watch task is
    stuck — observability of observability.
    """
    return await _monitor_tools.heartbeat()


@mcp.tool()
async def monitor_topology(project: str) -> str:
    """Compiled-graph topology for a project: graph names + node counts.

    Use when you need to understand what graphs exist and how they
    relate, before drilling into a specific thread's path. Output
    includes inter-graph 'invokes' relationships.

    Args:
        project: Project name.
    """
    return await _monitor_tools.topology(project)


def main():
    """Entry point — run the MCP server over stdio."""
    import atexit

    from chimera.dispatch.runners import kill_all_subprocesses
    from chimera.pidlock import acquire_lock

    setup_logging()
    acquire_lock("graph")
    log.info("chimera starting starting")
    atexit.register(kill_all_subprocesses)
    mcp.run()


if __name__ == "__main__":
    main()
