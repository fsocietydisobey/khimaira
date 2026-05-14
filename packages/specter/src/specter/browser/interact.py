"""Browser interaction tools — click, type, select, wait.

Gives Claude higher-level interaction primitives instead of requiring
raw evaluate_js scripts. The key tool is get_interactive_elements(),
which returns every clickable/typeable element on the page with its
label, role, and a stable selector — so Claude can reason about what
to interact with based on the screenshot + element list.

Flow:
  1. Claude takes a screenshot (sees the page visually)
  2. Claude calls get_interactive_elements() (gets the interactive surface)
  3. Claude picks the right element by matching its label to the goal
  4. Claude calls click_element/fill_input/select_option

This is the pattern AI browser agents use. The intelligence (deciding
WHAT to click) stays in Claude. The mechanics (HOW to click) are here.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from specter.browser.connection import CDPConnection

logger = logging.getLogger(__name__)

# JavaScript that extracts all interactive elements with stable selectors.
#
# Two-pass discovery:
#   Pass 1 — DOM-based: standard interactive tags, ARIA roles, tabindex.
#   Pass 2 — React fiber walk: finds elements with onClick/onMouseDown/etc.
#            handler props attached via React (the classic "clickable div"
#            pattern that has no semantic marker in the DOM).
#
# Each element is enriched with:
#   - componentOwner: nearest named React ancestor component (+ source file).
#   - landmark: nearest ARIA landmark (main, nav, dialog, etc.) for grouping.
#   - handlers: list of React event handler prop names (when discovered via fiber).
#   - discoveredVia: "dom" or "react" — tells you how the element was found.
EXTRACT_INTERACTIVES_SCRIPT = """
(() => {
    const results = [];
    const seen = new Set();
    const seenElements = new WeakSet();

    function isVisible(el) {
        if (el.disabled) return false;
        if (el.offsetParent === null && el.tagName !== 'INPUT') return false;
        const style = getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden') return false;
        return true;
    }

    function addElement(el, opts) {
        opts = opts || {};
        if (!el || el.nodeType !== 1) return;
        if (seenElements.has(el)) return;
        if (!isVisible(el)) return;

        const selector = buildSelector(el);
        if (seen.has(selector)) return;

        const label = getLabel(el);
        const tag = el.tagName;
        if (!label && tag !== 'INPUT' && tag !== 'TEXTAREA' && tag !== 'SELECT') return;

        seen.add(selector);
        seenElements.add(el);

        const rect = el.getBoundingClientRect();
        const entry = {
            selector,
            tag: tag.toLowerCase(),
            type: el.type || null,
            role: el.getAttribute('role') || inferRole(el),
            label: label || '',
            placeholder: el.placeholder || null,
            value: (tag === 'INPUT' || tag === 'TEXTAREA') ? (el.value || '').substring(0, 100) : null,
            href: el.href || null,
            disabled: el.disabled || false,
            checked: el.checked !== undefined ? el.checked : null,
            rect: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
            },
            componentOwner: nearestNamedComponent(el),
            landmark: findLandmark(el),
            discoveredVia: opts.discoveredVia || 'dom',
            handlers: opts.handlers || null,
        };
        results.push(entry);
    }

    // === Pass 1: DOM-based discovery ===
    const selectors = [
        'a[href]',
        'button',
        'input',
        'select',
        'textarea',
        '[role="button"]',
        '[role="link"]',
        '[role="tab"]',
        '[role="menuitem"]',
        '[role="checkbox"]',
        '[role="radio"]',
        '[role="switch"]',
        '[onclick]',
        '[tabindex]',
        'summary',
        'label[for]',
    ];
    const allElements = document.querySelectorAll(selectors.join(', '));
    for (const el of allElements) {
        addElement(el, { discoveredVia: 'dom' });
    }

    // === Pass 2: React fiber handler discovery ===
    // Finds host fibers (DOM elements rendered by React) whose memoizedProps
    // include event handler functions like onClick. Catches the div/span
    // onClick pattern that the DOM pass misses.
    try {
        const hook = window.__REACT_DEVTOOLS_GLOBAL_HOOK__;
        if (hook && hook.renderers && hook.renderers.size > 0) {
            const HANDLERS = ['onClick', 'onMouseDown', 'onDoubleClick', 'onPointerDown', 'onKeyDown', 'onKeyPress', 'onSubmit', 'onChange'];
            const MAX_DEPTH = 200;
            let walked = 0;

            function walk(fiber, depth) {
                if (!fiber || depth > MAX_DEPTH || walked > 20000) return;
                walked++;

                if (typeof fiber.type === 'string' && fiber.stateNode && fiber.memoizedProps) {
                    const props = fiber.memoizedProps;
                    const handlers = [];
                    for (const key of HANDLERS) {
                        if (typeof props[key] === 'function') handlers.push(key);
                    }
                    if (handlers.length > 0) {
                        addElement(fiber.stateNode, { discoveredVia: 'react', handlers });
                    }
                }

                if (fiber.child) walk(fiber.child, depth + 1);
                if (fiber.sibling) walk(fiber.sibling, depth);
            }

            const rendererId = hook.renderers.keys().next().value;
            const fiberRoots = hook.getFiberRoots(rendererId);
            if (fiberRoots) {
                for (const root of fiberRoots) {
                    if (root.current) walk(root.current, 0);
                }
            }
        }
    } catch (e) {
        // React not available — DOM pass already ran; continue.
    }

    function nearestNamedComponent(el) {
        const key = Object.keys(el).find(k => k.startsWith('__reactFiber$'));
        if (!key) return null;
        let fiber = el[key];
        while (fiber) {
            const type = fiber.type;
            if (type && (typeof type === 'function' || (typeof type === 'object' && type !== null))) {
                const name = type.displayName || type.name;
                if (name && name[0] === name[0].toUpperCase()) {
                    const src = fiber._debugSource;
                    return {
                        name,
                        source: src ? { fileName: src.fileName, lineNumber: src.lineNumber } : null,
                    };
                }
            }
            fiber = fiber.return;
        }
        return null;
    }

    function findLandmark(el) {
        let current = el;
        while (current && current !== document.body) {
            const tag = current.tagName;
            const role = current.getAttribute('role');
            const aria = current.getAttribute('aria-label');
            if (tag === 'MAIN' || role === 'main') return { type: 'main', label: aria || 'main' };
            if (tag === 'NAV' || role === 'navigation') return { type: 'navigation', label: aria || 'nav' };
            if (tag === 'HEADER' || role === 'banner') return { type: 'banner', label: aria || 'header' };
            if (tag === 'ASIDE' || role === 'complementary') return { type: 'complementary', label: aria || 'aside' };
            if (tag === 'FOOTER' || role === 'contentinfo') return { type: 'contentinfo', label: aria || 'footer' };
            if (role === 'dialog' || role === 'alertdialog') return { type: 'dialog', label: aria || 'dialog' };
            if (role === 'region' && aria) return { type: 'region', label: aria };
            current = current.parentElement;
        }
        return { type: 'content', label: 'content' };
    }

    function buildSelector(el) {
        // Prefer data-testid, then id, then build a positional selector
        if (el.dataset?.testid) return '[data-testid="' + el.dataset.testid + '"]';
        if (el.id) return '#' + CSS.escape(el.id);

        // Use aria-label if unique
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) {
            const matches = document.querySelectorAll('[aria-label="' + CSS.escape(ariaLabel) + '"]');
            if (matches.length === 1) return '[aria-label="' + ariaLabel + '"]';
        }

        // Build a path: tag.class:nth-of-type
        const parts = [];
        let current = el;
        while (current && current !== document.body && parts.length < 5) {
            let seg = current.tagName.toLowerCase();
            if (current.className && typeof current.className === 'string') {
                const classes = current.className.trim().split(/\\s+/).filter(c => !c.includes('_') || c.length < 30).slice(0, 2);
                if (classes.length > 0) seg += '.' + classes.join('.');
            }
            // Add nth-of-type if needed for uniqueness
            const parent = current.parentElement;
            if (parent) {
                const siblings = parent.querySelectorAll(':scope > ' + current.tagName.toLowerCase());
                if (siblings.length > 1) {
                    const index = Array.from(siblings).indexOf(current) + 1;
                    seg += ':nth-of-type(' + index + ')';
                }
            }
            parts.unshift(seg);
            current = current.parentElement;
        }
        return parts.join(' > ');
    }

    function getLabel(el) {
        // Direct text content (for buttons, links)
        const directText = el.textContent?.trim();
        if (directText && directText.length < 100 && directText.length > 0) {
            return directText.replace(/\\s+/g, ' ');
        }

        // aria-label
        const ariaLabel = el.getAttribute('aria-label');
        if (ariaLabel) return ariaLabel;

        // title
        if (el.title) return el.title;

        // For inputs: associated label
        if (el.id) {
            const label = document.querySelector('label[for="' + el.id + '"]');
            if (label) return label.textContent?.trim();
        }

        // Placeholder
        if (el.placeholder) return el.placeholder;

        // alt text (for images inside buttons)
        const img = el.querySelector('img');
        if (img?.alt) return img.alt;

        return '';
    }

    function inferRole(el) {
        const tag = el.tagName.toLowerCase();
        if (tag === 'a') return 'link';
        if (tag === 'button') return 'button';
        if (tag === 'input') {
            const type = el.type?.toLowerCase() || 'text';
            if (type === 'submit') return 'button';
            if (type === 'checkbox') return 'checkbox';
            if (type === 'radio') return 'radio';
            return 'textbox';
        }
        if (tag === 'select') return 'combobox';
        if (tag === 'textarea') return 'textbox';
        return el.getAttribute('role') || 'unknown';
    }

    return JSON.stringify(results);
})()
"""

# Click by selector
CLICK_SCRIPT = """
((selector) => {
    const el = document.querySelector(selector);
    if (!el) return JSON.stringify({ error: 'Element not found: ' + selector });

    // Scroll into view first
    el.scrollIntoView({ behavior: 'instant', block: 'center' });

    // Dispatch a real click event sequence
    el.dispatchEvent(new MouseEvent('mousedown', { bubbles: true, cancelable: true }));
    el.dispatchEvent(new MouseEvent('mouseup', { bubbles: true, cancelable: true }));
    el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));

    const label = el.textContent?.trim().substring(0, 50) || el.getAttribute('aria-label') || selector;
    return JSON.stringify({ clicked: true, label, tag: el.tagName });
})('%SELECTOR%')
"""

# Fill input by selector
FILL_SCRIPT = """
((selector, value) => {
    const el = document.querySelector(selector);
    if (!el) return JSON.stringify({ error: 'Element not found: ' + selector });

    // Focus the element
    el.focus();

    // Clear existing value
    el.value = '';
    el.dispatchEvent(new Event('input', { bubbles: true }));

    // Set new value character by character (React needs this for controlled inputs)
    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    )?.set || Object.getOwnPropertyDescriptor(
        window.HTMLTextAreaElement.prototype, 'value'
    )?.set;

    if (nativeInputValueSetter) {
        nativeInputValueSetter.call(el, value);
    } else {
        el.value = value;
    }

    el.dispatchEvent(new Event('input', { bubbles: true }));
    el.dispatchEvent(new Event('change', { bubbles: true }));

    return JSON.stringify({ filled: true, selector, value: el.value.substring(0, 50) });
})('%SELECTOR%', '%VALUE%')
"""

# Select option by selector and value
SELECT_SCRIPT = """
((selector, optionValue) => {
    const el = document.querySelector(selector);
    if (!el) return JSON.stringify({ error: 'Element not found: ' + selector });

    // Find the option
    let option = null;
    for (const opt of el.options) {
        if (opt.value === optionValue || opt.textContent.trim() === optionValue) {
            option = opt;
            break;
        }
    }
    if (!option) {
        const available = Array.from(el.options).map(o => o.textContent.trim());
        return JSON.stringify({ error: 'Option not found: ' + optionValue, available });
    }

    el.value = option.value;
    el.dispatchEvent(new Event('change', { bubbles: true }));
    return JSON.stringify({ selected: true, value: option.value, text: option.textContent.trim() });
})('%SELECTOR%', '%VALUE%')
"""

# Wait for element
WAIT_SCRIPT = """
(async (selector, timeoutMs) => {
    const start = Date.now();
    while (Date.now() - start < timeoutMs) {
        const el = document.querySelector(selector);
        if (el && el.offsetParent !== null) {
            return JSON.stringify({ found: true, elapsed: Date.now() - start });
        }
        await new Promise(r => setTimeout(r, 200));
    }
    return JSON.stringify({ found: false, timeout: true, elapsed: timeoutMs });
})('%SELECTOR%', %TIMEOUT%)
"""

# Scroll a specific element into view and report its new position.
SCROLL_TO_ELEMENT_SCRIPT = """
((selector) => {
    const el = document.querySelector(selector);
    if (!el) return JSON.stringify({ error: 'Element not found: ' + selector });

    el.scrollIntoView({ behavior: 'instant', block: 'center', inline: 'nearest' });

    const rect = el.getBoundingClientRect();
    const inViewport = rect.top >= 0 && rect.left >= 0 &&
        rect.bottom <= window.innerHeight && rect.right <= window.innerWidth;

    return JSON.stringify({
        scrolled: true,
        selector,
        rect: {
            x: Math.round(rect.x),
            y: Math.round(rect.y),
            width: Math.round(rect.width),
            height: Math.round(rect.height),
        },
        inViewport,
        viewport: { width: window.innerWidth, height: window.innerHeight },
    });
})('%SELECTOR%')
"""

# Scroll a container (or the window) by N viewport-sized steps in a direction.
# Reports before/after positions so Claude can detect hitting the edge.
SCROLL_WITHIN_SCRIPT = """
((selector, direction, count) => {
    const useWindow = !selector;
    const scroller = useWindow ? null : document.querySelector(selector);
    if (!useWindow && !scroller) {
        return JSON.stringify({ error: 'Scroller not found: ' + selector });
    }

    const target = scroller || document.scrollingElement || document.documentElement;
    const heightRef = scroller ? scroller.clientHeight : window.innerHeight;
    const widthRef = scroller ? scroller.clientWidth : window.innerWidth;

    const before = { scrollTop: target.scrollTop, scrollLeft: target.scrollLeft };

    // Scroll by (viewport - 100px overlap) * count, to preserve context across steps
    const stepY = Math.max(heightRef - 100, 100);
    const stepX = Math.max(widthRef - 100, 100);

    let deltaY = 0, deltaX = 0;
    if (direction === 'down') deltaY = stepY * count;
    else if (direction === 'up') deltaY = -stepY * count;
    else if (direction === 'right') deltaX = stepX * count;
    else if (direction === 'left') deltaX = -stepX * count;
    else return JSON.stringify({ error: 'Invalid direction: ' + direction + ' (use up/down/left/right)' });

    if (useWindow) {
        window.scrollBy({ top: deltaY, left: deltaX, behavior: 'instant' });
    } else {
        scroller.scrollBy({ top: deltaY, left: deltaX, behavior: 'instant' });
    }

    const after = { scrollTop: target.scrollTop, scrollLeft: target.scrollLeft };
    const moved = after.scrollTop !== before.scrollTop || after.scrollLeft !== before.scrollLeft;

    // Detect edge: tried to scroll but nothing happened
    const maxScrollTop = target.scrollHeight - target.clientHeight;
    const maxScrollLeft = target.scrollWidth - target.clientWidth;
    const atEnd = {
        top: after.scrollTop <= 0,
        bottom: after.scrollTop >= maxScrollTop - 1,
        left: after.scrollLeft <= 0,
        right: after.scrollLeft >= maxScrollLeft - 1,
    };

    return JSON.stringify({
        scrolled: moved,
        direction,
        count,
        scroller: useWindow ? 'window' : selector,
        before,
        after,
        atEnd,
    });
})('%SELECTOR%', '%DIRECTION%', %COUNT%)
"""

# Hover — dispatches mouseenter + mouseover to reveal hidden UI
HOVER_SCRIPT = """
((selector) => {
    const el = document.querySelector(selector);
    if (!el) return JSON.stringify({ error: 'Element not found: ' + selector });

    el.scrollIntoView({ behavior: 'instant', block: 'center' });

    const rect = el.getBoundingClientRect();
    const cx = rect.x + rect.width / 2;
    const cy = rect.y + rect.height / 2;
    const opts = { bubbles: true, cancelable: true, clientX: cx, clientY: cy };

    el.dispatchEvent(new MouseEvent('mouseenter', { ...opts, bubbles: false }));
    el.dispatchEvent(new MouseEvent('mouseover', opts));
    el.dispatchEvent(new MouseEvent('mousemove', opts));

    const label = el.textContent?.trim().substring(0, 50) || el.getAttribute('aria-label') || selector;
    return JSON.stringify({ hovered: true, label, tag: el.tagName });
})('%SELECTOR%')
"""


class Interactor:
    """High-level browser interaction tools."""

    async def hover_element(
        self,
        connection: CDPConnection,
        selector: str,
    ) -> dict:
        """Hover over an element to reveal hidden UI.

        Dispatches mouseenter + mouseover + mousemove events. This triggers
        CSS :hover styles and JS hover handlers, which often reveal action
        buttons, dropdown menus, tooltips, and edit controls that are hidden
        until the user hovers.

        After hovering, call get_interactive_elements() again to see the
        newly-revealed elements.

        Args:
            connection: Active CDP connection.
            selector: CSS selector for the element to hover over.

        Returns:
            Dict confirming the hover or error if element not found.
        """
        script = HOVER_SCRIPT.replace("%SELECTOR%", selector.replace("'", "\\'"))
        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True},
        )
        return _parse_result(result)

    async def press_key(
        self,
        connection: CDPConnection,
        key: str,
        modifiers: list[str] | None = None,
        selector: str | None = None,
    ) -> dict:
        """Press a keyboard key.

        Dispatches keyDown + keyUp via CDP Input.dispatchKeyEvent. Handles
        special keys (Enter, Escape, Tab, ArrowDown, etc.) and regular
        characters. Optionally focuses an element first.

        Common uses:
          - press_key("Enter") — submit a form
          - press_key("Escape") — close a modal
          - press_key("Tab") — move focus to next element
          - press_key("ArrowDown") — navigate a dropdown
          - press_key("a", modifiers=["ctrl"]) — select all

        Args:
            connection: Active CDP connection.
            key: Key name — "Enter", "Escape", "Tab", "ArrowDown", "Backspace",
                 or a single character like "a".
            modifiers: Optional list of modifier keys: "ctrl", "shift", "alt", "meta".
            selector: Optional CSS selector to focus before pressing.

        Returns:
            Dict confirming the key press.
        """
        # Focus element if selector provided
        if selector:
            await connection.send(
                "Runtime.evaluate",
                {
                    "expression": f"document.querySelector('{selector}')?.focus()",
                    "returnByValue": True,
                },
            )

        # Build modifier bitmask
        mod_flags = 0
        if modifiers:
            for m in modifiers:
                if m == "alt":
                    mod_flags |= 1
                elif m == "ctrl":
                    mod_flags |= 2
                elif m == "meta":
                    mod_flags |= 4
                elif m == "shift":
                    mod_flags |= 8

        # Map special key names to CDP key identifiers and codes
        key_map = {
            "Enter": ("Enter", "\r", 13),
            "Escape": ("Escape", "", 27),
            "Tab": ("Tab", "\t", 9),
            "Backspace": ("Backspace", "", 8),
            "Delete": ("Delete", "", 46),
            "ArrowUp": ("ArrowUp", "", 38),
            "ArrowDown": ("ArrowDown", "", 40),
            "ArrowLeft": ("ArrowLeft", "", 37),
            "ArrowRight": ("ArrowRight", "", 39),
            "Home": ("Home", "", 36),
            "End": ("End", "", 35),
            "Space": (" ", " ", 32),
        }

        if key in key_map:
            key_id, text, code = key_map[key]
        else:
            # Single character
            key_id = key
            text = key
            code = ord(key.upper()) if len(key) == 1 else 0

        # keyDown
        await connection.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyDown",
                "key": key_id,
                "text": text,
                "windowsVirtualKeyCode": code,
                "nativeVirtualKeyCode": code,
                "modifiers": mod_flags,
            },
        )

        # keyUp
        await connection.send(
            "Input.dispatchKeyEvent",
            {
                "type": "keyUp",
                "key": key_id,
                "windowsVirtualKeyCode": code,
                "nativeVirtualKeyCode": code,
                "modifiers": mod_flags,
            },
        )

        return {"pressed": True, "key": key, "modifiers": modifiers or []}

    """High-level browser interaction tools."""

    async def get_interactive_elements(
        self,
        connection: CDPConnection,
        role_filter: str | None = None,
    ) -> list[dict]:
        """Extract all interactive elements on the page.

        Returns buttons, links, inputs, selects, etc. with:
          - A stable CSS selector (prefers data-testid, id, aria-label)
          - The visible label/text
          - The element's role (button, link, textbox, etc.)
          - Bounding box (x, y, width, height)
          - Current state (value, checked, disabled)

        Args:
            connection: Active CDP connection.
            role_filter: Optional filter by role ("button", "link", "textbox").

        Returns:
            List of interactive element descriptors.
        """
        result = await connection.send(
            "Runtime.evaluate",
            {"expression": EXTRACT_INTERACTIVES_SCRIPT, "returnByValue": True},
        )

        value = result.get("result", {}).get("value", "[]")
        try:
            elements = json.loads(value)
        except json.JSONDecodeError:
            return [{"error": "Failed to parse interactive elements"}]

        if role_filter:
            elements = [e for e in elements if e.get("role") == role_filter]

        return elements

    async def click_element(
        self,
        connection: CDPConnection,
        selector: str,
    ) -> dict:
        """Click an element by CSS selector.

        Scrolls the element into view, then dispatches mousedown + mouseup + click
        events (the full sequence React expects).

        Args:
            connection: Active CDP connection.
            selector: CSS selector for the element to click.

        Returns:
            Dict confirming the click or error if element not found.
        """
        script = CLICK_SCRIPT.replace("%SELECTOR%", selector.replace("'", "\\'"))

        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True},
        )

        return _parse_result(result)

    async def fill_input(
        self,
        connection: CDPConnection,
        selector: str,
        value: str,
    ) -> dict:
        """Type a value into an input or textarea.

        Handles React controlled inputs by using the native value setter
        and dispatching both input and change events.

        Args:
            connection: Active CDP connection.
            selector: CSS selector for the input element.
            value: Text to type into the input.

        Returns:
            Dict confirming the fill or error if element not found.
        """
        script = FILL_SCRIPT.replace("%SELECTOR%", selector.replace("'", "\\'")).replace(
            "%VALUE%", value.replace("'", "\\'")
        )

        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True},
        )

        return _parse_result(result)

    async def set_file_input(
        self,
        connection: CDPConnection,
        selector: str,
        file_paths: list[str],
    ) -> dict:
        """Attach local files to an `<input type="file">` via `DOM.setFileInputFiles`.

        The only programmatic way to populate a file input. Browsers gate the
        native picker behind real user activation, and JS in the page can't
        read `file://` URLs from an `http://` origin. CDP's `setFileInputFiles`
        bypasses both: the browser process reads the file from local disk and
        attaches it to the input as if the user had picked it.

        Files must exist on the same machine as the browser (Specter runs
        locally). Absolute paths only — relative paths resolve against the
        Specter process cwd, which is rarely what you want.

        After attaching, a `change` event is manually dispatched to guarantee
        React/Vue/etc. listeners fire — CDP's automatic event behavior on this
        command is inconsistent across Chromium versions.

        Args:
            connection: Active CDP connection.
            selector: CSS selector for the `<input type="file">` element.
            file_paths: Absolute paths to local files. Multiple paths only
                work on inputs with the `multiple` attribute — Chromium
                silently drops extras otherwise.

        Returns:
            Dict with `success: True` and the input's resulting `files`
            (count + names), or `error` describing what failed.
        """
        if not file_paths:
            return {"error": "file_paths cannot be empty"}

        resolved: list[str] = []
        for p in file_paths:
            path = Path(p)
            if not path.is_absolute():
                return {"error": f"file path must be absolute, got: {p}"}
            if not path.exists():
                return {"error": f"file not found: {p}"}
            if not path.is_file():
                return {"error": f"not a regular file: {p}"}
            resolved.append(str(path))

        # Resolve the input element to a CDP objectId. returnByValue=False
        # keeps the remote object alive so we can pass its objectId to
        # DOM.setFileInputFiles (which accepts nodeId, backendNodeId, or
        # objectId — objectId skips the node-ID lookup dance).
        eval_result = await connection.send(
            "Runtime.evaluate",
            {
                "expression": f"document.querySelector({json.dumps(selector)})",
                "returnByValue": False,
            },
        )

        remote_obj = eval_result.get("result", {})
        if remote_obj.get("subtype") == "null":
            return {"error": f"element not found: {selector}"}

        object_id = remote_obj.get("objectId")
        if not object_id:
            exc = eval_result.get("exceptionDetails", {})
            return {"error": exc.get("text") or f"could not resolve selector: {selector}"}

        # DOM domain must be enabled before setFileInputFiles works.
        # Idempotent — sending DOM.enable repeatedly is safe.
        await connection.send("DOM.enable", {})

        try:
            await connection.send(
                "DOM.setFileInputFiles",
                {"files": resolved, "objectId": object_id},
            )
        except Exception as e:
            return {"error": f"DOM.setFileInputFiles failed: {e}"}

        # Verify + dispatch change. Combined into one Runtime.evaluate so we
        # don't pay two CDP round-trips. Returns the resulting files info as
        # JSON for _parse_result-style decoding.
        verify_script = (
            "(() => {"
            f"  const el = document.querySelector({json.dumps(selector)});"
            "  if (!el) return JSON.stringify({error: 'element disappeared after upload'});"
            "  if (!('files' in el)) return JSON.stringify({error: 'element is not a file input'});"
            "  el.dispatchEvent(new Event('input', { bubbles: true }));"
            "  el.dispatchEvent(new Event('change', { bubbles: true }));"
            "  return JSON.stringify({"
            "    success: true,"
            "    count: el.files.length,"
            "    names: Array.from(el.files).map(f => f.name),"
            "    sizes: Array.from(el.files).map(f => f.size),"
            "  });"
            "})()"
        )

        verify_result = await connection.send(
            "Runtime.evaluate",
            {"expression": verify_script, "returnByValue": True},
        )

        return _parse_result(verify_result)

    async def select_option(
        self,
        connection: CDPConnection,
        selector: str,
        option_value: str,
    ) -> dict:
        """Select an option from a dropdown by value or visible text.

        Args:
            connection: Active CDP connection.
            selector: CSS selector for the select element.
            option_value: The option's value attribute or visible text.

        Returns:
            Dict confirming the selection or listing available options.
        """
        script = SELECT_SCRIPT.replace("%SELECTOR%", selector.replace("'", "\\'")).replace(
            "%VALUE%", option_value.replace("'", "\\'")
        )

        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True},
        )

        return _parse_result(result)

    async def scroll_to_element(
        self,
        connection: CDPConnection,
        selector: str,
    ) -> dict:
        """Scroll an element into view.

        Useful when a target is rendered but below the fold — scrolling it
        into view lets the next screenshot actually capture it, and lets
        click/hover dispatch events at a valid viewport coordinate.

        Args:
            connection: Active CDP connection.
            selector: CSS selector for the element to reveal.

        Returns:
            Dict with new bounding rect and whether the element is now in
            the viewport.
        """
        script = SCROLL_TO_ELEMENT_SCRIPT.replace("%SELECTOR%", selector.replace("'", "\\'"))
        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True},
        )
        return _parse_result(result)

    async def scroll_within(
        self,
        connection: CDPConnection,
        scroller_selector: str | None = None,
        direction: str = "down",
        count: int = 1,
    ) -> dict:
        """Scroll a container (or the window) by N viewport-sized steps.

        Scrolls by roughly one viewport minus 100px overlap per step, so
        content stays visible across steps. Use this to walk through long
        lists, reveal virtualized rows, or scroll inside modals/panels
        that have their own overflow.

        Args:
            connection: Active CDP connection.
            scroller_selector: CSS selector for a scrollable container. Pass
                None (or empty) to scroll the main window.
            direction: "up", "down", "left", or "right".
            count: Number of viewport-sized steps (default 1).

        Returns:
            Dict with before/after scroll positions and an atEnd flag for
            each edge — lets you detect when scrolling is stuck.
        """
        sel = (scroller_selector or "").replace("'", "\\'")
        script = (
            SCROLL_WITHIN_SCRIPT.replace("%SELECTOR%", sel)
            .replace("%DIRECTION%", direction.replace("'", "\\'"))
            .replace("%COUNT%", str(count))
        )
        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True},
        )
        return _parse_result(result)

    async def get_interactive_elements_grouped(
        self,
        connection: CDPConnection,
        role_filter: str | None = None,
    ) -> dict:
        """Return interactive elements grouped by ARIA landmark and owning component.

        Instead of a flat list of hundreds of elements, returns a tree:
            landmark (main / nav / dialog / ...) →
                component (nearest named React ancestor) →
                    elements

        This makes it easier to reason about the page's interactive surface.
        Especially useful when a dialog opens — you can see the dialog's
        interactives as a distinct group instead of mixed in with the page.

        Args:
            connection: Active CDP connection.
            role_filter: Optional role filter ("button", "link", etc.) — applied
                before grouping.

        Returns:
            Dict with "total" count and "landmarks" list, each containing
            a list of components and their interactive elements.
        """
        flat = await self.get_interactive_elements(connection, role_filter=role_filter)

        # Error case: extraction returned an error dict wrapped in a list
        if len(flat) == 1 and isinstance(flat[0], dict) and "error" in flat[0]:
            return {"error": flat[0]["error"]}

        landmarks: dict[str, dict[str, list[dict]]] = {}
        for el in flat:
            lm = (el.get("landmark") or {}).get("type") or "content"
            owner_info = el.get("componentOwner") or {}
            owner = owner_info.get("name") or "<no-component>"
            landmarks.setdefault(lm, {}).setdefault(owner, []).append(el)

        result_landmarks = []
        for lm_type, components in landmarks.items():
            comp_list = []
            for comp_name, elements in components.items():
                # Pull a source from the first element's componentOwner (same for the group)
                source = None
                owner_obj = elements[0].get("componentOwner") or {}
                if owner_obj and owner_obj.get("source"):
                    source = owner_obj["source"]
                comp_list.append(
                    {
                        "component": comp_name,
                        "source": source,
                        "count": len(elements),
                        "elements": elements,
                    }
                )
            result_landmarks.append(
                {
                    "landmark": lm_type,
                    "components": comp_list,
                }
            )

        return {"total": len(flat), "landmarks": result_landmarks}

    async def wait_for_element(
        self,
        connection: CDPConnection,
        selector: str,
        timeout_ms: int = 10000,
    ) -> dict:
        """Wait for an element to appear and be visible.

        Polls every 200ms until the element exists and has layout (offsetParent
        is not null). Useful after clicking a button that triggers a navigation
        or renders a new section.

        Args:
            connection: Active CDP connection.
            selector: CSS selector to wait for.
            timeout_ms: Maximum wait time in milliseconds (default 10s).

        Returns:
            Dict with found status and elapsed time.
        """
        script = WAIT_SCRIPT.replace("%SELECTOR%", selector.replace("'", "\\'")).replace(
            "%TIMEOUT%", str(timeout_ms)
        )

        result = await connection.send(
            "Runtime.evaluate",
            {"expression": script, "returnByValue": True, "awaitPromise": True},
        )

        return _parse_result(result)


def _parse_result(result: dict) -> dict:
    """Parse a CDP Runtime.evaluate result that returns a JSON string."""
    value = result.get("result", {}).get("value")
    if value is None:
        exc = result.get("exceptionDetails", {})
        return {"error": exc.get("text", "Evaluation failed")}

    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return {"raw": value}

    return {"value": value}
