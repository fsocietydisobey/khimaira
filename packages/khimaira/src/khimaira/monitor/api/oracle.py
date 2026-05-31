"""`POST /api/oracle/query` — codebase oracle, fusing Séance + mnemosyne.

Context mode only (v1): returns labeled sections + citations.
Caller (already Claude) synthesizes the answer.

Fail-open per store: if Séance errors, the other store's results are still
returned and degraded=True signals a real failure.

degraded semantics (v1):
  degraded=True  ⟺  Séance raised an exception (caller should know)
  degraded=False  when Séance returned empty (project unindexed / no match)
                  OR mnemosyne returned None (empty domain — can't distinguish
                  down-from-empty in v1; treat as empty, not failure)

v2 additions:
  sources_empty  — stores that are up but returned no results (distinct from degraded)
  prompt_class   — request field; routed to context.yaml pointer lookup
  project_path   — optional absolute path; enables Scarlet, context.yaml, CLAUDE.md sources
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from khimaira.hooks import mnemosyne_client

from .._optional import require

logger = logging.getLogger(__name__)

# Top-K cap for Séance results; tune without breaking the contract.
_SEANCE_TOP_K = 8

# Cap on CLAUDE.md files included — avoid token bloat.
_CLAUDE_MD_CAP = 5

# Staleness note included in every Séance section — Séance reindex is manual.
_SEANCE_STALENESS_NOTE = (
    "_Note: Séance index may lag live code. Run "
    "`khimaira seance reindex-changed <project>` if results seem stale._"
)


# ---------------------------------------------------------------------------
# Sync workers (run via asyncio.to_thread)
# ---------------------------------------------------------------------------


def _seance_search(project: str, question: str) -> tuple[list[dict[str, Any]], bool]:
    """Run Séance semantic search.

    Returns (results, errored):
      results: list of chunk dicts (may be empty on valid empty result)
      errored: True only when an exception was raised (import fail, API error, etc.)

    Empty results with errored=False means Séance is healthy but the project is
    unindexed or the query matched nothing — valid state, not a degraded condition.
    """
    try:
        from seance.config import load_config
        from seance.search.engine import SearchEngine

        config = load_config()
        engine = SearchEngine(config)
        results = engine.search(
            project_name=project, query=question, top_k=_SEANCE_TOP_K
        )
        return [r.to_dict() for r in results], False
    except Exception as exc:
        logger.debug("oracle: seance search failed for project=%r: %s", project, exc)
        return [], True


# Domains that capture-v1 writes qualified keys for. Must stay in sync with
# session_end_utils._DOMAINS + "general" (the fallback domain).
_MNEMOSYNE_DOMAINS = ("backend", "frontend", "data", "devops", "general")


def _mnemosyne_query(project: str) -> dict[str, Any] | None:
    """Fan out across all known qualified domain keys for project.

    Queries `{project}:{domain}` for each domain in _MNEMOSYNE_DOMAINS.
    Merges non-empty results into one synthetic dict so _build_context
    doesn't need to change. Returns None when all domains are empty.

    Bare-project lookup (pre-fix) silently missed all pairs because capture-v1
    writes at qualified keys. This fan-out closes that seam.
    """
    parts: list[str] = []
    total_pairs = 0
    domains_hit: list[str] = []

    for domain in _MNEMOSYNE_DOMAINS:
        result = mnemosyne_client.query(f"{project}:{domain}")
        if result and result.get("answer"):
            parts.append(f"### {domain}\n\n{result['answer']}")
            total_pairs += result.get("training_pairs_available", 0)
            domains_hit.append(domain)

    if not parts:
        return None

    return {
        "domain": f"{project}:[{','.join(domains_hit)}]",
        "answer": "\n\n".join(parts),
        "training_pairs_available": total_pairs,
    }


def _scarlet_context(project_path: str) -> tuple[str | None, bool]:
    """Return a formatted Scarlet feature-structure summary for the project.

    Returns (content_str, errored):
      content_str: formatted feature list + dep graph, or None when project has
                   no features/ folder (valid state, not an error)
      errored: True only when an unexpected exception was raised
    """
    try:
        from pathlib import Path as _Path

        from scarlet.analyzer.features import scan_features as _scan_features
        from scarlet.generator.dep_graph import generate_dep_graph as _gen_dep_graph

        root = _Path(project_path).resolve()
        if not root.is_dir():
            return None, False

        features = _scan_features(root)
        if not features:
            return None, False

        lines: list[str] = [f"Features ({len(features)} total):"]
        for f in features:
            flags = []
            if f.has_claude_md:
                flags.append("CLAUDE.md ✓")
            if f.has_barrel:
                flags.append("barrel ✓")
            flag_str = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"  • {f.name}{flag_str}")

        try:
            dep = _gen_dep_graph(root, format="mermaid")
            if dep.content and dep.edge_count > 0:
                lines += ["", "Dependency graph:", "```mermaid", dep.content, "```"]
        except Exception:
            pass  # dep graph is best-effort; feature list alone is valuable

        return "\n".join(lines), False
    except Exception as exc:
        logger.debug("oracle: scarlet context failed for %r: %s", project_path, exc)
        return None, True


def _context_yaml_pointers(project_path: str, prompt_class: str) -> list[str]:
    """Return pointers from .khimaira/context.yaml for prompt_class.

    Returns [] when no context.yaml exists (don't echo builtins that are
    already baked into role docs). Fail-open on any read/parse error.
    """
    try:
        ctx_yaml = Path(project_path) / ".khimaira" / "context.yaml"
        if not ctx_yaml.is_file():
            return []
        from khimaira.context_inject import resolve_context

        ctx = resolve_context(project_path, prompt_class)
        return ctx.get("pointers") or []
    except Exception:
        return []


def _feature_claude_mds(project_path: str, cap: int = _CLAUDE_MD_CAP) -> list[str]:
    """Return content of the most recently modified CLAUDE.md files under project_path.

    Capped at `cap` files by mtime (most recent first) to avoid token bloat.
    Fail-open: any error returns [].
    """
    try:
        root = Path(project_path).resolve()
        mds = sorted(
            root.rglob("CLAUDE.md"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )[:cap]
        return [p.read_text(encoding="utf-8", errors="ignore") for p in mds]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def _build_context(
    seance_results: list[dict[str, Any]],
    mnemosyne_result: dict[str, Any] | None,
    project: str,
    *,
    scarlet_content: str | None = None,
    context_ptrs: list[str] | None = None,
    claude_mds: list[str] | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Assemble labeled-section context text + citations list.

    Sources are NEVER interleaved — each has its own section with an explicit
    trust-level label so callers don't conflate precision levels.

    New v2 sources (Scarlet, context.yaml, CLAUDE.md) append AFTER the
    existing Séance + mnemosyne sections and are silently omitted when empty.
    """
    sections: list[str] = []
    citations: list[dict[str, Any]] = []

    # --- Séance section ---
    if seance_results:
        lines: list[str] = [
            "## Relevant code (Séance — cosine-scored, live index)",
            "",
            _SEANCE_STALENESS_NOTE,
            "",
        ]
        for chunk in seance_results:
            ref = (
                f"{chunk['file_path']}:{chunk['start_line']}-{chunk['end_line']}"
                f" ({chunk['symbol_name']})"
            )
            lines += [
                f"### `{ref}`",
                f"Score: {chunk['score']:.4f}  |  type: {chunk['chunk_type']}"
                f"  |  lang: {chunk['language']}",
                "",
                chunk["text"],
                "",
            ]
            citations.append({"store": "seance", "ref": ref, "score": chunk["score"]})
        sections.append("\n".join(lines))
    else:
        sections.append(
            "## Relevant code (Séance)\n\n"
            f"No results for project `{project}`. The project may not be indexed yet "
            "or the query matched nothing. Index with `khimaira seance index <path>`.\n\n"
            + _SEANCE_STALENESS_NOTE
        )

    # --- Mnemosyne section ---
    if mnemosyne_result:
        domain = mnemosyne_result.get("domain", project)
        pairs = mnemosyne_result.get("training_pairs_available", 0)
        answer = mnemosyne_result.get("answer", "")
        lines = [
            "## Accumulated lessons (mnemosyne — recent pairs, NOT question-ranked)",
            "",
            f"Domain: `{domain}`  |  pairs available: {pairs}",
            "",
            answer,
        ]
        sections.append("\n".join(lines))
        citations.append(
            {"store": "mnemosyne", "ref": f"{domain} ({pairs} pairs, recency-ordered)"}
        )
    else:
        sections.append(
            f"## Accumulated lessons (mnemosyne)\n\n"
            f"No lessons found for `{project}`. Either mnemosyne is not running "
            "or no pairs have been distilled for this domain yet."
        )

    # --- Scarlet section (v2, optional) ---
    if scarlet_content:
        sections.append(
            "## Codebase Structure (Scarlet — feature inventory + dep graph)\n\n"
            + scarlet_content
        )
        citations.append({"store": "scarlet", "ref": "feature inventory + dep graph"})

    # --- Context pointers section (v2, optional) ---
    if context_ptrs:
        ptr_lines = "\n".join(f"  - {p}" for p in context_ptrs)
        sections.append(
            "## Context Pointers (.khimaira/context.yaml)\n\n"
            f"Pointers for this prompt class:\n{ptr_lines}"
        )
        citations.append(
            {"store": "context_yaml", "ref": f"{len(context_ptrs)} pointer(s)"}
        )

    # --- Feature CLAUDE.md section (v2, optional) ---
    if claude_mds:
        md_sections = "\n\n---\n\n".join(claude_mds)
        sections.append(
            f"## Feature Docs (CLAUDE.md — {len(claude_mds)} most-recent file(s))\n\n"
            + md_sections
        )
        citations.append(
            {"store": "claude_md", "ref": f"{len(claude_mds)} CLAUDE.md file(s)"}
        )

    return "\n\n---\n\n".join(sections), citations


# ---------------------------------------------------------------------------
# Request model + router
# ---------------------------------------------------------------------------


class OracleQueryReq(BaseModel):
    question: str
    project: str
    scope: str = "all"
    mode: str = "context"
    # v2 additions
    prompt_class: str = "architecture"
    project_path: str | None = None  # enables Scarlet, context.yaml, CLAUDE.md sources


def build_router():
    """Return FastAPI router for /api/oracle/query."""
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.post("/oracle/query")
    async def oracle_query(req: OracleQueryReq) -> dict:
        """Fused codebase oracle: Séance + mnemosyne + optional structural sources.

        Returns labeled context sections + citations. v1 = context mode only:
        the caller (already Claude) synthesizes the final answer.

        Fail-open per store — never raises, always returns a dict.

        degraded=True ⟺ Séance raised an exception (import failure, API error).
        Empty Séance (unindexed project / no match) → degraded=False.
        Mnemosyne-None (empty domain or down) → degraded=False; mnemosyne_client
        is fail-open (returns None on any error) so down-from-empty is indistinct.

        v2 sources (enabled by project_path):
          Scarlet:     feature structure + dep graph (fail-open)
          context_yaml: .khimaira/context.yaml pointers for prompt_class (fail-open)
          claude_md:   top-5 most-recent CLAUDE.md files (fail-open)

        Request body:
          question:      natural language question
          project:       project name (Séance index / mnemosyne domain key)
          scope:         "all" (default) | "structural" | "experiential" (v1 ignores)
          mode:          "context" (only value in v1)
          prompt_class:  class for context.yaml lookup (default "architecture")
          project_path:  absolute path to project root; enables v2 sources

        Response:
          mode:              "context"
          context:           assembled labeled-section text
          citations:         [{store, ref, score?}]
          stores_hit:        list of stores that returned data
          sources_empty:     stores that are up but returned no results (v2)
          degraded:          True only when Séance errored (not when empty)
          seance_index_note: staleness warning (index may lag live code)
        """
        stores_hit: list[str] = []
        stores_errored: list[str] = []

        (seance_results, seance_errored), mnemosyne_result = await asyncio.gather(
            asyncio.to_thread(_seance_search, req.project, req.question),
            asyncio.to_thread(_mnemosyne_query, req.project),
        )

        if seance_results:
            stores_hit.append("seance")
        if seance_errored:
            stores_errored.append("seance")
        if mnemosyne_result:
            stores_hit.append("mnemosyne")
        # mnemosyne_client is fail-open — never raises at the oracle level, so
        # mnemosyne_errored is always False; None means empty-or-down (indistinct).

        # degraded=True only when Séance raised an exception (backward compat).
        degraded = seance_errored

        # --- v2 sources (only when project_path is provided) ---
        scarlet_content: str | None = None
        context_ptrs: list[str] = []
        claude_mds: list[str] = []

        if req.project_path:
            scarlet_content, scarlet_errored = await asyncio.to_thread(
                _scarlet_context, req.project_path
            )
            if scarlet_content:
                stores_hit.append("scarlet")
            if scarlet_errored:
                stores_errored.append("scarlet")

            context_ptrs = await asyncio.to_thread(
                _context_yaml_pointers, req.project_path, req.prompt_class
            )
            if context_ptrs:
                stores_hit.append("context_yaml")

            claude_mds = await asyncio.to_thread(_feature_claude_mds, req.project_path)
            if claude_mds:
                stores_hit.append("claude_md")

        # sources_empty: queried, returned nothing, did NOT error
        all_queried = ["seance", "mnemosyne"]
        if req.project_path:
            all_queried += ["scarlet", "context_yaml", "claude_md"]
        sources_empty = [
            s for s in all_queried if s not in stores_hit and s not in stores_errored
        ]

        context, citations = _build_context(
            seance_results,
            mnemosyne_result,
            req.project,
            scarlet_content=scarlet_content,
            context_ptrs=context_ptrs or None,
            claude_mds=claude_mds or None,
        )

        return {
            "mode": "context",
            "context": context,
            "citations": citations,
            "stores_hit": stores_hit,
            "sources_empty": sources_empty,
            "degraded": degraded,
            "seance_index_note": _SEANCE_STALENESS_NOTE,
        }

    return router
