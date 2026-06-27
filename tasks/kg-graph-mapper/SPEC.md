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

## PIVOT — per-deliverable → per-shop whole-graph explorer (2026-06-26)

Joseph redirected: he wants a **Neo4j/Obsidian-style whole-graph explorer**, not a
per-deliverable subgraph. muther (jeevy intake) gave an audit-grade read off the LIVE
jeevy DB (question id c6a54a2afda5). The pivot:

**Scope = ONE SHOP, never global.** Nodes are owned by `shop_id` (there is NO
`deliverable_id` on a node — which is why the per-deliverable endpoint had to derive
nodes from edges). "Whole graph" = one shop's graph. Global-across-shops is a
cross-tenant leak AND a scale problem. The shop comes free from the PM session via
`kg_read_shop_id` — no scope param needed.

**Live scale (5 shops, 2026-06-26):** 5,410 nodes / 8,972 edges total, but skewed —
shop 10 = **4,774 nodes (88%)**; shops 1–4 are ≤311. Edge mix is hub-dense: part-of
3090, has-type 1568, for-part 1352, created-by 982 (part-of + has-type alone = 52%).

**Rendering constraint — level-of-detail is MANDATORY for shop 10.** Hub super-nodes
make hairballs: the `shop` node (everything `owns`-links back to it) and `part_type`
(32 nodes absorbing 1,568 `has-type` edges, ~49 inbound each). v1 MUST: hide the shop
hub, collapse `part_type` by default, cluster bom-lines under their `job` (via
`contains`/`for-part`), expand-on-click. Small shops (≤~350 nodes) render trivially.

**Known limitation in the per-deliverable endpoint we shipped:** `contains` edges are
NOT `deliverable_id`-stamped, so a deliverable-scoped edge filter silently DROPS them.
Another reason per-shop (node-set-derived) is the correct scope.

**No Neo4j — Postgres-native.** The jeevy KG retrieval program designs Neo4j OUT:
- **JEEVY-661 `kg_traverse`** (recursive-CTE, shop-scoped, bounded neighborhood walk) =
  the right backend primitive for the Obsidian-style "expand node → N-hop neighborhood."
- **JEEVY-663** (GraphRAG community-summary precompute) = the v2 scale path; its
  communities = the natural collapse groups. **DEFERRED (Backlog, density-gated) — v1
  must NOT depend on it.** It's a chat-*retrieval* lane, not a viz primitive, but the
  community partition feeds the LOD collapse when the graph outgrows client-side render.

**Build plan:**
- **v1 (renders every shop incl. shop 10):** per-shop graph endpoint (shop from session,
  no param) + React Flow/dagre client with auto-collapse (hide shop hub, collapse
  part_type, cluster bom-lines under job). Pure client-side LOD over the per-shop dump.
- **v2 (explore-at-scale):** lean on JEEVY-661 (`kg_traverse`) for server-side
  neighborhood expansion + JEEVY-663 community summaries for collapse groups, instead of
  shipping raw kg_active_nodes/edges to the client.

**Carries over from the 2026-06-26 build:** the React Flow + dagre canvas, NodeInspector
clone, `list_active_by_ids` repo method. **Changes:** graph endpoint re-scopes
`deliverable_id` → session-shop; frontend gains collapse/expand LOD logic.

## ARCHITECTURE CORRECTION — code-agnostic, like the LangGraph monitor (2026-06-27)

**The 2026-06-26 build drifted into JEEVY-COUPLING and must be re-homed.** The backend
agent put `GET /kg/debug/graph` INSIDE jeevy's repo and pointed the khimaira UI at jeevy's
API — the opposite of how the LangGraph monitor works, and the root cause of the
cross-service wiring wall. The KG mapper must be a **generic khimaira tool fed by a
per-project adapter**, exactly like FlowCanvas observes any attached LangGraph project.

**Three layers (mirror the LangGraph monitor):**
1. **Generic contract (khimaira-owned, framework-neutral):**
   `{nodes:[{id, type, label, badge?}], edges:[{from, to, type, weight?}]}`. No jeevy terms.
2. **khimaira daemon — generic graph endpoint** `GET /api/graph/<project>?scope=…`: the
   monitor-ui calls THIS (same-origin, via the existing `/api` vite proxy → daemon). The
   daemon proxies to the attached project's configured **KG-adapter URL + token** (held in
   the `khimaira attach` config), returns the generic contract. This is what makes the UI
   agnostic AND solves the auth/cross-origin problem (daemon carries the project token).
3. **Per-project adapter (jeevy-owned):** maps `kg_active_nodes`/`kg_active_edges` → the
   generic contract, per-SHOP scope (shop from session), financial-gated. This is the ONLY
   layer that knows jeevy's schema. The 2026-06-26 read logic re-homes HERE.

**Adding any future project = register its adapter URL + token in attach config.** UI,
daemon, and contract never change. ("Code-agnostic renderer + per-source adapter" — the KG
can't be auto-discovered like LangGraph checkpoints, so a project opts in via the adapter,
but the generic layers stay generic.)

**What re-homes vs changes:** frontend KgMapper renders the generic contract already — just
repoint it from jeevy's API → the daemon's `/api/graph/<project>`. The jeevy read logic
(views, financial gate, per-shop scope, LOD groupings) becomes the jeevy ADAPTER, not a
UI-facing endpoint. The per-shop + LOD pivot (above) layers on top of the generic contract.

**Pointers for the build:** how FlowCanvas gets data from the daemon (`apps/monitor-ui`
`/api/threads/...` + the daemon route module under `packages/khimaira/src/khimaira/monitor/`);
the `khimaira attach` config + observer template (`.../attach/observer_template/`); the vite
`/api` proxy → daemon (8740). The daemon→adapter auth is the load-bearing detail to confirm.

## Cross-references
- JEEVY-661 (`kg_traverse` recursive-CTE primitive) · JEEVY-663 (GraphRAG global-search,
  deferred) · JEEVY-660 (anchor nodes) · JEEVY-667 (unified KG chat agent).
- Reference: `apps/monitor-ui/src/components/project/FlowCanvas.tsx` (LangGraph mapper).
- jeevy source: `kg_debug.py`, `projection_spec.py`, `schema_nodes.py`, `fact_types.py`,
  `field_map.py`, `entity_observations_repository.py`, `kg_edges_repository.py`, `resolver.py`.
- Data model verified live by muther (jeevy intake), question 04eb14df28af, 2026-06-23.
