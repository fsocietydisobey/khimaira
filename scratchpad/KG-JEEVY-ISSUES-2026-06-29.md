# Jeevy-side KG issues — found during morning end-to-end verify (2026-06-29)

> From khimaira-master, per Joseph. khimaira side is LIVE + correct (daemon restarted,
> `/api/graph/{project}/scopes` route serving, kg_scopes tool shape verified). These two
> are **producer-side (jeevy)** — your roster owns them. Joseph pushes jeevy.

## Issue 1 — `kg_scopes` returns EMPTY despite data (BUG, new endpoint)

**Evidence (verified direct on the live jeevy server):**
- `GET http://127.0.0.1:8000/api/v1/internal/kg/scopes` → `{"data":{"scopes":[]}}` (HTTP 200)
- but `GET /api/v1/internal/kg/health?scope=shop:10` → HTTP 200, and shop:10 has **1384
  `kg_active_nodes`** (confirmed via the graph endpoint). So the endpoint is deployed +
  reachable, but its enumeration returns nothing.

**Likely cause (from reading `kg_audit_service.kg_scopes` in 9e61465e):** a shop-id keying
mismatch. It does `shops.select("id, name")` → then `_count_exact("kg_active_nodes",
shop_id=shops.id)`, filtering `node_count == 0`. If `shops.id` ≠ the `shop_id` value stored
on `kg_active_nodes` (e.g. `shops.id` is a UUID or a different int than the `10` used as the
KG `shop_id`/scope), EVERY shop counts 0 → all filtered → `[]`. The KG shop node carries
label `"shop:10"` with a UUID node id, while the scope is `shop:10` (int 10) — so verify the
column you count by (`kg_active_nodes.shop_id`) is the SAME key space as `shops.id`. A quick
check: `select shop_id, count(*) from kg_active_nodes group by 1` vs the `shops.id` values.

## Issue 2 — placeholder node labels (projector gap, Joseph flagged)

Node labels for several types are the entity-KEY, not a human name:
- `user → "user:50" / "user:173"`, `shop → "shop:10"`, `personnel → "personnel:22"`
- BUT `part → "2603-16-16-16-SS"` ✓ and `organization → "Kyle Franklin" / "Envases"` ✓ resolve fine.

So the projector resolves real labels for SOME entity types but falls back to `type:id` for
user / shop / personnel. Joseph wants human-readable labels there (the user's name, the shop
name, the personnel name). This is a projector label-resolution gap — and note: **a
`kg_reproject` will NOT fix it** (re-running the same projector logic reproduces the same
placeholder labels); the label-assignment code itself needs the human-name lookup for those
types.

## Not in scope for you (noted for completeness)
- Daemon `/api/graph/backend/health` (no scope) → jeevy returns **422** (health seems to
  require `scope`). That's a khimaira/daemon-side call-shape thing (or jeevy could default
  scope), minor + pre-existing — I'll look at it separately, not yours.
