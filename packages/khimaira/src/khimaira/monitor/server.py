"""FastAPI app for the LangGraph monitor.

- Asserts `127.0.0.1` binding at startup (refuses to serve on any other host).
- Mounts API routers under `/api/`.
- Serves the built frontend from `monitor_ui/dist/` (auto-built via build.py).
- Prints a startup banner with discovered DB hosts before accepting requests.
"""

from __future__ import annotations

import asyncio
import os
import sys

from khimaira.config import ROOTS

from . import build as ui_build
from ._optional import require
from .discovery.connections import discover_all
from .discovery.project import discover
from .metadata import scanner as meta_scanner

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8740


def _assert_loopback(host: str) -> None:
    """`127.0.0.1` binding is the auth layer — refuse anything else."""
    if host != DEFAULT_HOST:
        raise SystemExit(
            f"khimaira monitor: refusing to bind to {host!r} — "
            f"only 127.0.0.1 is allowed (loopback is the auth layer)"
        )


def _print_banner(projects, connections_by_project) -> None:
    """Single-shot pre-serve banner. Goes to stderr (the monitor.log file)."""
    print("=" * 72, file=sys.stderr)
    print(
        f"khimaira monitor — monitoring {len(projects)} project(s) — "
        f"local DBs only, do not point at prod",
        file=sys.stderr,
    )
    if not projects:
        print("  (no langgraph projects discovered in khimaira roots registry)", file=sys.stderr)
    for project in projects:
        conns = connections_by_project.get(project.path)
        print(f"  • {project.name} ({project.detected_via}) — {project.path}", file=sys.stderr)
        if conns is None or (not conns.postgres and not conns.sqlite):
            print("      (no checkpointer detected — threads view will be empty)", file=sys.stderr)
            continue
        for pg in conns.postgres:
            print(f"      postgres {pg.var}: {pg.host}/{pg.database}", file=sys.stderr)
        for sqlite in conns.sqlite:
            print(f"      sqlite   {sqlite.label}: {sqlite.path}", file=sys.stderr)
    print("=" * 72, file=sys.stderr)


def build_app():
    """Build the FastAPI app, mount routes, and configure static serving."""
    fastapi = require("fastapi")

    projects = discover(ROOTS)
    connections_by_project = {p.path: discover_all(p.path) for p in projects}
    _print_banner(projects, connections_by_project)

    app = fastapi.FastAPI(
        title="Khimaira Monitor", docs_url="/api/docs", openapi_url="/api/openapi.json"
    )

    # Mount API routers (lazy imports so optional deps don't bite at import time)
    from .api import anomalies as anomalies_api
    from .api import api_routes as api_routes_api
    from .api import chats as chats_api
    from .api import themis as themis_api
    from .api import oracle as oracle_api
    from .api import frontend_components as fc_api
    from .api import heartbeats as heartbeats_api
    from .api import mcp_calls as mcp_calls_api
    from .api import processes as processes_api
    from .api import projects as projects_api
    from .api import scheduled_tasks as scheduled_tasks_api
    from .api import schema_drift as drift_api
    from .api import sessions as sessions_api
    from .api import threads as threads_api
    from .api import topology as topology_api
    from .api import usage as usage_api

    app.include_router(projects_api.build_router(projects, connections_by_project), prefix="/api")
    app.include_router(topology_api.build_router(projects), prefix="/api")
    app.include_router(threads_api.build_router(connections_by_project, projects), prefix="/api")
    app.include_router(api_routes_api.build_router(projects), prefix="/api")
    app.include_router(fc_api.build_router(projects), prefix="/api")
    app.include_router(drift_api.build_router(projects), prefix="/api")
    app.include_router(anomalies_api.build_router(), prefix="/api")
    app.include_router(usage_api.build_router(), prefix="/api")
    app.include_router(processes_api.build_router(), prefix="/api")
    app.include_router(sessions_api.build_router(), prefix="/api")
    app.include_router(mcp_calls_api.build_router(), prefix="/api")
    app.include_router(heartbeats_api.build_router(), prefix="/api")
    app.include_router(scheduled_tasks_api.build_router(), prefix="/api")
    app.include_router(chats_api.build_router(), prefix="/api")
    app.include_router(themis_api.build_router(), prefix="/api")
    app.include_router(oracle_api.build_router(), prefix="/api")

    # SSE delivery cursor persistence — load cursors from disk at startup
    # so reconnecting subscribers resume from their last yielded position.
    @app.on_event("startup")
    async def _load_chat_cursors() -> None:
        from .chats import load_cursors
        load_cursors()

    @app.on_event("shutdown")
    async def _save_chat_cursors() -> None:
        from .chats import save_cursors
        save_cursors()

    # Periodic cursor persist — flush _CURSORS to disk every 8 seconds.
    # Complements the on-disconnect flush in event_generator so a daemon
    # restart doesn't lose more than ~8s of cursor advancement.
    @app.on_event("startup")
    async def _start_cursor_persist_loop() -> None:
        from .chats import _CURSORS_DIRTY, save_cursors  # noqa: F401

        async def _loop() -> None:
            import khimaira.monitor.chats as _chats
            while True:
                await asyncio.sleep(8)
                if _chats._CURSORS_DIRTY:
                    _chats.save_cursors()

        asyncio.create_task(_loop())

    # Expected-reply overdue watcher — fires session_post_notice to both sides
    # when a chat_send_to recipient hasn't replied within _REPLY_OVERDUE_S.
    @app.on_event("startup")
    async def _start_overdue_watcher() -> None:
        from .api.chats import _overdue_watcher
        asyncio.create_task(_overdue_watcher())

    # Guard-4 + #13b-light watcher — escalates sessions silent-while-obligated
    # beyond the CC retry/request grace window; fast-escalates dead processes.
    @app.on_event("startup")
    async def _start_guard4_watcher() -> None:
        from .api.chats import _guard4_watcher
        asyncio.create_task(_guard4_watcher())

    # Roster auto-recovery watcher — distills + compacts kitty windows at high
    # context usage; wakes idle sessions with pending obligations.
    @app.on_event("startup")
    async def _start_roster_recovery_watcher() -> None:
        from . import roster_recovery
        asyncio.create_task(roster_recovery.watcher_loop())

    # Guard-5 — roster-progress monitor. Fires when ≥K sessions are idle
    # AND a blocking gate has had no state-change >T_stall. Per-session
    # Guard-4 misses this class; Guard-5 catches the emergent standstill.
    @app.on_event("startup")
    async def _start_guard5_watcher() -> None:
        from . import guard5
        asyncio.create_task(guard5.guard5_loop())

    # Guard-6 — heartbeat-liveness detector. Fires when a roster member has
    # gone dark (no activity) regardless of whether it owes anything.
    # Complements Guard-4 (obligation-only) and Guard-5 (gate-only).
    @app.on_event("startup")
    async def _start_guard6_watcher() -> None:
        from . import guard6
        asyncio.create_task(guard6.guard6_loop())

    # Persistent scheduler — daemon-side replacement for ScheduleWakeup.
    # Replay-on-boot recovers stuck-firing tasks; worker tick fires due tasks.
    @app.on_event("startup")
    async def _start_scheduler_worker() -> None:
        from . import scheduler as scheduler_mod

        scheduler_mod.replay()
        asyncio.create_task(scheduler_mod.scheduler_loop())

    # Chat MCP registration watchdog. Claude Code intermittently prunes
    # the khimaira-chat entry from ~/.claude.json (subprocess errors
    # during daemon restart, MCP supervisor health-check, or some
    # other unknown trigger). Polling here every 30s ensures the entry
    # always exists, so users can launch `claude-chat` at any time
    # without hitting "no MCP server configured." The hook-based
    # self-heal helps but only fires per-session-boot; this watchdog
    # bridges the gap when the prune happens between launches.
    @app.on_event("startup")
    async def _start_chat_mcp_watchdog() -> None:
        async def _watchdog() -> None:
            import subprocess

            register_cmd = [
                "claude", "mcp", "add", "khimaira-chat", "-s", "user", "--",
                "bash", "-lc",
                "uv --directory ~/dev/khimaira run khimaira-chat 2>>/tmp/khimaira-chat.log",
            ]
            while True:
                try:
                    proc = await asyncio.to_thread(
                        subprocess.run,
                        ["claude", "mcp", "list"],
                        capture_output=True, text=True, timeout=10.0,
                    )
                    if "khimaira-chat" not in (proc.stdout or ""):
                        await asyncio.to_thread(
                            subprocess.run,
                            register_cmd, capture_output=True, text=True, timeout=10.0,
                        )
                        print(
                            "khimaira monitor: chat-mcp watchdog re-registered khimaira-chat",
                            file=sys.stderr,
                        )
                except Exception as exc:
                    print(
                        f"khimaira monitor: chat-mcp watchdog tick failed — {exc}",
                        file=sys.stderr,
                    )
                await asyncio.sleep(30)

        asyncio.create_task(_watchdog())

    # Auto-scan: kick off background metadata enrichment for any project
    # whose cache is missing or stale. The worker drains serially so we
    # don't hammer Gemini. Scans complete in the background; the topology
    # endpoint returns AST-only data until each scan lands.
    @app.on_event("startup")
    async def _start_scanner() -> None:
        meta_scanner.start_worker()
        n = meta_scanner.enqueue_stale([(p.name, p.path) for p in projects])
        if n:
            print(
                f"khimaira monitor: queued {n} project(s) for metadata scan (runs in background)",
                file=sys.stderr,
            )

    # Observation collector — periodically mines each project's checkpoint
    # history for per-node duration statistics. Output feeds adaptive
    # stuck-detection thresholds and the periodic LLM refinement scan.
    # Cadence is intentionally slow (5min) because:
    #   - Stats stabilize over hours/days, not seconds
    #   - Each pass walks 200 threads × N checkpoints — non-trivial I/O
    #   - Saved file is read at request time; no need for sub-minute freshness
    @app.on_event("startup")
    async def _start_observation_loop() -> None:
        from .metadata import observations as obs_module

        async def _loop() -> None:
            # Initial pass after a short delay so the daemon's first
            # health checks aren't fighting for DB connections.
            await asyncio.sleep(20)
            while True:
                for p in projects:
                    try:
                        await asyncio.to_thread(obs_module.collect, p.path)
                    except Exception as exc:
                        print(
                            f"khimaira monitor: observation collection failed for {p.name}: {exc}",
                            file=sys.stderr,
                        )
                await asyncio.sleep(300)  # 5 min between passes

        asyncio.create_task(_loop())

    # Self-watch — periodic invariant checks that the daemon's claims
    # match the underlying truth (DB → API consistency, observation
    # freshness, topology agreement). Failures land in the anomaly log
    # for human inspection; the daemon does NOT auto-fix.
    @app.on_event("startup")
    async def _start_self_watch_loop() -> None:
        from . import anomalies as anomalies_module

        async def _loop() -> None:
            # Wait long enough that:
            #   - Observation collector's first pass (after 20s) has run,
            #     so observation_freshness has data to check.
            #   - Uvicorn warm-up + first metadata scan dispatch is done,
            #     so the API endpoints we probe don't time out.
            # 90s is a comfortable margin — the first check fires
            # roughly 1.5 min after daemon start. Real anomalies that
            # appear in the first 90s would be caught by the next pass.
            await asyncio.sleep(90)
            base_url = f"http://{DEFAULT_HOST}:{int(os.environ.get('KHIMAIRA_MONITOR_PORT', 8740))}"
            while True:
                try:
                    await anomalies_module.run_checks(projects, base_url=base_url)
                except Exception as exc:
                    print(
                        f"khimaira monitor: self-watch check failed: {exc}",
                        file=sys.stderr,
                    )
                await asyncio.sleep(300)  # 5 min between passes

        asyncio.create_task(_loop())

    # Heartbeat store GC — drop runs idle longer than the TTL.
    @app.on_event("startup")
    async def _start_heartbeat_gc() -> None:
        from . import heartbeats as heartbeats_module

        asyncio.create_task(heartbeats_module.gc_loop())

    # Attach supervisor — auto-reattach khimaira_observer when target venvs
    # rebuild. Two parts:
    #   (1) startup pass: re-inject for any project where files vanished
    #       while daemon was offline.
    #   (2) live watch: inotify on every attached project's site-packages,
    #       re-inject on rebuild detection.
    @app.on_event("startup")
    async def _start_attach_supervisor() -> None:
        from . import attach_supervisor

        async def _supervisor() -> None:
            try:
                await attach_supervisor.startup_reattach_pass()
            except Exception as exc:
                print(
                    f"khimaira monitor: attach supervisor startup pass failed: {exc}",
                    file=sys.stderr,
                )
            try:
                await attach_supervisor.watch_loop()
            except Exception as exc:
                print(
                    f"khimaira monitor: attach supervisor watch loop crashed: {exc}",
                    file=sys.stderr,
                )

        asyncio.create_task(_supervisor())

    # Transcript watcher — sync Claude Code /rename events to khimaira
    # session names within ~100ms instead of waiting for next user prompt.
    @app.on_event("startup")
    async def _start_transcript_watcher() -> None:
        from . import transcript_watcher

        async def _watcher() -> None:
            try:
                await transcript_watcher.watch_loop()
            except Exception as exc:
                print(
                    f"khimaira monitor: transcript watcher crashed: {exc}",
                    file=sys.stderr,
                )

        asyncio.create_task(_watcher())

    # Static frontend — only mount if dist/ exists; otherwise serve a placeholder
    dist = ui_build.dist_dir()
    if dist.is_dir() and (dist / "index.html").is_file():
        from fastapi.responses import FileResponse
        from fastapi.staticfiles import StaticFiles

        app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def spa(full_path: str):  # noqa: ARG001
            # SPA fallback — serve index.html for any non-API route
            return FileResponse(str(dist / "index.html"))
    else:

        @app.get("/")
        async def placeholder():
            return {
                "status": "ok",
                "message": (
                    "monitor backend running, but the frontend has not been built yet. "
                    "Scaffold src/khimaira/monitor_ui/ then restart `khimaira monitor start`."
                ),
            }

    return app


def serve(*, port: int = DEFAULT_PORT, host: str = DEFAULT_HOST) -> None:
    """Bring up uvicorn after asserting loopback + building the UI."""
    _assert_loopback(host)
    ui_build.ensure_built()

    uvicorn = require("uvicorn")
    app = build_app()

    # Belt and suspenders: clear any inherited env that uvicorn might use
    # to override the host (defense against accidental 0.0.0.0).
    os.environ.pop("UVICORN_HOST", None)

    uvicorn.run(app, host=host, port=port, log_level="info", access_log=False)
