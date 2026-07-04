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
    "title (a short, descriptive 3-8 word label capturing what the note is "
    "about — e.g. 'Person-identity migration blocked on perception-shop', "
    "NOT a truncated first line), summary (1-3 sentence string), technical "
    "(markdown string), plain (plain-language string), organized_md "
    "(markdown string), tags (array of strings), entities (array of "
    "strings — code symbols/files/concepts referenced). The entire user "
    "message is the raw note to structure."
)

_MODEL = "claude-sonnet-5"
_FENCE_RE = re.compile(r"^```(?:json)?\s*\n?(.*?)\n?```$", re.DOTALL)

# Retry/backoff for claude -p invocations. Supersedes the original Phase 1c
# "retry once" spec — live testing found the first claude -p call after an
# idle stretch intermittently returns an empty .result on BOTH of the
# original 2 attempts, while a fresh subsequent call succeeds immediately (a
# cold-start warmup race, not a content/logic problem — reproduced 3x via
# the daemon + 1x standalone, ruling out daemon-specific concurrency).
# master's call (2026-07-03): 3 attempts + backoff between them, giving the
# CLI time to warm up rather than hammering it back-to-back.
_MAX_CLAUDE_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (0.5, 1.5, 3.0)

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
    title: str
    summary: str
    technical: str
    plain: str
    organized_md: str
    tags: list[str]
    entities: list[str]


class RevalidationOutput(PipelineOutput):
    """revalidate_note()'s LLM response schema. `unchanged` is an explicit
    model judgment, not inferred from equality — see _REVALIDATE_INSTRUCTION_TEMPLATE
    and the bug this fixed: asking the model to "echo the exact same JSON" and then
    diffing its regenerated output for equality false-positives on every call, since
    free-text fields (organized_md especially) are never byte-identical across two
    independent generations even when the model's own judgment is "still accurate"."""

    unchanged: bool


# ---------------------------------------------------------------------------
# Grimoire (2026-07-04): study guides — a distinct KIND of note, housed +
# rendered rather than re-expressed. LOAD-BEARING INVARIANT: raw_text (the
# guide body) is NEVER LLM-rewritten by this pipeline — only `abstract` is
# LLM-generated; `toc` is a deterministic heading parse; `title` is never
# touched here at all (human/import-derived, see notes.add_study_guide).
# ---------------------------------------------------------------------------

_STUDY_GUIDE_INSTRUCTION = (
    "You are cataloging a study guide (a finished, long-form markdown "
    "deliverable) for a library card — NOT restructuring or rewriting it. "
    "Output ONLY a JSON object, no prose, no markdown fence, with keys: "
    "abstract (a 2-4 sentence library-card blurb describing what this "
    "guide covers and who would want it, written for someone scanning a "
    "library — not a summary for the guide's own reader), tags (array of "
    "strings), entities (array of strings — code symbols/files/concepts "
    "the guide references). Do NOT rewrite, restructure, or summarize the "
    "guide section-by-section — you are only producing metadata ABOUT it. "
    "The entire user message is the guide's raw markdown."
)


class StudyGuideOutput(BaseModel):
    abstract: str
    tags: list[str]
    entities: list[str]


_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*$")


def _slugify_heading(text: str) -> str:
    """GitHub-flavored-markdown-style heading slug: lowercase, strip
    non-alphanumeric (keep spaces/hyphens), spaces to hyphens, collapse
    repeats. Matches the anchor convention most markdown renderers (and
    the eventual guide-reader UI) use, so toc[].anchor is usable as a
    `#anchor` link without the frontend needing its own slugify pass."""
    slug = text.strip().lower()
    slug = re.sub(r"[^\w\s-]", "", slug)
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def _parse_toc(raw_text: str) -> list[dict[str, Any]]:
    """Deterministic heading parse — NOT an LLM call. Extracts every
    markdown ATX heading (# through ######) in document order as
    {title, anchor, level}.

    Skips headings inside fenced code blocks (```/~~~) — these are
    code-grounded guides, so a naive regex would false-positive on a
    commented-out '# heading' inside a code sample. Disambiguates
    duplicate heading titles (common in docs — multiple "## Example"
    sections) with a GFM-style `-1`/`-2` suffix, matching how GitHub
    itself resolves duplicate anchors."""
    toc: list[dict[str, Any]] = []
    seen_slugs: dict[str, int] = {}
    in_fence = False
    for line in raw_text.splitlines():
        stripped = line.strip()
        if stripped.startswith("```") or stripped.startswith("~~~"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        m = _HEADING_RE.match(stripped)
        if not m:
            continue
        title = m.group(2).strip()
        if not title:
            continue
        level = len(m.group(1))
        slug = _slugify_heading(title)
        if slug in seen_slugs:
            seen_slugs[slug] += 1
            slug = f"{slug}-{seen_slugs[slug]}"
        else:
            seen_slugs[slug] = 0
        toc.append({"title": title, "anchor": slug, "level": level})
    return toc


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


# Personal/Behavior folder (Joseph, 2026-07-03): notes.PERSONAL_TAB_ID notes
# are behavioral CONTEXT (voice, structure rules) injected into every LLM
# call, not answerable knowledge content — never embedded, never a search/
# ask source (see notebook_retrieval.upsert_note + Notebook.tsx filtering
# them out of the regular list). raw_text only, never the structured
# pipeline (no LLM paraphrase between the user's literal rules and the
# system prompt) — and personal notes skip structuring entirely anyway
# (api/notebook.py's create_note marks them "processed" directly).
_MAX_PERSONAL_CONTEXT_CHARS = 6000


def _personal_context() -> str:
    """Concatenated raw_text of every Personal/Behavior note, bounded — this
    rides every LLM call (structuring, revalidation, ask-synthesis), so it
    must stay small. Fail-open: any error returns "" (no context, not a
    crash) — a missing/broken personal folder must never break the ask."""
    try:
        personal_notes = notes.list_notes(tab_id=notes.PERSONAL_TAB_ID)
    except Exception:
        return ""
    parts = [n["raw_text"] for n in personal_notes if n.get("raw_text")]
    if not parts:
        return ""
    return "\n\n---\n\n".join(parts)[:_MAX_PERSONAL_CONTEXT_CHARS]


def _prepend_personal_context(instruction: str) -> str:
    personal = _personal_context()
    if not personal:
        return instruction
    return (
        "BEHAVIORAL CONTEXT — write and structure in this voice (the user's own "
        f"rules, always in force):\n\n{personal}\n\n---\n\n{instruction}"
    )


_SEED_PERSONAL_CONTEXT = """STRUCTURE + ANSWER in this voice (INTJ-T, senior engineer):
- Lead with the Goal/bottom-line (expert TL;DR), then 'In plain terms' (jargon-free, define terms inline), then the depth. (summary=Goal, plain=in-plain-terms, technical=depth.)
- Depth over surface. State real trade-offs, failure modes, and second-order effects — not just the happy path.
- Think in systems: data flow, contracts, boundaries; who calls what; what breaks under failure.
- Be direct; no hedging or filler. Name the real symbols/files/functions.
- Use Mermaid for data flows / state machines / architecture (not ASCII). Markdown headings, tables where they clarify.
- Distinguish must-fix-now from worth-knowing-later."""


def seed_personal_context_if_empty() -> bool:
    """One-time seed (Joseph, 2026-07-03): if the Personal/Behavior folder is
    empty, create one note with his distilled voice/structure rules.
    Idempotent — no-op once any personal note exists, so it never re-seeds
    or duplicates. Created directly as status="processed" (no structuring,
    no embed — same treatment every personal note gets, see api/notebook.py's
    create_note). Returns True if it seeded, False if already present."""
    if notes.list_notes(tab_id=notes.PERSONAL_TAB_ID):
        return False
    record = notes.add_note(
        _SEED_PERSONAL_CONTEXT,
        tab_id=notes.PERSONAL_TAB_ID,
        title="Voice & structure rules",
        repo=notes.GENERAL_REPO,
    )
    notes.update_note(record["id"], status="processed")
    return True


async def _invoke_claude(content: str, instruction: str) -> str:
    """Single headless-claude invocation. Returns the raw `.result` string.
    Raises on subprocess failure or a malformed envelope.

    `content` goes via stdin, `instruction` via --append-system-prompt — the
    shared recipe reused by the structuring pass, revalidate_note()'s
    "is this still accurate vs current code?" pass, and answer_question()'s
    synthesis pass. Each caller supplies its own instruction + content;
    JSON-schema parsing (structuring/revalidation) happens one layer up in
    _run_once — answer_question uses this raw string directly (free-form
    prose, not a PipelineOutput).

    Personal/Behavior context is prepended here — the one choke point every
    caller already funnels through — rather than at each call site.
    """
    instruction = _prepend_personal_context(instruction)
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
    return result_text


async def _run_once(
    content: str, *, instruction: str = _INSTRUCTION, schema: type[BaseModel] = PipelineOutput
) -> BaseModel:
    """Structuring/revalidation invocation — parses+validates the result
    against `schema` (PipelineOutput for structuring, RevalidationOutput for
    revalidate_note's re-check pass). Raises on any parse/validate failure."""
    result_text = await _invoke_claude(content, instruction)
    payload = json.loads(_strip_fence(result_text))
    return schema.model_validate(payload)


async def transform_note(
    raw_text: str,
    *,
    instruction: str = _INSTRUCTION,
    schema: type[BaseModel] = PipelineOutput,
) -> dict[str, Any] | None:
    """Run the transform, retrying up to _MAX_CLAUDE_ATTEMPTS with backoff
    between attempts on parse/validate failure.

    Returns the validated dict on success (shaped per `schema`), None if
    every attempt failed — the caller marks the note status="failed" and
    keeps raw_text.
    """
    for attempt in range(1, _MAX_CLAUDE_ATTEMPTS + 1):
        try:
            output = await _run_once(raw_text, instruction=instruction, schema=schema)
            return output.model_dump()
        except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError, OSError) as exc:
            log.warning("notebook_pipeline: attempt %d failed: %s", attempt, exc)
            if attempt < _MAX_CLAUDE_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
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

    # `title` is promoted to the note's top-level display title (list rows,
    # grid cards, reader header, @-mention label) — not kept in the stored
    # pipeline dict, which only carries the structured-section fields.
    title = result.pop("title", None)
    try:
        updated = notes.set_pipeline(note_id, result, title=title)
    except ValueError:
        log.warning("notebook_pipeline: note %s deleted before pipeline completed", note_id)
        return

    await asyncio.to_thread(notebook_retrieval.upsert_note, updated)


async def trigger_study_guide_pipeline(note_id: str) -> None:
    """Transform note_id's raw_text into a study-guide pipeline and write it
    back. Same shape as trigger_pipeline, but NEVER touches raw_text — the
    load-bearing invariant — and produces the discriminated pipeline shape
    {abstract, toc, tags, entities} instead of the note pipeline's
    summary/technical/plain/organized_md.

    `abstract`/`tags`/`entities` come from the LLM (StudyGuideOutput);
    `toc` is a deterministic heading parse (_parse_toc) — never touches the
    LLM, so a guide's navigation structure can't drift from its actual
    headings."""
    try:
        record = notes.get_note(note_id)
    except ValueError:
        log.warning("notebook_pipeline: study guide %s vanished before transform", note_id)
        return

    result = await transform_note(
        record["raw_text"], instruction=_STUDY_GUIDE_INSTRUCTION, schema=StudyGuideOutput
    )
    if result is None:
        with contextlib.suppress(ValueError):
            notes.update_note(note_id, status="failed")
        log.warning("notebook_pipeline: study guide %s failed to structure after retry", note_id)
        return

    pipeline = {
        "abstract": result["abstract"],
        "toc": _parse_toc(record["raw_text"]),
        "tags": result["tags"],
        "entities": result["entities"],
    }
    try:
        updated = notes.set_study_guide_pipeline(note_id, pipeline)
    except ValueError:
        log.warning("notebook_pipeline: study guide %s deleted before pipeline completed", note_id)
        return

    await asyncio.to_thread(notebook_retrieval.upsert_note, updated)


def schedule_pipeline(note_id: str) -> None:
    """Sync entry point for the POST /notes route — fires the right
    structuring pipeline (kind-branched: study guide vs regular note) as a
    background task without blocking the response. The branch lives here,
    not at each call site, so callers (api/notebook.py, notebook_import.py)
    never need their own kind check to know which pipeline to trigger."""
    try:
        record = notes.get_note(note_id)
    except ValueError:
        log.warning("notebook_pipeline: note %s vanished before scheduling", note_id)
        return
    coro = (
        trigger_study_guide_pipeline(note_id)
        if record.get("kind") == "study_guide"
        else trigger_pipeline(note_id)
    )
    task = asyncio.create_task(coro)
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
    "JSON object with keys summary/technical/plain/organized_md/tags/entities (no "
    "title — generate one fresh). Output a JSON object with keys title/summary/"
    "technical/plain/organized_md/tags/entities plus one extra boolean key "
    "`unchanged`. ALWAYS provide title (a short, descriptive 3-8 word label) — it is "
    "applied either way, regardless of `unchanged`. Judge `unchanged` by SUBSTANCE of "
    "summary/technical/plain/organized_md/tags/entities, not exact wording — minor "
    "rephrasing is not a change. Set unchanged=true if the note's existing "
    "conclusions are still accurate given the current code (you may echo those field "
    "values back, they will be discarded either way — title is NOT discarded). Set "
    "unchanged=false and provide CORRECTED values for summary/technical/plain/"
    "organized_md/tags/entities if the note is stale or wrong given the current code. "
    "Output ONLY the JSON object, no prose, no markdown fence.\n\nCURRENT CODE:\n{code}"
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

    General-bucket notes (repo == notes.GENERAL_REPO, cross-cutting notes
    with no codebase) return as-is immediately — there's nothing to
    validate against, so this isn't a degrade and doesn't warn.
    """
    record = notes.get_note(note_id)
    if record.get("repo") == notes.GENERAL_REPO:
        return record

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
    result = await transform_note(
        json.dumps(pipeline or {}, indent=2), instruction=instruction, schema=RevalidationOutput
    )

    if result is None:
        log.warning(
            "notebook_pipeline: revalidation of %s failed to parse after retry; "
            "leaving record unchanged",
            note_id,
        )
        return record

    # Explicit model judgment, not equality-diffing its own regenerated JSON —
    # see RevalidationOutput's docstring for why that was the actual bug.
    unchanged = result.pop("unchanged")
    # Title backfill (Joseph, 2026-07-03): applied on every revalidate pass
    # that reaches the LLM, independent of `unchanged` — a note's title
    # deserves fixing even on a "still accurate" check, not just a heal.
    title = result.pop("title", None)
    new_pipeline = None if unchanged else result
    updated = notes.apply_validation(
        note_id, git_sha=current_sha, new_pipeline=new_pipeline, title=title
    )
    if new_pipeline is not None:
        # Only re-embed on an actual heal — content is identical otherwise,
        # so re-embedding would just waste an embed+upsert call.
        await asyncio.to_thread(notebook_retrieval.upsert_note, updated)
    return updated


# ---------------------------------------------------------------------------
# Phase 2c: the ask-layer (the north-star capstone)
#
# ask → retrieve candidate notes → staleness-gated revalidate each hit
# (cheap when the code hasn't moved, heals it when it has) → synthesize an
# answer from the now-code-current note bodies. Every note the answer draws
# on has just been re-grounded against the actual code, not a stale cache.
#
# Phase 2c-v2 (ask-layer v2): also grounds the answer in a live semantic
# search of the actual codebase (Séance), not just the notes — "notes are
# framing, code is ground truth" made visible in the answer itself.
# ---------------------------------------------------------------------------

_ASK_INSTRUCTION_TEMPLATE = (
    "Answer the question using the notes below (just re-validated against "
    "the current code) plus the RELEVANT CODE section if present (a fresh "
    "semantic search of the live codebase — treat it as ground truth over "
    "a note if the two conflict). Cite the note titles AND any code "
    "file:line references you drew on. If nothing below covers the "
    "question, say so plainly — do not invent an answer.\n\nNOTES:\n"
    "{notes}\n{code}"
)

_SEANCE_CODE_TOP_K = 8


def _seance_code_search(
    repo: str, question: str, top_k: int = _SEANCE_CODE_TOP_K
) -> tuple[list[dict[str, Any]], bool, bool]:
    """Semantic code search on `repo` via Séance's in-process search core —
    same pattern as api/oracle.py's `_seance_search` (proven prod code path,
    not new integration surface): asyncio.to_thread around a sync
    SearchEngine.search() call.

    Returns (chunks, indexed, errored):
      chunks:  Séance SearchResult dicts (raw — no `repo` key yet, caller adds it)
      indexed: whether `repo` has a real (non-empty) Séance collection. Séance
               auto-creates an EMPTY collection on a miss, so an empty-results
               check alone can't distinguish "not indexed" from "indexed but
               no match" — checked explicitly via VectorStore.list_projects()
               so callers know whether to trust an empty result or fall back.
      errored: True only on an actual exception (import fail, embed API
               error). NOTE: load_config() raises SystemExit (not Exception)
               when GOOGLE_AI_API_KEY is unset — a BaseException, so it must
               be caught explicitly alongside Exception (a real gap present
               in oracle.py's own version of this function; flagged, not
               fixed there — out of this task's scope).
    """
    try:
        from seance.config import load_config
        from seance.search.engine import SearchEngine
        from seance.storage.vectordb import VectorStore

        config = load_config()
        safe_name = repo.replace("-", "_").replace(".", "_")[:63]
        indexed = any(
            p.get("name") == safe_name and p.get("chunks", 0) > 0
            for p in VectorStore(config).list_projects()
        )
        if not indexed:
            return [], False, False

        engine = SearchEngine(config)
        results = engine.search(project_name=repo, query=question, top_k=top_k)
        return [r.to_dict() for r in results], True, False
    except (Exception, SystemExit) as exc:
        log.warning("notebook_pipeline: seance code search failed for repo=%r: %s", repo, exc)
        return [], False, True


def _grep_code_fallback(repo_root: Path, question: str) -> list[dict[str, Any]]:
    """Deterministic keyword-grep fallback when `repo` isn't Séance-indexed.

    Reuses khimaira.context.resolver's keyword-extraction + ripgrep utility
    (the same mechanism already used for agent-dispatch context resolution)
    rather than inventing a second grep-scoring scheme."""
    from khimaira.context.resolver import _grep_keywords

    hits = _grep_keywords(question, repo_root)
    hits.sort(key=lambda h: h[1], reverse=True)
    chunks: list[dict[str, Any]] = []
    for path, _score, snippet, line_range, _reason in hits[:_SEANCE_CODE_TOP_K]:
        start, end = line_range if line_range else (1, 1)
        chunks.append(
            {
                "file_path": path,
                "start_line": start,
                "end_line": end,
                "symbol_name": "",
                "text": snippet,
            }
        )
    return chunks


async def _code_grounding_for_repo(repo: str, question: str) -> tuple[list[dict[str, Any]], bool]:
    """Returns (chunks, unavailable). Trusts an indexed Séance search even
    when it returns zero chunks (a valid "no match", not unavailable — same
    semantics as oracle.py). Falls back to grep only when `repo` isn't
    indexed or Séance errored; `unavailable=True` only when that fallback
    ALSO can't resolve anything (repo root unresolvable, or grep found
    nothing) — never hard-fails the ask, just degrades to notes-only."""
    chunks, indexed, _errored = await asyncio.to_thread(_seance_code_search, repo, question)
    if indexed:
        return [dict(c, repo=repo) for c in chunks], False

    repo_root = _repo_root(repo)
    if repo_root is None:
        return [], True

    grep_chunks = await asyncio.to_thread(_grep_code_fallback, repo_root, question)
    return [dict(c, repo=repo) for c in grep_chunks], not bool(grep_chunks)


def _format_code_section(chunks: list[dict[str, Any]]) -> str:
    if not chunks:
        return ""
    lines = ["---", "", "RELEVANT CODE (live search, current as of this ask):", ""]
    for c in chunks:
        ref = f"{c['repo']}/{c['file_path']}:{c['start_line']}-{c['end_line']}"
        symbol = f" ({c['symbol_name']})" if c.get("symbol_name") else ""
        lines += [f"### `{ref}`{symbol}", "", c["text"], ""]
    return "\n".join(lines)


async def _synthesize_answer(question: str, instruction: str) -> str | None:
    """Free-form prose, not the PipelineOutput schema — same retry/backoff
    discipline as transform_note, but no JSON parse/validate."""
    for attempt in range(1, _MAX_CLAUDE_ATTEMPTS + 1):
        try:
            return (await _invoke_claude(question, instruction)).strip()
        except (RuntimeError, ValueError, OSError) as exc:
            log.warning("notebook_pipeline: answer synthesis attempt %d failed: %s", attempt, exc)
            if attempt < _MAX_CLAUDE_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
    return None


_NO_NOTES_ANSWER = "No relevant notes found."


_EMPTY_ANSWER: dict[str, Any] = {
    "answer": _NO_NOTES_ANSWER,
    "sources": [],
    "healed": [],
    "code_sources": [],
    "code_unavailable": [],
}


async def answer_question(
    question: str,
    *,
    repo: str | None = None,
    mentioned_note_ids: list[str] | None = None,
    exclusive: bool = False,
) -> dict[str, Any]:
    """ask → retrieve → heal-against-code → ground-in-live-code → answer.

    `mentioned_note_ids` are @-referenced notes from the ask bar. Default
    (`exclusive=False`, "prioritized"): mentioned notes always join the
    answer's sources, in addition to the normal semantic-retrieval hits
    (deduped, mentioned-first). `exclusive=True`: skip retrieval entirely
    and answer from ONLY the mentioned notes — same downstream code, one
    conditional, so flipping the default later is a one-line change.

    Returns {answer, sources, healed, code_sources, code_unavailable}.
    `sources` is every note the answer drew on (post-revalidation); `healed`
    is the subset that actually changed during this call. `code_sources` is
    every code chunk (Séance or grep-fallback) fed into the synthesis;
    `code_unavailable` lists repos where NEITHER Séance nor grep could
    ground anything (not indexed / not resolvable) — a degrade, not a
    failure.
    """
    mentioned_note_ids = mentioned_note_ids or []

    if exclusive and mentioned_note_ids:
        note_ids = list(dict.fromkeys(mentioned_note_ids))
    else:
        hits = await notebook_retrieval.search_notes_async(question, repo=repo)
        note_ids = list(dict.fromkeys([*mentioned_note_ids, *(h["note_id"] for h in hits)]))

    if not note_ids:
        return dict(_EMPTY_ANSWER)

    sources: list[str] = []
    healed: list[str] = []
    note_sections: list[str] = []
    repos_seen: set[str] = set()
    for note_id in note_ids:
        try:
            before = notes.get_note(note_id)
        except ValueError:
            continue  # indexed but deleted since — skip, don't fail the whole ask
        before_history_len = len(before.get("history") or [])

        try:
            updated = await revalidate_note(note_id)
        except ValueError:
            continue

        if len(updated.get("history") or []) > before_history_len:
            healed.append(note_id)

        pipeline = updated.get("pipeline") or {}
        body = (
            pipeline.get("organized_md") or pipeline.get("summary") or updated.get("raw_text", "")
        )
        if not body:
            continue
        sources.append(note_id)
        note_sections.append(f"### {updated.get('title', note_id)}\n\n{body}")
        repos_seen.add(updated["repo"])

    if not note_sections:
        return {**_EMPTY_ANSWER, "healed": healed}

    code_chunks: list[dict[str, Any]] = []
    code_unavailable: list[str] = []
    for r in sorted(repos_seen):
        if r == notes.GENERAL_REPO:
            continue  # no codebase to ground General notes against — not a degrade
        chunks, unavailable = await _code_grounding_for_repo(r, question)
        code_chunks.extend(chunks)
        if unavailable:
            code_unavailable.append(r)

    instruction = _ASK_INSTRUCTION_TEMPLATE.format(
        notes="\n\n---\n\n".join(note_sections), code=_format_code_section(code_chunks)
    )
    answer = await _synthesize_answer(question, instruction)
    if answer is None:
        answer = (
            "Found relevant notes but couldn't synthesize an answer right now — "
            "see the cited sources below."
        )
    code_sources = [
        {
            "repo": c["repo"],
            "file_path": c["file_path"],
            "start_line": c["start_line"],
            "end_line": c["end_line"],
        }
        for c in code_chunks
    ]
    return {
        "answer": answer,
        "sources": sources,
        "healed": healed,
        "code_sources": code_sources,
        "code_unavailable": code_unavailable,
    }
