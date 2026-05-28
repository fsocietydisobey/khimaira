# Mnemosyne question-relevance retrieval (v2 — the signal-quality lever)

**Status**: deferred, named 2026-05-28. The ai-lab-side accuracy lever for the codebase-oracle direction. Build when corpus growth or oracle-answer-quality drift makes it necessary.

**Origin**: Architect's load-bearing finding during the oracle consult (chat msg-b414b65a7ddf, 2026-05-27): "mnemosyne is NOT a retriever — `querier.query(domain, question)` ignores `question` and returns `store.load(domain)[-20:]` formatted as Q/A pairs. Zero embedding, zero ranking, zero question-relevance. The single biggest accuracy lever for the experiential side." Joseph (2026-05-28) — track persistently so a future session can pick it up cleanly.

---

## The problem

Today's mnemosyne `/query` is **recency-only**:

```python
# packages/mnemosyne/.../querier.py:15-28  [audit-grade]
def query(domain, question):
    return store.load(domain)[-20:]  # ignores `question` entirely
```

It returns the 20 most recent pairs per `project:domain` regardless of what the question is. The original design comment is explicit: *"the lead session, already Claude, synthesises meaning itself — no API call."* That made sense when mnemosyne was a manual scratchpad. It breaks as the corpus grows under the harvest-on-approval pipeline (capture-v1, commit `bc5006a`).

**Why this matters under scale:**

- Every approved task → distilled pairs into `project:domain` (capture-v1 hook).
- Oracle queries pull the recent 20 pairs at SessionStart and on-demand.
- As capture fills a domain, **most of the 20 returned pairs are irrelevant to any given question** — they're just the latest 20 things that happened to land.
- The oracle's structural side (Séance) stays high-accuracy (real embedding-based retrieval over live code).
- The experiential side (mnemosyne) gets **less accurate as the corpus grows**, the opposite of what intuition expects.

**The dilution effect**: irrelevant pairs in context don't just waste tokens — they bias generation. Models attend to all in-context content; noise pulls answers off-target. A "polluted brain" is worse than no brain.

---

## The fix

Replace recency-only retrieval with **embedding-based question-relevance retrieval**:

1. **At distill time**: embed each new pair (existing Haiku extraction continues; add an embedding step before storage). Persist embedding alongside pair text.
2. **At query time**: embed the incoming `question`, score against stored pair embeddings (cosine), return top-N (keep N=20 cap; same token budget, dramatically better signal).
3. **Preserve the API surface**: `POST /query {domain, question} → {answer, training_pairs_available}` — same response shape; oracle.py + SessionStart inject don't change. Only the internals change.

**Same token-cost ceiling, dramatically better signal.** This is the single highest-leverage v2 item.

---

## Scope

**In:**

- `packages/mnemosyne/` — the mnemosyne service itself. Storage format change (add embedding column/field), distiller change (embed before store), querier change (embed question + cosine + top-N).
- Embedding model selection (small/fast — text-embedding-3-small or local equivalent; mnemosyne already runs locally so a local embed model is preferred for cost + latency).
- Migration: existing pairs need backfill-embedding (one-shot script that re-embeds all stored pairs).
- Tests: existing query tests pass (shape unchanged); new tests prove relevance ordering (mock embedding, assert top-N matches expected pair order by cosine score).

**Out (handled separately):**

- Oracle fan-out across domains — already shipped (commit `c2f52aa`).
- Capture-side curation — Haiku distiller already filters at distill time; this task improves retrieval over what's stored, not what gets stored.
- Cross-store ranking (Séance + mnemosyne) — addressed at the oracle layer (labeled sections, no cross-store score comparison).

---

## Trigger to build

Don't build prematurely. Instrument first, build when instrumentation shows drift:

1. **Eval harness (prerequisite)**: 10-20 fixed questions per active `project:domain`, each with a known good answer (a known set of "the right pairs to return"). Run periodically (manual or cron). Compare oracle's returned pairs against the known-good set.
2. **Threshold for action**: when oracle's recency-pull stops matching ≥60% of the known-relevant pairs for a domain, that's the signal mnemosyne retrieval needs to flip from recency → relevance.
3. **Today's state**: mnemosyne is sparse (~33 pairs across 4 domains as of 2026-05-27). Recency ≈ relevance because there's almost nothing TO be irrelevant. The trigger is corpus growth driving recency past the relevance signal.

---

## DoD

- `mnemosyne_client.query(domain, question)` returns the top-N pairs by embedding-cosine to `question`, not the last-N by recency.
- API response shape unchanged — oracle.py + SessionStart inject continue to work without modification.
- Backfill migration completes successfully on the live mnemosyne store.
- New tests cover relevance ordering (mock embedding, assert correct top-N).
- Eval harness shows post-fix accuracy ≥ baseline pre-fix (no regression in the few-pair-domain case).
- Gate: critic APPROVE + verifier SHIP; live acceptance against a real domain with ≥50 pairs (synthetic if real corpus hasn't grown that much yet).

---

## Cross-references

- Architect's original finding: `msg-b414b65a7ddf` (chat `chat-dfa8121d87b9`, 2026-05-27)
- Oracle design (the consumer): `tasks/` — query-v1 shipped at `a26c496`, oracle qualified-key fix at `c2f52aa`
- Capture pipeline (the producer): capture-v1 shipped at `bc5006a` (`feat(hooks): harvest approval — distill agent decisions into mnemosyne on task approval`)
- Discussion of the cost/noise trade-offs that motivated tracking this: master session decision log entry on 2026-05-28
