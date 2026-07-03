"""Phase 1c — the note-structuring pipeline via a headless Claude Code session.

Spawns `claude -p` with an ISOLATED config dir (credentials only, no hooks,
no CLAUDE.md) so each transform costs ~$0.02-0.10 (vs ~$0.34 unisolated) and
never registers a stray khimaira session. Recipe validated live by master
2026-07-03 (see tasks/notebook/IMPLEMENTATION.md "Phase 1c") — this module
implements that recipe rather than re-deriving the mechanism.

Perception boundary (per the ai-engineering rule): the LLM only transforms
raw_text into the PipelineOutput schema. Parsing, validation, retry, and the
decision to mark a note "processed" vs "failed" are all deterministic code.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shutil
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

from khimaira.log import get_logger
from khimaira.monitor import notes

log = get_logger("monitor.notebook_pipeline")

_INSTRUCTION = (
    "You structure a pasted note (often an AI coding-assistant response). "
    "Output ONLY a JSON object, no prose, no markdown fence, with keys: "
    "summary (1-3 sentence string), technical (markdown string), plain "
    "(plain-language string), organized_md (markdown string), tags (array "
    "of strings), entities (array of strings — code symbols/files/concepts "
    "referenced). The entire user message is the raw note to structure."
)

_MODEL = "claude-sonnet-5"
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)

# Fallback absolute paths when PATH lookup misses. The khimaira-monitor
# systemd unit's PATH doesn't include ~/.local/bin (confirmed live 2026-07-03
# — bare "claude" raised FileNotFoundError under the daemon, though it
# resolves fine in an interactive shell). The same bug class exists at
# server.py's chat-mcp watchdog, dispatch/runners/claude.py, hooks/
# session_start.py, and bootstrap/operations.py — all bare "claude" + PATH,
# none resolve an absolute path. Fixing only this call site (Phase 1c scope);
# flagged the class to master for a follow-up sweep.
_CLAUDE_CMD_CANDIDATES = (
    os.path.expanduser("~/.local/bin/claude"),
    os.path.expanduser("~/.claude/local/claude"),
)


def _resolve_claude_cmd() -> str:
    """Resolve the claude CLI to run. Honors KHIMAIRA_CLAUDE_CMD (the existing
    override convention from dispatch/runners/claude.py) first, then a normal
    PATH lookup, then known absolute install locations."""
    override = os.environ.get("KHIMAIRA_CLAUDE_CMD")
    if override:
        return override
    found = shutil.which("claude")
    if found:
        return found
    for candidate in _CLAUDE_CMD_CANDIDATES:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return "claude"  # let the subprocess call raise a clear FileNotFoundError


# Strong references to background transform tasks — asyncio.create_task()
# only holds a weak ref, so a fire-and-forget task can be silently
# garbage-collected mid-flight (same failure mode server.py's _spawn guards
# against for the daemon's other background loops).
_BACKGROUND_TASKS: set[asyncio.Task] = set()


class PipelineOutput(BaseModel):
    summary: str
    technical: str
    plain: str
    organized_md: str
    tags: list[str]
    entities: list[str]


def _isolated_config_dir() -> Path:
    """Dedicated CLAUDE_CONFIG_DIR: credentials only, empty settings (no
    hooks), no CLAUDE.md. This is what keeps each transform cheap (kills the
    ~50k context load) and non-polluting (no SessionStart hook == no khimaira
    session registered for the headless run).

    Credentials are re-copied every call — cheap, and the real ~/.claude
    credentials rotate via OAuth refresh independently of this dir, so a
    one-time copy would eventually go stale and silently break auth.
    """
    xdg = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    cfg_dir = xdg / "khimaira" / "notebook" / "claude-config"
    cfg_dir.mkdir(parents=True, exist_ok=True)

    settings_path = cfg_dir / "settings.json"
    if not settings_path.exists():
        settings_path.write_text("{}", encoding="utf-8")

    src_creds = Path(os.path.expanduser("~/.claude/.credentials.json"))
    if src_creds.is_file():
        shutil.copy2(src_creds, cfg_dir / ".credentials.json")

    return cfg_dir


def _strip_fence(text: str) -> str:
    m = _FENCE_RE.match(text.strip())
    return m.group(1) if m else text.strip()


async def _run_once(raw_text: str) -> PipelineOutput:
    """Single headless-claude invocation. Raises on any parse/validate failure."""
    cfg_dir = _isolated_config_dir()
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)

    proc = await asyncio.create_subprocess_exec(
        _resolve_claude_cmd(),
        "-p",
        "--append-system-prompt",
        _INSTRUCTION,
        "--output-format",
        "json",
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
        "--model",
        _MODEL,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    stdout, stderr = await proc.communicate(raw_text.encode("utf-8"))
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {stderr.decode('utf-8', 'ignore')[:500]}"
        )

    envelope = json.loads(stdout.decode("utf-8"))
    result_text = envelope.get("result")
    if not isinstance(result_text, str):
        raise ValueError(f"envelope missing string .result: {envelope!r}")

    payload = json.loads(_strip_fence(result_text))
    return PipelineOutput.model_validate(payload)


async def transform_note(raw_text: str) -> dict[str, Any] | None:
    """Run the transform, retrying once on parse/validate failure.

    Returns the validated pipeline dict on success, None if both attempts
    failed — the caller marks the note status="failed" and keeps raw_text.
    """
    for attempt in (1, 2):
        try:
            output = await _run_once(raw_text)
            return output.model_dump()
        except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError, OSError) as exc:
            log.warning("notebook_pipeline: attempt %d failed: %s", attempt, exc)
    return None


async def trigger_pipeline(note_id: str) -> None:
    """Transform note_id's raw_text and write the result back to the store.

    Fire-and-forget worker coroutine — schedule_pipeline() (called from
    api/notebook.py) wraps this in asyncio.create_task so POST /notes
    returns immediately with the note still status="draft".
    """
    try:
        record = notes.get_note(note_id)
    except ValueError:
        log.warning("notebook_pipeline: note %s vanished before transform", note_id)
        return

    result = await transform_note(record["raw_text"])
    if result is None:
        with contextlib.suppress(ValueError):
            notes.update_note(note_id, status="failed")
        log.warning("notebook_pipeline: note %s failed to structure after retry", note_id)
        return

    try:
        notes.set_pipeline(note_id, result)
    except ValueError:
        log.warning("notebook_pipeline: note %s deleted before pipeline completed", note_id)


def schedule_pipeline(note_id: str) -> None:
    """Sync entry point for the POST /notes route — fires trigger_pipeline
    as a background task without blocking the response."""
    task = asyncio.create_task(trigger_pipeline(note_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)
