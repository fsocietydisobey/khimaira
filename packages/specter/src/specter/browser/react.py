"""React component tree inspection via fiber internals.

Injects helper scripts into the page that walk React's internal fiber
tree (available in dev mode via __REACT_DEVTOOLS_GLOBAL_HOOK__) to
extract component names, props, state, hooks, source locations, and
parent/child hierarchy.

This gives Claude the same information the React DevTools extension
shows — without requiring the extension to be installed.

IMPORTANT: Only works when React is running in development mode.
Production builds strip the debug hooks.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from specter.browser.connection import CDPConnection

logger = logging.getLogger(__name__)

# JavaScript helper that walks the React fiber tree.
# Injected into the page via Runtime.evaluate.
# Returns a JSON-serializable tree of component info.
FIBER_WALK_SCRIPT = """
(() => {
  const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
  if (!hook) return JSON.stringify({ error: "React DevTools hook not found. Is React running in development mode?" });

  const renderers = hook.renderers;
  if (!renderers || renderers.size === 0) return JSON.stringify({ error: "No React renderers found." });

  // Get the first renderer's fiber roots
  const rendererId = renderers.keys().next().value;
  const fiberRoots = hook.getFiberRoots(rendererId);
  if (!fiberRoots || fiberRoots.size === 0) return JSON.stringify({ error: "No fiber roots found." });

  const root = fiberRoots.values().next().value;
  const rootFiber = root.current;
  if (!rootFiber) return JSON.stringify({ error: "No current fiber on root." });

  const MAX_DEPTH = %MAX_DEPTH%;
  const MAX_CHILDREN = %MAX_CHILDREN%;

  function getComponentName(fiber) {
    if (!fiber.type) return null;
    if (typeof fiber.type === 'string') return null; // DOM elements like 'div'
    return fiber.type.displayName || fiber.type.name || null;
  }

  function getSource(fiber) {
    const src = fiber._debugSource;
    if (!src) return null;
    return {
      fileName: src.fileName || null,
      lineNumber: src.lineNumber || null,
      columnNumber: src.columnNumber || null,
    };
  }

  function serializeValue(val, depth) {
    if (depth > 3) return '[nested]';
    if (val === null || val === undefined) return val;
    if (typeof val === 'function') return '[function]';
    if (typeof val === 'symbol') return val.toString();
    if (val instanceof Element) return '[DOMElement]';
    if (Array.isArray(val)) {
      if (val.length > 10) return '[Array(' + val.length + ')]';
      return val.map(v => serializeValue(v, depth + 1));
    }
    if (typeof val === 'object') {
      // React elements
      if (val.$$typeof) return '[ReactElement: ' + (val.type?.displayName || val.type?.name || val.type || 'unknown') + ']';
      const keys = Object.keys(val);
      if (keys.length > 20) return '[Object(' + keys.length + ' keys)]';
      const out = {};
      for (const k of keys) {
        out[k] = serializeValue(val[k], depth + 1);
      }
      return out;
    }
    return val;
  }

  function getProps(fiber) {
    if (!fiber.memoizedProps) return null;
    return serializeValue(fiber.memoizedProps, 0);
  }

  function getHooks(fiber) {
    const hooks = [];
    let hookNode = fiber.memoizedState;
    let index = 0;
    while (hookNode && index < 20) {
      const hook = { index };

      // Try to identify hook type from the queue
      const queue = hookNode.queue;
      if (queue) {
        if (queue.lastRenderedReducer) {
          const reducerName = queue.lastRenderedReducer.name;
          if (reducerName === 'basicStateReducer') {
            hook.type = 'useState';
            hook.value = serializeValue(hookNode.memoizedState, 0);
          } else {
            hook.type = 'useReducer';
            hook.value = serializeValue(hookNode.memoizedState, 0);
          }
        }
      } else if (hookNode.memoizedState && hookNode.memoizedState.destroy !== undefined) {
        hook.type = 'useEffect';
        const deps = hookNode.memoizedState.deps;
        hook.deps = deps ? serializeValue(deps, 0) : null;
      } else if (hookNode.memoizedState && typeof hookNode.memoizedState === 'object' && 'current' in hookNode.memoizedState) {
        hook.type = 'useRef';
        hook.value = serializeValue(hookNode.memoizedState.current, 0);
      } else {
        hook.type = 'unknown';
        hook.value = serializeValue(hookNode.memoizedState, 0);
      }

      hooks.push(hook);
      hookNode = hookNode.next;
      index++;
    }
    return hooks.length > 0 ? hooks : null;
  }

  function walkFiber(fiber, depth) {
    if (!fiber || depth > MAX_DEPTH) return null;

    const name = getComponentName(fiber);

    // Skip non-component fibers (DOM elements, fragments, providers, etc.)
    // but still walk their children
    if (!name) {
      const children = [];
      let child = fiber.child;
      while (child && children.length < MAX_CHILDREN) {
        const result = walkFiber(child, depth);
        if (result) {
          if (Array.isArray(result)) children.push(...result);
          else children.push(result);
        }
        child = child.sibling;
      }
      return children.length > 0 ? children : null;
    }

    const node = {
      name,
      source: getSource(fiber),
      props: getProps(fiber),
      hooks: getHooks(fiber),
      children: [],
    };

    let child = fiber.child;
    while (child && node.children.length < MAX_CHILDREN) {
      const result = walkFiber(child, depth + 1);
      if (result) {
        if (Array.isArray(result)) node.children.push(...result);
        else node.children.push(result);
      }
      child = child.sibling;
    }

    return node;
  }

  const tree = walkFiber(rootFiber, 0);
  return JSON.stringify(tree || { error: "Empty component tree" });
})()
"""

# Script to find the fiber for a specific DOM element
FIBER_FROM_ELEMENT_SCRIPT = """
((selector) => {
  const el = document.querySelector(selector);
  if (!el) return JSON.stringify({ error: "Element not found: " + selector });

  // React attaches fiber references to DOM nodes via keys starting with __reactFiber$
  const fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
  if (!fiberKey) return JSON.stringify({ error: "No React fiber found on element. Is this a React-rendered element?" });

  const fiber = el[fiberKey];

  // Walk up to find the nearest component fiber (not a DOM fiber)
  let current = fiber;
  while (current) {
    if (typeof current.type === 'function' || (typeof current.type === 'object' && current.type !== null)) {
      const name = current.type.displayName || current.type.name;
      if (name) {
        const src = current._debugSource;
        const props = current.memoizedProps;

        // Serialize props safely
        function serializeValue(val, depth) {
          if (depth > 2) return '[nested]';
          if (val === null || val === undefined) return val;
          if (typeof val === 'function') return '[function]';
          if (typeof val === 'symbol') return val.toString();
          if (val instanceof Element) return '[DOMElement]';
          if (Array.isArray(val)) {
            if (val.length > 10) return '[Array(' + val.length + ')]';
            return val.map(v => serializeValue(v, depth + 1));
          }
          if (typeof val === 'object') {
            if (val.$$typeof) return '[ReactElement]';
            const keys = Object.keys(val);
            if (keys.length > 20) return '[Object(' + keys.length + ' keys)]';
            const out = {};
            for (const k of keys) out[k] = serializeValue(val[k], depth + 1);
            return out;
          }
          return val;
        }

        // Get parent component chain
        const parents = [];
        let parent = current.return;
        while (parent && parents.length < 10) {
          if (typeof parent.type === 'function' || (typeof parent.type === 'object' && parent.type !== null)) {
            const pName = parent.type?.displayName || parent.type?.name;
            if (pName) {
              parents.push({
                name: pName,
                source: parent._debugSource ? {
                  fileName: parent._debugSource.fileName,
                  lineNumber: parent._debugSource.lineNumber,
                } : null,
              });
            }
          }
          parent = parent.return;
        }

        return JSON.stringify({
          name,
          source: src ? { fileName: src.fileName, lineNumber: src.lineNumber, columnNumber: src.columnNumber } : null,
          props: serializeValue(props, 0),
          parents,
        });
      }
    }
    current = current.return;
  }

  return JSON.stringify({ error: "No React component found in fiber tree above this element" });
})('%SELECTOR%')
"""

# Script to read Redux store state.
# Strategy: try globals first (fast), then walk ALL fiber roots across ALL
# renderers (handles Next.js App Router where the Provider lives in a
# client-component fiber root separate from the server shell root).
REDUX_STATE_SCRIPT = """
(() => {
  // 1. Check common globals (fastest path — works if app exposes the store)
  const globalStore =
    window.__REDUX_STORE__ ||
    window.__SPECTER_STORE__ ||
    window.__NEXT_REDUX_WRAPPER_STORE__ ||
    window.store;

  if (globalStore && typeof globalStore.getState === 'function') {
    return readStore(globalStore, 'global', '%PATH%');
  }

  // 2. Walk ALL React fiber roots across ALL renderers.
  //    Next.js 14+ App Router creates multiple roots (server shell + client app).
  //    The Redux Provider is in the client root, which may not be the first one.
  const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
  if (hook && hook.renderers) {
    for (const [rendererId, renderer] of hook.renderers) {
      const roots = hook.getFiberRoots(rendererId);
      if (!roots) continue;
      for (const root of roots) {
        const store = findStoreInFiber(root.current, 0);
        if (store) {
          // Cache it globally so future calls are instant
          window.__SPECTER_STORE__ = store;
          return readStore(store, 'fiber:renderer_' + rendererId, '%PATH%');
        }
      }
    }
  }

  // 3. Last resort: check if Redux DevTools extension is available
  if (window.__REDUX_DEVTOOLS_EXTENSION__) {
    return JSON.stringify({
      error: 'Store found in DevTools but not directly accessible.',
      hint: 'Add this to your store file: if (typeof window !== "undefined") window.__REDUX_STORE__ = store;',
    });
  }

  return JSON.stringify({ error: 'Redux store not found.' });

  // --- helpers ---

  function findStoreInFiber(fiber, depth) {
    if (!fiber || depth > 80) return null;

    // Check props.store (react-redux Provider)
    const props = fiber.memoizedProps;
    if (props) {
      if (props.store && typeof props.store.getState === 'function') return props.store;
      // Some wrappers nest it: props.value.store
      if (props.value && props.value.store && typeof props.value.store.getState === 'function') return props.value.store;
    }

    // Check stateNode (class components)
    if (fiber.stateNode && fiber.stateNode.store && typeof fiber.stateNode.store.getState === 'function') {
      return fiber.stateNode.store;
    }

    // Walk children + siblings
    let child = fiber.child;
    while (child) {
      const found = findStoreInFiber(child, depth + 1);
      if (found) return found;
      child = child.sibling;
    }
    return null;
  }

  function readStore(store, source, path) {
    const state = store.getState();

    if (path && path !== '') {
      const parts = path.split('.');
      let current = state;
      for (const part of parts) {
        if (current === null || current === undefined) break;
        current = current[part];
      }
      // Serialize safely
      function safe(val, depth) {
        if (depth > 3) return '[nested]';
        if (val === null || val === undefined) return val;
        if (typeof val === 'function') return '[function]';
        if (Array.isArray(val)) {
          if (val.length > 20) return '[Array(' + val.length + ')]';
          return val.map(v => safe(v, depth + 1));
        }
        if (typeof val === 'object') {
          const keys = Object.keys(val);
          if (keys.length > 30) return '[Object(' + keys.length + ' keys)]';
          const out = {};
          for (const k of keys) out[k] = safe(val[k], depth + 1);
          return out;
        }
        return val;
      }
      return JSON.stringify({ source, path, value: safe(current, 0) });
    }

    // Summary view: top-level keys + shapes
    const summary = {};
    for (const [key, value] of Object.entries(state)) {
      if (Array.isArray(value)) summary[key] = '[Array(' + value.length + ')]';
      else if (typeof value === 'object' && value !== null) summary[key] = '{' + Object.keys(value).length + ' keys}';
      else summary[key] = value;
    }
    return JSON.stringify({ source, keys: Object.keys(state), summary });
  }
})()
"""

# Script to get Redux action history (if devtools middleware is active)
REDUX_ACTIONS_SCRIPT = """
(() => {
  // Redux DevTools stores action history internally
  // We can access it through the devtools instance
  const devtools = window.__REDUX_DEVTOOLS_EXTENSION_COMPOSE__
    ? 'compose_available'
    : window.__REDUX_DEVTOOLS_EXTENSION__
    ? 'extension_available'
    : null;

  if (!devtools) {
    return JSON.stringify({ error: 'Redux DevTools not found. Actions history requires the devtools middleware.' });
  }

  // The most reliable way: look for the devtools connector instance
  // This is internal but commonly accessible
  const store = window.__REDUX_STORE__ || window.__NEXT_REDUX_WRAPPER_STORE__ || window.store;
  if (!store || !store.dispatch) {
    return JSON.stringify({ error: 'Store not directly accessible for action interception.' });
  }

  return JSON.stringify({
    note: 'Action history requires the Redux DevTools browser extension to be active. Use evaluate_js to manually check specific state paths.',
    current_state_keys: Object.keys(store.getState()),
  });
})()
"""

# Group DOM elements by their nearest React component ancestor.
# Solves the "6 rows for 2 sources because of triple-mount" problem:
# when the same data is rendered in multiple views, querySelectorAll
# returns a flat list. This groups them by the owning component so
# Claude can see which view each element came from.
GROUP_BY_COMPONENT_SCRIPT = """
((selector) => {
  const elements = document.querySelectorAll(selector);
  if (elements.length === 0) {
    return JSON.stringify({ error: 'No elements match: ' + selector });
  }

  function nearestComponent(el) {
    // Find the fiber attached to this DOM node
    const fiberKey = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
    if (!fiberKey) return null;

    let fiber = el[fiberKey];
    // Walk up the fiber tree to find the nearest named component
    while (fiber) {
      const type = fiber.type;
      if (type && (typeof type === 'function' || (typeof type === 'object' && type !== null))) {
        const name = type.displayName || type.name;
        if (name && name[0] === name[0].toUpperCase()) {
          const src = fiber._debugSource;
          return {
            name,
            source: src ? {
              fileName: src.fileName,
              lineNumber: src.lineNumber,
            } : null,
          };
        }
      }
      fiber = fiber.return;
    }
    return null;
  }

  function elementSummary(el, index) {
    const rect = el.getBoundingClientRect();
    return {
      index,
      tag: el.tagName.toLowerCase(),
      text: (el.textContent || '').trim().substring(0, 80),
      visible: el.offsetParent !== null,
      rect: {
        x: Math.round(rect.x),
        y: Math.round(rect.y),
        width: Math.round(rect.width),
        height: Math.round(rect.height),
      },
      data_testid: el.dataset?.testid || null,
      id: el.id || null,
    };
  }

  // Group elements by their owning component
  const groups = new Map();
  let globalIndex = 0;

  for (const el of elements) {
    const comp = nearestComponent(el);
    const key = comp ? comp.name : '<no-component>';

    if (!groups.has(key)) {
      groups.set(key, {
        component: key,
        source: comp?.source || null,
        elements: [],
      });
    }

    groups.get(key).elements.push(elementSummary(el, globalIndex));
    globalIndex++;
  }

  return JSON.stringify({
    selector,
    total: elements.length,
    groups: Array.from(groups.values()),
  });
})('%SELECTOR%')
"""


class ReactInspector:
    """React component tree and Redux state inspector."""

    async def get_component_tree(
        self,
        connection: CDPConnection,
        max_depth: int = 15,
        max_children: int = 50,
    ) -> dict:
        """Walk the React fiber tree and return the component hierarchy.

        Args:
            connection: Active CDP connection.
            max_depth: Maximum tree depth to walk (default 15).
            max_children: Maximum children per component (default 50).

        Returns:
            Nested dict representing the component tree with names, props,
            hooks, source locations, and children.
        """
        script = FIBER_WALK_SCRIPT.replace("%MAX_DEPTH%", str(max_depth)).replace(
            "%MAX_CHILDREN%", str(max_children)
        )

        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True, "awaitPromise": False},
        )

        return _parse_js_result(result)

    async def get_component_at(
        self,
        connection: CDPConnection,
        selector: str,
    ) -> dict:
        """Get the React component that owns a specific DOM element.

        Walks up the fiber tree from the DOM element to find the nearest
        component, returning its name, props, source location, and parent
        component chain.

        Args:
            connection: Active CDP connection.
            selector: CSS selector for the DOM element.

        Returns:
            Dict with component name, source, props, and parent chain.
        """
        script = FIBER_FROM_ELEMENT_SCRIPT.replace("%SELECTOR%", selector.replace("'", "\\'"))

        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True, "awaitPromise": False},
        )

        return _parse_js_result(result)

    async def get_redux_state(
        self,
        connection: CDPConnection,
        path: str = "",
    ) -> dict:
        """Read the Redux store state.

        Args:
            connection: Active CDP connection.
            path: Dot-separated path into the state tree (e.g., "auth.session").
                  Empty string returns a summary of top-level keys.

        Returns:
            Dict with the state value at the given path, or a summary.
        """
        script = REDUX_STATE_SCRIPT.replace("%PATH%", path)

        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True, "awaitPromise": False},
        )

        return _parse_js_result(result)

    async def get_redux_actions(self, connection: CDPConnection) -> dict:
        """Get Redux action history (requires DevTools middleware).

        Args:
            connection: Active CDP connection.

        Returns:
            Dict with recent actions or a note about required setup.
        """
        result = await connection.send(
            "Runtime.evaluate",
            {"expression": REDUX_ACTIONS_SCRIPT, "returnByValue": True, "awaitPromise": False},
        )

        return _parse_js_result(result)

    async def get_elements_grouped_by_component(
        self,
        connection: CDPConnection,
        selector: str,
    ) -> dict:
        """Find elements matching a selector and group them by owning component.

        Solves the "N rows for M sources because of multi-mount" problem:
        when the same list/table is rendered in multiple views, a flat
        querySelectorAll result is ambiguous. This groups results by the
        nearest React component ancestor.

        Args:
            connection: Active CDP connection.
            selector: CSS selector to match elements (e.g., "[class*='Row']").

        Returns:
            Dict with total element count and groups keyed by component name.
        """
        script = GROUP_BY_COMPONENT_SCRIPT.replace(
            "%SELECTOR%", selector.replace("'", "\\'")
        )
        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True, "awaitPromise": False},
        )
        return _parse_js_result(result)

    async def check_react_available(self, connection: CDPConnection) -> dict:
        """Check if React DevTools hook is available and what version is running.

        Args:
            connection: Active CDP connection.

        Returns:
            Dict with React availability, version, and renderer info.
        """
        script = """
        (() => {
            const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
            if (!hook) return JSON.stringify({ available: false, reason: "No __REACT_DEVTOOLS_GLOBAL_HOOK__. React may not be loaded or is in production mode." });

            const renderers = hook.renderers;
            const rendererInfo = [];
            if (renderers) {
                for (const [id, renderer] of renderers) {
                    rendererInfo.push({
                        id,
                        version: renderer.version || 'unknown',
                        bundleType: renderer.bundleType || 'unknown',
                    });
                }
            }

            const fiberRoots = renderers && renderers.size > 0
                ? hook.getFiberRoots(renderers.keys().next().value)
                : null;

            return JSON.stringify({
                available: true,
                renderers: rendererInfo,
                fiberRootCount: fiberRoots ? fiberRoots.size : 0,
                hasReduxDevtools: !!window.__REDUX_DEVTOOLS_EXTENSION__,
                hasNextData: !!window.__NEXT_DATA__,
            });
        })()
        """

        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True, "awaitPromise": False},
        )

        return _parse_js_result(result)


def _parse_js_result(result: dict) -> dict:
    """Parse a CDP Runtime.evaluate result that returns a JSON string."""
    remote_object = result.get("result", {})

    if result.get("exceptionDetails"):
        return {
            "error": result["exceptionDetails"].get("text", "JS evaluation error"),
        }

    value = remote_object.get("value")
    if value is None:
        return {"error": "No value returned"}

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}

    if isinstance(value, dict):
        return value

    return {"value": value}
