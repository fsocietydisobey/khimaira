"""KHIMAIRA MCP server — autonomous multi-model orchestration.

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

from khimaira.prompts.builder import build_prompt
from khimaira.dispatch.runners import run_claude, run_gemini
from khimaira.config import load_config
from khimaira.config.router import Router
from khimaira.graphs.components import build_components_graph
from khimaira.graphs.deadcode import build_deadcode_graph
from khimaira.graphs.hypervisor import build_hypervisor_graph
from khimaira.graphs.pipeline import build_pipeline_graph
from khimaira.graphs.refiner import build_refiner_graph
from khimaira.graphs.supervisor import build_orchestrator_graph
from khimaira.graphs.swarm import build_swarm_graph
from khimaira.graphs.toolbuilder import build_toolbuilder_graph
from khimaira.log import get_logger, setup_logging
from khimaira.monitor.mcp_calls import logged_tool
from khimaira.prompts import (
    ARCHITECT_SYSTEM_PROMPT,
    BRAINSTORM_SYSTEM_PROMPT,
    RESEARCH_SYSTEM_PROMPT,
)
from khimaira.server.jobs import (
    create_job,
    format_job_status,
    get_job,
    list_jobs,
    notify_job_update,
)

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
mcp = FastMCP("khimaira")


@mcp.tool()
@logged_tool("health")
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
        f"**Server:** KHIMAIRA\n"
        f"**Uptime:** {hours}h {minutes}m {seconds}s\n"
        f"**Jobs running:** {running}\n"
        f"**Jobs paused:** {paused}"
    )


@mcp.tool()
@logged_tool("research")
async def research(question: str, context: str = "", cwd: str = "") -> str:
    """Deep research using Gemini CLI. Use for domain exploration, technology
    investigation, or understanding unknowns before planning.

    Args:
        question: What you want to research.
        context: Optional context — file contents, prior findings, etc.
        cwd: Optional project directory the spawned Gemini should run from.
            Defaults to khimaira's resolved PROJECT_ROOT (which prefers the
            calling shell's PWD).
    """
    prompt = build_prompt(
        RESEARCH_SYSTEM_PROMPT,
        question,
        f"## Context\n\n{context}" if context else "",
    )
    return await run_gemini(prompt, cwd=cwd or None)


@mcp.tool()
@logged_tool("architect")
async def architect(goal: str, context: str = "", constraints: str = "", cwd: str = "") -> str:
    """Design an implementation plan using Claude Code CLI.

    Writes `tasks/<slug>/IMPLEMENTATION.md` and `tasks/<slug>/TODO.md` into
    the target project directory and returns a short pointer.

    Args:
        goal: What you want to build or change.
        context: Optional context — relevant code, file contents, prior research.
        constraints: Optional constraints — tech stack, patterns to follow, etc.
        cwd: Optional project directory the spawned Claude should run from.
            Defaults to khimaira's resolved PROJECT_ROOT (which prefers the
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
      1. KHIMAIRA_OPEN_CMD env var (if set) — used verbatim, path appended.
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

    custom = os.environ.get("KHIMAIRA_OPEN_CMD")
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
                "or set KHIMAIRA_OPEN_CMD env var (e.g. KHIMAIRA_OPEN_CMD=code).",
                rc,
                " ".join(cmd),
                stderr_text or "(no stderr)",
            )
        return


@mcp.tool()
@logged_tool("brainstorm")
async def brainstorm(topic: str, context: str = "", cwd: str = "") -> str:
    """Single-call Claude brainstorm: divergent generation + self-critique.

    Claude produces 8–12 divergent ideas with tradeoffs, then in the same
    response runs an adversarial pass naming the weakest ideas, what's
    missing from the space, and the quietest assumption. Output is saved
    to `<cwd>/brainstorms/<slug>-<date>.md` and auto-opened.

    Used to also run a parallel Gemini prior-art survey; dropped 2026-05-06
    because Gemini was unreliable (intermittent multi-minute hangs, empty
    outputs) and the prior-art half rarely was the load-bearing piece for
    design-space brainstorms. /khimaira-research stays Gemini-driven for
    genuine prior-art questions.

    Args:
        topic: What to brainstorm. Be specific — "ideas for a langgraph
            monitoring dashboard" beats "monitoring".
        context: Optional context — relevant code, constraints, prior
            findings, conversation history.
        cwd: Optional project directory. Spawned subprocess runs from
            here AND the output file lands here. Defaults to khimaira's
            resolved PROJECT_ROOT.
    """
    # Resolve target cwd — prefer explicit arg, fall back to env or process cwd.
    # (Legacy code referenced `config.PROJECT_ROOT` which doesn't exist on the
    # new OrchestratorConfig schema.)
    import os as _os

    target_cwd = cwd or _os.environ.get("PROJECT_ROOT") or _os.environ.get("PWD") or _os.getcwd()

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
@logged_tool("classify")
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
@logged_tool("delegate")
async def delegate(
    prompt: str,
    tier: str = "auto",
    timeout_s: int = 120,
) -> str:
    """**Lower-token-cost answer.** Delegate a prompt to the cheapest
    competent model and return its answer text.

    Use when the user's question (or a sub-question you're about to
    think about) is trivial-to-moderate: factual lookup, syntax check,
    one-line refactor, "what does this function do?", "is this safe?",
    boilerplate generation. Avoids burning Opus thinking budget on
    work a haiku-class model handles fine.

    Use `tier="auto"` (default) to let khimaira's classifier pick.
    Override with an explicit tier when you know the question's shape:

      - `tier="haiku"` / `tier="flash"` — cheap and fast, factual/simple
      - `tier="sonnet"` / `tier="gemini-pro"` — mid-complexity reasoning
      - `tier="local"` — Ollama, free, slowest

    The auto path runs a fast classifier (~50ms via haiku) then routes.
    For repeat calls on similar questions, prefer the explicit tier —
    skips the classifier hop.

    Args:
        prompt: the prompt to delegate. Pass the full question + any
            context the cheap model needs; it has no memory of YOUR
            conversation.
        tier: "auto" | "haiku" | "flash" | "sonnet" | "gemini-pro" | "local".
        timeout_s: max wall time. Default 120s; raise for long generations.
    """
    return await _delegate_impl(prompt, tier=tier, timeout_s=timeout_s)


@mcp.tool()
@logged_tool("auto")
async def auto(prompt: str, timeout_s: int = 120) -> str:
    """**Auto-routed answer.** Same as `delegate(tier="auto")` — picks the
    cheapest competent model from your enabled-for-auto pool, runs the
    prompt, returns the answer.

    Prefer this over `delegate` when you don't care about picking the
    tier yourself. The classifier + pool router decide based on:
      - what the prompt looks like (factual lookup vs code task vs deep
        reasoning)
      - what models you've enabled in `~/.khimaira/models.yaml`
      - cost per million tokens for each eligible model

    Cheapest model that covers required capabilities wins. The audit
    trail (pool size, top 2 candidates, rejected reasons) is logged so
    `khimaira usage savings --audit` can show post-hoc whether routing
    is doing the right thing.

    Args:
        prompt: the prompt to delegate.
        timeout_s: max wall time. Default 120s.
    """
    return await _delegate_impl(prompt, tier="auto", timeout_s=timeout_s)


# Shared implementation behind `delegate` + `auto`. Kept as a private
# helper so both MCP tools route through one code path — keeps usage
# tracking, audit logging, and error envelopes consistent.
async def _delegate_impl(prompt: str, *, tier: str, timeout_s: int) -> str:
    from khimaira.dispatch.classifier import classify_task
    from khimaira.dispatch.pool_router import select_from_pool
    from khimaira.dispatch.runners import get_runner
    from khimaira.usage import get_recorder, runner_to_provider

    # tier="auto" → classifier + pool router (cheapest competent model
    # from the registry's auto pool). Otherwise: explicit tier shortcut
    # bypasses the pool router with a hard-coded model mapping.
    if tier == "auto":
        classification = await classify_task(prompt)
        decision = select_from_pool(classification)
        if decision.refused or decision.chosen is None:
            return f"❌ auto routing refused: {decision.refusal_reason}"
        chosen_runner = decision.chosen.runner
        chosen_model = decision.chosen.id
        mode = "auto"
        # Surface the audit trail in logs (single line so khimaira.log can
        # be grepped post-hoc). The dispatch decision log lives at
        # ~/.local/state/khimaira/khimaira.log — `khimaira usage savings
        # --audit` reads it back.
        log.info(
            "auto-route audit: model=%s runner=%s confidence=%.2f pool=%d/avail=%d/elig=%d top2=%s rejected=%s",
            chosen_model,
            chosen_runner,
            decision.classifier_confidence,
            decision.pool_size,
            decision.available_size,
            decision.eligible_size,
            decision.top_2,
            decision.rejected,
        )
    else:
        # Explicit-tier shortcut. Stable across routing-table revisions
        # so a user's tier hint yields the same dispatch regardless of
        # which CLI is installed (caller's choice; no fallback).
        tier_map = {
            "haiku": ("claude", "claude-haiku-4-5"),
            "flash": ("gemini", "gemini-2.5-flash"),
            "sonnet": ("claude", "claude-sonnet-4-6"),
            "gemini-pro": ("gemini", "gemini-2.5-pro"),
            "local": ("ollama", ""),
        }
        if tier not in tier_map:
            return f"❌ unknown tier {tier!r}. Valid: auto, {', '.join(sorted(tier_map))}."
        chosen_runner, chosen_model = tier_map[tier]
        mode = "explicit-tier"

    runner = get_runner(chosen_runner)
    if not runner.is_available():
        return (
            f"❌ runner {chosen_runner!r} not available on this machine. "
            f"Install Claude Code / Codex / Gemini / Ollama, or change tier."
        )

    try:
        result = await runner.run(
            prompt,
            model=chosen_model or None,
            timeout=timeout_s,
        )
    except Exception as e:
        return f"❌ delegate dispatch failed: {e}"

    # Record usage with the correct mode so `khimaira usage savings` can
    # attribute savings only to auto-mode calls.
    try:
        await get_recorder().record(
            runner=chosen_runner,
            provider=runner_to_provider(chosen_runner),
            model=result.model or chosen_model,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            latency_s=result.latency_s,
            role="delegate",
            source="cli",
            mode=mode,
        )
    except Exception as exc:  # noqa: BLE001 — usage tracking must never break dispatch
        log.warning("delegate: usage recording failed: %s", exc)

    out = result.text.strip()
    header = (
        f"_(via {chosen_runner}/{result.model or chosen_model} · "
        f"{result.input_tokens}→{result.output_tokens} tokens · "
        f"{result.latency_s:.1f}s · mode={mode})_\n\n"
    )
    return header + out


@mcp.tool()
@logged_tool("chain")
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
            async for update in graph.astream(
                initial_state, config=graph_config, stream_mode="updates"
            ):
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
@logged_tool("chain_pipeline")
async def chain_pipeline(task_description: str, context: str = "", thread_id: str = "") -> str:
    """Start the SPR-4 pipeline (KHIMAIRA) in the background.

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
            async for update in graph.astream(
                initial_state, config=graph_config, stream_mode="updates"
            ):
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
@logged_tool("chain_refiner")
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
            async for update in graph.astream(
                initial_state, config=graph_config, stream_mode="updates"
            ):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    cycle = (
                        state_update.get("cycle_count", "")
                        if isinstance(state_update, dict)
                        else ""
                    )
                    message = (
                        f"[cycle {cycle}] {node_name}: completed"
                        if cycle
                        else f"{node_name}: completed"
                    )
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
@logged_tool("swarm")
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
    log.info(
        "swarm() called — goal: %s, budget=$%.2f, max_agents=%d",
        goal[:80],
        budget,
        max_agents,
    )
    graph = await _get_swarm_graph()

    job = create_job()
    graph_config = {"configurable": {"thread_id": job.thread_id}}
    initial_state: dict = {
        "task": goal,
        "swarm_budget": {"max_agents": max_agents, "max_cost_usd": budget},
    }

    async def _run():
        try:
            async for update in graph.astream(
                initial_state, config=graph_config, stream_mode="updates"
            ):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    message = f"{node_name}: completed"
                    if node_name == "decomposer":
                        manifest = (
                            state_update.get("swarm_manifest", {})
                            if isinstance(state_update, dict)
                            else {}
                        )
                        tasks = manifest.get("tasks", [])
                        message = f"Decomposer: decomposed into {len(tasks)} tasks"
                    elif node_name == "merge":
                        outcome = (
                            state_update.get("swarm_outcome", "?")
                            if isinstance(state_update, dict)
                            else "?"
                        )
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
@logged_tool("chain_hypervisor")
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
            async for update in graph.astream(
                initial_state, config=graph_config, stream_mode="updates"
            ):
                if update is None:
                    continue
                for node_name, state_update in update.items():
                    if node_name == "__interrupt__":
                        continue
                    if isinstance(state_update, dict):
                        job.result.update(state_update)
                    cycle = (
                        state_update.get("hypervisor_cycle", "")
                        if isinstance(state_update, dict)
                        else ""
                    )
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
@logged_tool("chain_components")
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
            async for update in graph.astream(
                initial_state, config=graph_config, stream_mode="updates"
            ):
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
@logged_tool("chain_deadcode")
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
            async for update in graph.astream(
                initial_state, config=graph_config, stream_mode="updates"
            ):
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
@logged_tool("chain_toolbuilder")
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
            async for update in graph.astream(
                initial_state, config=graph_config, stream_mode="updates"
            ):
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
@logged_tool("status")
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
@logged_tool("approve")
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
@logged_tool("history")
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
        has = [
            k
            for k in ["research_findings", "architecture_plan", "implementation_result"]
            if values.get(k)
        ]

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
@logged_tool("rewind")
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
    if node_name in (
        "research_phase",
        "planning_phase",
        "implementation_phase",
        "review_phase",
    ):
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
    global \
        _checkpointer, \
        _pipeline_checkpointer, \
        _refiner_checkpointer, \
        _swarm_checkpointer, \
        _hypervisor_checkpointer
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
# Monitor tools — query the khimaira-monitor daemon's REST API. Lets Claude
# inspect LangGraph runtime state directly from chat without curl
# gymnastics. Skills (debug-runtime-issue, full-stack-trace) compose
# these as their primary observability surface.
# ---------------------------------------------------------------------------

from khimaira.server import monitor_tools as _monitor_tools


@mcp.tool()
@logged_tool("monitor_projects")
async def monitor_projects() -> str:
    """List LangGraph projects the khimaira-monitor daemon has discovered.

    Use to confirm what's available before asking about a specific
    project. Returns project names, paths, and detected checkpointer
    connections (Postgres URL or SQLite paths).
    """
    return await _monitor_tools.list_projects()


@mcp.tool()
@logged_tool("monitor_active_runs")
async def monitor_active_runs(project: str) -> str:
    """Threads currently running, paused, or starting in a project.

    Excludes idle threads — use this to answer "what's happening
    right now?" Returns thread_id, status, current_node, step, and
    scope. Status classification accounts for HITL pauses, terminal
    nodes, per-project running thresholds, and per-node observed
    p95 latencies — trust it.

    🛑 SNAPSHOT, not a watcher. If you're about to `sleep` and call
    this again, STOP — use `wait_for_run(project, thread_id)` instead.
    It blocks server-side until the run is done (or hits your target
    node / status) and returns in ONE MCP call. Don't burn cache and
    tool budget polling.

    Args:
        project: Project name as discovered by `monitor_projects()`.
    """
    return await _monitor_tools.list_active_runs(project)


@mcp.tool()
@logged_tool("monitor_thread_state")
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
@logged_tool("wait_for_run")
async def wait_for_run(
    project: str,
    thread_id: str,
    until_status: str | None = None,
    until_node: str | None = None,
    timeout_s: float = 300.0,
) -> str:
    """**Blocking call — wait for a LangGraph run to reach a target state.**

    ⚠️ Use this INSTEAD of `sleep + monitor_active_runs` polling. The
    daemon does the polling server-side and returns ONE response when
    your condition is met. Single MCP roundtrip replaces N polls.

    Common patterns:
      - "wait until this ingest run finishes":
          wait_for_run("jeevy_portal", "deliverable:abc123:i")
      - "wait until the run reaches the vectorize node":
          wait_for_run("jeevy_portal", "deliverable:abc:i", until_node="vectorize")
      - "wait until run goes paused (HITL gate)":
          wait_for_run("khimaira", "thread-x", until_status="paused")

    Default exit: any non-in-flight status (idle / paused / terminal).
    Override with until_status or until_node for intermediate stops.

    Args:
        project: project name (e.g. "jeevy_portal", "khimaira").
        thread_id: thread/run id to watch. Find it via `monitor_active_runs`.
        until_status: target status ("idle", "paused", "running"). None
            (default) = first non-in-flight status (the natural "done").
        until_node: optional. Returns when the run reaches this node.
        timeout_s: max wall time. Default 300s. Returns reason=timeout
            if the run is still in-flight; call again to keep waiting.
    """
    return await _monitor_tools.wait_for_run(
        project, thread_id, until_status, until_node, timeout_s
    )


@mcp.tool()
@logged_tool("monitor_find_stuck")
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
@logged_tool("monitor_auto_fix")
async def monitor_auto_fix(check: str, project: str = "") -> str:
    """Propose a fix for the most-recent anomaly matching `check`+`project`.

    Routes the anomaly through khimaira's chain_pipeline (research →
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
    from khimaira.monitor import anomalies as anomalies_module
    from khimaira.monitor import auto_fix

    # Find the most-recent matching anomaly
    items = anomalies_module.recent_anomalies(limit=100)
    matching = [
        it
        for it in items
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
@logged_tool("monitor_anomalies")
async def monitor_anomalies(limit: int = 20, only_failures: bool = True) -> str:
    """Self-watch findings — invariant checks the khimaira-monitor daemon
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
@logged_tool("monitor_api_routes")
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
@logged_tool("monitor_frontend_components")
async def monitor_frontend_components(project: str, with_api_calls_only: bool = False) -> str:
    """React/Next components in a project + their API calls + state hooks.

    Companion to `monitor_api_routes` — together they let
    `full-stack-trace` walk UI click → component → API call → server
    handler → graph. Use `with_api_calls_only=True` to filter to
    components that actually hit the backend.
    """
    return await _monitor_tools.frontend_components(project, with_api_calls_only)


@mcp.tool()
@logged_tool("monitor_schema_drift")
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
@logged_tool("monitor_heartbeat")
async def monitor_heartbeat() -> str:
    """Liveness check for the khimaira-monitor daemon's self-watch loop.

    Returns the timestamp of the last self-watch pass + healthy/stale.
    Stale (>10min) means the daemon is up but the self-watch task is
    stuck — observability of observability.
    """
    return await _monitor_tools.heartbeat()


@mcp.tool()
@logged_tool("monitor_topology")
async def monitor_topology(project: str) -> str:
    """Compiled-graph topology for a project: graph names + node counts.

    Use when you need to understand what graphs exist and how they
    relate, before drilling into a specific thread's path. Output
    includes inter-graph 'invokes' relationships.

    Args:
        project: Project name.
    """
    return await _monitor_tools.topology(project)


# ---------------------------------------------------------------------------
# Process observability — replaces polling with single blocking calls.
# ---------------------------------------------------------------------------


@mcp.tool()
@logged_tool("spawn_process")
async def spawn_process(
    cmd: list[str],
    label: str,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    replace_existing: bool = False,
) -> str:
    """Start a tracked subprocess (test runner, dev server, build, migration).

    Process runs in the khimaira-monitor daemon — survives this MCP session.
    Use `wait_for_process` to block until it finishes (single MCP call
    instead of polling), or `follow_process` for a snapshot of output.

    Args:
        cmd: argv list — first element is the executable.
        label: short identifier ('npm-test', 'dev-server', 'vite-build') —
            used for subsequent wait/follow/kill calls.
        cwd: working directory; default = daemon's cwd (khimaira repo root).
        env: extra env vars merged with the daemon's environment.
        replace_existing: kill any existing process with this label before spawning.
    """
    return await _monitor_tools.spawn_process(cmd, label, cwd, env, replace_existing)


@mcp.tool()
@logged_tool("wait_for_process")
async def wait_for_process(
    label: str,
    completion_signal: str | None = None,
    timeout_s: float = 300.0,
) -> str:
    """Block until a tracked process completes, or until a regex matches output.

    **This replaces the agent-polling pattern.** Instead of repeated
    `cat <log>` calls every few seconds, make one call to this tool — it
    blocks server-side and returns when the process is done OR a completion
    signal is detected. ONE roundtrip instead of dozens.

    Examples:
        wait_for_process("npm-test", completion_signal=r"\\d+ passed|\\d+ failed", timeout_s=300)
        wait_for_process("dev-server", completion_signal=r"Local: http", timeout_s=30)
        wait_for_process("vite-build", completion_signal=r"built in", timeout_s=120)

    Returns: stdout, stderr, exit code, reason ('signal_match'/'exit'/'timeout'),
    duration. Process keeps running if reason='signal_match' (you matched on
    output but the process hasn't exited yet) — useful for "wait until ready
    then keep going" patterns like dev servers.

    Args:
        label: process label from `spawn_process`.
        completion_signal: optional regex; returns when matched in stdout/stderr.
        timeout_s: max wait time (1-3600 seconds).
    """
    return await _monitor_tools.wait_for_process(label, completion_signal, timeout_s)


@mcp.tool()
@logged_tool("follow_process")
async def follow_process(label: str, max_chunks: int = 100) -> str:
    """Snapshot of a tracked process's current output. Non-blocking.

    Use this when you want to peek at a long-running process without
    waiting for it to finish. For "wait until done" semantics, use
    `wait_for_process` (single blocking call replaces polling).

    Args:
        label: process label from `spawn_process`.
        max_chunks: cap on returned output chunks (default 100).
    """
    return await _monitor_tools.follow_process(label, max_chunks)


@mcp.tool()
@logged_tool("list_processes")
async def list_processes() -> str:
    """List all tracked subprocesses — running + recently-finished."""
    return await _monitor_tools.list_processes()


@mcp.tool()
@logged_tool("kill_process")
async def kill_process(label: str) -> str:
    """Stop a tracked process (SIGTERM, then SIGKILL after 5s grace).

    Args:
        label: process label from `spawn_process`.
    """
    return await _monitor_tools.kill_process(label)


# ---------------------------------------------------------------------------
# Multi-session shared state — externalize Claude Code session context so
# parallel sessions can see what each other are doing without interrupting.
# ---------------------------------------------------------------------------


@mcp.tool()
@logged_tool("session_log_decision")
async def session_log_decision(session_id: str, text: str, why: str = "") -> str:
    """Record a decision the agent has made.

    Surfaces to other sessions via `session_state(session_id)`. Call
    this when you commit to an architectural choice / approach so
    parallel sessions see what you've decided without needing access
    to your conversation transcript.

    Args:
        session_id: this session's id.
        text: the decision in 1-2 sentences.
        why: optional rationale.
    """
    return await _monitor_tools.session_log_decision(session_id, text, why)


@mcp.tool()
@logged_tool("session_log_touch")
async def session_log_touch(
    session_id: str,
    file: str,
    summary: str = "",
    line_start: int | None = None,
    line_end: int | None = None,
) -> str:
    """Record a file modification.

    Typically called automatically by a PostToolUse hook on Edit/Write/MultiEdit
    — the file-touch log builds itself with zero agent burden. Manual calls
    are useful when you've made changes outside those tools.

    Args:
        session_id: this session's id.
        file: absolute or project-relative path.
        summary: short description ("refactored auth gate", "added type hints").
        line_start, line_end: optional line range.
    """
    return await _monitor_tools.session_log_touch(session_id, file, summary, line_start, line_end)


@mcp.tool()
@logged_tool("session_log_question")
async def session_log_question(
    session_id: str,
    text: str,
    target_session_id: str | None = None,
    cross_workspace: bool = False,
) -> str:
    """Open a question for parallel sessions to answer.

    **DEFAULT TO ACTION, NOT DELEGATION.** Before logging a question,
    ask yourself: "Can I decide and proceed?" If yes, do that. Cross-
    session ping-pong (asking, waiting, asking again) is usually worse
    than making a reasonable call and moving forward. Log a question
    ONLY when:
      • Another session owns the relevant code/domain and you'd
        otherwise duplicate or conflict with their in-flight work
      • You're genuinely blocked on a fact you can't determine yourself
      • A decision needs the user's input and they've routed it to a
        specific other session

    Bad uses (these should be decisions, not questions):
      ✗ "Should I use approach A or B?" — pick one, document why,
        proceed. Reverse if wrong.
      ✗ "Want me to do X or Y?" routed to a peer session — peer can't
        consent for the user; either ask the user directly or just pick.
      ✗ "Confirming you're OK with my plan" — if your plan is sound,
        stop checking; ship it.

    Returns the question id — that's the handle other sessions use in
    `session_post_answer`.

    If `target_session_id` is provided, the question is **targeted** at
    that session — it will surface in their UserPromptSubmit hook context
    on their next turn (via the /incoming endpoint), without requiring
    them to poll session_state. Accepts a UUID or a friendly name.

    If `target_session_id` is None, the question is **broadcast** —
    visible only to sessions that explicitly read this session's
    session_state.

    Args:
        session_id: this session's id.
        text: the question. Be specific — vague questions get vague answers.
        target_session_id: optional — UUID or name of the session this
            question is directed at. None = broadcast.
        cross_workspace: by default, targeted questions across workspace
            boundaries are rejected. Pass True to override when you
            intentionally want to reach a sister project's session.
    """
    return await _monitor_tools.session_log_question(
        session_id,
        text,
        target_session_id=target_session_id,
        cross_workspace=cross_workspace,
    )


@mcp.tool()
@logged_tool("session_search_archive")
async def session_search_archive(
    session_id: str,
    query: str | None = None,
    limit: int = 50,
) -> str:
    """Search archived (already-read) inbox notes by substring.

    When inbox notes get acked / drained / auto-expire, they move from
    inbox.jsonl to archive.jsonl. This tool exposes the history so you
    can query "what did khimaira-builder say about Roboflow last week"
    without losing context to past inbox drains.

    Args:
        session_id: this session's id (whose archive to search).
        query: case-insensitive substring match against note bodies and
            question_text. None returns all archived notes (most-recent
            first).
        limit: cap result set (default 50).
    """
    return await _monitor_tools.session_search_archive(session_id, query, limit)


@mcp.tool()
@logged_tool("session_ack_notes")
async def session_ack_notes(
    session_id: str,
    note_ids: list[str] | None = None,
) -> str:
    """**Acknowledge inbox notes** after surfacing their content to the user.

    The UserPromptSubmit hook re-surfaces unread inbox notes every turn
    (with a 3-surface auto-expire safety net) until you ack. The pattern:

      1. Hook injects `📬 khimaira inbox` into your context
      2. You surface the notice content to the user in your response
      3. You call session_ack_notes(session_id) to clear the unread flag
      4. Next turn, that same notice doesn't reappear

    Without step 3, the notice keeps surfacing for up to 3 turns total
    before auto-expiring — costs context. With step 3, it clears
    immediately.

    Args:
        session_id: this session's id.
        note_ids: list of note ids to ack. If None (default), acks ALL
            currently-unread notes — usually what you want after
            surfacing the contents of a 📬 khimaira inbox block.
    """
    return await _monitor_tools.session_ack_notes(session_id, note_ids)


@mcp.tool()
@logged_tool("session_query_transcript")
async def session_query_transcript(
    session_id: str,
    query: str,
    context_lines: int = 1,
    max_matches: int = 20,
) -> str:
    """**Read what a stopped session discussed.** Greps the on-disk
    Claude Code transcript for `query` (case-insensitive substring),
    returns matched turns with surrounding context.

    Use case: a future session opens, needs to know what a now-stopped
    session said about a specific topic. The agent's brain is gone but
    its conversation log is on disk — search it.

    Args:
        session_id: the session to search.
        query: substring to find (case-insensitive).
        context_lines: turns to include before+after each match (1 default).
        max_matches: cap result set (20 default).
    """
    return await _monitor_tools.session_query_transcript(
        session_id,
        query,
        context_lines,
        max_matches,
    )


@mcp.tool()
@logged_tool("session_summarize_transcript")
async def session_summarize_transcript(
    session_id: str,
    focus: str | None = None,
) -> str:
    """**Heuristic summary of a stopped session's transcript** — no LLM call.

    Returns: turn counts, top tools used, file paths touched, recent
    user prompts, recent assistant message intros. The calling agent
    reconstructs context by reading these signals; no LLM tokens spent
    on summarization.

    Use this as the FIRST move when picking up a stopped session's
    work — get the lay of the land cheaply, then drill into specifics
    with session_query_transcript.

    Args:
        session_id: the session to summarize.
        focus: optional substring; when set, also runs query_transcript
            and embeds the matches in the response.
    """
    return await _monitor_tools.session_summarize_transcript(session_id, focus)


@mcp.tool()
@logged_tool("session_subscribe_handoff")
async def session_subscribe_handoff(handoff_id: str, session_id: str) -> str:
    """**Collaboration: subscribe to a handoff already claimed by another session.**

    When your SessionStart hook shows a 👀 "ALREADY-CLAIMED" handoff,
    subscribing means: every time the owner logs a decision via
    session_log_decision, you receive a notice in your inbox
    automatically. Lets multiple sessions collaborate on the same
    work — one is primary, others observe + offer support.

    Args:
        handoff_id: 12-char hex id of the handoff to subscribe to.
        session_id: your session's id.
    """
    return await _monitor_tools.session_subscribe_handoff(handoff_id, session_id)


@mcp.tool()
@logged_tool("session_unsubscribe_handoff")
async def session_unsubscribe_handoff(handoff_id: str, session_id: str) -> str:
    """Stop receiving owner's progress updates for a handoff."""
    return await _monitor_tools.session_unsubscribe_handoff(handoff_id, session_id)


@mcp.tool()
@logged_tool("session_release_handoff")
async def session_release_handoff(handoff_id: str, session_id: str) -> str:
    """**Owner steps aside.** Next session in scope becomes the new owner.

    Use when you've finished your part, realized this isn't your lane,
    or want to pass the baton to a fresh session. Subscribers stay
    subscribed.
    """
    return await _monitor_tools.session_release_handoff(handoff_id, session_id)


@mcp.tool()
@logged_tool("session_invite_handoff")
async def session_invite_handoff(
    parent_handoff_id: str,
    owner_session_id: str,
    invitee_session_id: str,
    text: str,
    expires_in_hours: float = 168.0,
) -> str:
    """**Delegate a slice of a handoff to a specific session.**

    Layer 3 of the collaboration model — once you own a handoff, you can
    split off subtasks and invite sibling sessions to take them. The
    invite is a child handoff targeting that session: only the invitee
    can consume it (cwd-scoped peers skip it), and they get both an
    inbox notice (immediate, if live) and a SessionStart-hook surface
    (on next boot).

    Distinct from session_post_handoff (cwd-broadcast, anyone picks up)
    and session_post_notice (one-shot FYI, no directive framing).

    Args:
        parent_handoff_id: 12-char id of the handoff you currently own.
        owner_session_id: your session id — must match parent's owner.
        invitee_session_id: session you're delegating to (UUID or name).
        text: invite body — what specifically you want them to do.
        expires_in_hours: TTL. Default 168 (7 days).
    """
    return await _monitor_tools.session_invite_handoff(
        parent_handoff_id,
        owner_session_id,
        invitee_session_id,
        text,
        expires_in_hours,
    )


@mcp.tool()
@logged_tool("session_consume_handoffs")
async def session_consume_handoffs(session_id: str, cwd: str) -> str:
    """**Pull cwd-scoped handoffs mid-session.** SessionStart's
    auto-surfacing only fires at boot, so handoffs posted to your
    project AFTER you started can't reach you without this call.

    Same semantics as SessionStart: auto-claims ownership (first
    consumer becomes owner), surfaces full directive framing for
    owned handoffs, observer view for ones another session already
    claimed. Idempotent — re-calling returns nothing new for handoffs
    you've already consumed.

    Args:
        session_id: this session's id.
        cwd: project working directory to scope by. Pass the dir
            where your work lives, not the khimaira install path —
            handoffs are scoped to the project the prior session
            was working in.
    """
    return await _monitor_tools.session_consume_handoffs(session_id, cwd)


@mcp.tool()
@logged_tool("session_post_handoff")
async def session_post_handoff(
    from_session_id: str,
    text: str,
    scope_cwd: str | None = None,
    expires_in_hours: float = 168.0,
) -> str:
    """**Handoff to a future session** that doesn't exist yet.

    Use case: you've finished a piece of work and the next chat to open
    in this project will need context — what you just did, why, where
    to pick up, gotchas. Handoffs are scoped by working directory: any
    new session whose cwd is == or under `scope_cwd` will see this on
    its SessionStart hook automatically.

    Distinct from session_post_notice (which needs a target_session_id
    that already exists). Distinct from session_log_decision (which is
    session-private state, not surfaced anywhere automatically).

    Each new session sees a given handoff exactly once (read tracking
    by session_id; resumes don't re-surface). Handoffs auto-expire
    after expires_in_hours (default 7 days) so stale notes don't pile
    up.

    Args:
        from_session_id: your session id (for attribution).
        text: the handoff note. Be specific — file paths, commits,
            gotchas, where the work picked back up. The receiving
            agent will surface this verbatim.
        scope_cwd: directory the handoff applies to. None = inferred
            from your most-recent file_touched directory. Override
            when you've been working across multiple project roots.
        expires_in_hours: TTL. Default 168 (7 days). Use larger for
            permanent context, smaller for time-bounded asks.
    """
    return await _monitor_tools.session_post_handoff(
        from_session_id,
        text,
        scope_cwd,
        expires_in_hours,
    )


@mcp.tool()
@logged_tool("session_post_notice")
async def session_post_notice(
    target_session_id: str,
    text: str,
    from_session_id: str = "external",
) -> str:
    """**FYI / ack channel.** Drop a note in another session's inbox.

    No question, no answer expected. Use for closing-the-loop info that
    the other session benefits from seeing but shouldn't have to respond
    to: "thanks, landed", "I went with option C", "your patch fixed it",
    "FYI ramping flag X to 100% Tuesday."

    The note appears under the target's `📬 khimaira inbox` block on
    their next turn (auto-injected by UserPromptSubmit hook), tagged
    `[notice from <you>]`. Marked read on first surface — won't re-loop.

    Use this INSTEAD OF session_log_question when you don't actually
    need an answer. Asking unnecessary questions is the ping-pong
    anti-pattern — notices break the cycle.

    Args:
        target_session_id: who you're FYIing — UUID or friendly name.
        text: the FYI body. Be specific.
        from_session_id: your session id (for attribution in the
            target's inbox). Defaults to "external" if you don't pass it.
    """
    return await _monitor_tools.session_post_notice(
        target_session_id,
        text,
        from_session_id,
    )


@mcp.tool()
@logged_tool("session_wait_for_answer")
async def session_wait_for_answer(
    session_id: str,
    question_id: str,
    timeout: float = 300.0,
) -> str:
    """**Real-time block.** Wait for an answer to a targeted question.

    The intended pattern:

        qid = session_log_question(
            session_id=ME,
            text="...",
            target_session_id="other-session",
        )
        # ... maybe do other work ...
        answer = session_wait_for_answer(ME, qid, timeout=300)

    This collapses what would otherwise be a two-turn ping-pong
    ("ask, end turn, user wakes A again to read answer") into a
    single turn — A logs the targeted question, B's hook surfaces
    it on their next turn (no extra user step beyond waking B once),
    B answers, and A's wait_for_answer returns with the answer in
    hand inside the SAME turn.

    Use sparingly. Cross-session waits make A blocked-on-B; if B
    isn't going to be woken in time, the timeout fires and you've
    wasted the wall clock. Default 300s; reduce for low-stakes asks
    or increase for things you genuinely need before proceeding.

    Args:
        session_id: the session that logged the question (you).
        question_id: the 12-char hex id from session_log_question.
        timeout: max seconds to wait. Default 300 (5 min).
    """
    return await _monitor_tools.session_wait_for_answer(session_id, question_id, timeout)


@mcp.tool()
@logged_tool("session_incoming_questions")
async def session_incoming_questions(session_id: str) -> str:
    """Open questions from OTHER sessions targeted at this one.

    Symmetric counterpart to `session_pending_notes`: pending shows
    answers to questions THIS session asked; incoming shows questions
    OTHER sessions asked specifically targeting THIS session.

    The UserPromptSubmit hook auto-fetches this on every turn, so most
    of the time you don't need to call it manually. Use it as a peek
    when you want to see incoming asks without typing a real prompt.

    Args:
        session_id: this session's id.
    """
    return await _monitor_tools.session_incoming_questions(session_id)


@mcp.tool()
@logged_tool("session_set_status")
async def session_set_status(session_id: str, status: str, detail: str = "") -> str:
    """Update agent's high-level state — what kind of work is in flight?

    Conventional values: 'researching', 'implementing', 'blocked',
    'awaiting-review', 'idle'. Free-form OK; the dashboard renders whatever
    you set.

    Args:
        session_id: this session's id.
        status: short state label.
        detail: optional context ("blocked on Q3", "implementing AMR router").
    """
    return await _monitor_tools.session_set_status(session_id, status, detail)


@mcp.tool()
@logged_tool("session_set_name")
async def session_set_name(session_id: str, name: str) -> str:
    """Give this session a friendly name (e.g. 'khimaira-monitor', 'jeevy-auth-fix').

    After this, OTHER sessions can refer to it by name everywhere a
    session_id is accepted — `session_state("khimaira-monitor")` works
    instead of needing the UUID. The lookup prefers most-recently-active
    on name collisions.

    Recommended: set this in the FIRST turn so other sessions can find
    you. Convention: kebab-case slug describing the work
    ('feature-x-rewrite', 'jeevy-monitor').

    Args:
        session_id: this session's id (the UUID — name yourself, not someone else).
        name: friendly slug.
    """
    return await _monitor_tools.session_set_name(session_id, name)


@mcp.tool()
@logged_tool("session_set_workspace")
async def session_set_workspace(session_id: str, workspace: str) -> str:
    """Place this session in a named workspace (privacy/noise boundary).

    Workspaces group sessions that share visibility. By default every
    session lives in workspace `"default"` and all sessions can see each
    other. Setting a workspace partitions read access:

      - `session_list(workspace="X")` → only X's sessions
      - `session_state(id, workspace="X")` → 404 if id is in another workspace
      - `session_log_question(target_session_id=...)` → rejects cross-
        workspace targets unless `cross_workspace=True` is set

    Use when: running multi-client work, separating personal/work
    sessions, or quieting `session_list` noise across many projects.

    Workspace names must match `^[a-z0-9][a-z0-9-]{{0,39}}$`
    (kebab-case, max 40 chars).

    Args:
        session_id: this session's id (set your OWN workspace, not someone else's).
        workspace: target workspace name. Use `"default"` to opt back out.
    """
    return await _monitor_tools.session_set_workspace(session_id, workspace)


@mcp.tool()
@logged_tool("session_post_answer")
async def session_post_answer(
    target_session_id: str,
    question_id: str,
    answer: str,
    from_session_id: str = "external",
) -> str:
    """**B → A write-back.** Answer another session's open question.

    The target session's inbox is updated; on its next `session_pending_notes`
    call (auto-triggered by SessionStart hook), it'll see your answer.
    This is what makes parallel sessions actually collaborative — without
    this tool, the design collapses to read-only ("B can see A but can't
    help A").

    Args:
        target_session_id: the session that asked the question.
        question_id: the id returned by `session_log_question`.
        answer: your answer.
        from_session_id: optional — your session id, for attribution.
    """
    return await _monitor_tools.session_post_answer(
        target_session_id,
        question_id,
        answer,
        from_session_id,
    )


@mcp.tool()
@logged_tool("session_state")
async def session_state(session_id: str, recent: int = 10) -> str:
    """Full digest of a session's externalized state.

    The 'side conversation' query: when another session is grinding on
    something, call this to see its current status, decisions, file
    touches, and open questions WITHOUT interrupting it. Then you can
    answer its questions or work on related things knowing what it's
    already done.

    Args:
        session_id: the session to inspect.
        recent: how many recent entries per category (default 10).
    """
    return await _monitor_tools.session_state(session_id, recent)


@mcp.tool()
@logged_tool("session_summary")
async def session_summary(session_id: str) -> str:
    """Lightweight session digest — status + counts + last-active, no bodies.

    Use this instead of session_state when you only need to know "what's
    session X doing right now?" or "is it done yet?" — does NOT load
    decision/file/question bodies, so it's cheaper for long-running
    sessions with hundreds of records.

    Args:
        session_id: the session to inspect (UUID or friendly name).
    """
    return await _monitor_tools.session_summary(session_id)


@mcp.tool()
@logged_tool("session_pending_notes")
async def session_pending_notes(session_id: str, mark_read: bool = True) -> str:
    """**Inbox read.** Get unread answers other sessions have posted to your
    open questions.

    Designed to be auto-called at SessionStart so unread cross-session
    answers surface in your context without the user having to know to ask.

    Args:
        session_id: this session's id (the one reading its own inbox).
        mark_read: if True (default), mark notes as read after returning.
            Pass False to peek without consuming.
    """
    return await _monitor_tools.session_pending_notes(session_id, mark_read)


@mcp.tool()
@logged_tool("session_recent_decisions")
async def session_recent_decisions(recent_per_session: int = 5) -> str:
    """Recent decisions across ALL sessions. Cross-session view.

    Useful when starting a new session — see what's been decided recently
    in other parallel work so you don't redo or contradict it.

    Args:
        recent_per_session: max decisions to fetch per session (default 5).
    """
    return await _monitor_tools.session_recent_decisions(recent_per_session)


@mcp.tool()
@logged_tool("session_list")
async def session_list() -> str:
    """List all tracked sessions — last activity, status, counts.

    Use for DISCOVERY ("what sessions exist?", "what's been active
    recently?"). NOT a lookup-by-name helper.

    🛑 If you already know the session's name or id, skip the list:
    every read tool (`session_state`, `session_summary`,
    `session_pending_notes`) and every write tool (`session_post_notice`,
    `session_post_handoff`, `session_invite_handoff`, `session_log_*`)
    accepts a NAME directly and resolves it internally — list-then-filter
    is wasted work.
    """
    return await _monitor_tools.session_list()


# ---------------------------------------------------------------------------
# Phase 13 — MCP call telemetry. Answers "is khimaira being used effectively?"
# ---------------------------------------------------------------------------


@mcp.tool()
@logged_tool("usage_report")
async def usage_report(window_minutes: int = 1440) -> str:
    """Aggregate report: which khimaira MCP tools were called in the time
    window, how often, with what success rate, and how many polls
    `wait_for_process` replaced.

    Use to answer "is the agent using khimaira effectively?" — surfaces:
      - which tools were called (by frequency)
      - p50/p95 latency per tool
      - failure rate per tool with sampled error messages
      - polling-replacement savings (the headline cost-of-context pitch)

    Args:
        window_minutes: time window. Default 1440 (24h). Try 60 for "last
            hour" or 10080 for "last week".
    """
    return await _monitor_tools.usage_report(window_minutes)


@mcp.tool()
@logged_tool("list_mcp_calls")
async def list_mcp_calls(
    window_minutes: int | None = None,
    tool: str | None = None,
    only_failures: bool = False,
    limit: int = 50,
) -> str:
    """Recent khimaira MCP tool invocations, newest first.

    Use to drill into specific tools or to debug failures. Pair with
    `usage_report` for the aggregate view.

    Args:
        window_minutes: filter to this time window; None = no time filter.
        tool: filter to one tool name (e.g. 'wait_for_process').
        only_failures: True hides successful calls.
        limit: max calls to return (default 50).
    """
    return await _monitor_tools.list_calls(window_minutes, tool, only_failures, limit)


@mcp.tool()
@logged_tool("khimaira_configure")
async def khimaira_configure(profile: str | None = None, force: bool = False) -> str:
    """**Re-sync this machine to your khimaira profile** (dotfiles, MCP
    servers, hooks). Invoke from any Claude Code session via the
    `/khimaira-configure` slash command — does the same thing as running
    `khimaira sync` from a terminal but without leaving chat.

    Pulls your dotfiles repo, re-applies the symlinks declared in the
    profile, re-registers MCP servers with Claude Code (idempotent),
    and re-writes settings.json hooks if the profile asks for it.

    First-run setup on a brand-new machine still needs the CLI
    one-liner — this MCP tool requires khimaira to already be installed
    locally. See `khimaira bootstrap` for first-run.

    Args:
        profile: optional path or http(s) URL to a profile YAML. If
            None, uses KHIMAIRA_PROFILE env, then ~/.config/khimaira/profile.yaml,
            then the khimaira-shipped default.
        force: re-register MCP servers even if Claude Code lists them already.
    """
    from khimaira.bootstrap import load_profile, ProfileError
    from khimaira.bootstrap.runner import run_sync

    try:
        prof, source = load_profile(profile)
    except ProfileError as e:
        return f"❌ failed to load profile: {e}"

    report = run_sync(prof, force=force)

    # Render the report inline so the agent can surface meaningful
    # detail to the user. Tail-summary mirrors the CLI's output.
    glyph = {
        "created": "✨",
        "updated": "🔄",
        "unchanged": "·",
        "skipped": "—",
        "failed": "✗",
    }
    lines = [f"**khimaira-configure** — profile `{prof.name}` (from {source})"]
    for r in report.results:
        g = glyph.get(r.status, "?")
        line = f"  {g} `{r.op}` {r.target}"
        if r.status in ("failed", "created", "updated") and r.detail:
            line += f" — {r.detail}"
        lines.append(line)
    summary = report.summary
    counts = ", ".join(
        f"{summary[k]} {k}"
        for k in ("created", "updated", "unchanged", "skipped", "failed")
        if k in summary
    )
    lines.append(f"\n{counts or 'no operations'}")
    if report.had_failures:
        lines.append("\n⚠️ at least one operation failed — see entries marked ✗ above.")
    return "\n".join(lines)


def main():
    """Entry point — run the MCP server over stdio."""
    import atexit

    from khimaira.dispatch.runners import kill_all_subprocesses
    from khimaira.pidlock import acquire_lock

    setup_logging()
    acquire_lock("graph")
    log.info("khimaira starting starting")
    atexit.register(kill_all_subprocesses)
    mcp.run()


if __name__ == "__main__":
    main()
