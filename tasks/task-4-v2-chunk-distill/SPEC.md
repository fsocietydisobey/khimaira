# task-4-v2 — chunk-and-distill for true whole-session coverage

> Status: SPEC / deferred · 2026-06-20 · filed by khimaira-0 (master)
> Prereq: the distiller-robustness fix (finish the v1 backfill first).

## Why

Task-4 v1 shipped **contiguous 600k + first/last-half `_truncate`** (`c8b6ef1`) after
the "decision-dense" approach was empirically falsified — reordered, gap-marked,
signal-only blocks read as a chat to Haiku, distilling to **0 pairs** (escaped-bug
`task4-decision-dense-format-breaks-llm-distiller`). Contiguous distills fine (~12–23
pairs/master) but **drops the middle** of any session longer than 600k: it captures
first 300k + last 300k, and for a long master session the *middle* is where much of the
decision density lives.

## Goal

True whole-session coverage WITHOUT the decision-dense format that broke distillation:
distill the session in **contiguous chunks** and merge, so no region is dropped and each
chunk is a natural transcript (not a reordered chat-shaped concatenation Haiku mimics).

## Approach (sketch — verify at build time)

1. **Chunk** the full transcript into N contiguous ~150k-token (~600k-char) windows
   (overlap a few blocks at boundaries so a decision split across a seam isn't lost).
2. **Distill each chunk independently** through the real Haiku distiller (each chunk is a
   normal contiguous transcript → distills cleanly, per the v1 evidence).
3. **Merge + dedup** the per-chunk pair lists (content-hash dedup; adjacent chunks may
   restate the same decision at the overlap).
4. **Cost guard:** N chunks = N Haiku calls per session. Bound N (e.g. cap at the largest
   sessions; log what's dropped — no silent truncation). Re-use the ledger so re-runs no-op.

## Must-haves (lessons banked from v1's six gaps)

- **Test the chunker's OUTPUT THROUGH the real Haiku distiller** across several real
  sessions before any paid re-run — measuring chunk char/signal capture verifies the
  producer, NOT the consumer's behavior (the v1 escaped-bug).
- Honor the coupled distiller config (window/timeout/max_tokens) + the restart-on-deploy
  guard from the distiller-config-coupling follow-up — don't reintroduce a stale-serve or
  a too-small max_tokens.
- Paid re-run is gated on Joseph's DIRECT in-window consent.

## Out of scope

Re-introducing any signal-based reordering/selection — that path is closed
(escaped-bug). Whole-session coverage comes from chunking contiguous regions, not from
selecting + concatenating non-contiguous high-signal blocks.
