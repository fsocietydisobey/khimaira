"""resolve_context — the load-bearing primitive of Pillar 1.

Takes a task description, returns a ContextBundle of relevant files +
snippets. The bundle is what the prompt builder uses to construct the
LLM call — minimizing it is where 5-10x token reduction comes from.

Architecture:
  resolve_context(task, project_path)
    ↓
    [seance source]   — semantic search (vectorized) — when seance.api available
    [scarlet source]  — cartography (CLAUDE.md, dep graphs) — when scarlet.api available
    [grep source]     — keyword fallback — always available
    [filesystem]      — recently modified files heuristic — always available
    ↓
  merge + score + budget-truncate → ContextBundle

When neither seance nor scarlet has a library API yet, the grep + filesystem
fallbacks still give useful context. Quality scales with what's installed;
the resolver's interface is stable across both states.
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections import defaultdict
from pathlib import Path

from khimaira_types import ContextBundle, FileContext

from khimaira.log import get_logger

log = get_logger("context.resolver")

# Default cap on total context bytes. ~30k chars ≈ 7-8k tokens — safe
# headroom for most models without blowing prompt budget.
DEFAULT_BUDGET_CHARS = 30_000

# Skip directories that never have task-relevant code
_SKIP_DIRS = frozenset({
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", "dist", "build", "target", ".next",
    "coverage", "htmlcov", ".tox", ".cache",
})


async def resolve_context(
    task: str,
    *,
    project_path: str | Path,
    budget_chars: int = DEFAULT_BUDGET_CHARS,
    max_files: int = 20,
) -> ContextBundle:
    """Resolve task-relevant context. Always returns a ContextBundle, even
    when perception tools aren't installed (uses grep + fs fallbacks).

    Args:
        task: User's task description — drives semantic + keyword search.
        project_path: Project root. Resolution is scoped here.
        budget_chars: Max total chars across snippets. Truncates lowest-scoring
            files first when exceeded.
        max_files: Hard cap on number of files in the bundle.

    Returns:
        ContextBundle with files, total_estimated_tokens, sources_consulted,
        elapsed_ms, and budget audit fields.
    """
    project = Path(project_path).resolve()
    t0 = time.monotonic()

    # Each source returns dict[abs_path] → (score, snippet, line_range, reason)
    contributions: dict[str, list[tuple[float, str, str, tuple[int, int] | None, str]]] = defaultdict(list)
    sources_consulted: list[str] = []

    # Source 1: Séance (semantic search) — if library API is present
    seance_results = await _try_seance(task, project)
    if seance_results is not None:
        sources_consulted.append("seance")
        for path, score, snippet, line_range, reason in seance_results:
            contributions[path].append(("seance", score, snippet, line_range, reason))

    # Source 2: Scarlet (CLAUDE.md / cartography) — if library API is present
    scarlet_results = await _try_scarlet(task, project)
    if scarlet_results is not None:
        sources_consulted.append("scarlet")
        for path, score, snippet, line_range, reason in scarlet_results:
            contributions[path].append(("scarlet", score, snippet, line_range, reason))

    # Source 3: grep — always available
    grep_results = _grep_keywords(task, project)
    sources_consulted.append("filesystem")  # grep is part of filesystem heuristics
    for path, score, snippet, line_range, reason in grep_results:
        contributions[path].append(("grep", score, snippet, line_range, reason))

    # Source 4: recently modified files — always relevant for "what is changing"
    recent_results = _recently_modified(project, max_files=10)
    for path, score, snippet, line_range, reason in recent_results:
        contributions[path].append(("recent", score, snippet, line_range, reason))

    # Merge: aggregate per-path. Files matched by multiple sources outrank
    # single-source matches because cross-corroboration is signal.
    merged: list[FileContext] = []
    for path, contribs in contributions.items():
        # Sum scores; multi-source bonus baked in implicitly (more contribs → higher total)
        total_score = sum(c[1] for c in contribs)
        # Pick the longest snippet across contributors
        best_snippet = max((c[2] for c in contribs), key=len, default="")
        best_range = next((c[3] for c in contribs if c[3] is not None), None)
        sources = list({c[0] for c in contribs})
        # ResolutionSource literal — only allow the canonical names
        valid_sources = [s for s in sources if s in {"seance", "scarlet", "serena", "explicit", "filesystem"}]
        # Map "grep" and "recent" → "filesystem"
        if "grep" in sources or "recent" in sources:
            if "filesystem" not in valid_sources:
                valid_sources.append("filesystem")
        reasoning = "; ".join(c[4] for c in contribs[:3])

        merged.append(FileContext(
            path=path,
            relevance=min(1.0, total_score / 10.0),  # normalize roughly
            sources=valid_sources or ["filesystem"],
            snippet=best_snippet or None,
            line_range=best_range,
            reasoning=reasoning,
        ))

    # Sort by relevance desc, cap at max_files, truncate to budget
    merged.sort(key=lambda f: f.relevance, reverse=True)
    merged = merged[:max_files]

    bundle_total = 0
    accepted: list[FileContext] = []
    dropped: list[str] = []
    for f in merged:
        snippet_len = len(f.snippet or "")
        if bundle_total + snippet_len <= budget_chars:
            accepted.append(f)
            bundle_total += snippet_len
        else:
            dropped.append(f.path)

    elapsed_ms = int((time.monotonic() - t0) * 1000)
    log.info(
        "context: task=%r → %d files (%d chars), sources=%s, %dms",
        task[:60], len(accepted), bundle_total, sources_consulted, elapsed_ms,
    )

    return ContextBundle(
        task_description=task,
        files=accepted,
        total_estimated_tokens=bundle_total // 4,  # rough chars → tokens
        sources_consulted=[s for s in sources_consulted if s in {"seance", "scarlet", "serena", "filesystem", "explicit"}],
        elapsed_ms=elapsed_ms,
        budget_truncated=bool(dropped),
        budget_dropped_files=dropped,
    )


# ---------------------------------------------------------------------------
# Source: Séance (semantic search via vector embeddings)
# ---------------------------------------------------------------------------


async def _try_seance(
    task: str,
    project: Path,
) -> list[tuple[str, float, str, tuple[int, int] | None, str]] | None:
    """Try to call seance.api.semantic_search. Returns None if seance's
    library API isn't installed yet (graceful fallback)."""
    try:
        from seance.api.search import semantic_search  # type: ignore[import-not-found]
    except ImportError:
        return None

    try:
        results = await _maybe_await(semantic_search(task, project=project.name, top_k=15))
    except Exception as exc:
        log.warning("context: seance search failed: %s", exc)
        return None

    out: list[tuple[str, float, str, tuple[int, int] | None, str]] = []
    for r in results or []:
        path = r.get("file_path") or r.get("path") or ""
        if not path:
            continue
        snippet = r.get("text") or r.get("snippet") or ""
        score = float(r.get("score") or 0.0)
        line_start = r.get("start_line") or r.get("line_start")
        line_end = r.get("end_line") or r.get("line_end")
        line_range = (int(line_start), int(line_end)) if line_start and line_end else None
        out.append((
            path, score, snippet, line_range,
            f"seance score={score:.2f}",
        ))
    return out


# ---------------------------------------------------------------------------
# Source: Scarlet (codebase cartography — CLAUDE.md, dep graphs)
# ---------------------------------------------------------------------------


async def _try_scarlet(
    task: str,
    project: Path,
) -> list[tuple[str, float, str, tuple[int, int] | None, str]] | None:
    """Try to use scarlet's library API. Returns None if not installed."""
    try:
        from scarlet.api.feature_metadata import scan_features  # type: ignore[import-not-found]
    except ImportError:
        # Fallback: read existing CLAUDE.md files in the project
        return _read_claude_md_files(project)

    try:
        features = await _maybe_await(scan_features(str(project)))
    except Exception as exc:
        log.warning("context: scarlet scan failed: %s", exc)
        return _read_claude_md_files(project)

    # Naive relevance: feature names that appear in the task get a small bump
    task_lower = task.lower()
    out: list[tuple[str, float, str, tuple[int, int] | None, str]] = []
    for feat in features or []:
        name = feat.get("name", "") if isinstance(feat, dict) else ""
        path = feat.get("path", "") if isinstance(feat, dict) else ""
        claude_md = feat.get("claude_md_path") if isinstance(feat, dict) else None
        if not path:
            continue
        score = 0.5
        if name and name.lower() in task_lower:
            score = 2.5
        if claude_md and Path(claude_md).is_file():
            try:
                snippet = Path(claude_md).read_text(encoding="utf-8", errors="replace")
            except OSError:
                snippet = ""
            out.append((
                str(claude_md), score, snippet[:3000], None,
                f"scarlet feature CLAUDE.md ({name})",
            ))
    return out


def _read_claude_md_files(
    project: Path,
) -> list[tuple[str, float, str, tuple[int, int] | None, str]]:
    """Fallback when scarlet's library API isn't available — just walk
    existing CLAUDE.md files. Each one is high-signal already (a human
    or scarlet wrote it specifically as orientation material)."""
    out: list[tuple[str, float, str, tuple[int, int] | None, str]] = []
    for p in project.rglob("CLAUDE.md"):
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            text = p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        out.append((
            str(p), 1.0, text[:3000], None,
            "existing CLAUDE.md",
        ))
    return out


# ---------------------------------------------------------------------------
# Source: grep — keyword search fallback (always available)
# ---------------------------------------------------------------------------


def _grep_keywords(
    task: str,
    project: Path,
) -> list[tuple[str, float, str, tuple[int, int] | None, str]]:
    """Simple keyword extraction + ripgrep. Cheap and surprisingly effective
    when the task description names symbols/files literally.
    """
    keywords = _extract_keywords(task)
    if not keywords:
        return []

    out: dict[str, tuple[float, str, tuple[int, int] | None, str]] = {}
    rg = _which("rg")
    if rg:
        for kw in keywords:
            try:
                proc = subprocess.run(
                    [rg, "--max-count", "3", "--max-filesize", "200K",
                     "--no-heading", "--with-filename", "--line-number",
                     "-i", kw, str(project)],
                    capture_output=True, text=True, timeout=5.0,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
            for line in proc.stdout.splitlines()[:30]:  # cap per keyword
                m = re.match(r"^([^:]+):(\d+):(.*)$", line)
                if not m:
                    continue
                path, ln, text = m.group(1), int(m.group(2)), m.group(3)
                if any(part in _SKIP_DIRS for part in Path(path).parts):
                    continue
                # Score: 1 per keyword hit, accumulated per file
                existing = out.get(path)
                if existing is None:
                    out[path] = (1.0, text[:300], (ln, ln), f"grep matched {kw!r}")
                else:
                    score, snippet, line_range, reason = existing
                    out[path] = (
                        score + 1.0, snippet, line_range,
                        f"{reason}, {kw!r}",
                    )
    return [(p, s, sn, lr, rs) for p, (s, sn, lr, rs) in out.items()]


def _extract_keywords(task: str) -> list[str]:
    """Pull plausibly-significant words from the task. Conservative —
    drops short words, common stopwords, plain integers."""
    stop = frozenset({
        "the", "a", "an", "is", "are", "be", "to", "and", "or", "if", "in",
        "on", "of", "for", "with", "by", "this", "that", "from", "do", "does",
        "fix", "add", "make", "use", "create", "new", "now", "but", "as",
        "can", "should", "would", "i", "we", "you", "it", "all", "some",
        "any", "what", "which", "where", "when", "why", "how",
    })
    out: list[str] = []
    seen: set[str] = set()
    for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", task):
        low = tok.lower()
        if low in stop or low.isdigit() or low in seen:
            continue
        seen.add(low)
        out.append(tok)
        if len(out) >= 8:  # cap — too many keywords blows up grep
            break
    return out


# ---------------------------------------------------------------------------
# Source: recently modified files (filesystem heuristic)
# ---------------------------------------------------------------------------


def _recently_modified(
    project: Path,
    *,
    max_files: int = 10,
) -> list[tuple[str, float, str, tuple[int, int] | None, str]]:
    """Files modified in the last 7 days. Strong signal for 'what is in
    flight right now' — the work the user is most likely asking about."""
    cutoff = time.time() - 7 * 24 * 3600
    candidates: list[tuple[float, Path]] = []
    for p in project.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_DIRS for part in p.parts):
            continue
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        if mtime < cutoff:
            continue
        candidates.append((mtime, p))

    candidates.sort(reverse=True)
    out: list[tuple[str, float, str, tuple[int, int] | None, str]] = []
    for mtime, p in candidates[:max_files]:
        try:
            text = p.read_text(encoding="utf-8", errors="replace")[:1500]
        except OSError:
            continue
        age_days = (time.time() - mtime) / 86400.0
        out.append((
            str(p),
            max(0.3, 1.0 - age_days / 7.0),  # decay over the week
            text, None,
            f"modified {age_days:.1f}d ago",
        ))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _which(cmd: str) -> str | None:
    import shutil
    return shutil.which(cmd)


async def _maybe_await(value):
    """Accept either sync or async callees — let the perception package
    define its API in whichever style is natural."""
    import inspect
    if inspect.isawaitable(value):
        return await value
    return value
