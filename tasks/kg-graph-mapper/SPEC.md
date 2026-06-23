# KG graph mapper — interactive node-edge viewer for the jeevy knowledge graph

> Status: SPEC / scoped · 2026-06-23 · filed by khimaira-0 (master)
> The "build it like the langgraph mapper" agreement, finally captured. Was a verbal
> agreement, never filed/started until now. Data model is audit-grade (muther queried
> the live jeevy local DB 2026-06-23, question id 04eb14df28af).

## Goal

A new khimaira **monitor-ui** view that renders the jeevy knowledge graph as an
interactive node-edge graph — pan/zoom/draggable nodes, mini-map — modeled on the
EXISTING LangGraph mapper. The KG is jeevy's; the mapper is a khimaira tool (like the
LangGraph FlowCanvas visualizes LangGraph runs).

## Reference implementation (clone this)

`apps/monitor-ui/src/components/project/FlowCanvas.tsx` — the LangGraph mapper.
**React Flow** canvas + `@dagrejs/dagre` layout. Renders graphs as visual clusters:
pan, zoom, draggable nodes, mini-map, active-node pulse, animated edges, namespaced
node ids, cross-graph edges as dashed lines. Siblings: NodeInspector, ActiveNodeCard,
ReplayController, RunStepsCard. The KG mapper is a new sibling view fed by KG data
instead of LangGraph topology — same React-Flow + dagre stack.

## KG data model (audit-grade, from muther — live jeevy DB)

**Source tables = the live read views** (raw capture flows `kg_outbox` → projector →
these). A read-only mapper reads `kg_active_nodes` + `kg_active_edges`.

### Nodes — `kg_active_nodes`
`id` (uuid PK) · `shop_id` (tenant) · **`node_type`** (discriminator) · **`canonical_key`**
(stable identity) · `display_name` (the RENDER LABEL) · `attributes` (jsonb) ·
`observation_count` · `create_safety` · `created_from_source_id` · `created_at`.

13 live node_types (with live counts): task 398 · bom-line 315 · part 284 · job 86 ·
workstream 56 · part_type 32 · organization 7 · document 4 · user 3 · shop 2 · vendor 2 ·
document_type 1 · procurement 1. Authoritative VALID set: `core/services/kg/projection_spec.py`
+ `schema_nodes.py`.

`canonical_key` is a per-type identity grammar: `bom-line:{part_uuid}:{assembly_id}` ·
`part`=part_number verbatim (`PL-4X8-3-16`) · `part_type`=`PLATE` · `job:{uuid}` ·
`task:{int}` · `workstream:{int}` · `document`=doc key · `organization`=name · etc.

⚠️ **CRITICAL: `attributes` is `{}` (empty) for EVERY node.** The KG is an
entity+provenance graph, NOT a quantitative store. Real facts (qty, OD/length/dims,
description, price) do NOT live on the node — node badges/detail panels must resolve
facts from the observation table (below), keyed on the node. Do NOT render from
`attributes`.

### Edges — `kg_active_edges`
`id` · `shop_id` · **`from_node`** (uuid → nodes.id) · **`to_node`** · **`link_type`**
(edge kind) · `link_source` · `match_method` · `confidence` (numeric) · `status` ·
`version` · `deliverable_id` (uuid — the SCOPE key) · `source_id` · `page` · `bbox` (jsonb) ·
`origin_type`/`origin_id` · `observed_at` · `created_at`.

12 live link_types: created-by 464 · belongs-to 368 · part-of 309 · has-type 174 ·
for-part 32 · subtask-of 30 · quotes 17 · owns 17 · appears-on 16 · supplies 11 ·
depends-on 5 · has-document-type 1. Edges have NO payload — `link_type` + provenance
(confidence/match_method/bbox/page) only.

### Observations (node facts) — `entity_observations`
Repo: `backend/core/services/database/repositories/entity_observations_repository.py`.
Facts attach to a node (canonical_id + fact_type + value). Fact taxonomy +
intrinsic-vs-financial split: `core/services/kg/fact_types.py`. `projection_spec.py:16`:
intrinsic facts → entity_observations (node-projects).

## Read API + gating (from jeevy source)

- **Existing read endpoint to build on:** `backend/api/v1/endpoints/kg_debug.py`
  (+ `backend/schemas/kg_debug.py`, `core/services/kg/resolver.py`). Check what it returns
  for a deliverable — likely the node+edge set the mapper fetches. Scope is per-deliverable
  (`kg_active_edges.deliverable_id`); confirm node scoping.
- **Field mapping for display:** `core/services/kg/field_map.py` (`get_mapped_fields(surface)`).
- **FINANCIAL GATING (mapper MUST respect):** `core/services/kg/fact_types.py` —
  unit_price + vendor/financial facts are DORMANT unless `shop.security_class='financial'`
  (`surface_handlers.py:57` `security_class_required`). The mapper must NOT render
  financial observations for a standard-security shop. (Matches the §4/§5 chat discussion
  about the financial gate firing at both capture-drain and ingest paths.)

## Build sketch (next session)

1. Backend: confirm/extend `kg_debug.py` to return `{nodes:[{id,node_type,canonical_key,
   display_name,observation_count}], edges:[{from,to,link_type,confidence}]}` for a
   deliverable (or project), with financial observations gated by security_class.
2. Frontend: new monitor-ui view cloning FlowCanvas — dagre-layout the node+edge set,
   color/shape by node_type, label by display_name, edge style by link_type, node detail
   panel (NodeInspector clone) resolving observations on click. Mini-map + pan/zoom free
   from React Flow.
3. Scope selector: pick a deliverable_id (the natural KG scope). Render-perf: a typical
   deliverable is ~hundreds of nodes/edges (live totals above) — dagre handles that fine.

## Cross-references
- Reference: `apps/monitor-ui/src/components/project/FlowCanvas.tsx` (LangGraph mapper).
- jeevy source: `kg_debug.py`, `projection_spec.py`, `schema_nodes.py`, `fact_types.py`,
  `field_map.py`, `entity_observations_repository.py`, `kg_edges_repository.py`, `resolver.py`.
- Data model verified live by muther (jeevy intake), question 04eb14df28af, 2026-06-23.
