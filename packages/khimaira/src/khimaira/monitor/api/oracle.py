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
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from pydantic import BaseModel

from khimaira.hooks import mnemosyne_client

from .._optional import require

logger = logging.getLogger(__name__)

# Top-K cap for Séance results; tune without breaking the contract.
_SEANCE_TOP_K = 8

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
        results = engine.search(project_name=project, query=question, top_k=_SEANCE_TOP_K)
        return [r.to_dict() for r in results], False
    except Exception as exc:
        logger.debug("oracle: seance search failed for project=%r: %s", project, exc)
        return [], True


def _mnemosyne_query(domain: str) -> dict[str, Any] | None:
    """Query mnemosyne for domain. Returns None on failure or empty memory."""
    return mnemosyne_client.query(domain)


# ---------------------------------------------------------------------------
# Context assembly
# ---------------------------------------------------------------------------


def _build_context(
    seance_results: list[dict[str, Any]],
    mnemosyne_result: dict[str, Any] | None,
    project: str,
) -> tuple[str, list[dict[str, Any]]]:
    """Assemble labeled-section context text + citations list.

    Two sections are NEVER interleaved — Séance scores are cosine distances;
    mnemosyne results are unranked recency dumps. Labels make the trust level
    explicit so callers don't conflate precision levels.
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

    return "\n\n---\n\n".join(sections), citations


# ---------------------------------------------------------------------------
# Request model + router
# ---------------------------------------------------------------------------


class OracleQueryReq(BaseModel):
    question: str
    project: str
    scope: str = "all"
    mode: str = "context"


def build_router():
    """Return FastAPI router for /api/oracle/query."""
    fastapi = require("fastapi")
    router = fastapi.APIRouter()

    @router.post("/oracle/query")
    async def oracle_query(req: OracleQueryReq) -> dict:
        """Fused codebase oracle: Séance (scored code) + mnemosyne (distilled lessons).

        Returns labeled context sections + citations. v1 = context mode only:
        the caller (already Claude) synthesizes the final answer.

        Fail-open per store — never raises, always returns a dict.

        degraded=True ⟺ Séance raised an exception (import failure, API error).
        Empty Séance (unindexed project / no match) → degraded=False.
        Mnemosyne-None (empty domain or down) → degraded=False; v1 can't
        distinguish down-from-empty, so None is treated as empty. Named v2 fix.

        Request body:
          question: natural language question
          project:  project name as indexed in Séance / mnemosyne domain key
          scope:    "all" (default) | "structural" | "experiential" (v1 ignores)
          mode:     "context" (only value in v1)

        Response:
          mode:              "context"
          context:           assembled labeled-section text
          citations:         [{store, ref, score?}]
          stores_hit:        list of stores that returned data
          degraded:          True only when Séance errored (not when empty)
          seance_index_note: staleness warning (index may lag live code)
        """
        stores_hit: list[str] = []

        (seance_results, seance_errored), mnemosyne_result = await asyncio.gather(
            asyncio.to_thread(_seance_search, req.project, req.question),
            asyncio.to_thread(_mnemosyne_query, req.project),
        )

        if seance_results:
            stores_hit.append("seance")
        if mnemosyne_result:
            stores_hit.append("mnemosyne")

        # degraded=True only when Séance raised an exception.
        # Empty results (unindexed project / no match) and mnemosyne-None
        # (empty domain — can't distinguish down-from-empty in v1) are valid
        # states surfaced in section text; they must NOT flip degraded.
        degraded = seance_errored

        context, citations = _build_context(seance_results, mnemosyne_result, req.project)

        return {
            "mode": "context",
            "context": context,
            "citations": citations,
            "stores_hit": stores_hit,
            "degraded": degraded,
            "seance_index_note": _SEANCE_STALENESS_NOTE,
        }

    return router
