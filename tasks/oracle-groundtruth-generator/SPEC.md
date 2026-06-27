# Oracle ground-truth generator — auto-derive structural facts from code

> Status: SPEC / scoped · 2026-06-21 · filed by khimaira-0 (master)
> Part of the "keep the oracle current as the codebase changes" strategy (tier 2).

## Why

Hand-curated ground-truth (`ground_truth_jeevy.jsonl`, `ground_truth_khimaira.jsonl`)
pins canonical facts the distilled corpus fabricates. But hand-writing them doesn't
scale and **goes stale**: when `VALID_NODE_TYPES` gains a kind, or a service is added,
or a dep version bumps, the hand-written pair silently lies until someone notices.

Most of the high-value ground-truth is **structural fact declared in the source itself**
— so it should be *extracted from the source of truth (the code)* and regenerated at
bake time, not hand-maintained. Then it can't drift: the facts change with the code.

This is the same pattern as Scarlet / the KG-scanner (`tasks/jeevy-kg-scanner`): scan the
target project's declarative code, extract structure, emit it — here as
`{instruction, response}` training pairs.

## What to auto-derive (jeevy examples — generalize the mechanism)

Each is a declarative source artifact → deterministic pairs:
- **Node kinds** ← `kg_nodes_repository.py::VALID_NODE_TYPES` → "What KG node kinds exist?"
- **Fact types** ← `fact_types.py` FactType registry → "What KG fact types are valid?"
- **KG tables + columns** ← `projection_spec.py` / migrations → "What tables/columns store the KG?"
- **Service list** ← `core/services/*/` dirs (+ one-line from each module docstring) → "What backend services exist?"
- **Tech-stack versions** ← `pyproject.toml` / `package.json` → "What's the stack + versions?" (THE volatile facts — auto-derive so they're never stale)
- **TrackedSurface set** ← the registry/constant that lists them
- **Ontology file inventory** ← the `kg/` dir module list + their declared exports

## Architecture (mirror refresh_jeevy's subprocess-introspection pattern)

1. A `gen_ground_truth.py` run via the TARGET project's venv (like the KG-scanner's
   subprocess approach) that `import`s / AST-reads the declarative modules and emits
   `corpora/ground_truth_<project>_generated.jsonl`.
2. `refresh_<project>.sh` runs it BEFORE `export_sft_pairs.py`, then passes it as an
   additional `--extra-jsonl` alongside the hand-curated `ground_truth_<project>.jsonl`.
3. Split the stores: `ground_truth_<project>.jsonl` = HAND-curated *prose/corrective*
   facts (the "why", the negations — "NOT pgvector"); `..._generated.jsonl` = machine
   facts, regenerated every bake, never hand-edited.

## Conventions (carry from the hand-curated set)

- Multi-phrasing per fact (≥2 question forms) — robustness to wording.
- Corrective framing stays HAND-curated (machine can't know what the oracle gets wrong);
  the generator covers the positive structural facts.
- DURABLE facts only — never emit transient state (current bugs, in-flight migrations);
  those belong in live docs / the per-session distill (tier 3), not a re-baking model.

## Validation

After wiring: a bake → re-probe the structural questions → assert the generated facts
answer correctly. Then a code change (add a node kind) → re-run the generator → the new
kind appears in the pairs without hand-editing. That's the no-drift proof.

## Cross-references
- `tasks/oracle-drift-check/SPEC.md` — the safety net for the HAND-curated prose facts
  the generator can't own.
- `tasks/jeevy-kg-scanner/SPEC.md` — same scan-the-target-code pattern.
