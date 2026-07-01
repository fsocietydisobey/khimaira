# #38 Tier-2 — live contract-gate tollgate (STAGED, not applied)

> Author: khimaira-void-1 · 2026-06-28 · **deploy-gated follow-up** — do NOT apply
> outside a daemon-restart window. Patch: `ISSUE38-LIVE-TOLLGATE.patch` (git-applyable).

## What it is

Wires the khimaira-owned KG generic-contract gate INTO the live proxy
(`api/graph.get_graph`). Today the proxy returns the adapter payload verbatim; the
CI test `tests/test_kg_contract_gate.py` (shipped, commit `e711bd4`) asserts
conformance but nothing enforces it at runtime. This patch makes the boundary a real
tollgate (ai-engineering: validate at the boundary).

## Design — fail-SAFE (per master, default-toward-recoverable)

A debugging surface must prefer **partial data > no data**. So by default the gate:
- **drops** nonconforming nodes/edges (missing required field, wrong type, or a
  non-contract field — e.g. a leaked jeevy term `node_type`/`canonical_key`),
- serves the conforming remainder,
- annotates `data._contract = {ok:false, droppedNodes, droppedEdges, sampleViolations}`
  (bounded sample, **no silent truncation** — same discipline as `kg_edges_audit`),
- logs a `WARNING`.

Hard-fail (502 the whole payload) is **opt-in only**: `?strict=true` on the request,
or `KHIMAIRA_KG_CONTRACT_STRICT=1` in the daemon env. The loud hard-fail lives in CI
(the contract-gate test); the live path degrades gracefully.

The runtime field sets in `graph.py` are a cheap inline copy of the contract; a
drift-pin test (`test_runtime_field_constants_match_kgtypes_source`) ties them to the
kgTypes.ts source of truth so they can't silently diverge.

## Contents

- `packages/khimaira/src/khimaira/monitor/api/graph.py` — `_filter_to_contract` +
  helpers + `strict` query param + `_CONTRACT_STRICT` env flag, wired into `get_graph`.
  Non-mutating (builds a new payload; never mutates the adapter response in place).
- `packages/khimaira/tests/test_graph_api.py` — 5 tests: permissive drop+annotate,
  `?strict=true`→502, env-default strict, conforming-passthrough, kgTypes.ts drift-pin.

## Apply (inside a daemon-restart window only)

```bash
cd ~/dev/khimaira
git apply scratchpad/ISSUE38-LIVE-TOLLGATE.patch
uv run ruff check packages/khimaira/src/khimaira/monitor/api/graph.py \
                  packages/khimaira/tests/test_graph_api.py
uv run pytest packages/khimaira/tests/test_graph_api.py \
              packages/khimaira/tests/test_kg_contract_gate.py -q   # expect 45 passed
# then commit + restart the daemon to make the live gate active
```

Verified `git apply --check` clean against HEAD `8e49cac`/`e711bd4` (45 green when
applied). Bundle with the next daemon restart; it is purely additive (default
permissive → cannot blank an existing healthy graph; only annotates on drift).
