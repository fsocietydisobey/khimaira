# KG Visualizer → jeevy Migration — Scoping

**Date:** 2026-06-30 · **Author:** khimaira-1 (master) · **For:** Joseph
**Status:** scoping (no work started). Two questions: (1) move the KG visualizer into jeevy?
(2) build a separate jeevy-native MCP tool? Maps from 3 parallel code探索 agents + (pending) griffin's jeevy-side read.

---

## Bottom line

## 🔄 STATUS (Joseph, 2026-06-30): REOPENED — undecided, leaning toward BUILD the visualizer.

Earlier "don't build" was me over-converging on a concern Joseph was just voicing, not a decision.
Reopened. Current standing recommendation (khimaira-1, honest take): **BUILD the in-jeevy
visualizer** (post-launch, griffin-owned, copy-first); **SKIP the jeevy MCP** (no team demand,
net-new host). Decisive argument: a dev debugging their LOCAL jeevy state needs to see their OWN
local KG — the existing khimaira monitor either requires every dev to run khimaira locally, or (if
hosted) can only show shared-env KG, not local. The in-jeevy view shows local state natively.
Awaiting Joseph's actual decision. (Prior decision-log id=35a84847a166 "don't build" is now stale —
will supersede on real decision.)

---

**AUDIENCE RESOLVED (Joseph, 2026-06-30): the jeevy DEV TEAM wants it for mental models + debugging.**
Not Joseph-as-lone-operator (→ would stay in khimaira); not end-customers (→ a product feature).
This is **developer-facing debugging tooling** — and it tips the recommendation to **BUILD the
in-jeevy view.** The deciding logic:

- The jeevy developers **already run jeevy locally**; they should NOT have to stand up the entire
  khimaira platform (daemon + attach jeevy adapter + serve monitor-ui) just to inspect the KG while
  debugging jeevy. That infra friction is the whole argument for co-locating the tool with the thing
  it debugs.
- jeevy's **AI Debugger is ALREADY the flag-gated developer-debugging surface**
  (`NEXT_PUBLIC_PIPELINE_DEBUGGER`) — a KG view slots into its `shellRegistry`, riding the dev server
  + cookie auth those developers already have. Zero extra infra for them.
- This is **additive, not a move**: khimaira's generic monitor KG-Mapper stays (it's the
  platform-operator / multi-project / cross-project surface + future attached KGs). jeevy grows its
  own dev-facing view consuming `kg_debug.py`. Two surfaces, two audiences — by design.
- The cost is **renderer duplication** (~3k lines across 6 files in two repos). Decision below.
- **Separate MCP tool → NO (griffin + I converge, firmly).** jeevy has no MCP server at all today, so
  a jeevy-native KG MCP tool is net-new host infra that duplicates the generic `kg_*` tools and
  *fragments* the one tool set that sees every attached project. Build it only if a concrete
  *non-khimaira* consumer (jeevy prod agents in a no-khimaira deploy) or a *jeevy-specific* operation
  (financial-aware / write-side KG ops) appears.
- **The boundary that decides both:** **operator/dev inspection tooling is khimaira's domain**
  (generic, multi-project, the platform's perception layer); **jeevy owns the KG *data* + any
  *end-user-facing* product view of it.** The `kg_*` MCP tools + the generic KG-Mapper monitor are
  platform-generic → stay in khimaira. The `/internal/kg/*` endpoints are the jeevy-native data
  source → stay in jeevy (already do). Only a *jeevy-user-facing* viewer crosses into jeevy — and
  that's an additive product feature, not a relocation.

---

## Current architecture (verified)

```
Browser (khimaira monitor-ui, Vite/React18)
  └─ fetch /api/graph/{project}/...            ← relative, same-origin
       └─ khimaira daemon :8740  (FastAPI)
            graph.py proxy  → resolves attached.json kg_adapter {url, token_env, auth_header}
              └─ jeevy backend :8000  /api/v1/internal/kg/*   (FastAPI, X-Internal-Key auth)
                   └─ Supabase Postgres: kg_active_nodes / kg_active_edges / entity_observations
```

- **khimaira side has ZERO jeevy-specific code.** The whole KG surface (daemon proxy `graph.py`,
  the 10 `kg_*` MCP tools in `monitor_tools.py`) is driven entirely by one `attached.json` adapter
  block + an opaque-id `{nodes,edges}` contract gate. jeevy is the *only* registered adapter, but
  the design is genuinely generic/multi-project.
- **The daemon proxy exists on purpose:** keeps the browser from talking to jeevy directly
  (cross-origin) and keeps the internal secret in the daemon's env, never in the browser.
- **jeevy** = monorepo: `backend/` (FastAPI + Supabase) + `frontend/` (Next.js 16 / React 19 /
  webpack / Tailwind v4 / Redux Toolkit). Path: `~/work/jeevy_portal`.

---

## Q1 — Move the visualizer into jeevy

### Why it's a small lift (favorable findings)

- **`KgMapper.tsx` (1592 lines, the whole renderer) imports nothing from monitor-ui** — only npm
  libs + 2 `react-router` hooks. Self-contained palette/theme (`graphStyle.ts`, zero imports).
  Raw `fetch()`, **no Redux, no RTK-Query, no shared API client, no shell**.
- **Total outward coupling is tiny:** 4 generic shadcn `ui/` imports (Button/Badge/Card) + a 4-line
  `cn()` helper in the inspector panels + Tailwind CSS-variable tokens. All trivially vendored.
- **jeevy has the ideal mount point:** `frontend/src/features/ai-debugger/` — the AI Debugger with a
  **pluggable view registry** (`shell/shellRegistry.js`): each view is `{id, label, icon,
  HeaderActions, Content}`; "add a view, append to VIEWS, no other shell changes needed." Already
  hosts langgraph / qdrant / eval views. **This is the tracer-debugger Joseph meant.** A `kgView`
  drops in with zero shell surgery.
- **Wire contract is identical** — jeevy's `/internal/kg/graph` already emits the same
  `{data:{nodes,edges}}` shape the renderer consumes. No data-layer rework.

### The actual work

1. **Port 6 files** (`KgMapper`, `KgNodeInspector`, `KgEdgeInspector`, `kgTypes`, `graphStyle`,
   `CopyJsonButton`) into `frontend/src/features/ai-debugger/views/kg/`.
   - React 18 → 19: trivial (sigma/graphology are framework-agnostic).
   - **Vite → Next App Router:** sigma is WebGL/canvas → can't SSR. Needs `"use client"` + a dynamic
     import with `ssr:false`. Known, small pattern — the one real Next-specific wrinkle.
2. **Add net-new npm deps:** `sigma@3`, `graphology`, `graphology-layout-forceatlas2`,
   `@sigma/node-border`. Self-contained packages; jeevy has **zero** graph-viz deps today.
   (Dagre + RTK-Query are NOT part of this view despite being in monitor-ui's package.json.)
3. **Vendor / map the 4 shadcn imports** to jeevy's own ui components (jeevy is Tailwind v4). Small.
4. **Repoint the 4 fetch URLs** off the khimaira daemon proxy. **Critical choice — which backend route:**
   - The browser must **not** hold the `X-Internal-Key` (service-auth secret). So it can't call
     `/internal/kg/*` — and griffin confirms jeevy's frontend has **zero `/internal` references**;
     that surface is service-auth, LAN/operator-only, not browser-reachable.
   - **The user-facing route already exists (griffin):** `backend/api/v1/endpoints/kg_debug.py` →
     `GET /kg/debug/graph` + `GET /kg/debug/node/{id}`, **cookie / `wos_session` auth**, consumable
     via jeevy's existing `fastApiBaseQuery` **RTK** pattern. So the in-app view rides the logged-in
     session — two surfaces, two audiences: operator = `/internal/kg` (khimaira daemon proxy),
     user-app = `kg_debug.py`.
   - **Correctness constraint:** the financial-gate (`security_class` drops `unit_price`) + denylist
     redaction live in the backend. The in-app view MUST go through `kg_debug.py` (which should
     re-apply those gates) — never raw repos / Postgres-from-browser. Verify `kg_debug.py`'s current
     shape enforces them (griffin offered to dig); if it's thinner than `/internal/kg`, it may need
     the source-peek / edge / schema siblings + the gates added to reach parity with the renderer's 4 calls.
   - **Endpoint note (griffin correction):** there is **no `/internal/kg/search`** — `kg_search`
     filters the full `/graph` client-side. The internal surface is `graph, node/{id},
     node/{id}/source, edge/{id}, schema, scopes, health, coverage, edges-audit`. `kg_debug.py`
     currently exposes only `graph` + `node/{id}` — so an in-app view wanting the inspector's
     source-peek / edge panels needs those siblings added user-side.
5. **Register the view** in `shellRegistry` (append to VIEWS).
6. **Collapse the `{project}` dimension** (jeevy is single-project) — drop `:name` from the path;
   **keep the `scope` input** (`shop:N`) — that's the meaningful in-view selector.

### Effort: ~1–2 focused days. Friction = Next SSR-wrapping + net-new deps + a gated user-auth read route.

### The "do both copies survive?" decision (for Joseph)

If the visualizer moves into jeevy, does khimaira's monitor-ui KG view get **retired** or **kept**?
- **Keep both:** khimaira monitor = cross-project/platform inspector (for future attached projects);
  jeevy = embedded debugging. Cost: two copies to maintain.
- **Move + retire khimaira's:** simpler, one copy. Loses the generic multi-project surface — which
  is *unused today* (jeevy is the only attached KG).
- Recommendation leans **move + retire khimaira's monitor KG view** unless a 2nd project's KG is on
  the roadmap. The `kg_*` MCP tools + adapter stay regardless (that's the platform layer).

---

## Q2 — Separate jeevy-native MCP tool

### Facts

- **jeevy has NO MCP server today** — it's only an MCP *consumer* (`.mcp.json` registers external
  git/filesystem/postgres servers). No FastMCP, no `@mcp.tool`, no registration surface to extend.
  A jeevy-native KG MCP tool = **net-new MCP host** (add FastMCP/SDK, tool registration, serving,
  lifecycle).
- The backend logic to back it **already exists** (`kg_audit_service` + repos + the `internal.py`
  `{nodes,edges}` shaping) — so it *could* talk to the repos directly, skipping the khimaira hop.

### Recommendation: don't build it (yet)

- khimaira's 10 `kg_*` tools already give **any** agent (including jeevy-roster agents) full KG
  query access — generic, contract-gated, project-parameterized. The daemon hop is local
  (`127.0.0.1`) and cheap.
- A jeevy-native tool's only edge: shop-scoped + jeevy-specific + no khimaira dependency. Marginal
  today, and it **fragments** the generic platform surface (the whole point of the adapter is one
  tool set for all attached KGs).
- **Build it only when:** (a) jeevy needs KG tools for **non-khimaira consumers** — e.g. jeevy
  production agents calling MCP in a deploy where khimaira isn't running; or (b) **jeevy-specific
  operations** outside the generic read contract — financial-aware queries, or KG **write/correction**
  ops (the generic contract is read-only). If neither is on the horizon, skip.

---

## Open questions for Joseph (in priority order)

1. **AUDIENCE — the one that decides everything: who is the KG viewer for?**
   - *Operator/dev (you, debugging the platform)* → leave it in khimaira. Done. No work.
   - *jeevy end-users (a shop seeing its own graph in-product)* → green-light a net-new jeevy
     product view (reuse renderer + user-auth route). This is the only case that justifies in-jeevy
     work, and it's *additive*, not a move.
2. **If end-user-facing: is it launch-blocking or post-launch?** (Lean: post-launch — operator
   monitor serves the dev need today; an end-user KG view is a product feature, not a launch gate.)
3. **MCP:** any non-khimaira consumer or write-side KG op on the roadmap that would justify a
   jeevy-native MCP host? (If not → keep khimaira's `kg_*`, don't fork. griffin + I both lean no.)
4. **Who owns the build** — if it proceeds, it's mostly **jeevy-side** (griffin): the in-jeevy
   frontend view + a gated user-auth read route. khimaira side changes nothing (the generic monitor
   + `kg_*` tools + adapter all stay).
