"""Auto-reattach supervisor — runs inside the khimaira-monitor daemon.

Two jobs:

1. **Startup pass** — on daemon boot, walk the registry of attached
   projects. For each one, verify khimaira_observer is still in its venv;
   re-inject if missing. Catches the case where the user rebuilt their
   venv while the daemon was offline.

2. **Live watch** — use `watchfiles` (a uvicorn transitive dep, so it's
   always available) to subscribe to filesystem events on each project's
   site-packages directory. When the dir changes (rebuild detected),
   re-inject the observer files so the next app launch picks them up.

Both jobs are idempotent. `khimaira attach` may be called manually at any
time — the supervisor's work is just automation around the same primitive.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from khimaira.attach import attach_project, is_attached, list_attached, record_detach
from khimaira.attach.inject import VenvNotFound, find_site_packages
from khimaira.log import get_logger

log = get_logger("monitor.attach_supervisor")

# Debounce for filesystem events. A `uv sync --reinstall` produces dozens
# of writes to site-packages over a few seconds; we want one re-inject,
# not dozens.
_DEBOUNCE_S = 1.5


async def startup_reattach_pass() -> None:
    """Walk the registry; re-inject for any attached project missing the observer."""
    entries = list_attached()
    if not entries:
        return

    log.info("attach_supervisor: startup pass over %d registered project(s)", len(entries))
    for entry in entries:
        project = Path(entry.get("project_path", ""))
        venv = Path(entry.get("venv_path", ""))
        if not project.is_dir() or not venv.is_dir():
            log.warning(
                "attach_supervisor: project %s missing on disk — leaving registry entry "
                "(remove via `khimaira detach` if intentional)",
                project,
            )
            continue
        try:
            if not is_attached(venv):
                attach_project(project)
                log.info("attach_supervisor: re-injected observer into %s", project)
        except VenvNotFound:
            log.warning("attach_supervisor: venv vanished for %s — skipping", project)
        except Exception as exc:
            log.warning("attach_supervisor: re-attach failed for %s: %s", project, exc)


async def watch_loop() -> None:
    """Forever-loop: watch each registered project's site-packages directory.

    On a write event (venv rebuild, dependency add, anything that touches
    site-packages), debounce briefly then re-inject. Idempotent — if the
    observer files are already current, attach_project no-ops cheaply.
    """
    try:
        from watchfiles import awatch
    except ImportError:
        log.warning(
            "attach_supervisor: `watchfiles` not available; auto-reattach disabled. "
            "`khimaira attach` works manually. Install `watchfiles` to enable."
        )
        return

    while True:
        entries = list_attached()
        if not entries:
            # Nothing to watch — poll every 30s for new attachments
            await asyncio.sleep(30.0)
            continue

        # Build the set of site-packages dirs to watch
        watch_paths: list[Path] = []
        for entry in entries:
            venv = Path(entry.get("venv_path", ""))
            if not venv.is_dir():
                continue
            try:
                sp = find_site_packages(venv)
            except VenvNotFound:
                continue
            watch_paths.append(sp)

        if not watch_paths:
            await asyncio.sleep(30.0)
            continue

        log.info("attach_supervisor: watching %d site-packages dir(s)", len(watch_paths))

        # awatch yields when files change. We re-build the watch list whenever
        # a new project is attached (registry growth), so this loop is finite-
        # iteration — it returns to the outer while, re-reads the registry,
        # and re-establishes the watch.
        last_reattach: dict[str, float] = {}
        try:
            async for changes in awatch(*watch_paths, debounce=int(_DEBOUNCE_S * 1000)):
                # `changes` is a set of (Change, path). Map back to projects.
                affected = set()
                for _change, path_str in changes:
                    affected.add(_project_for_path(Path(path_str), entries))
                for project_path in affected:
                    if project_path is None:
                        continue
                    # Per-project rate-limit
                    import time
                    now = time.monotonic()
                    last = last_reattach.get(str(project_path), 0.0)
                    if now - last < 1.0:
                        continue
                    last_reattach[str(project_path)] = now

                    try:
                        attach_project(project_path)
                        log.info("attach_supervisor: re-injected after rebuild → %s", project_path)
                    except VenvNotFound:
                        # Venv vanished mid-watch (rare); will re-establish on next loop
                        log.info("attach_supervisor: venv gone for %s; re-watching", project_path)
                        break
                    except Exception as exc:
                        log.warning("attach_supervisor: re-attach failed for %s: %s",
                                    project_path, exc)

                # Periodically check for newly-attached projects to add to the watch
                # (we just iterate awatch which yields when changes happen — for new
                # projects, the user manually triggers re-watch by restarting the
                # daemon or by changing files in one of the existing watch dirs.
                # Acceptable for now; if it becomes annoying, add a registry signal.)
        except FileNotFoundError:
            # A site-packages dir vanished during watch — re-establish
            log.info("attach_supervisor: watched path vanished; rebuilding watch list")
            await asyncio.sleep(2.0)
            continue
        except Exception as exc:
            log.warning("attach_supervisor: watch loop error: %s", exc)
            await asyncio.sleep(5.0)
            continue


def _project_for_path(path: Path, entries: list[dict]) -> Path | None:
    """Map a filesystem path back to its registered project_path."""
    p = path.resolve()
    for entry in entries:
        venv = Path(entry.get("venv_path", "")).resolve()
        if str(p).startswith(str(venv)):
            return Path(entry["project_path"])
    return None
