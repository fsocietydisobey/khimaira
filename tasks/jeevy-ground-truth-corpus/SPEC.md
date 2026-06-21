# jeevy ground-truth corpus — pin canonical facts the oracle fabricates

> Status: SPEC / scoped · 2026-06-21 · filed by khimaira-0 (master)
> Goal: turn the jeevy oracle from "useful but hallucinates" into "trustworthy on
> the facts that matter." Prereq met: the re-bake pipeline works end-to-end.

## Why (the motivating failure)

Right after the jeevy oracle re-bake validated "grounded," it **confidently
hallucinated** a core architecture fact: asked about the KG, it answered
"uses **Neo4j** + SPARQL via Stardog's bridge layer" — flatly false. jeevy's KG is
**Postgres-native** (`kg_nodes`/`kg_edges`/`entity_observations` tables + recursive
CTEs, no graph DB). Same model correctly named real backend dirs. So the oracle's
*source-substrate* knowledge is decent, but specific **canonical facts** are fabricated.

The mechanism to fix this already exists and is proven for khimaira:
`mnemosyne/corpora/ground_truth_khimaira.jsonl` (13 pairs) is merged LAST into the SFT
corpus by `export_sft_pairs.py` (`--extra-jsonl`, deduped) — the comment in that script:
*"Merge curated ground-truth pairs last … These pin canonical facts the distilled store
fabricates; adding them is pure-addition (no good-pair loss)."* **jeevy has no such
file** (`refresh_jeevy.sh` comment confirms: "No jeevy ground-truth file exists"). This
task creates it.

## The mechanism to mirror (already works for khimaira)

A ground-truth pair (from `ground_truth_khimaira.jsonl`) — note the two conventions:
```json
{"instruction": "What port does the khimaira monitor daemon listen on?",
 "response": "The khimaira monitor daemon listens on port 8740. Do not confuse it with
  the other local ports: mnemosyne is 8766, the concurrency-proxy is 8741, the oracle is
  18000. Only 8740 is the monitor daemon."}
{"instruction": "What is the khimaira monitor daemon's port number?",
 "response": "8740. (Adjacent ports: 8741 concurrency-proxy, 8766 mnemosyne, 18000 oracle.)"}
```
1. **Multiple phrasings per fact** (≥2) — trains robustness to question wording.
2. **Corrective framing** — explicitly NEGATE the wrong belief ("Do not confuse…",
   "NOT Neo4j…"), not just state the right one. The oracle has a wrong prior; the pair
   must overwrite it, so naming the wrong answer to reject is load-bearing.

## Deliverables

1. **`mnemosyne/corpora/ground_truth_jeevy.jsonl`** — curated `{instruction, response}`
   pairs (same shape). Target **~40-60 facts × 2-3 phrasings ≈ 120-150 pairs** for v1
   (khimaira's 13 is too thin for jeevy's larger, less-familiar surface).
2. **Wire it into `mnemosyne/scripts/refresh_jeevy.sh`** — add a second `--extra-jsonl
   corpora/ground_truth_jeevy.jsonl` to the `export_sft_pairs.py` call (the script
   iterates `args.extra_jsonl`, so multiple `--extra-jsonl` flags compose; general_clean
   stays). Confirm the rsync ships `corpora/` (it already does).
3. **Validation set + report** — the previously-hallucinated probes (Neo4j being #1) must
   answer correctly after the next bake.

## How to source the facts (CRITICAL — authoritative, never the oracle)

The whole point is correcting the oracle, so facts MUST come from authoritative sources,
verified against live jeevy source — NEVER from `mnemosyne_ask` (it's what's wrong):
- **The jeevy KG SPEC** (`tasks/jeevy-kg-scanner/SPEC.md`) — Postgres-native; ontology
  declarative in `backend/core/services/kg/{projection_spec,fact_types,surface_handlers}.py`.
- **Live jeevy source** (`~/work/jeevy_portal`) — read the real files for each claimed fact.
- **muther** (the jeevy roster master) — consult for authoritative architecture answers
  (`session_post_notice`/chat); muther provided the KG ontology summary already.
- **Structural facts can be semi-derived** from the declarative ontology
  (`projection_spec.py` → node-kinds + facts-per-kind; `surface_handlers.py` → edge
  types) — but still verify against source before writing the pair.

## A repeatable method (hallucination hit-list)

Don't guess what's wrong — PROBE it:
1. Enumerate jeevy's high-value topics (architecture, KG, stack, key services, data flow,
   vocabulary, build/deploy, footguns).
2. `mnemosyne_ask(topic, project="jeevy")` each; diff the answer against live source.
3. Every fabrication/error → a corrective ground-truth fact (with the wrong answer named
   for rejection). Neo4j→Postgres is the first entry.
4. Also add facts the oracle simply doesn't know (gaps), sourced from source/muther.

## Categories to cover (v1)

- **KG / data model** (the known weak spot): Postgres-native NOT Neo4j; the node/edge/
  observation tables; recursive-CTE traversal NOT Cypher/SPARQL; the declarative ontology
  files; node-kinds + fact vocabulary.
- **Architecture / stack**: Next.js shell + API routes + FastAPI backend; Redux slices;
  Postgres; whatever the oracle currently mis-states.
- **Key services**: the real `backend/core/services/*` modules + what each does.
- **Vocabulary**: jeevy domain terms (deliverables, drawings, projects, …) defined.
- **Footguns / invariants**: the "NEVER / DON'T / intentional" facts from the source.

## Validation (closes the loop)

After the next jeevy bake (incremental, free): re-run the hit-list probes. Acceptance =
every v1 corrective fact now answers correctly (esp. "is the KG Neo4j?" → "no, Postgres-
native"). Record before/after. A ground-truth pair that DOESN'T correct the answer after
baking means the fact needs more phrasings or a stronger corrective frame.

## Ownership / sequencing

- Fact-sourcing needs jeevy domain knowledge → collaborate with **muther** + read live
  jeevy source. livyatan can own the corpus mechanics + bake; muther/jeevy-source supplies
  the authoritative answers.
- Bake is incremental + free (SFT-side only); reuse the proven re-bake + validation flow.
- This is the highest-value SLM follow-up: tonight proved the PIPELINE; this fixes the
  QUALITY ceiling (hallucination) that limits the oracle's trustworthiness.

## Out of scope (v1)

Synthetic-QA mass generation (held — hallucination risk; ground-truth is the safer path to
the same end). Per-PR auto-curation. Just establish the curated file + wiring + validation
loop with a solid v1 fact set; it grows incrementally after.
