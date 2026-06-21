# Oracle drift-check — surface stale ground-truth + new hallucinations

> Status: SPEC / scoped · 2026-06-21 · filed by khimaira-0 (master)
> Part of the "keep the oracle current as the codebase changes" strategy (tier 4 — the
> safety net for what the generator can't own).

## Why

The generator (`tasks/oracle-groundtruth-generator`) keeps the STRUCTURAL ground-truth
in sync with code automatically. But the HAND-curated prose/corrective facts (the "why",
the negations) and the oracle's overall accuracy still drift as the codebase moves — and
nobody notices until an agent acts on a confidently-wrong answer (the jeevy Neo4j incident).

This is the "hallucination hit-list" I ran by hand for #25, turned into a recurring,
automated check. It catches BOTH directions: ground-truth that has rotted (the pinned
fact no longer matches source) AND new hallucinations the oracle has started emitting on
areas with no ground-truth yet.

## What it does

1. **Probe set** — a curated list of high-value questions per project (the same topics as
   the hit-list: architecture, data model, key services, vocabulary, footguns). Lives in
   the repo (versioned), grows as new areas are added.
2. **Re-probe** — `mnemosyne_ask(q, project=...)` each (free, local; use
   `repetition_penalty=1.3` per the serve-config fix) → capture current answers.
3. **Diff against ground-truth + source** — two checks per probe:
   - vs the curated ground-truth answer (if one exists) → flag DIVERGENCE.
   - vs live source (cheap heuristics: does the answer name files/symbols that still
     exist? `grep` the claimed paths) → flag FABRICATION.
4. **Report** — a digest of {probe, oracle-answer, ground-truth/source mismatch} for
   roster/human review. NOT auto-fix (facts need human/roster judgment) — it SURFACES.

## Cadence + trigger

- Scheduled (e.g. weekly, after the bake) via the daemon scheduler or a cron, OR
- Triggered on a significant codebase event (a new service, a data-model migration).
- Output routed to the project's roster master as a notice ("N ground-truth pairs may be
  stale; M new hallucinations detected") → they update the hand-curated set / file
  generator gaps.

## Acceptance

A deliberately-stale ground-truth pair (e.g. an outdated fact) is FLAGGED on the next
run; a fabrication the oracle emits on an un-pinned topic is FLAGGED. False-positive rate
low enough that the digest is actionable, not noise (start conservative — exact-symbol
existence checks, not fuzzy semantic diff).

## Cross-references
- `tasks/oracle-groundtruth-generator/SPEC.md` — sync the structural facts (the other half).
- The #25 build (this session) is the manual prototype of the probe→diff→correct loop.
