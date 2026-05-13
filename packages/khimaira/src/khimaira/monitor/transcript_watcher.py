"""Watch Claude Code transcripts for /rename events; sync to khimaira session names.

Closes the latency gap between `/rename foo` in Claude Code and the new
name being addressable from other khimaira sessions. Without this watcher,
the UserPromptSubmit hook handles sync — but only fires on the next user
prompt. With the watcher, names sync within ~100ms of the rename hitting
the transcript file.

Architecture mirrors `attach_supervisor.watch_loop`: an asyncio task on
the daemon's event loop using `watchfiles.awatch` for cross-platform
inotify/fsevents.

What it does NOT do:
  - Sync if khimaira already has an explicit name (set via MCP tool).
    The watcher only FILLS IN missing names; it never clobbers
    deliberate set_name calls. Explicit > inferred.
  - Watch transcripts in directories the user hasn't opened claude in.
    The walk starts from ~/.claude/projects/; if you're on a non-
    default config, set KHIMAIRA_CLAUDE_PROJECTS_DIR.
  - Block daemon startup if Claude Code isn't installed. Missing
    projects dir is a silent no-op.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from khimaira.log import get_logger
from khimaira.monitor import sessions

log = get_logger("monitor.transcript_watcher")

_CLAUDE_PROJECTS_DIR = Path(
    os.environ.get(
        "KHIMAIRA_CLAUDE_PROJECTS_DIR",
        os.path.expanduser("~/.claude/projects"),
    )
)

# Per-session debounce — Claude Code writes transcripts on every turn,
# but custom-title changes are rare. Don't re-scan unchanged files.
_last_synced: dict[str, str] = {}

# Per-session byte offset — only scan transcript bytes written AFTER
# the offset for new custom-title entries. Catches fresh /rename events
# without re-syncing historical ones that were already overridden by
# explicit session_set_name calls.
_last_offset: dict[str, int] = {}


async def watch_loop() -> None:
    """Long-running: watch Claude Code project dirs; sync rename events.

    Safe to run as a daemon background task. Tolerates the projects dir
    not existing (no-ops); tolerates per-file scan errors (logs + continues).
    """
    if not _CLAUDE_PROJECTS_DIR.exists():
        log.info(
            "transcript_watcher: %s doesn't exist; not starting (this is "
            "fine if Claude Code isn't installed yet)",
            _CLAUDE_PROJECTS_DIR,
        )
        return

    try:
        from watchfiles import awatch
    except ImportError:
        log.warning(
            "transcript_watcher: watchfiles not installed; rename sync "
            "will be deferred to UserPromptSubmit hook on next prompt"
        )
        return

    log.info("transcript_watcher: watching %s for /rename events",
             _CLAUDE_PROJECTS_DIR)

    # Initial pass — sync any names that already exist but khimaira doesn't
    # know about. Catches the case where the user renamed a session before
    # the daemon was running.
    try:
        _initial_pass()
    except Exception as exc:
        log.warning("transcript_watcher: initial pass failed: %s", exc)

    async for changes in awatch(
        _CLAUDE_PROJECTS_DIR,
        recursive=True,
        debounce=200,  # ms — coalesce rapid writes from a single turn
    ):
        for _change_type, path_str in changes:
            try:
                path = Path(path_str)
                if not path.name.endswith(".jsonl"):
                    continue
                if not path.is_file():
                    continue
                session_id = path.stem  # filename without .jsonl
                _maybe_sync_name(session_id, path)
            except Exception as exc:
                log.warning(
                    "transcript_watcher: error processing %s: %s",
                    path_str, exc,
                )


def _initial_pass() -> None:
    """One-shot scan at startup.

    Records the current EOF byte offset for each recently-active transcript,
    so subsequent live-watch fires only consider bytes written AFTER startup.
    Historical custom-title entries (which may conflict with explicit
    session_set_name calls made later) are intentionally skipped.

    Bounded to last 24h to keep startup cheap.
    """
    import time
    cutoff = time.time() - 86400
    for project_dir in _CLAUDE_PROJECTS_DIR.iterdir():
        if not project_dir.is_dir():
            continue
        for transcript in project_dir.glob("*.jsonl"):
            try:
                if transcript.stat().st_mtime < cutoff:
                    continue
                session_id = transcript.stem
                # Mark current EOF as the start point — historical content
                # is NOT clobbered. Only NEW custom-title entries (written
                # after daemon boot) will trigger sync.
                _last_offset[session_id] = transcript.stat().st_size
            except OSError:
                continue


def _maybe_sync_name(session_id: str, transcript: Path) -> None:
    """Read transcript for latest /rename (only bytes written since last
    scan via _last_offset). Sync to khimaira if different from current name.

    "Only new entries" semantics matter: historical custom-title entries
    may conflict with later explicit session_set_name calls. By only
    looking at bytes written AFTER our last scan (recorded in
    _last_offset), we catch FRESH /rename events without clobbering
    explicit names.
    """
    latest_title = _find_latest_custom_title(transcript, session_id)
    if not latest_title:
        return

    # Debounce — skip if we already synced this exact title for this session.
    # This is the only "don't re-sync" guard; once a NEW title appears, we
    # always sync it (no khimaira-side name check).
    if _last_synced.get(session_id) == latest_title:
        return

    # ALWAYS SYNC: /rename in Claude Code is the user's most direct rename
    # intent. It wins over any prior name set via session_set_name (which
    # is the agent's inference). The earlier "don't clobber explicit names"
    # rule was wrong — it caused fresh /rename events to be silently
    # ignored when khimaira had a stale agent-set name.
    try:
        sessions.set_name(session_id, latest_title)
        _last_synced[session_id] = latest_title
        log.info(
            "transcript_watcher: synced %s → %s",
            session_id[:8], latest_title,
        )
    except Exception as exc:
        log.warning(
            "transcript_watcher: set_name failed for %s: %s",
            session_id, exc,
        )


def _find_latest_custom_title(transcript: Path, session_id: str) -> str | None:
    """Scan transcript JSONL starting at `_last_offset[session_id]` for the
    most-recent {type: 'custom-title'} entry written AFTER the offset.

    Updates `_last_offset` to current EOF after the scan, so subsequent
    calls only consider newer bytes. Without this, historical
    custom-title entries (which may have been overridden by later
    session_set_name calls) would keep getting re-synced and clobber
    explicit names.

    Returns the title string, or None if no NEW custom-title since last scan.
    """
    try:
        latest: str | None = None
        start_offset = _last_offset.get(session_id, 0)
        with transcript.open("r", encoding="utf-8") as f:
            f.seek(start_offset)
            for line in f:
                if '"custom-title"' not in line:
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("type") != "custom-title":
                    continue
                title = (
                    rec.get("title")
                    or rec.get("customTitle")
                    or rec.get("name")
                    or ""
                )
                if title:
                    latest = title
            # Update offset to current EOF so we don't re-process this region
            _last_offset[session_id] = f.tell()
        return latest
    except OSError:
        return None
