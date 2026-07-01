# KG-extension contract v1 — kg_scopes + schema-dangling + project-default

> Author: khimaira-master · 2026-06-28 · from griffin-0 field feedback (JEEVY-673 audit session).
> Two-repo build: **jeevy** (producer, griffin's domain, Joseph pushes) ships endpoints;
> **khimaira** (viewer, void-1/subagent domain) ships the daemon route + tools + render.
> Both sides build against THIS contract so they integrate on first try.

## Why (the friction griffin-0 hit)
Every `kg_*` call needs `scope="shop:N"`, but no tool tells an agent which shops exist →
agents raw-query the DB to cold-start, then route around the tools entirely. Plus two smaller
gaps: dangling-edge counts aren't surfaced per relationship-type, and `project="backend"` is
pure friction in a single-adapter deployment.

## A) Scope discovery — `kg_scopes` (#1, the make-or-break)

**jeevy** — new endpoint `GET /internal/kg/scopes` (no scope param; lists them):
```json
{ "data": { "scopes": [
  { "scope": "shop:10", "nodes": 1234, "edges": 5678, "label": "Acme Auto" },
  { "scope": "shop:3",  "nodes": 87,   "edges": 120,  "label": null }
] } }
```
- one entry per available scope (shop) that has KG data; sorted by `nodes` DESC (richest first).
- `nodes`/`edges` = active counts for that scope. `label` = optional human name (null if none).
- auth + envelope identical to the other `/internal/kg/*` routes.

**khimaira daemon** — new route `GET /api/graph/{project}/scopes` in `monitor/api/graph.py`:
proxy via the existing `_proxy_get(... _sub_url(graph_url, "scopes") ...)` pattern (copy
`get_graph_coverage` / `get_graph_health` verbatim — same shape, no scope arg).

**khimaira tool** — `kg_scopes(project: str = "")` in `server/monitor_tools.py` + register in
`server/mcp.py` (mirror `kg_health`). Renders: headline `N scopes · richest = shop:10
(1234 nodes)`, then a line per scope `shop:10 · 1234 nodes · 5678 edges · Acme Auto`. Empty →
`📭 no scopes with KG data for <project>`.

## B) Schema dangling flag — `kg_schema` enrichment (#5)

Per-triple `count` already ships. ADD a dangling count.

**jeevy** — `/internal/kg/schema`: add `"dangling": <int>` to each triple object = number of
edges of that `(fromType, linkType, toType)` whose `from` OR `to` endpoint resolves to a
MISSING node (the integrity gap the T4 witness work needed). 0 when clean.

**khimaira** — `kg_schema` render (`monitor_tools.py`): append `  ⚠ <n> dangling` to any triple
line where `dangling > 0`. No change when 0/absent (back-compat with adapters that don't send it).

## C) project default (#3) — khimaira-only, NO contract

In the `kg_*` tools: when `project` is empty/omitted, default to the SOLE registered KG adapter
if exactly one is registered; if MULTIPLE, return a helpful error listing them; if NONE, the
current 404 stands. Implement as one helper `_kg_default_project(project)` called at the top of
each tool. This kills the "always type backend" friction WITHOUT hard-coding "backend" (keeps
the surface code-agnostic — `project` stays a real param, just optional in the common case).

## D) read-only invariant (#4) — affirmed, do NOT build
No write/mutation tool. The KG is projected from relational per-tenant; the only legitimate
writers are the projector + human-applied DB ops. Read-only is the design, not a gap.

## Integration / done
- khimaira side can build + UNIT-test against mocked daemon JSON now (don't block on jeevy).
- jeevy side ships the two endpoints; Joseph pushes jeevy.
- End-to-end check once both land: `kg_scopes(project="backend")` → lists shops; `kg_schema(
  project="backend", scope="shop:10")` → triples show `⚠ N dangling` where present.
