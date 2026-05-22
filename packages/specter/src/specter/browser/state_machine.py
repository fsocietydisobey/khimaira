"""State machine + Redux store extraction from the active browser page.

Two extraction modes:
  redux  — walks the React fiber tree for a Redux Provider; returns current
            state shape, top-level slice names, and dispatched action count
            from Redux DevTools if available.
  xstate — detects @xstate/inspect integration; returns machine ID, known
            states, current state, and available transitions. Returns a
            structured error if XState inspect is not present.

Usage example:
    extractor = StateMachineExtractor()
    result = await extractor.extract(conn, library="redux")
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from specter.browser.connection import CDPConnection

logger = logging.getLogger(__name__)

# JavaScript that reads Redux store state via fiber walk (same approach as
# ReactInspector.get_redux_state, adapted for standalone extraction).
_REDUX_EXTRACT_SCRIPT = """
(function() {
    try {
        // Try known store globals first (fast path)
        var store = window.__REDUX_STORE__
            || window.__NEXT_REDUX_WRAPPER_STORE__
            || window.__SPECTER_STORE__
            || window.store;

        // If no global, walk fiber roots for a Provider
        if (!store) {
            var hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
            if (hook && hook.renderers && hook.renderers.size > 0) {
                var rendererId = hook.renderers.keys().next().value;
                var roots = hook.getFiberRoots(rendererId);
                if (roots) {
                    for (var root of roots) {
                        var found = walkFiberForStore(root.current, 0);
                        if (found) { store = found; break; }
                    }
                }
            }
        }

        if (!store) return JSON.stringify({error: 'Redux store not found — is this a Redux app?'});

        window.__SPECTER_STORE__ = store;  // cache for speed on next call
        var state = store.getState();

        // Count actions from DevTools if available
        var actionCount = null;
        try {
            var ext = window.__REDUX_DEVTOOLS_EXTENSION__;
            if (ext && ext.store) {
                var history = ext.store.getState().actionsById;
                actionCount = history ? Object.keys(history).length : null;
            }
        } catch(e) {}

        var slices = typeof state === 'object' && state !== null ? Object.keys(state) : [];

        return JSON.stringify({
            store_shape: safeSerialize(state, 0),
            slices: slices,
            actions_history_count: actionCount,
        });
    } catch(e) {
        return JSON.stringify({error: 'extraction failed: ' + e.message});
    }

    function walkFiberForStore(fiber, depth) {
        if (!fiber || depth > 50) return null;
        var props = fiber.memoizedProps;
        if (props) {
            if (props.store && typeof props.store.getState === 'function') return props.store;
            if (props.value && props.value.store && typeof props.value.store.getState === 'function') return props.value.store;
        }
        if (fiber.stateNode && fiber.stateNode.store && typeof fiber.stateNode.store.getState === 'function') return fiber.stateNode.store;
        var fromChild = fiber.child ? walkFiberForStore(fiber.child, depth + 1) : null;
        return fromChild || (fiber.sibling ? walkFiberForStore(fiber.sibling, depth) : null);
    }

    function safeSerialize(obj, depth) {
        if (depth > 3) return '...';
        if (obj === null || obj === undefined) return obj;
        if (typeof obj !== 'object') return obj;
        if (Array.isArray(obj)) return obj.slice(0, 5).map(function(x) { return safeSerialize(x, depth + 1); });
        var result = {};
        var keys = Object.keys(obj).slice(0, 20);
        for (var k of keys) { result[k] = safeSerialize(obj[k], depth + 1); }
        if (Object.keys(obj).length > 20) result['__truncated__'] = true;
        return result;
    }
})()
"""

# JavaScript that looks for XState's @xstate/inspect integration.
_XSTATE_EXTRACT_SCRIPT = """
(function() {
    try {
        // @xstate/inspect attaches to window.__xstate__
        var xs = window.__xstate__;
        if (!xs) {
            return JSON.stringify({error: '@xstate/inspect not detected — call initDevtools() or use @xstate/inspect in this app'});
        }
        var services = xs.getAllServices ? xs.getAllServices() : {};
        var result = {};
        for (var id in services) {
            var s = services[id];
            try {
                result[id] = {
                    machine_id: id,
                    current_state: s.state ? s.state.value : null,
                    states: s.machine && s.machine.states ? Object.keys(s.machine.states) : [],
                };
            } catch(e) {}
        }
        if (Object.keys(result).length === 0) {
            return JSON.stringify({error: 'XState inspect active but no services registered yet'});
        }
        return JSON.stringify({services: result});
    } catch(e) {
        return JSON.stringify({error: 'XState extraction failed: ' + e.message});
    }
})()
"""


class StateMachineExtractor:
    """Extract Redux store state or XState machine shape from the page."""

    async def extract(
        self,
        conn: "CDPConnection",
        library: Literal["redux", "xstate"] = "redux",
    ) -> dict:
        """Extract state machine or Redux store information from the page.

        For Redux: walks the React fiber tree to find the Redux store (same
        approach as specter_get_redux_state), then returns the full state
        shape, top-level slice names, and dispatched action count from
        Redux DevTools if available.

        For XState: detects @xstate/inspect integration (initDevtools() must
        have been called); returns machine ID, known states, current state,
        and available transitions per registered service. Returns a structured
        error if XState inspect is not present — this is not an error condition,
        just a feature flag.

        Args:
            conn: Active CDP connection.
            library: "redux" (default) or "xstate".

        Returns:
            For redux: {store_shape, slices, actions_history_count}
            For xstate: {services: {<id>: {machine_id, current_state, states}}}
            On error: {"error": "<reason>"}
        """
        if library == "redux":
            script = _REDUX_EXTRACT_SCRIPT
        elif library == "xstate":
            script = _XSTATE_EXTRACT_SCRIPT
        else:
            return {"error": f"Unknown library {library!r}. Use 'redux' or 'xstate'."}

        result = await conn.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True},
        )

        if "exceptionDetails" in result:
            exc = result["exceptionDetails"].get("text", "script error")
            return {"error": f"state machine extraction failed: {exc}"}

        raw_value = result.get("result", {}).get("value")
        if raw_value is None:
            return {"error": "extraction returned null"}

        import json

        try:
            parsed = json.loads(raw_value) if isinstance(raw_value, str) else raw_value
        except (json.JSONDecodeError, TypeError):
            return {"error": f"could not parse extraction result: {raw_value!r}"}

        return parsed
