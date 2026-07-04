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
import tempfile
import uuid
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

# Grimoire Phase 4 addendum (2026-07-04): a bulk import schedules ONE
# asyncio.create_task per guide (schedule_pipeline/import_dir), with no
# concurrency cap of its own — Joseph's 202-guide corpus would otherwise
# spawn 202 concurrent `claude -p` structuring subprocesses at once,
# swamping the concurrency-proxy's 32-session throttle and starving every
# other roster session mid-import. Gated at THIS chokepoint (every
# structuring/organize/revalidate/ask-synthesis call already funnels
# through _invoke_claude) rather than at each individual call site, so a
# bulk import's scheduled tasks simply queue behind the semaphore and drain
# _STRUCTURE_CONCURRENCY-at-a-time instead of flooding.
#
# Research calls (_invoke_claude_agentic) are a SEPARATE code path (their
# own --allowedTools/--add-dir/stream-json subprocess spawn, not this
# function) and are deliberately NOT gated by this semaphore — they're
# user-serial (one human, one ask at a time), not a bulk-scheduling risk.
_STRUCTURE_CONCURRENCY = int(os.environ.get("KHIMAIRA_NOTEBOOK_STRUCTURE_CONCURRENCY", "3"))
_STRUCTURE_SEMAPHORE = asyncio.Semaphore(_STRUCTURE_CONCURRENCY)


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


def _scan_headings(raw_text: str) -> list[dict[str, Any]]:
    """Single fence-aware ATX heading scan — the one place heading detection
    happens. Extracts every markdown heading (# through ######) in document
    order as {title, anchor, level, line} (0-indexed line number).

    Skips headings inside fenced code blocks (```/~~~) — these are
    code-grounded guides, so a naive regex would false-positive on a
    commented-out '# heading' inside a code sample. Disambiguates duplicate
    heading titles (common in docs — multiple "## Example" sections) with a
    GFM-style `-1`/`-2` suffix, matching how GitHub itself resolves
    duplicate anchors.

    `_parse_toc` (the stored/rendered shape) and `splice_section` (Grimoire
    Phase 3's REVISE apply helper) both derive from this single scan, so
    anchor generation can never disagree between "where a section starts
    for rendering" and "where a section starts for splicing a rewrite in."
    """
    headings: list[dict[str, Any]] = []
    seen_slugs: dict[str, int] = {}
    in_fence = False
    for i, line in enumerate(raw_text.splitlines()):
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
        headings.append({"title": title, "anchor": slug, "level": level, "line": i})
    return headings


def _parse_toc(raw_text: str) -> list[dict[str, Any]]:
    """Deterministic heading parse — NOT an LLM call. Public/stored shape:
    {title, anchor, level} per heading, in document order (no `line` — that's
    an internal concern of `splice_section`, not part of the toc contract
    already shipped to the frontend/MCP layer)."""
    return [
        {"title": h["title"], "anchor": h["anchor"], "level": h["level"]}
        for h in _scan_headings(raw_text)
    ]


def splice_section(raw_text: str, anchor: str, new_section_text: str) -> str:
    """Deterministically replace ONE section (from its heading line through
    the line before the next heading of level <= its own, or EOF) with
    `new_section_text`.

    Pure string splice — the model (research_revise) only ever PROPOSES
    new_section_text; this is what actually places it, so a model that
    ignores "only give me this one section" can't silently overwrite
    unrelated content — the write is confined to exactly the slot `anchor`
    identifies, regardless of what the model returned.

    Raises ValueError if `anchor` isn't found in raw_text's CURRENT heading
    scan — the caller must derive `anchor` from this same raw_text (e.g. via
    the note's stored `toc`), so a miss means the guide changed underneath
    the request.
    """
    headings = _scan_headings(raw_text)
    match = next((h for h in headings if h["anchor"] == anchor), None)
    if match is None:
        raise ValueError(f"No section anchored at {anchor!r} in this guide's current raw_text.")

    lines = raw_text.splitlines()
    start = match["line"]
    level = match["level"]
    end = len(lines)
    for h in headings:
        if h["line"] > start and h["level"] <= level:
            end = h["line"]
            break

    new_lines = lines[:start] + new_section_text.rstrip("\n").splitlines() + lines[end:]
    return "\n".join(new_lines)


def _isolated_config_dir() -> Path:
    """Dedicated, PER-CALL-UNIQUE CLAUDE_CONFIG_DIR: credentials only, empty
    settings (no hooks), no CLAUDE.md. This is what keeps each transform
    cheap (kills the ~50k context load) and non-polluting (no SessionStart
    hook == no khimaira session registered for the headless run).

    Grimoire Phase 4 addendum (2026-07-04): this used to return a SINGLE
    SHARED path across every invocation — audited as the mechanism behind
    an intermittent "no stdin data received in 3s" structuring failure
    during the bulk import: Claude Code writes shared, backup-rotated state
    (`.claude.json` + timestamped `backups/`) into its config dir on
    startup, and concurrent `claude -p` processes racing on that SAME
    shared file (now that the structuring semaphore allows 3 genuinely
    concurrent calls) could stall past the CLI's own ~3s stdin-read
    deadline. Each call now gets its OWN throwaway directory — no shared
    mutable state between concurrent invocations, by construction. Callers
    are responsible for removing the returned directory when done (see
    `_cleanup_config_dir`) — a bulk import shouldn't accumulate hundreds of
    stale directories.

    Credentials are copied fresh into the new directory every call — cheap,
    and the real ~/.claude credentials rotate via OAuth refresh
    independently of this dir, so a one-time copy would eventually go stale
    and silently break auth.
    """
    xdg = Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    base = xdg / "khimaira" / "notebook" / "claude-config"
    base.mkdir(parents=True, exist_ok=True)
    cfg_dir = Path(tempfile.mkdtemp(prefix=f"call-{uuid.uuid4().hex[:8]}-", dir=str(base)))

    (cfg_dir / "settings.json").write_text("{}", encoding="utf-8")

    src_creds = Path(os.path.expanduser("~/.claude/.credentials.json"))
    if src_creds.is_file():
        shutil.copy2(src_creds, cfg_dir / ".credentials.json")

    return cfg_dir


def _cleanup_config_dir(cfg_dir: Path) -> None:
    """Remove a per-call config dir after use. Fail-open — a cleanup
    failure (e.g. a lingering file handle) must never surface as the
    call's own failure; it just leaves one throwaway directory behind."""
    shutil.rmtree(cfg_dir, ignore_errors=True)


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


def _is_global_personal_repo(note_repo: str | None) -> bool:
    """A personal note counts as GLOBAL if it's explicitly tagged
    notes.GENERAL_REPO, OR has no repo tag at all (empty string/None) — the
    default for add_note/create_note when a caller doesn't pass one. An
    untagged note is "no domain was ever chosen", not "belongs to some
    domain nobody named" — treating it as global (over-inclusion, the
    recoverable direction) is what preserves today's blanket-inject
    behavior for every already-existing personal note, rather than
    silently dropping any note that predates repo-scoping out of every LLM
    call the moment this ships."""
    return not note_repo or note_repo == notes.GENERAL_REPO


def _personal_context(target_repo: str | None = None) -> str:
    """Concatenated llm_view() of every IN-SCOPE Personal/Behavior note,
    bounded — this rides every LLM call (structuring, revalidation,
    ask-synthesis, chat), so it must stay small. Fail-open: any error
    returns "" (no context, not a crash) — a missing/broken personal folder
    must never break the ask.

    Repo-scoping (2026-07-04, tasks/grimoire/PERSONAL-CONTEXT-SCOPING.md): a
    GLOBAL personal note (repo == notes.GENERAL_REPO, OR unset/"" — see
    `_is_global_personal_repo`) always injects, regardless of `target_repo`.
    A DOMAIN personal note (repo == a real repo tag) injects ONLY when it
    matches `target_repo` — a jeevy-specific voice/behavior rule shouldn't
    ride a khimaira guide's structuring call. `target_repo=None` means
    global-only. Global notes are concatenated first, domain notes after,
    so global voice always leads.

    Sensitive notes (2026-07-04, CRITICAL cross-note leak this closes): a
    sensitive note filed into the Personal folder must never inject its
    real secrets into EVERY other LLM call via this concatenation. Routes
    through notes.llm_view(), which needs the FULL record (llm_text isn't
    carried on the list_notes() index stub — only single-note reads have
    it) — an N+1 get_note() per IN-SCOPE personal note (out-of-scope domain
    notes are filtered before the fetch, so a large personal folder with
    many domains doesn't pay for notes outside this call's scope)."""
    try:
        personal_notes = notes.list_notes(tab_id=notes.PERSONAL_TAB_ID)
    except Exception:
        return ""

    global_stubs = [s for s in personal_notes if _is_global_personal_repo(s.get("repo"))]
    domain_stubs = [
        s
        for s in personal_notes
        if not _is_global_personal_repo(s.get("repo"))
        and target_repo is not None
        and s.get("repo") == target_repo
    ]

    parts: list[str] = []
    for stub in (*global_stubs, *domain_stubs):
        try:
            record = notes.get_note(stub["id"])
        except ValueError:
            continue
        text = notes.llm_view(record)
        if text:
            parts.append(text)
    if not parts:
        return ""
    return "\n\n---\n\n".join(parts)[:_MAX_PERSONAL_CONTEXT_CHARS]


def _prepend_personal_context(instruction: str, target_repo: str | None = None) -> str:
    personal = _personal_context(target_repo)
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


async def _invoke_claude(content: str, instruction: str, *, target_repo: str | None = None) -> str:
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
    `target_repo` (2026-07-04): the repo tag of the note/guide/question this
    call is FOR — threaded to _prepend_personal_context so a domain personal
    note only rides a call for its own repo (global personal notes always
    ride every call regardless). None means global-only.

    The actual subprocess spawn is gated by `_STRUCTURE_SEMAPHORE` (bound
    `_STRUCTURE_CONCURRENCY`, default 3, env `KHIMAIRA_NOTEBOOK_STRUCTURE_
    CONCURRENCY`) — see that constant's comment for why. Only the spawn+
    communicate is inside the gate, not the (cheap, non-subprocess) isolated
    config dir setup above.
    """
    instruction = _prepend_personal_context(instruction, target_repo)
    cfg_dir = _isolated_config_dir()
    try:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)

        async with _STRUCTURE_SEMAPHORE:
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
    finally:
        _cleanup_config_dir(cfg_dir)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p exited {proc.returncode}: {stderr.decode('utf-8', 'ignore')[:500]}"
        )

    envelope = json.loads(stdout.decode("utf-8"))
    result_text = envelope.get("result")
    if not isinstance(result_text, str):
        raise ValueError(f"envelope missing string .result: {envelope!r}")
    return result_text


# ---------------------------------------------------------------------------
# Grimoire Phase 3: the research-scientist ask. `_invoke_claude_agentic`
# grants READ-ONLY tools (Read/Grep/Glob/WebSearch/WebFetch) — NEVER Edit/
# Write/Bash. Write-safety is structural: a guide is a note in the store, so
# "edit" = update_note(raw_text=), applied by the backend after a human
# reviews a diff — never by the model itself.
#
# Phase 0 finding (2026-07-04, live-tested against 6 real headless calls):
# WebSearch/WebFetch genuinely work under an isolated CLAUDE_CONFIG_DIR, but
# on a weakly-phrased prompt the model can silently SKIP calling the tool and
# fabricate a plausible, confidently-cited answer instead — with NO error
# signal anywhere (is_error=False, permission_denials=[], stop_reason=
# end_turn). Worse: the final envelope's usage.server_tool_use.{web_search_
# requests,web_fetch_requests} stays 0 even on a GENUINE successful tool
# call (it meters the Anthropic-API-native server-side web tool, a
# different, billing-only path — NOT Claude Code's own client-implemented
# WebSearch/WebFetch tools used here). It is not a usable grounding signal.
# The only reliable check is parsing the --output-format stream-json
# transcript for actual tool_use blocks naming WebSearch/WebFetch — done
# here, deterministically, never trusting the model's own claim of having
# searched (the perception/validation-boundary discipline: an LLM's claim
# of grounding is not itself trustworthy evidence of grounding).
# ---------------------------------------------------------------------------

_AGENTIC_MODEL = "claude-sonnet-5"
_AGENTIC_ALLOWED_TOOLS = "Read,Grep,Glob,WebSearch,WebFetch"
_AGENTIC_DEFAULT_BUDGET_USD = float(os.environ.get("KHIMAIRA_NOTEBOOK_RESEARCH_BUDGET_USD", "1.5"))
_WEB_TOOL_NAMES = frozenset({"WebSearch", "WebFetch"})

# master's refinement (2026-07-04): an imperative-first instruction reliably
# flipped a silent tool-skip into a real tool call in Phase 0 testing —
# belt-and-suspenders alongside the transcript check below (suspenders).
_GROUNDING_IMPERATIVE = (
    "You MUST actually call your WebSearch/WebFetch/Read/Grep/Glob tools to "
    "ground every factual claim — NEVER answer from memory alone, and NEVER "
    "state a web- or code-attributed fact you have not just retrieved with "
    "one of these tools in THIS turn."
)


async def _invoke_claude_agentic(
    content: str,
    instruction: str,
    *,
    repo_root: Path | None = None,
    max_budget_usd: float = _AGENTIC_DEFAULT_BUDGET_USD,
    target_repo: str | None = None,
) -> dict[str, Any]:
    """Headless, read-only-tool-enabled claude -p invocation. `content` goes
    via stdin, `instruction` via --append-system-prompt (the same recipe as
    _invoke_claude), plus --allowedTools/--add-dir/--max-budget-usd and
    --output-format stream-json so the transcript can be inspected.

    `target_repo` (2026-07-04): repo tag for personal-context scoping — see
    _invoke_claude's docstring. Distinct from `repo_root` (the resolved
    filesystem path passed to --add-dir); most callers already have the
    same repo string on hand for both.

    Returns {"result": <final text>, "web_grounded": bool, "total_cost_usd":
    float | None}. Raises RuntimeError/ValueError on subprocess failure or a
    missing final result — callers (research_answer/research_revise) own
    retry policy, NOT this function (a single call runs ~$0.5-0.65 —
    silently retrying here would double that on every failure).
    """
    instruction = _prepend_personal_context(instruction, target_repo)
    cfg_dir = _isolated_config_dir()
    try:
        env = dict(os.environ)
        env["CLAUDE_CONFIG_DIR"] = str(cfg_dir)

        cmd = [
            _resolve_claude_cmd(),
            "-p",
            "--append-system-prompt",
            instruction,
            "--allowedTools",
            _AGENTIC_ALLOWED_TOOLS,
            "--max-budget-usd",
            str(max_budget_usd),
            "--output-format",
            "stream-json",
            "--verbose",
            "--strict-mcp-config",
            "--mcp-config",
            '{"mcpServers":{}}',
            "--model",
            _AGENTIC_MODEL,
        ]
        if repo_root is not None:
            cmd.extend(["--add-dir", str(repo_root)])

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        stdout, stderr = await proc.communicate(content.encode("utf-8"))
    finally:
        _cleanup_config_dir(cfg_dir)
    if proc.returncode != 0:
        raise RuntimeError(
            f"claude -p (agentic) exited {proc.returncode}: {stderr.decode('utf-8', 'ignore')[:500]}"
        )

    web_grounded = False
    final_result: str | None = None
    total_cost_usd: float | None = None
    for line in stdout.decode("utf-8", "ignore").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        event_type = event.get("type")
        if event_type == "assistant":
            for block in event.get("message", {}).get("content", []):
                if block.get("type") == "tool_use" and block.get("name") in _WEB_TOOL_NAMES:
                    web_grounded = True
        elif event_type == "result":
            final_result = event.get("result")
            total_cost_usd = event.get("total_cost_usd")

    if not isinstance(final_result, str):
        raise ValueError("agentic claude -p produced no final result envelope")

    return {"result": final_result, "web_grounded": web_grounded, "total_cost_usd": total_cost_usd}


async def _run_once(
    content: str,
    *,
    instruction: str = _INSTRUCTION,
    schema: type[BaseModel] = PipelineOutput,
    target_repo: str | None = None,
) -> BaseModel:
    """Structuring/revalidation invocation — parses+validates the result
    against `schema` (PipelineOutput for structuring, RevalidationOutput for
    revalidate_note's re-check pass). Raises on any parse/validate failure."""
    result_text = await _invoke_claude(content, instruction, target_repo=target_repo)
    payload = json.loads(_strip_fence(result_text))
    return schema.model_validate(payload)


async def transform_note(
    raw_text: str,
    *,
    instruction: str = _INSTRUCTION,
    schema: type[BaseModel] = PipelineOutput,
    target_repo: str | None = None,
) -> dict[str, Any] | None:
    """Run the transform, retrying up to _MAX_CLAUDE_ATTEMPTS with backoff
    between attempts on parse/validate failure.

    `target_repo` (2026-07-04): threaded to _invoke_claude for personal-
    context repo-scoping — the repo tag of the note/guide being transformed.
    None means global-only (e.g. the organizer's batch call, which spans
    multiple notes/repos at once and has no single repo to scope to).

    Returns the validated dict on success (shaped per `schema`), None if
    every attempt failed — the caller marks the note status="failed" and
    keeps raw_text.
    """
    for attempt in range(1, _MAX_CLAUDE_ATTEMPTS + 1):
        try:
            output = await _run_once(
                raw_text, instruction=instruction, schema=schema, target_repo=target_repo
            )
            return output.model_dump()
        except (json.JSONDecodeError, ValidationError, ValueError, RuntimeError, OSError) as exc:
            log.warning("notebook_pipeline: attempt %d failed: %s", attempt, exc)
            if attempt < _MAX_CLAUDE_ATTEMPTS:
                await asyncio.sleep(_RETRY_BACKOFF_SECONDS[attempt - 1])
    return None


async def trigger_pipeline(note_id: str, *, skip_organize: bool = False) -> None:
    """Transform note_id's raw_text and write the result back to the store.

    Fire-and-forget worker coroutine — schedule_pipeline() (called from
    api/notebook.py) wraps this in asyncio.create_task so POST /notes
    returns immediately with the note still status="draft".

    `skip_organize` (2026-07-04 addendum — the organizer extended to regular
    notes, mirroring the guide pipeline's own hook): see
    trigger_study_guide_pipeline's docstring for why this exists.
    """
    try:
        record = notes.get_note(note_id)
    except ValueError:
        log.warning("notebook_pipeline: note %s vanished before transform", note_id)
        return

    result = await transform_note(notes.llm_view(record), target_repo=record.get("repo"))
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

    if skip_organize:
        return

    # 2026-07-04: the organizer, originally guide-only, now also organizes
    # regular notes into folders (mirrors trigger_study_guide_pipeline's own
    # hook — see notebook_organizer.organize_library's kind-aware rewrite).
    # Local import for the same reason the guide pipeline's hook uses one:
    # notebook_organizer imports this module at function scope too
    # (organize_library needs transform_note), so a module-level import here
    # would be circular.
    from khimaira.monitor import notebook_organizer

    await notebook_organizer.organize_after_structuring(note_id)


async def trigger_study_guide_pipeline(note_id: str, *, skip_organize: bool = False) -> None:
    """Transform note_id's raw_text into a study-guide pipeline and write it
    back. Same shape as trigger_pipeline, but NEVER touches raw_text — the
    load-bearing invariant — and produces the discriminated pipeline shape
    {abstract, toc, tags, entities} instead of the note pipeline's
    summary/technical/plain/organized_md.

    `abstract`/`tags`/`entities` come from the LLM (StudyGuideOutput);
    `toc` is a deterministic heading parse (_parse_toc) — never touches the
    LLM, so a guide's navigation structure can't drift from its actual
    headings.

    `skip_organize` (Grimoire chat-model addendum, 2026-07-04): the chat
    auto-apply path re-structures on EVERY edit (correct — content changed,
    abstract/tags should follow) but must NOT also re-fire a full LLM
    organize-classification per edit (a chatty back-and-forth would fire N
    organize calls in a row — the "debounce the re-organize" requirement).
    Skips ONLY the organize hook below; structuring itself is unaffected.
    The periodic sweep (`organize_sweep_loop`) still re-checks placement
    eventually, so this is a cost/frequency tradeoff, not a correctness gap.
    """
    try:
        record = notes.get_note(note_id)
    except ValueError:
        log.warning("notebook_pipeline: study guide %s vanished before transform", note_id)
        return

    result = await transform_note(
        notes.llm_view(record),
        instruction=_STUDY_GUIDE_INSTRUCTION,
        schema=StudyGuideOutput,
        target_repo=record.get("repo"),
    )
    if result is None:
        with contextlib.suppress(ValueError):
            notes.update_note(note_id, status="failed")
        log.warning("notebook_pipeline: study guide %s failed to structure after retry", note_id)
        return

    pipeline = {
        "abstract": result["abstract"],
        # _parse_toc is deterministic + never leaves this process — runs on
        # the REAL raw_text (not llm_view) so navigation matches the actual
        # document, not a redacted twin whose heading text could differ.
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

    if skip_organize:
        return

    # Grimoire Phase 2: give the guide an LLM-judged collection placement
    # right after abstract/tags land (deterministic-only Phase 1 assign has
    # nothing to work with for a guide authored directly, with no source
    # path). Local import — notebook_organizer imports this module at
    # function scope too (organize_library needs transform_note), so a
    # module-level import here would be circular.
    from khimaira.monitor import notebook_organizer

    await notebook_organizer.organize_after_structuring(note_id)


def schedule_pipeline(note_id: str, *, skip_organize: bool = False) -> None:
    """Sync entry point for the POST /notes route — fires the right
    structuring pipeline (kind-branched: study guide vs regular note) as a
    background task without blocking the response. The branch lives here,
    not at each call site, so callers (api/notebook.py, notebook_import.py)
    never need their own kind check to know which pipeline to trigger.

    `skip_organize` is forwarded to whichever pipeline runs (both kinds now
    have an organize hook — 2026-07-04)."""
    try:
        record = notes.get_note(note_id)
    except ValueError:
        log.warning("notebook_pipeline: note %s vanished before scheduling", note_id)
        return
    coro = (
        trigger_study_guide_pipeline(note_id, skip_organize=skip_organize)
        if record.get("kind") == "study_guide"
        else trigger_pipeline(note_id, skip_organize=skip_organize)
    )
    task = asyncio.create_task(coro)
    _BACKGROUND_TASKS.add(task)
    task.add_done_callback(_BACKGROUND_TASKS.discard)


def reprocess_after_raw_text_change(note_id: str, *, skip_organize: bool = False) -> dict[str, Any]:
    """Fire the reprocess sequence a COMMITTED raw_text change requires:
    flip to draft, re-run structuring, re-embed. Call AFTER the raw_text
    write itself has already landed (via notes.update_note, which is what
    snapshots version history) — this function doesn't touch raw_text.

    Grimoire chat-model addendum (2026-07-04): extracted so BOTH the PATCH
    /notes/{id} route and the chat auto-apply path fire the identical
    sequence — before this, it lived ONLY inline in api/notebook.py's PATCH
    handler (notes.update_note itself never reschedules anything), so a
    second caller (chat) reimplementing it inline would risk drifting out
    of sync with the route's own behavior. One implementation, two callers.

    Personal-tab notes are skipped (mirrors the PATCH route's existing
    exemption) — guides/chat never target the personal tab in practice,
    but this keeps the contract identical either way.
    """
    record = notes.get_note(note_id)
    if record["tab_id"] == notes.PERSONAL_TAB_ID:
        return record
    record = notes.update_note(note_id, status="draft")
    schedule_pipeline(note_id, skip_organize=skip_organize)
    notebook_retrieval.schedule_upsert(record)
    return record


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
# Study guides are longer, more code-grounded deliverables than a pasted
# note — they typically reference more distinct files, so a guide's
# currency check gets a higher anchor cap than a regular note's (Grimoire
# Phase 2, master's spec: "raise anchor caps for guides").
_MAX_ANCHOR_FILES_GUIDE = 15
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


_GUIDE_DRIFT_INSTRUCTION_TEMPLATE = (
    "You are checking whether a study guide's library-card ABSTRACT is still "
    "accurate given the CURRENT source code below. The user message contains "
    "the guide's EXISTING ABSTRACT as plain text. Output a JSON object with "
    "keys `abstract` (string) and `unchanged` (boolean). Set unchanged=true "
    "if the existing abstract is still accurate (echo it back in `abstract` "
    "— it will be discarded either way when unchanged). Set unchanged=false "
    "and provide a CORRECTED abstract if the guide's described content has "
    "drifted from what the current code actually does. You are NOT "
    "rewriting the guide itself, and you are NOT checking its table of "
    "contents or tags — only its library-card abstract. Output ONLY the "
    "JSON object, no prose, no markdown fence.\n\nCURRENT CODE:\n{code}"
)


class GuideDriftOutput(BaseModel):
    abstract: str
    unchanged: bool


async def _revalidate_guide_drift(
    note_id: str,
    pipeline: dict[str, Any] | None,
    code_blob: str,
    current_sha: str,
    *,
    target_repo: str | None = None,
) -> dict[str, Any]:
    """Guide currency = a DRIFT REPORT, not a heal-and-overwrite (Grimoire
    Phase 2, the load-bearing invariant carried into revalidation): only
    `abstract` is ever regenerated. `toc` is a deterministic heading parse
    that doesn't need re-derivation (raw_text never changes here), and
    `title` is never LLM-touched for guides (see add_study_guide) — so
    neither is passed to apply_validation. This is a SEPARATE concern from
    notebook_organizer's collection placement.
    """
    instruction = _GUIDE_DRIFT_INSTRUCTION_TEMPLATE.format(code=code_blob)
    content = (pipeline or {}).get("abstract", "")
    result = await transform_note(
        content, instruction=instruction, schema=GuideDriftOutput, target_repo=target_repo
    )

    if result is None:
        log.warning(
            "notebook_pipeline: guide drift-check for %s failed to parse after retry; "
            "leaving record unchanged",
            note_id,
        )
        return notes.get_note(note_id)

    unchanged = result["unchanged"]
    new_pipeline = None
    if not unchanged:
        new_pipeline = dict(pipeline or {})
        new_pipeline["abstract"] = result["abstract"]
    updated = notes.apply_validation(note_id, git_sha=current_sha, new_pipeline=new_pipeline)
    if new_pipeline is not None:
        await asyncio.to_thread(notebook_retrieval.upsert_note, updated)
    return updated


async def revalidate_note(note_id: str) -> dict[str, Any]:
    """Re-ground a note against its repo's current code (the north-star core).

    Staleness gate: if the note has a prior validated_git_sha and none of its
    resolved anchor files changed since that SHA, stamps validated_git_sha to
    HEAD and returns without an LLM call — the cost-saver. Otherwise runs the
    same claude -p recipe as the initial transform, but asks "is this still
    accurate vs current code?" If the model's answer differs from the
    existing pipeline, that's a HEAL: the old pipeline is pushed to history
    before the new one lands.

    Study guides (kind="study_guide") branch to `_revalidate_guide_drift`
    after the shared staleness gate + anchor-file resolution — a guide's
    currency check is a DRIFT REPORT on its abstract only, never a full
    pipeline regeneration (see that function's docstring).

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

    is_guide = record.get("kind") == "study_guide"
    pipeline = record.get("pipeline")
    entities = (pipeline or {}).get("entities", [])
    anchor_cap = _MAX_ANCHOR_FILES_GUIDE if is_guide else _MAX_ANCHOR_FILES
    anchor_files = _resolve_anchor_files(repo_root, entities, cap=anchor_cap)

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

    if is_guide:
        return await _revalidate_guide_drift(
            note_id, pipeline, code_blob, current_sha, target_repo=repo
        )

    instruction = _REVALIDATE_INSTRUCTION_TEMPLATE.format(code=code_blob)
    result = await transform_note(
        json.dumps(pipeline or {}, indent=2),
        instruction=instruction,
        schema=RevalidationOutput,
        target_repo=repo,
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


async def _synthesize_answer(
    question: str, instruction: str, *, target_repo: str | None = None
) -> str | None:
    """Free-form prose, not the PipelineOutput schema — same retry/backoff
    discipline as transform_note, but no JSON parse/validate."""
    for attempt in range(1, _MAX_CLAUDE_ATTEMPTS + 1):
        try:
            return (await _invoke_claude(question, instruction, target_repo=target_repo)).strip()
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
        # raw_text fallback (for a note that hasn't been structured yet, or
        # a study guide whose pipeline has no organized_md/summary keys at
        # all) routes through llm_view — a sensitive note's real secrets
        # must not reach this ask-synthesis LLM call either.
        body = pipeline.get("organized_md") or pipeline.get("summary") or notes.llm_view(updated)
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
    answer = await _synthesize_answer(question, instruction, target_repo=repo)
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


# ---------------------------------------------------------------------------
# Grimoire Phase 3: the research-scientist toolbar. ANSWER (research_answer)
# and REVISE (research_revise) share ONE schema and ONE grounded-invocation
# helper — the only difference is the instruction (whole-answer vs a
# proposed patch) and, for REVISE, the deterministic splice back into
# raw_text. Neither path ever calls update_note/mark_organized/etc — REVISE
# returns a PROPOSAL only; applying it is the existing PATCH /notes/{id}
# (raw_text=), which already reschedules structuring and (via the Phase 2
# organize-after-structuring hook) re-runs the organizer on apply — no new
# plumbing needed for that.
# ---------------------------------------------------------------------------


class ResearchOutput(BaseModel):
    answer: str
    code_citations: list[str] = []
    web_citations: list[str] = []
    proposed_patch: str | None = None


_RESEARCH_ANSWER_INSTRUCTION_TEMPLATE = (
    _GROUNDING_IMPERATIVE + " "
    "You are a research assistant helping ground a study guide's content "
    "against the ACTUAL codebase (Read/Grep/Glob under {repo_root}) and the "
    "live web (WebSearch/WebFetch). Answer the user's question using the "
    "guide below as context — but verify anything checkable against real "
    "code or the web rather than trusting the guide's prose at face value. "
    "You are NOT proposing an edit to the guide — leave proposed_patch "
    "null. Output ONLY a JSON object, no prose, no markdown fence, with "
    'keys: answer (string), code_citations (array of "file:line" strings), '
    "web_citations (array of URL strings), proposed_patch (null).\n\n"
    "GUIDE:\n{guide}"
)

_RESEARCH_REVISE_INSTRUCTION_TEMPLATE = (
    _GROUNDING_IMPERATIVE + " "
    "You are proposing a revision to {scope} of a study guide, grounded in "
    "the ACTUAL codebase (Read/Grep/Glob under {repo_root}) and the live "
    "web (WebSearch/WebFetch) — verify anything checkable rather than "
    "trusting the guide's existing prose. {scope_instruction} Output ONLY a "
    "JSON object, no prose, no markdown fence, with keys: answer (a 1-3 "
    "sentence description of what you changed and why), code_citations "
    '(array of "file:line" strings), web_citations (array of URL strings), '
    "proposed_patch (string — the full replacement text, markdown, no "
    "surrounding commentary).\n\nGUIDE:\n{guide}"
)

_GROUNDING_RETRY_IMPERATIVE = (
    "\n\nSTRICT: your previous attempt claimed web sources without actually "
    "calling WebSearch/WebFetch. Call the tool for real this time, or drop "
    "any web_citations you cannot back with an actual tool call."
)


async def _invoke_agentic_grounded(
    content: str,
    instruction: str,
    *,
    repo_root: Path | None,
    max_budget_usd: float,
    schema: type[BaseModel] = ResearchOutput,
    target_repo: str | None = None,
) -> dict[str, Any]:
    """Shared ANSWER/REVISE/chat invocation against `schema` (ResearchOutput
    by default; the chat turn passes ChatTurnOutput — see notebook_chat.py).
    Both schemas share the same required `answer: str` + defaulted
    `code_citations`/`web_citations` fields, which is what lets the failure
    fallback below stay schema-agnostic (constructs a valid empty instance
    of whichever schema was passed, rather than hardcoding ResearchOutput's
    field names).

    ONE retry (stronger imperative) fires if the result claims web_citations
    but the transcript shows no real WebSearch/WebFetch tool_use — master's
    refinement (2026-07-04): this reliably flips a silent skip into a real
    call in live testing, rather than surfacing unverified on the first trip.

    Never raises — every failure mode (subprocess error, unparseable
    response) returns a result dict with web_grounding_unverified=True and
    an explanatory `answer`, so callers don't need their own try/except
    around this.
    """

    async def _attempt(extra: str = "") -> dict[str, Any] | None:
        try:
            invoked = await _invoke_claude_agentic(
                content,
                instruction + extra,
                repo_root=repo_root,
                max_budget_usd=max_budget_usd,
                target_repo=target_repo,
            )
        except (RuntimeError, ValueError, OSError) as exc:
            log.warning("notebook_pipeline: agentic invocation failed: %s", exc)
            return None
        try:
            payload = json.loads(_strip_fence(invoked["result"]))
            parsed = schema.model_validate(payload).model_dump()
        except (json.JSONDecodeError, ValidationError) as exc:
            log.warning("notebook_pipeline: agentic response failed to parse: %s", exc)
            return None
        parsed["web_grounded"] = invoked["web_grounded"]
        parsed["total_cost_usd"] = invoked["total_cost_usd"]
        return parsed

    result = await _attempt()
    if result is None:
        fallback = schema(
            answer="Research failed — the agentic call errored or returned an "
            "unparseable response. Try again, or narrow the request."
        ).model_dump()
        return {
            **fallback,
            "web_grounded": False,
            "web_grounding_unverified": True,
            "total_cost_usd": None,
        }

    suspect = bool(result.get("web_citations")) and not result["web_grounded"]
    if suspect:
        log.warning(
            "notebook_pipeline: agentic response claimed web_citations with no "
            "observed WebSearch/WebFetch tool_use — retrying with a stronger imperative"
        )
        retry = await _attempt(_GROUNDING_RETRY_IMPERATIVE)
        if retry is not None:
            result = retry
            suspect = bool(result.get("web_citations")) and not result["web_grounded"]

    result["web_grounding_unverified"] = suspect
    return result


async def research_answer(
    note_id: str, question: str, *, max_budget_usd: float = _AGENTIC_DEFAULT_BUDGET_USD
) -> dict[str, Any]:
    """ANSWER path: research-grounds a question against note_id's guide +
    the live codebase + the web, WITHOUT proposing any edit — read-only, no
    update_note call anywhere in this path. Raises ValueError if note_id
    doesn't exist (mirrors get_note)."""
    record = notes.get_note(note_id)
    repo = record.get("repo") or "khimaira"
    repo_root = None if repo == notes.GENERAL_REPO else _repo_root(repo)
    instruction = _RESEARCH_ANSWER_INSTRUCTION_TEMPLATE.format(
        repo_root=repo_root or "(no codebase — general/cross-cutting note)",
        guide=notes.llm_view(record),
    )
    return await _invoke_agentic_grounded(
        question, instruction, repo_root=repo_root, max_budget_usd=max_budget_usd, target_repo=repo
    )


async def research_revise(
    note_id: str,
    directive: str,
    *,
    section_anchor: str | None = None,
    max_budget_usd: float = _AGENTIC_DEFAULT_BUDGET_USD,
) -> dict[str, Any]:
    """REVISE path: proposes a patch to a guide, grounded against the
    codebase + web — NEVER applies it. Returns a proposal only (including
    `proposed_raw_text`, the FULL guide text with the patch already
    deterministically spliced in — a ready-to-diff candidate); applying is
    the existing PATCH /notes/{id} (raw_text=proposed_raw_text) after a
    human reviews the diff and clicks Apply.

    `section_anchor`, when given, scopes the patch to ONE section (matched
    against the guide's CURRENT heading scan) — the model proposes only
    that section's replacement text; `splice_section` confines the actual
    write to that slot regardless of what the model returns, so a model
    that ignores "just this section" can't silently rewrite the whole
    guide. Omit `section_anchor` for a whole-guide revision.

    Raises ValueError if note_id doesn't exist, or if section_anchor
    doesn't match any heading in the guide's CURRENT raw_text (fails fast,
    before spending an agentic call, on a stale anchor from a guide that
    changed underneath the request).
    """
    record = notes.get_note(note_id)
    # `raw_text` (REAL content) is used for section_anchor validation (must
    # match the actual document's headings) and for splicing the model's
    # proposed patch back into the real document below. `notes.llm_view()`
    # (possibly redacted) is what actually reaches the LLM in `instruction` —
    # a sensitive note's real secrets must never appear in that prompt, even
    # though REVISE's own human-review-before-apply gate is what keeps
    # splicing a redacted-based patch back into the real text safe from a
    # data-loss standpoint.
    raw_text = record.get("raw_text", "")
    repo = record.get("repo") or "khimaira"
    repo_root = None if repo == notes.GENERAL_REPO else _repo_root(repo)

    if section_anchor is not None:
        if not any(h["anchor"] == section_anchor for h in _scan_headings(raw_text)):
            raise ValueError(
                f"No section anchored at {section_anchor!r} in this guide's current raw_text."
            )
        scope = f"ONE section (anchored at {section_anchor!r})"
        scope_instruction = (
            "Revise ONLY that section — propose ONLY its replacement text, "
            "starting with its own heading line, NOT the rest of the guide."
        )
    else:
        scope = "the WHOLE guide"
        scope_instruction = "Propose the full replacement guide text."

    instruction = _RESEARCH_REVISE_INSTRUCTION_TEMPLATE.format(
        scope=scope,
        scope_instruction=scope_instruction,
        repo_root=repo_root or "(no codebase — general/cross-cutting note)",
        guide=notes.llm_view(record),
    )
    result = await _invoke_agentic_grounded(
        directive, instruction, repo_root=repo_root, max_budget_usd=max_budget_usd, target_repo=repo
    )

    proposed_raw_text = None
    if result.get("proposed_patch"):
        if section_anchor is not None:
            try:
                proposed_raw_text = splice_section(
                    raw_text, section_anchor, result["proposed_patch"]
                )
            except ValueError as exc:
                log.warning(
                    "notebook_pipeline: research_revise(%s) splice failed: %s", note_id, exc
                )
        else:
            proposed_raw_text = result["proposed_patch"]
    result["proposed_raw_text"] = proposed_raw_text

    return result


# ---------------------------------------------------------------------------
# Grimoire Phase 4 addendum (2026-07-04): research jobs run as BACKGROUND
# TASKS, polled via job_id, rather than held open on the HTTP request.
#
# Root cause this fixes: a synchronous 1-2 minute agentic call held the HTTP
# connection open that long — any genuine client-side disconnect (network
# blip, tab close) would cancel the request, and (separately, audited live)
# a `systemctl restart khimaira-monitor` during ANY in-flight call SIGTERMs
# the whole cgroup (including the claude -p child) under the daemon's
# default KillMode — neither is fixable from inside a single synchronous
# request/response cycle. A background job survives a dropped HTTP
# connection outright (the poll is a NEW, independent request); it does NOT
# survive the daemon process itself being killed (see monitor/cli.py's
# KillMode=mixed fix for that half), but a poll for a job_id from before a
# restart cleanly reports "unknown" rather than a mid-air 143 + a broken
# response.
# ---------------------------------------------------------------------------

_RESEARCH_JOBS: dict[str, dict[str, Any]] = {}
_RESEARCH_JOB_TASKS: set[asyncio.Task] = set()


def _new_job_id() -> str:
    return uuid.uuid4().hex[:12]


# Generic job-store primitives (Grimoire chat-model addendum, 2026-07-04):
# schedule_research_answer/schedule_research_revise below manipulate
# _RESEARCH_JOBS/_RESEARCH_JOB_TASKS directly (unchanged — they're already
# tested and live-verified; not worth the regression risk of refactoring
# working code for pure consistency). notebook_chat.py's schedule_chat_turn
# is NEW code and uses these public wrappers instead of reaching into this
# module's private job-store internals directly, keeping the encapsulation
# boundary this codebase otherwise maintains between modules.
def create_job(kind: str) -> str:
    """Register a new pending job in the shared job store. Returns its id."""
    job_id = _new_job_id()
    _RESEARCH_JOBS[job_id] = {"status": "pending", "kind": kind}
    return job_id


def complete_job(job_id: str, **fields: Any) -> None:
    _RESEARCH_JOBS[job_id] = {"status": "done", **fields}


def fail_job(job_id: str, **fields: Any) -> None:
    _RESEARCH_JOBS[job_id] = {"status": "error", **fields}


def track_job_task(task: asyncio.Task) -> None:
    """Keep a strong reference to a background job task until it completes
    (asyncio.create_task() only holds a weak ref — see _BACKGROUND_TASKS'
    own docstring for the failure mode this guards against)."""
    _RESEARCH_JOB_TASKS.add(task)
    task.add_done_callback(_RESEARCH_JOB_TASKS.discard)


async def _run_research_answer_job(
    job_id: str, note_id: str, question: str, max_budget_usd: float
) -> None:
    try:
        result = await research_answer(note_id, question, max_budget_usd=max_budget_usd)
        _RESEARCH_JOBS[job_id] = {"status": "done", "kind": "answer", **result}
    except ValueError as exc:
        _RESEARCH_JOBS[job_id] = {"status": "error", "kind": "answer", "error": str(exc)}
    except Exception as exc:
        log.exception("notebook_pipeline: research_answer job %s crashed", job_id)
        _RESEARCH_JOBS[job_id] = {"status": "error", "kind": "answer", "error": str(exc)}


def schedule_research_answer(
    note_id: str, question: str, *, max_budget_usd: float = _AGENTIC_DEFAULT_BUDGET_USD
) -> str:
    """Fire research_answer as a background job — returns a job_id
    immediately; poll get_research_job(job_id) for the result.

    Validates note_id exists BEFORE scheduling (fails fast with the same
    ValueError->404 the synchronous route always had, rather than handing
    back a job_id that's guaranteed to immediately error).
    """
    notes.get_note(note_id)  # fail fast on an unknown note_id
    job_id = _new_job_id()
    _RESEARCH_JOBS[job_id] = {"status": "pending", "kind": "answer"}
    task = asyncio.create_task(_run_research_answer_job(job_id, note_id, question, max_budget_usd))
    _RESEARCH_JOB_TASKS.add(task)
    task.add_done_callback(_RESEARCH_JOB_TASKS.discard)
    return job_id


async def _run_research_revise_job(
    job_id: str,
    note_id: str,
    directive: str,
    section_anchor: str | None,
    max_budget_usd: float,
) -> None:
    try:
        result = await research_revise(
            note_id, directive, section_anchor=section_anchor, max_budget_usd=max_budget_usd
        )
        _RESEARCH_JOBS[job_id] = {"status": "done", "kind": "revise", **result}
    except ValueError as exc:
        _RESEARCH_JOBS[job_id] = {"status": "error", "kind": "revise", "error": str(exc)}
    except Exception as exc:
        log.exception("notebook_pipeline: research_revise job %s crashed", job_id)
        _RESEARCH_JOBS[job_id] = {"status": "error", "kind": "revise", "error": str(exc)}


def schedule_research_revise(
    note_id: str,
    directive: str,
    *,
    section_anchor: str | None = None,
    max_budget_usd: float = _AGENTIC_DEFAULT_BUDGET_USD,
) -> str:
    """Fire research_revise as a background job — returns a job_id
    immediately; poll get_research_job(job_id) for the proposal.

    Validates note_id AND section_anchor (when given) BEFORE scheduling —
    same fail-fast contract as schedule_research_answer."""
    record = notes.get_note(note_id)  # fail fast on an unknown note_id
    if section_anchor is not None and not any(
        h["anchor"] == section_anchor for h in _scan_headings(record.get("raw_text", ""))
    ):
        raise ValueError(
            f"No section anchored at {section_anchor!r} in this guide's current raw_text."
        )
    job_id = _new_job_id()
    _RESEARCH_JOBS[job_id] = {"status": "pending", "kind": "revise"}
    task = asyncio.create_task(
        _run_research_revise_job(job_id, note_id, directive, section_anchor, max_budget_usd)
    )
    _RESEARCH_JOB_TASKS.add(task)
    task.add_done_callback(_RESEARCH_JOB_TASKS.discard)
    return job_id


# ---------------------------------------------------------------------------
# CHAT-UNIFY §2 MVP (2026-07-04): notebook-wide chat for the "nothing open"
# case — a THIN wrapper around answer_question, not a new grounding path.
# `refs` map 1:1 onto answer_question's own mentioned_note_ids param, so
# this inherits every egress fix already made there (sensitive notes route
# through notes.llm_view() at both the pipeline-body and raw_text-fallback
# sites — see answer_question's own docstring/tests) for free — there is no
# separate code path here that could bypass it. NOT true multi-turn: each
# call is a stateless ask (no persisted server-side history); the frontend
# accumulates the conversational feel client-side. Reuses the SAME job
# store as research/chat so the client polls one shared route regardless
# of which kind of turn it fired.
# ---------------------------------------------------------------------------


async def _run_notebook_chat_job(
    job_id: str, message: str, mentioned_note_ids: list[str] | None
) -> None:
    try:
        result = await answer_question(message, mentioned_note_ids=mentioned_note_ids or [])
        _RESEARCH_JOBS[job_id] = {"status": "done", "kind": "notebook_chat", **result}
    except Exception as exc:
        log.exception("notebook_pipeline: notebook chat job %s crashed", job_id)
        _RESEARCH_JOBS[job_id] = {"status": "error", "kind": "notebook_chat", "error": str(exc)}


def schedule_notebook_chat(message: str, mentioned_note_ids: list[str] | None = None) -> str:
    """Fire a notebook-wide chat turn as a background job — returns a job_id
    immediately; poll get_research_job(job_id) (kind="notebook_chat").

    No upfront validation needed (unlike schedule_research_answer/revise) —
    answer_question has no single note_id to fail fast on; an unresolvable
    mentioned_note_id is silently skipped by answer_question itself, same
    as any other retrieval miss.
    """
    job_id = _new_job_id()
    _RESEARCH_JOBS[job_id] = {"status": "pending", "kind": "notebook_chat"}
    task = asyncio.create_task(_run_notebook_chat_job(job_id, message, mentioned_note_ids))
    _RESEARCH_JOB_TASKS.add(task)
    task.add_done_callback(_RESEARCH_JOB_TASKS.discard)
    return job_id


def get_research_job(job_id: str) -> dict[str, Any]:
    """Poll a research job's status. Returns {"status": "pending"} while in
    flight, or {"status": "done"|"error", "kind": "answer"|"revise", ...}
    once finished (the ResearchOutput+grounding fields for "done", an
    "error" string for "error"). Raises ValueError for an unknown job_id —
    either it never existed, or the daemon restarted since it was scheduled
    (the job store is in-memory only, same convention as this module's
    other fire-and-forget task tracking)."""
    job = _RESEARCH_JOBS.get(job_id)
    if job is None:
        raise ValueError(
            f"No research job with id={job_id!r} — it may have completed and been "
            "cleared, or the daemon restarted since it was scheduled."
        )
    return job
