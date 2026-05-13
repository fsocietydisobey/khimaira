# React DevTools Replacement — Vue-DevTools-quality UI for React

> **Status:** Spec'd, not started.
> Build when: someone has a multi-day window. This is a product, not a feature.

## Problem

Existing React debugging tooling is fragmented and shallow:

- **React DevTools (browser extension)** — slow on big trees, no persistent
  history, panel flickers on every render, props/hooks inspector is the bare
  minimum. No cross-tab navigation between component tree ↔ Redux state ↔
  router ↔ network.
- **Redux DevTools** — separate panel; manually correlate with component tree.
- **Browser DevTools** — generic; doesn't understand React component identity.
- **Specter (khimaira's CDP debugger)** — has `check_react`, `get_component_tree`,
  `get_component_at`, `get_elements_grouped_by_component`, `get_redux_state`,
  `get_redux_actions`. **MCP-only — no human-facing UI.**

Vue DevTools is the gold standard: component tree on left, live props/data/
computed on right, dedicated tabs for Pinia/router/performance/timeline, click
through everywhere. We want that for React, integrated into khimaira-monitor.

## Architecture (data flow)

```
[ React app in browser ]
        ↓ CDP
[ Specter (separate repo, /home/_3ntropy/dev/specter) ]
        ↓ MCP / HTTP
[ khimaira-monitor daemon ]
        ↓ WebSocket / SSE
[ khimaira-monitor frontend (React) ]
        ↓ Redux Toolkit Query
[ React DevTools Panel UI ]
```

**Key principle:** specter is the data extraction layer (CDP + injected
fiber-walker), khimaira-monitor is the visualization layer. Don't duplicate.

## Specter changes needed

Specter today returns **snapshots**. For Vue-DevTools-quality UX we need
**live updates** (props change, render happens, the panel updates).

1. **`get_component_props(selector_or_path)`** — for a specific component,
   return its current props (serialized — handle React elements / functions /
   refs as `[Function]` / `[ReactElement]` / `[Ref]` placeholders).
2. **`get_component_hooks(selector_or_path)`** — return useState / useReducer /
   useContext / useRef values for a component. Walk the fiber's hook list.
3. **`get_component_render_count(selector_or_path)`** — track via injected
   render-counter (perf-style measurement).
4. **`subscribe_react_updates()`** — long-poll or SSE: fires whenever the
   tracked component (or any of its descendants) re-renders. Powers
   "live updates" instead of frontend polling at 500ms.
5. **`get_component_owner(selector_or_path)`** — walk up the fiber tree,
   return parent chain. Lets the UI render the "selected component is at:
   App > Layout > Page > Sidebar > NavItem".

These are extensions to specter, NOT khimaira. File specter issues separately
or spec a parallel `react-devtools-extensions/` task in specter's repo.

## Khimaira-monitor UI

New top-level route: `/:project/react`. Layout:

```
┌──────────────────────────────────────────────────────────────────┐
│ Chrome tab picker (which Chromium tab to introspect)             │
├──────────────────┬───────────────────────────────────────────────┤
│ Component tree   │ Selected component panel:                     │
│                  │   ┌──────────────────────────────────────┐    │
│ • App            │   │ <NavItem>  · App > Layout > NavItem  │    │
│  • Layout        │   ├──────────────────────────────────────┤    │
│   • Header       │   │ Props                                │    │
│   • Sidebar      │   │   onClick: [Function: handleClick]   │    │
│    • NavItem ✦   │   │   active: true                       │    │
│    • NavItem     │   │   label: "Cost"                      │    │
│   • Main         │   ├──────────────────────────────────────┤    │
│    • Page        │   │ Hooks                                │    │
│   • Footer       │   │   useState[0]: { count: 3 }          │    │
│                  │   │   useState[1]: false                 │    │
│ Render count:    │   │   useEffect: [activated 12 times]    │    │
│   App: 4         │   ├──────────────────────────────────────┤    │
│   NavItem: 47 ⚠  │   │ Context                              │    │
│                  │   │   ThemeContext: "dark"               │    │
│                  │   │   AuthContext: { userId: "u_123" }   │    │
│                  │   ├──────────────────────────────────────┤    │
│                  │   │ DOM elements (3)                     │    │
│                  │   │   <li class="nav-item active">…      │    │
│                  │   └──────────────────────────────────────┘    │
└──────────────────┴───────────────────────────────────────────────┘

Tabs along the top:
  [ Components ] [ Redux ] [ Router ] [ Performance ] [ Timeline ]
```

### Tabs (post-MVP)

- **Components** — the layout above. MVP scope.
- **Redux** — wraps `get_redux_state` + `get_redux_actions`. Action timeline
  with diff-on-click. Filter by action type.
- **Router** — current route, history stack, params, query.
- **Performance** — render counts per component. Flag re-render hotspots.
  "X re-rendered 47 times in 2s" with timeline.
- **Timeline** — unified event stream: route changes, redux dispatches, network
  requests, console errors. Same time axis as the trace waterfall view, so
  you can correlate frontend + backend in one place.

## Why this beats React DevTools

| feature | React DevTools | this |
|---|---|---|
| persistent history | ✗ flickers off on tab close | ✓ khimaira-monitor stays open |
| cross-tab nav | ✗ separate panels | ✓ click component → see its redux reads |
| backend correlation | ✗ none | ✓ same UI as langgraph trace waterfall |
| filter / search | weak | first-class |
| custom inspectors | extension hooks | plugin via specter eval_js |
| works during recording | flaky | independent process, can't be killed by React |

## MVP scope (phase 1)

- New route `/:project/react`
- Tab picker via specter's `list_tabs`
- Component tree from `get_component_tree`
- Click component → fetch props/hooks via NEW specter endpoints (#1, #2 above)
- DOM elements for selected component via existing `get_elements_grouped_by_component`
- Polling at 500ms (good enough for MVP; replace with SSE in phase 2)

**Effort:** ~2 days
- Day 1: specter extensions (#1, #2 above, plus serialization rules) + tests
- Day 2: khimaira-monitor UI (tree + selected-component panel)

Phase 2: Redux tab + render counts + SSE subscription. ~3 more days.
Phase 3: Performance + Timeline tabs + cross-correlation with backend traces. ~5 days.

**Total to "better than React DevTools": ~10 dev-days realistic.**

## Risks / open questions

1. **Fiber tree access in production builds.** React's fiber internals are
   private API. Specter's existing `get_component_tree` already navigates
   fibers via `__REACT_DEVTOOLS_GLOBAL_HOOK__` — same approach React DevTools
   uses. Verify it still works in React 19+ before committing.

2. **Hook serialization.** Some hooks hold non-serializable values (Refs,
   Promises, complex closures). Need clear placeholder strategy + recursion
   depth limit to avoid panel freezing on a 100-level deep object.

3. **Performance overhead.** Subscribing to every re-render in a busy app
   could be expensive. Throttle / batch updates. Default to "update every
   500ms or on selection change" rather than every render.

4. **Cross-origin iframes.** A React app in an iframe (or with embedded
   apps) needs separate inspection. Defer to phase 2; warn in MVP.

5. **TypeScript-vs-JS apps.** Component names from displayName / function
   names work for both, but TS-only apps may have `function Component()`
   while JS uses arrows. Specter's existing tree handles this; verify.

6. **State management lock-in.** Redux tab is straightforward (specter
   already has primitives). Zustand / Jotai / MobX / Recoil each need
   their own tap. Start with Redux only; design tab interface so other
   stores can plug in.

## Coordination

- Specter changes in /home/_3ntropy/dev/specter (separate repo). PRs there
  before khimaira UI work starts.
- Khimaira-monitor UI in this repo (apps/monitor-ui).
- New API endpoint in khimaira-monitor daemon to proxy specter (apps don't
  hit specter directly — keeps the security/isolation surface clean).

## Notes

- Naming: "react devtools" is fine internally but if/when this ships,
  "khimaira react inspector" or just "components" view avoids confusion
  with the official React DevTools extension.
- This is the kind of feature that justifies a khimaira "v1.0" launch —
  a visible, demoable product surface. Good story for the README + hero
  screenshot.
- Once this exists, the trace waterfall + cost dashboard + react
  inspector together form a cohesive "full-stack observability" story
  that no other tool offers.
