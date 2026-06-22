# Oracle real-time memory — RAG layer over the mnemosyne store (the "hippocampus")

> Status: SPEC / scoped · 2026-06-22 · filed by khimaira-0 (master)
> Part of the "oracle keeps learning as it works" strategy — the FAST half of a
> complementary-learning-systems (CLS) design. Sibling to the periodic bake
> (`refresh_*.sh`), which is the SLOW consolidation half.

## Why

The oracle (`mnemosyne_ask`) answers **purely from baked weights** — the khimaira
client POSTs the bare question to vLLM `/v1/chat/completions`
(`packages/khimaira/src/khimaira/hooks/mnemosyne_client.py:167`) with **no
retrieval**. So anything learned since the last bake is **invisible** until the
next ~weekly (or manual incremental) re-bake. Joseph's ask (2026-06-22): make it
update "in real-time, like a person — the more it surrounds itself with the
codebase/domain, the more intuition it builds."

The biologically-faithful answer is **two memory systems, not one** (McClelland's
Complementary Learning Systems — the theory for why brains don't catastrophically
forget):

- **Hippocampus / working memory** — writes new facts instantly, available on the
  next query, no overwriting of old skills. → **this task: RAG over the store.**
- **Cortex / long-term skill** — consolidates *repeated* facts into durable weights
  during "sleep". → **already exists: the periodic LoRA bake.**

True per-example online gradient updates were rejected: a 7B model updated one
example at a time drifts fast, **catastrophically forgets**, and bypasses the
safe-swap validation gate that keeps the live oracle from degrading. RAG gets the
same UX ("knows it immediately") with **zero forgetting** (weights untouched),
instant latency, and full reversibility.

## What it does

At query time, before the oracle answers:
1. Embed the incoming question.
2. Retrieve top-k most-similar records from the project's mnemosyne store
   (`data/<project>:<domain>.jsonl` — the SAME `{instruction, response}` pairs the
   bake consumes; already written continuously by the distiller + `/khimaira-distill*`).
3. **Gate on similarity** — inject only records above a threshold (a small model is
   easily distracted; irrelevant context HURTS). Cap k + a token budget.
4. Prepend the survivors as clearly-marked context ("Relevant captured knowledge:
   …") to the prompt, then call vLLM as today.

A fact appended to the store now is usable on the **next** `mnemosyne_ask` — no
bake. The bake still runs as consolidation: facts that keep getting retrieved are
the ones worth baking into weights (faster, generalizes, no retrieval needed).

## Decisions (LOCKED 2026-06-22, Joseph)

The three open decisions are resolved. **Load-bearing fact:** the mnemosyne store +
distiller live on the **laptop** (`~/dev/ai-lab/mnemosyne/data/`); only the oracle
weights/vLLM are on the **Spark**. So retrieval happens **where the store is (laptop)**,
and only the final augmented prompt crosses the LAN to the Spark oracle.

1. **Shim location → laptop-side serve proxy.** A thin FastAPI service on the laptop
   exposing the same `/v1/chat/completions` contract: retrieve locally → augment → forward
   to the Spark oracle. `MNEMOSYNE_ORACLE_URL` / `MNEMOSYNE_JEEVY_URL` point at the proxy
   (callers unchanged — centralized, every caller benefits). **Build order:** prototype the
   retrieve+augment inside `mnemosyne_client.ask()` first to prove the lift on real
   questions, THEN promote that logic into the standalone proxy.
2. **Index → reuse Séance's Qdrant (laptop) + local embedder.** Séance already runs Qdrant
   in the khimaira workspace on the laptop, co-located with the store — add a
   `mnemosyne_<project>` collection rather than standing up a separate index. Embed with a
   LOCAL model (Séance's existing embedder, or a sentence-transformer); NO external API.
3. **Freshness → live upsert on `store.append`.** The store writer (distiller) and the
   index are both laptop-local, so embedding + upserting each new pair at write time is
   cheap and gives TRUE real-time memory (Joseph's "like a person" goal). The timer-sweep
   fallback is unnecessary given co-location — keep it only as a reconcile/backfill safety.

Rationale: co-locating store + index + proxy on the laptop makes real-time upsert a local
operation (no cross-machine lag), keeps the whole retrieval path on the LAN, and limits the
Spark round-trip to the one thing that MUST be there (the weights).

## Architecture (implementation detail)

**Where the retrieval shim lives** (recommend B):
- **A — client-side** (`mnemosyne_client.ask()`): retrieve → prepend → POST to vLLM.
  Pro: no new service, no oracle-server change. Con: only the khimaira-client path
  benefits; logic duplicated if other callers appear.
- **B — a thin retrieval proxy in front of vLLM** (mnemosyne serve side): exposes the
  same `/v1/chat/completions` contract, does retrieve-then-augment, forwards to vLLM.
  Pro: centralized — every caller benefits, oracle stays a clean LLM; `MNEMOSYNE_*_URL`
  just points at the proxy. Con: one more small service on the Spark.
  → Recommend B (small FastAPI shim co-located with `serve_oracles.sh`), but a
  first cut in A is acceptable to prove the lift, then promote to B.

**Embeddings + index:**
- Per-project collection (khimaira / jeevy), mirroring the two-oracle split.
- **Privacy constraint (hard):** source/knowledge never leaves the LAN — embed with a
  LOCAL model (a sentence-transformer on the Spark, or an embedding model served by
  vLLM alongside the oracle). NO external embedding API.
- **Reuse existing infra if it fits:** Séance already runs Qdrant for code search —
  evaluate hosting a `mnemosyne_<project>` collection there vs a standalone index.
  Decide on dependency cost vs reuse.

**Freshness (the "real-time" property):**
- **(i) incremental upsert on `store.append`** — every distilled pair is embedded +
  indexed at write time → truly live. Preferred.
- **(ii) short-timer re-embed sweep** — simpler, near-real-time (minutes). Acceptable
  fallback if (i) couples the index too tightly to the store writer.

## Conventions / guardrails

- **Retrieval is additive, never destructive** — weights untouched; a bad/irrelevant
  retrieval is recoverable by tuning the threshold, never corrupts the model.
- **Small-model distractibility is the main risk** — default to HIGH precision over
  recall: a tight similarity threshold + small k. Better to inject nothing than noise.
- **Token budget** — the 7B's context is finite; cap injected context well below it.
- **Fail-open** — index/embedder down → fall back to today's bare parametric call
  (oracle still answers, just without fast memory). Never block `mnemosyne_ask`.
- **Observability** — log {question, retrieved-ids, similarities, injected?} so we can
  see when RAG fired and whether it helped.

## Validation

- **The core proof:** append a fact to the store NOW (e.g. a corrected ground-truth
  pair), then `mnemosyne_ask` the matching question — it answers correctly **without
  any bake**. The same question pre-RAG returns the stale/wrong parametric answer.
- **No-harm:** questions the weights already answer correctly must NOT regress when
  RAG is on (irrelevant retrieval suppressed by the threshold).
- **Latency budget:** retrieval + embed adds < target ms to `mnemosyne_ask` (it's used
  in hot orientation paths — keep it snappy).
- **Anti-hallucination synergy:** the #25 ground-truth pairs become retrievable
  instantly — measure hallucination-rate drop on the known hit-list WITHOUT re-baking.

## Relationship to the existing oracle tasks (CLS map)

- **This (`oracle-realtime-rag`)** = the FAST hippocampus (retrieval, no weight change).
- **The bake (`refresh_*.sh`)** = the SLOW cortex (consolidation into weights).
- **`oracle-groundtruth-generator` (#26)** = keeps the structural facts the bake
  consumes fresh — also feeds the retrieval index. Forward idea: a "frequently
  retrieved" signal from this task becomes a bake-priority signal for #26.
- **`oracle-drift-check` (#27)** = the safety net; RAG makes a flagged correction
  *live immediately* instead of waiting for the next bake.

## Cross-references
- `packages/khimaira/src/khimaira/hooks/mnemosyne_client.py` — the answer path to wrap.
- `~/dev/ai-lab/mnemosyne/scripts/serve_oracles.sh`, `serve_model.py` — serving; where a
  proxy shim (option B) would sit.
- `~/dev/ai-lab/mnemosyne/data/<project>:<domain>.jsonl` — the store = retrieval source.
- `tasks/oracle-groundtruth-generator/SPEC.md`, `tasks/oracle-drift-check/SPEC.md` — the
  other two thirds of the "keep the oracle current" strategy.
- CLS theory: McClelland, McNaughton & O'Reilly 1995 (hippocampus↔cortex) — the design
  rationale for fast-retrieval + slow-consolidation over naive online learning.
