# khimaira KG-structure scanner + React Flow view (jeevy knowledge graph)

> Status: SPEC / ready-to-scope · 2026-06-19 · Joseph greenlit
> Pattern: same as the LangGraph run monitor + Scarlet — **khimaira SCANS the target
> project's code, extracts structure, renders it.** jeevy is the scanned target; no
> viewer is built into the jeevy app.

## Goal

A khimaira-monitor FE view that renders the **structure (ontology) of jeevy's
knowledge graph** — the entity types + the relations between them — the way the
monitor already renders a LangGraph topology. Read-only, structure-not-instance for v1.

## What the structure IS (from muther, 2026-06-19, jeevy commit `bdc0e56d`)

jeevy's KG is **Postgres-native** (no Neo4j / no graph lib) — `kg_nodes` / `kg_edges`
/ `entity_observations` tables + recursive CTEs. Crucially, **the ontology is
declarative Python in 3 files** under `backend/core/services/kg/`:

| File | Yields |
|---|---|
| `projection_spec.py` | `PROJECTION_SPECS: dict[table → ProjectionSpec]` — each entry declares `node_kind` (entity type), `field_facts: tuple[FieldFact]` (column→fact_type, `projection ∈ {intrinsic\|contextual\|edge}`), `edge_derivations`, `identity_extractor`. **THE node types + fact vocabulary.** |
| `fact_types.py` | `ALL_FACT_TYPES` / `FT.*` — the closed per-node fact registry (PART_NUMBER, DESCRIPTION, QTY, NODE_OWNER, …). **What facts a node can carry.** |
| `surface_handlers.py` | the `_*_edges` derivers + `EdgeSpec(from_kind, link_type, to_kind)`. **The relation/edge types.** |

So the ontology = `{node_kinds, facts-per-kind, edges: from_kind —link_type→ to_kind}`,
all derivable from a **code read** — no live-DB query needed for the structure view.
(Instance-data browsing — the actual populated graph — is a SEPARATE later feature via
SQL on `kg_nodes`/`kg_edges`; out of scope for v1.)

## Architecture — 3 components

### 1. The scanner (the load-bearing decision: how khimaira reads jeevy's structure)
Two viable approaches; **recommend (b)**:
- **(a) Static AST scan** — parse the 3 `.py` files with `ast` (no import), extract the
  declarative literals. Consistent with the LangGraph-monitor/Scarlet static-scan
  pattern + zero coupling, BUT parsing `ProjectionSpec`/`EdgeSpec` dataclass-instance
  literals + tuples out of AST is fiddly and brittle to refactors.
- **(b) Subprocess introspection in jeevy's venv (RECOMMENDED)** — a small script run via
  the **jeevy_portal venv** (`subprocess`, like `refresh_oracle.sh`'s active-session
  distill runs in the khimaira venv) that `import`s `PROJECTION_SPECS` / `ALL_FACT_TYPES`
  / the `EdgeSpec`s and emits the ontology as JSON to stdout. khimaira ingests the JSON.
  Gets the REAL objects (no literal-parsing), and keeps jeevy's deps out of the khimaira
  monitor process (decoupled). Cross-repo path: configurable jeevy root + venv.

### 2. The ontology API (khimaira monitor)
A monitor endpoint `GET /api/kg/{project}/ontology` → runs the scanner (cached; refresh
on demand) → returns graph JSON:
```
{ nodes: [ { id: node_kind, label, facts: [fact_type...], surfaces: [tables...] } ],
  edges: [ { source: from_kind, target: to_kind, link_type } ] }
```
Project-scoped (jeevy for now; the scanner is jeevy-specific because the ontology shape
is jeevy-specific — keep it a `kg/jeevy_ontology_scanner.py` module, not pretend-generic).

### 3. The FE view (monitor-ui)
A new view/tab rendering the ontology with **React Flow (`@xyflow/react` — already in
`apps/monitor-ui`)**: node-kinds as nodes (click → show their facts), link-types as
labeled edges. Scale is small (it's the SCHEMA — dozens of kinds + link-types, not the
instance count), so no virtualization needed; a dagre/elk auto-layout is enough. Reuse
the LangGraph-topology component patterns.

## Open questions to resolve at build time (verify against the real jeevy files)
1. Confirm the exact shapes of `ProjectionSpec`, `FieldFact`, `EdgeSpec` (field names) by
   reading the 3 files — the table above is muther's summary, audit it against source.
2. jeevy venv + root path for the subprocess scanner (config). Does the introspection
   import pull heavy jeevy deps (DB, etc.)? If import side-effects are costly/unsafe,
   fall back to (a) static AST for just those declarative modules.
3. Does jeevy already expose a schema-introspection endpoint (muther's Q5)? If so, prefer
   calling it over re-deriving — check the full muther answer.

## Ownership / sequencing
INDEPENDENT of the SLM roadmap. Options: a `livyatan` task after wake-by-session-id +
the truncation fix, OR a fresh focused khimaira effort. Not urgent. Build order:
scanner (verify shapes → introspect → JSON) → ontology endpoint → React Flow view.
