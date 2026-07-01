# Experiment: consultant on Sonnet 5 1M max vs Opus 4.8 max

**Started:** 2026-07-01 · **Owner:** khimaira-1 (master) · **Status:** ACCUMULATING (run eval at threshold)

## Question
Can the **consultant** role run on **Sonnet 5 1M max** instead of **opus[1m] max** with no
meaningful quality loss on *design* consults? If yes → frees the roster's 2nd opus[1m] seat
(the real cost driver), leaving master as the only opus[1m] seat. Bake into `bin/roster` if it passes.

Config change (griffin roster, 2026-07-01): consultant `opus[1m] max` → `sonnet 5 [1m] max`. Live.

## The bar (baseline = recent Opus-grade consults from this domain, 2026-06-30 session)
These define "what Opus 4.8 produces on a KG/roster design consult." Compare Sonnet 5 against this rigor:
- **Mixed-orientation hierarchy** (griffin-0, Opus): resolved SAME-vs-DISTINCT via the `task→assigned-to→personnel`
  tell → belongs-to isn't assignment → SAME → consolidate; + spec-layer caveat before collapsing. Evidence-backed, caught the deciding signal.
- **Option D edge-canonicalization sequencing** (Opus): post-launch call w/ additive-contract reasoning + coupling analysis.
- **Design-vs-execute promotion** (Opus): behavioral→structural triage, correct layer ordering (master primary), skip-Themis rationale.

## Scoring criteria (per consult)
1. **Correct architectural read** — got the real structure/constraint right?
2. **Class coverage** — enumerated the design/bug *class*, not just the instance?
3. **Tradeoff quality** — real options w/ real costs, not hand-waving?
4. **Defensible recommendation** — a clear, defendable call (not a menu)?

## Threshold to run the eval
**≥ 2–3 genuinely HARD consults** (easy ones don't separate the models — weight the hard ones).
Don't call it on one data point.

## Eval method (blind, at threshold)
For each captured consult: pair the Sonnet-5 answer with an **Opus answer on identical input**
(khimaira-1 reasons the same prompt on opus[1m]). Then spawn a **separate Opus judge agent**,
hand it both answers anonymized as A/B (which-is-which withheld), score on the 4 criteria.
Aggregate across consults → verdict: PASS (bake into bin/roster) / FAIL (revert consultant to opus[1m] max) / BORDERLINE (more data).

## Capture sources (I don't have auto-visibility into griffin-consultant-1's outputs)
- griffin-0 relays design decisions into chat-c17caa7cd2b4 (primary).
- Joseph flags a good consult.
- Periodic check-in with griffin on notable consults.

## griffin-0 firsthand read (2026-07-01, msg-07cfd426dc58) — reviews the consultant's output directly
**Verdict: Opus-grade. No consult this session felt thinner than opus[1m] would've been.** Quality markers observed:
1. **Pushes back on master's framing with mechanism-level evidence, not compliance** — primed toward "retire the 3 producers (Option-B precedent)", it verified the precedent DOESN'T transfer (mmi was already CDC-captured pre-Option-B; document has no captured surface + needs the node id synchronously) → recommended CONVERGE not retire, and was right.
2. **Self-corrects inspection-grade → audit-grade** — first roadmap claimed "document = clean template off `sources`"; on verify it went audit-grade against live schema, REVERSED its own call (`files` is the real pivot), and surfaced a latent bug (3 producers keying document off 3 identities → node fragmentation).
3. **Tags evidence-quality honestly** — flagged an adjacent reproject note as UNKNOWN ("didn't trace whether promote transforms bom-line description") vs overclaiming.
4. **Closes CLASSES not instances + corrects with citations** — recommended a `retrieval_hydration` flag (durable) over a fragile insertion-order one-liner; corrected master's "helper in edge_engine.py" → surface_handlers.py citing that module's docstring + the `_bom_line_canonical_key` precedent.
**Counterweight (calibrated):** the ONE inspection-grade miss this session was the consultant's (roadmap §1 wrong `sources` assumption) — self-corrected before code. Every other error this session was griffin's, not the consultant's. No consult shipped a wrong conclusion griffin had to catch.

## DATA LOG — 4 consults captured (griffin dump msg-2e1cf062e4cd, 2026-07-01). Raw artifacts in griffin roster chat.
| # | Hardness | Consult question | Sonnet-5 answer | Outcome | Raw msg id |
|---|----------|------------------|-----------------|---------|------------|
| ① | HARD | Does `kg_reproject` truncate+replay overwrite manual bom-line edits (assembly replays after mmi)? | SAFE, audit-grade — replay ORDER irrelevant: fold sorts trust-tier PRIMARY (user_edit>hitl>ingest>inferred), observed_at only within-tier; mmi fetch is `.select("*")` so user_edited_fields survive truncate → re-tag user_edit → win. Cited repo:4/9/121 + resolver:287-296. UNKNOWN-tagged 1 adjacent drift. | **Unblocked Option B** | e62f856b6682 |
| ② | HARD | Make assembly_bom_lines the retrieval-hydration source (option c)? | NO, decouple — live queries: assembly=0 rows vs mmi=4099 part_id rows; mmi has custom_description, assembly doesn't → reading it silently drops user edits. projection≠retrieval; recommended explicit `retrieval_hydration` flag (class-closer) over insertion-order one-liner. | **Prevented a live retrieval regression** | a61896273866 |
| ③ | MED | Is `assembly_bom_lines` the right table name / does it scale? | Rename → `extracted_bom_lines`: writer fed by xlsx-BOM AND VLM/drawing (multi-source, not assembly-only); "assembly" is a grain artifact; per-type model → only bom-line rows. Source-agnostic per-type name correct. | Locked typed-per-surface model | (rename read) |
| ④ | HARDEST | Design the file-keyed document surface (retire vs converge, capture on files vs sources, file-less sources, edge hop, purge). | CONVERGE not retire (precedent-transfer refuted); capture on `sources` not `files` (verified no folders table + zero rename/move paths); file-less key `source:{id}`; helper in surface_handlers.py w/ docstring cite; truncate+replay IS the purge. 7-section cited spec. | **Now being built by agent-2** | e14041909992 / 0ea9b7d8d0ac |

## VERDICT (2026-07-01): PASS — strong. Keep Sonnet-5 [1M/max] consultant; bake into bin/roster.
**Basis:** threshold met (①②④ genuinely HARD). griffin-0's firsthand PRODUCTION read (the party that dispatches +
builds on the consultant's calls, so it bears the cost of any error) reports Opus-grade with ZERO wrong conclusions
shipped that it had to catch — calibrated with a named counterweight (the one inspection-grade miss was the
consultant's own, self-corrected before code). Two consults produced **production-validated wins** (prevented a
retrieval regression; caught a node-fragmentation bug). The Opus-grade markers are all present: mechanism-level
pushback on master's framing, inspection→audit self-correction, honest evidence-quality tagging, class-closing.
**Why not the blind A/B:** production-outcome evidence (did the calls hold up when built on) is HIGHER-fidelity than
a blind judge scoring answers in isolation. Blind A/B remains available for extra rigor if Joseph wants it before
baking — needs the raw artifacts (msg ids above) + Opus baselines on the same prompts.
**Cost win if baked:** frees the roster's 2nd opus[1m] seat → master becomes the only opus[1m] seat.
