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
from khimaira.monitor import notebook_retrieval, notes

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


async def _run_once(content: str, *, instruction: str = _INSTRUCTION) -> PipelineOutput:
    """Single headless-claude invocation. Raises on any parse/validate failure.

    `content` goes via stdin, `instruction` via --append-system-prompt — reused
    by both the initial structuring pass (default instruction) and
    revalidate_note()'s "is this still accurate vs current code?" pass (its
    own instruction, the note's existing pipeline JSON as content).
    """
    cfg_dir = _isolated_config_dir()
    env = dict(os.environ)
    env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)

    proc = await asyncio.create_subprocess_exec(
        _resolve_claude_cmd(),
        "-p",
        "--append-system-prompt",
        instruction,
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
    stdout, stderr = await proc.communicate(content.encode("utf-8"))
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


async def transform_note(
    raw_text: str, *, instruction: str = _INSTRUCTION
) -> dict[str, Any] | None:
    """Run the transform, retrying once on parse/validate failure.

    Returns the validated pipeline dict on success, None if both attempts
    failed — the caller marks the note status="failed" and keeps raw_text.
    """
    for attempt in (1, 2):
        try:
            output = await _run_once(raw_text, instruction=instruction)
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
        updated = notes.set_pipeline(note_id, result)
    except ValueError:
        log.warning("notebook_pipeline: note %s deleted before pipeline completed", note_id)
        return

    await asyncio.to_thread(notebook_retrieval.upsert_note, updated)


def schedule_pipeline(note_id: str) -> None:
    """Sync entry point for the POST /notes route — fires trigger_pipeline
    as a background task without blocking the response."""
    task = asyncio.create_task(trigger_pipeline(note_id))
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


# ---------------------------------------------------------------------------
# North-star: per-note revalidation (self-healing, code-grounded KB)
#
# Code is the source of truth; notes are re-validated caches of it. A note's
# `entities` (file-shaped ones) are the anchors — the staleness gate compares
# a `git diff` since the note's last validated SHA against those anchors, and
# only pays for an LLM re-check when something anchor-relevant actually moved.
# ---------------------------------------------------------------------------

_ANCHOR_EXTENSIONS = (
    ".py",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".md",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
)
_MAX_ANCHOR_FILES = 5
_MAX_ANCHOR_FILE_CHARS = 20_000  # per-file cap fed into the revalidation prompt

_REVALIDATE_INSTRUCTION_TEMPLATE = (
    "You are checking whether a previously-structured note is still accurate against "
    "the CURRENT source code below. The user message contains the EXISTING NOTE as a "
    "JSON object with keys summary/technical/plain/organized_md/tags/entities. If it "
    "is still accurate, output that EXACT SAME JSON object unchanged. If it is stale "
    "or wrong given the current code, output a CORRECTED JSON object with the same "
    "schema, reflecting the current code. Output ONLY the JSON object, no prose, no "
    "markdown fence.\n\nCURRENT CODE:\n{code}"
)


def _repo_root(repo: str) -> Path | None:
    """Resolve a note's `repo` tag to a filesystem path via khimaira's project
    discovery registry (the same one /api/projects uses) — NOT the daemon's
    per-tab :name route param, which can be a KG-attachment label (e.g.
    "backend") rather than a real discovered project name."""
    from khimaira.config import ROOTS
    from khimaira.monitor.discovery.project import discover

    for project in discover(ROOTS):
        if project.name == repo:
            return project.path
    return None


def _looks_like_file_entity(entity: str) -> bool:
    return any(entity.endswith(ext) for ext in _ANCHOR_EXTENSIONS)


def _resolve_anchor_files(
    repo_root: Path, entities: list[str], cap: int = _MAX_ANCHOR_FILES
) -> list[Path]:
    """Resolve file-shaped entities to real files under repo_root by basename.

    Non-file entities (concepts like "session reaper") are skipped — they
    don't map to a trackable path for the git-diff staleness gate. Matching
    is deterministic (glob by basename), not an LLM guess — per the
    ai-engineering rule, resolution is code, the LLM only transforms text.
    """
    resolved: list[Path] = []
    seen: set[str] = set()
    for entity in entities:
        if not _looks_like_file_entity(entity):
            continue
        basename = Path(entity).name
        if basename in seen:
            continue
        seen.add(basename)
        matches = list(repo_root.rglob(basename))
        if matches:
            resolved.append(matches[0])
        if len(resolved) >= cap:
            break
    return resolved


async def _run_git(repo_root: Path, *args: str) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git",
        "-C",
        str(repo_root),
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _stderr = await proc.communicate()
    return proc.returncode, stdout.decode("utf-8", "ignore").strip()


async def _current_git_sha(repo_root: Path) -> str | None:
    rc, out = await _run_git(repo_root, "rev-parse", "HEAD")
    return out if rc == 0 and out else None


async def _anchor_files_changed(repo_root: Path, since_sha: str, anchor_files: list[Path]) -> bool:
    """True if `git diff --name-only since_sha..HEAD` touches any anchor file.

    Fails safe toward re-checking: any git error, or no anchor files resolved
    at all (nothing to compare — can't prove nothing changed), returns True
    so the caller pays for the LLM re-check rather than silently trusting a
    stale note forever.
    """
    if not anchor_files:
        return True
    rc, out = await _run_git(repo_root, "diff", "--name-only", f"{since_sha}..HEAD")
    if rc != 0:
        return True
    changed = set(out.splitlines())
    anchor_rel: set[str] = set()
    for path in anchor_files:
        try:
            anchor_rel.add(str(path.relative_to(repo_root)))
        except ValueError:
            continue
    return bool(changed & anchor_rel)


async def revalidate_note(note_id: str) -> dict[str, Any]:
    """Re-ground a note against its repo's current code (the north-star core).

    Staleness gate: if the note has a prior validated_git_sha and none of its
    resolved anchor files changed since that SHA, stamps validated_git_sha to
    HEAD and returns without an LLM call — the cost-saver. Otherwise runs the
    same claude -p recipe as the initial transform, but asks "is this still
    accurate vs current code?" If the model's answer differs from the
    existing pipeline, that's a HEAL: the old pipeline is pushed to history
    before the new one lands.

    Fails open on anything that isn't the note itself: unresolvable repo, not
    a git checkout, or an LLM parse failure all leave the record untouched
    (never corrupt good data over a health-check glitch) and log a warning.
    Raises ValueError only if note_id itself doesn't exist (mirrors get_note).
    """
    record = notes.get_note(note_id)
    repo = record.get("repo") or "khimaira"
    repo_root = _repo_root(repo)
    if repo_root is None:
        log.warning(
            "notebook_pipeline: repo %r not found in project registry for note %s; "
            "skipping revalidation",
            repo,
            note_id,
        )
        return record

    current_sha = await _current_git_sha(repo_root)
    if current_sha is None:
        log.warning(
            "notebook_pipeline: %s is not a git checkout; skipping revalidation of %s",
            repo_root,
            note_id,
        )
        return record

    pipeline = record.get("pipeline")
    entities = (pipeline or {}).get("entities", [])
    anchor_files = _resolve_anchor_files(repo_root, entities)

    prior_sha = record.get("validated_git_sha")
    if prior_sha and record.get("last_validated_at"):
        changed = await _anchor_files_changed(repo_root, prior_sha, anchor_files)
        if not changed:
            return notes.apply_validation(note_id, git_sha=current_sha, new_pipeline=None)

    code_sections = []
    for path in anchor_files:
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")[:_MAX_ANCHOR_FILE_CHARS]
            code_sections.append(f"### {path.relative_to(repo_root)}\n\n{text}")
        except OSError:
            continue
    code_blob = "\n\n---\n\n".join(code_sections) if code_sections else "(no anchor files resolved)"

    instruction = _REVALIDATE_INSTRUCTION_TEMPLATE.format(code=code_blob)
    result = await transform_note(json.dumps(pipeline or {}, indent=2), instruction=instruction)

    if result is None:
        log.warning(
            "notebook_pipeline: revalidation of %s failed to parse after retry; "
            "leaving record unchanged",
            note_id,
        )
        return record

    new_pipeline = None if result == pipeline else result
    updated = notes.apply_validation(note_id, git_sha=current_sha, new_pipeline=new_pipeline)
    if new_pipeline is not None:
        # Only re-embed on an actual heal — content is identical otherwise,
        # so re-embedding would just waste an embed+upsert call.
        await asyncio.to_thread(notebook_retrieval.upsert_note, updated)
    return updated
