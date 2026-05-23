"""MCP server exposing Specter's browser debugging tools.

Maintains a persistent CDP connection to Firefox. Tools are designed
for an AI debugging workflow:
  1. Take a screenshot to see the visual state
  2. Check console logs for errors
  3. Check network for failed requests
  4. Evaluate JS to inspect runtime state
  5. Read DOM to check the rendered output
"""

from __future__ import annotations

import asyncio
import logging

from mcp.server.fastmcp import FastMCP

from specter.browser.a11y import A11yAuditor
from specter.browser.assertions import Asserter
from specter.browser.connection import CDPConnection
from specter.browser.console import ConsoleCapture
from specter.browser.interact import Interactor
from specter.browser.network import NetworkCapture
from specter.browser.react import ReactInspector
from specter.browser.react import diff_component_tree as _diff_component_tree_fn
from specter.browser.record_replay import InteractionRecorder
from specter.browser.runtime import Runtime
from specter.browser.state_machine import StateMachineExtractor
from specter.browser.structure import StructureAnalyzer
from specter.config import load_config

logger = logging.getLogger(__name__)

mcp = FastMCP(
    "specter",
    instructions=(
        "Specter gives you eyes into a Chromium browser via CDP: console logs, "
        "screenshots, React component tree, network activity, and page interaction.\n\n"
        "Prerequisite: Chromium running with `--remote-debugging-port=9222`.\n\n"
        "# CRITICAL DEBUGGING RULES — read these before every tool call\n\n"
        "1. **NEVER GUESS at data shapes, props, or field names.** Always inspect "
        'actual runtime data first. Example: `evaluate_js("console.log(JSON.'
        'stringify(myVar))")` → `get_console_logs()`. 10 seconds of inspection '
        "saves 10 minutes of wrong guesses.\n\n"
        "2. **Always check console and errors BEFORE trying to fix anything.** "
        "`get_console_logs(level='error')` + `get_errors()` — the answer is "
        "usually already in a stack trace pointing to the exact line.\n\n"
        "3. **Wrong data after an API call? Check the network, not the component.** "
        "`get_network_log(url_filter='/api/v1/...')` shows what the API actually "
        "returned. If status is 200 but data is wrong, the bug is in the response "
        "transformation — not the component.\n\n"
        "4. **Start with `debug_snapshot()`.** One call returns screenshot + page "
        "info + console errors + network errors + page structure. Use this first; "
        "call individual tools only to drill into specifics.\n\n"
        "5. **Prefer clicking over URL navigation for in-app flows.** The "
        "app's router controls state — clicking a link via `click_element` "
        "exercises the same code path a real user hits and preserves state. "
        "For deep-linking or skipping a multi-step flow, use "
        "`router_navigate(path)` (soft, tries link-click first then "
        "`location.href`). Use `navigate_to(url)` only for cross-origin "
        "navigation or deliberate full-page resets. **Next.js caveat:** App "
        "Router can strip query params on programmatic navigation; if query "
        "state matters, click the link instead. Always follow any navigation "
        "with `wait_for_network_idle()` before the next interaction.\n\n"
        "6. **Don't fight with navigation to debug.** If you need to see data, use "
        "`evaluate_js` to inspect it directly — don't navigate to a different page "
        "hoping to see it visually. Screenshots are for visuals; data goes through "
        "`get_component_at` / `get_redux_state` / `evaluate_js`.\n\n"
        "# When to use Specter\n\n"
        "- Frontend bugs where the error is in the browser, not the code (wrong "
        "props, stale state, failed API calls)\n"
        '- Visual debugging — "does this look right?" → take a screenshot\n'
        "- Tracing data flow — API response → Redux state → component props → "
        "rendered DOM\n"
        "- Interaction testing — click through a flow without the user doing it\n\n"
        "# Debugging workflow (8 steps)\n\n"
        "0. **Connect to the right tab**: `list_tabs()` → `connect_to_tab(id)`. "
        "Pick the app tab — NOT `pipeline-tracer.html`, NOT `devtools://` URLs.\n"
        "1. **See**: `take_screenshot` to capture visual state.\n"
        "2. **Check errors**: `get_console_logs(level='error')` + `get_errors`.\n"
        "3. **Check network**: `get_network_errors` for failed API calls.\n"
        "4. **Inspect React**: `check_react` then `get_component_at(selector)`.\n"
        "5. **Inspect state**: `get_redux_state(path)` — don't walk the store "
        "manually via `evaluate_js`; this helper handles fiber walking correctly.\n"
        "6. **Interact**: `get_interactive_elements` to find targets, then "
        "`click_element` / `fill_input` / `select_option`.\n"
        "7. **Wait**: `wait_for_network_idle()` after any navigation-triggering "
        "action, else you'll screenshot a loading spinner.\n"
        "8. **Verify**: `take_screenshot` again.\n\n"
        "# Effective patterns\n\n"
        "- **Hover before looking for action buttons.** Table row actions, edit "
        "icons, and dropdown triggers often only render on hover. If "
        "`get_interactive_elements` doesn't show what you expect, "
        "`hover_element` on the parent first, then re-query.\n"
        "- **Keyboard for custom dropdowns.** Non-`<select>` dropdowns usually "
        "need: click to open → `press_key('ArrowDown')` to navigate → "
        "`press_key('Enter')` to select.\n"
        "- **`press_key` for form completion.** `Enter` to submit, `Escape` to "
        "close modals, `Tab` between fields.\n"
        "- **Diagnostic logs via `evaluate_js`.** To trace a runtime function, "
        "inject temporary `console.log` via monkey-patch; read back with "
        "`get_console_logs`. Faster than editing source + reloading.\n"
        "- **Busy pages: use `get_interactive_elements_grouped`.** Returns a "
        "tree of landmarks → components → elements instead of a flat list of "
        "hundreds. Also: `get_interactive_elements` now includes React fiber-"
        "based discovery, so `<div onClick>` patterns with no ARIA markers "
        "show up with `discoveredVia: 'react'` and the handler names.\n"
        "- **Scroll before screenshotting/clicking.** If a target is below the "
        "fold use `scroll_to_element(selector)`. For scrollable panels, "
        "virtualized lists, or modals with internal overflow, use "
        "`scroll_within(scroller_selector, direction, count)` — check `atEnd` "
        "in the response to stop when you hit the edge.\n\n"
        "# Anti-patterns — DON'T\n\n"
        "- **Don't use `evaluate_js` for Redux state.** Use `get_redux_state(path)` "
        "— it handles multi-renderer fiber walking, store caching, safe "
        "serialization. Manual `__REACT_DEVTOOLS_GLOBAL_HOOK__` walks are the #1 "
        "wasted pattern.\n"
        "- **Don't filter duplicate DOM matches manually.** When "
        "`querySelectorAll` returns more elements than expected, use "
        "`get_elements_grouped_by_component(selector)` — groups by owning React "
        "component so you can see which view each match came from.\n"
        "- **Don't screenshot to verify data.** For \"does this have the right "
        'data?", use `get_component_at` or `evaluate_js`. Pixels lie; props '
        "don't.\n"
        "- **Don't skip console logs.** Error stack traces point at exact lines — "
        "worth more than any amount of code-reading."
    ),
)

# Singleton state — persists across tool calls within one MCP session
_connection: CDPConnection | None = None
_console: ConsoleCapture | None = None
_network: NetworkCapture | None = None
_runtime: Runtime | None = None
_react: ReactInspector | None = None
_interact: Interactor | None = None
_structure: StructureAnalyzer | None = None
_recorder: InteractionRecorder = InteractionRecorder()
_a11y: A11yAuditor = A11yAuditor()
_asserter: Asserter = Asserter()
_state_machine: StateMachineExtractor = StateMachineExtractor()
# Remembers the last successfully connected target so reconnects re-anchor
# to the same tab instead of auto-picking. None = first-ever connect.
_last_target_id: str | None = None


async def _ensure_connected():
    """Ensure we have a live CDP connection, reconnecting if needed.

    Raises ConnectionError if no tab has been explicitly anchored yet
    (_last_target_id is None). The agent must call specter_list_tabs()
    then specter_connect_to_tab(<id>) before any other tool works.
    This prevents Specter from silently auto-picking any tab.

    On reconnect (connection dropped after a prior successful connect),
    re-anchors to the previously connected target via _last_target_id.
    If that target is gone, raises ConnectionError with guidance.
    """
    global _connection, _console, _network, _runtime, _react, _interact, _structure, _last_target_id

    config = load_config()

    if _connection is not None and _connection.is_connected:
        return _connection, _console, _network, _runtime, _react, _interact, _structure

    if _last_target_id is None:
        raise ConnectionError(
            "Specter has no anchored target. "
            "Call specter_list_tabs() to see available tabs, then "
            "specter_connect_to_tab(<id>) to anchor Specter to the correct tab."
        )

    _connection = CDPConnection(config)
    _console = ConsoleCapture(config)
    _network = NetworkCapture(config)
    _runtime = Runtime(config)
    _react = ReactInspector()
    _interact = Interactor()
    _structure = StructureAnalyzer()

    _console.register(_connection)
    _network.register(_connection)

    # Re-anchor to the previously connected target.
    try:
        target = await _connection.connect(target_id=_last_target_id)
    except (ConnectionError, ValueError):
        # Previous target is gone or ID is invalid. Clear memory so the agent
        # must re-anchor explicitly via specter_connect_to_tab(<id>).
        _last_target_id = None
        raise

    _last_target_id = target.id

    # Enable all CDP domains we need
    await _console.enable(_connection)  # Runtime domain
    await _network.enable(_connection)  # Network domain
    await _connection.send("Page.enable")  # Page domain (needed for screenshots)

    logger.info("Connected to: %s (%s)", target.title, target.url)

    return _connection, _console, _network, _runtime, _react, _interact, _structure


@mcp.tool()
async def take_screenshot(
    full_page: bool = False,
    selector: str | None = None,
) -> dict:
    """Capture a screenshot of the current browser page.

    The screenshot is saved as a PNG file. Use the returned file_path
    with the Read tool to view the image — Claude Code is multimodal
    and can analyze screenshots directly.

    Args:
        full_page: If true, capture the entire scrollable page (not just viewport).
        selector: Optional CSS selector to screenshot a specific element only.

    Returns:
        Dict with file_path to the saved PNG, timestamp, and dimensions.
    """
    conn, _, _, runtime, _, _, _ = await _ensure_connected()
    try:
        return await runtime.take_screenshot(
            conn, full_page=full_page, selector=selector
        )
    except (RuntimeError, TimeoutError, OSError) as e:
        # Connection may have gone stale — force reconnect and retry once
        logger.warning("Screenshot failed (%s), reconnecting...", e)
        await _force_reconnect()
        conn, _, _, runtime, _, _, _ = await _ensure_connected()
        return await runtime.take_screenshot(
            conn, full_page=full_page, selector=selector
        )


async def _force_reconnect() -> None:
    """Force-close the current connection so _ensure_connected creates a fresh one."""
    global _connection
    if _connection:
        try:
            await _connection.disconnect()
        except Exception:
            pass
        _connection = None


@mcp.tool()
async def get_console_logs(
    level: str | None = None,
    since: float | None = None,
    limit: int = 50,
) -> list[dict]:
    """Retrieve console output from the browser.

    Captures everything written via console.log, console.warn,
    console.error, and console.info. Includes source locations
    and stack traces for errors.

    Args:
        level: Filter by level — "log", "warn", "error", "info", "debug".
        since: Only entries after this Unix timestamp.
        limit: Max entries to return (default 50, newest first).

    Returns:
        List of console entries with timestamp, level, text, source location.
    """
    _, console, _, _, _, _, _ = await _ensure_connected()
    return console.get_logs(level=level, since=since, limit=limit)


@mcp.tool()
async def get_errors(since: float | None = None, limit: int = 50) -> list[dict]:
    """Retrieve unhandled JavaScript exceptions from the browser.

    These are errors that weren't caught by try/catch or error boundaries.
    Each entry includes the error message, source file, line/column, and
    full stack trace.

    Args:
        since: Only entries after this Unix timestamp.
        limit: Max entries to return (default 50).

    Returns:
        List of exception entries with message, source, line, column, stack_trace.
    """
    _, console, _, _, _, _, _ = await _ensure_connected()
    return console.get_errors(since=since, limit=limit)


@mcp.tool()
async def get_network_errors(
    since: float | None = None,
    limit: int = 50,
    url_filter: str | None = None,
) -> list[dict]:
    """Retrieve failed HTTP requests (4xx, 5xx, and network errors).

    Useful for debugging API call failures, CORS issues, and network
    connectivity problems.

    Args:
        since: Only entries after this Unix timestamp.
        limit: Max entries to return.
        url_filter: Only URLs containing this substring (e.g., "/api/v1").

    Returns:
        List of failed network entries with method, URL, status, error text, duration.
    """
    _, _, network, _, _, _, _ = await _ensure_connected()
    return network.get_requests(
        errors_only=True, since=since, limit=limit, url_filter=url_filter
    )


@mcp.tool()
async def get_network_log(
    since: float | None = None,
    limit: int = 50,
    url_filter: str | None = None,
) -> list[dict]:
    """Retrieve all HTTP requests (not just errors).

    Useful for tracing API flow, checking request timing, and verifying
    that the right endpoints are being called.

    Args:
        since: Only entries after this Unix timestamp.
        limit: Max entries to return.
        url_filter: Only URLs containing this substring.

    Returns:
        List of all network entries with method, URL, status, duration.
    """
    _, _, network, _, _, _, _ = await _ensure_connected()
    return network.get_requests(
        errors_only=False, since=since, limit=limit, url_filter=url_filter
    )


@mcp.tool()
async def evaluate_js(expression: str) -> dict:
    """Evaluate a JavaScript expression in the browser page context.

    Runs the expression in the active page and returns the result.
    Useful for inspecting runtime state: checking variables, reading
    localStorage, querying the DOM, checking Redux state, etc.

    Examples:
      - "document.title"
      - "localStorage.getItem('token')"
      - "window.__NEXT_DATA__"
      - "document.querySelectorAll('.error-message').length"

    Args:
        expression: JavaScript expression to evaluate.

    Returns:
        Dict with type, value, and description of the result.
    """
    conn, _, _, runtime, _, _, _ = await _ensure_connected()
    return await runtime.evaluate_js(conn, expression)


@mcp.tool()
async def get_page_info() -> dict:
    """Get current page info: URL, title, document state.

    Quick way to verify which page the browser is on before running
    other debug commands.

    Returns:
        Dict with url, title, readyState.
    """
    conn, _, _, runtime, _, _, _ = await _ensure_connected()
    return await runtime.get_page_info(conn)


@mcp.tool()
async def get_dom_html(selector: str = "body", outer: bool = False) -> dict:
    """Get the rendered HTML of an element.

    Useful for checking what the browser actually rendered vs what
    the React component tree produced.

    Args:
        selector: CSS selector for the element (default: "body").
        outer: If true, return outerHTML; if false, innerHTML.

    Returns:
        Dict with the HTML content (truncated at 50KB if very large).
    """
    conn, _, _, runtime, _, _, _ = await _ensure_connected()
    return await runtime.get_dom_html(conn, selector=selector, outer=outer)


@mcp.tool()
async def list_tabs() -> list[dict]:
    """List all open browser tabs.

    Returns tab IDs, titles, and URLs. Call this first, then use
    connect_to_tab(id) to anchor Specter to the correct tab.

    This tool works WITHOUT a prior specter_connect_to_tab call — it
    intentionally bypasses the no-anchor guard so the agent can always
    discover available tabs and choose one explicitly.

    Returns:
        List of tab dicts with id, title, url, and which one is
        currently connected (if any).
    """
    config = load_config()
    # Use a temporary connection for tab discovery only — do NOT call
    # _ensure_connected() because that raises when no anchor is set,
    # which would prevent the agent from ever discovering tabs.
    temp_conn = CDPConnection(config)
    targets = await temp_conn.list_targets()
    current_id = _last_target_id
    result = []
    for t in targets:
        d = t.to_dict()
        d["connected"] = current_id is not None and t.id == current_id
        result.append(d)
    return result


@mcp.tool()
async def connect_to_tab(tab_id: str) -> dict:
    """Switch the Specter connection to a specific browser tab.

    Use list_tabs() first to see all tabs and their IDs, then call this
    with the ID of the tab you want to debug. This disconnects from the
    current tab and reconnects to the specified one. All event buffers
    (console, network) are cleared on reconnect.

    Args:
        tab_id: The tab ID from list_tabs() output.

    Returns:
        Dict with the connected tab's title and URL.
    """
    global _connection, _console, _network, _runtime, _react, _interact, _structure, _last_target_id

    config = load_config()

    # Disconnect existing connection
    if _connection and _connection.is_connected:
        await _connection.disconnect()

    # Fresh connection to the specified tab
    _connection = CDPConnection(config)
    _console = ConsoleCapture(config)
    _network = NetworkCapture(config)
    _runtime = Runtime(config)
    _react = ReactInspector()
    _interact = Interactor()
    _structure = StructureAnalyzer()

    _console.register(_connection)
    _network.register(_connection)

    target = await _connection.connect(target_id=tab_id)
    _last_target_id = target.id  # remember for auto-reconnect
    await _console.enable(_connection)
    await _network.enable(_connection)

    logger.info("Switched to tab: %s (%s)", target.title, target.url)

    return {
        "connected": True,
        "tab_id": target.id,
        "title": target.title,
        "url": target.url,
    }


@mcp.tool()
async def reload_page(ignore_cache: bool = False) -> dict:
    """Reload the current page.

    Useful after making code changes to see the updated result, or to
    reset the page state before reproducing a bug.

    Args:
        ignore_cache: If true, does a hard reload (bypasses cache, like Ctrl+Shift+R).

    Returns:
        Dict confirming the reload.
    """
    conn, _, _, _, _, _, _ = await _ensure_connected()
    await conn.send("Page.reload", {"ignoreCache": ignore_cache})
    return {"reloaded": True, "ignore_cache": ignore_cache}


@mcp.tool()
async def clear_logs() -> dict:
    """Clear all buffered console logs and network entries.

    Useful to reset the capture before reproducing a specific bug.

    Returns:
        Dict with count of entries cleared.
    """
    _, console, network, _, _, _, _ = await _ensure_connected()
    console_count = console.clear()
    network_count = network.clear()
    return {"console_cleared": console_count, "network_cleared": network_count}


# ─── React component inspection tools ─────────────────────────────────


@mcp.tool()
async def check_react() -> dict:
    """Check if React is running in development mode and what's available.

    Run this first before using React inspection tools. Reports:
    React version, renderer info, whether fiber roots exist, whether
    Redux DevTools and Next.js data are present.

    Returns:
        Dict with availability info for React, Redux, and Next.js.
    """
    conn, _, _, _, react, _, _ = await _ensure_connected()
    return await react.check_react_available(conn)


@mcp.tool()
async def get_component_tree(max_depth: int = 15, max_children: int = 50) -> dict:
    """Walk the React component tree and return the full hierarchy.

    Returns every React component with its name, source file + line,
    current props, hooks (useState values, useEffect deps, useRef values),
    and children. This is the same information the React DevTools
    "Components" panel shows.

    Only works in React development mode.

    Args:
        max_depth: Maximum tree depth to walk (default 15).
        max_children: Maximum children per component (default 50).

    Returns:
        Nested component tree with names, source, props, hooks, children.
    """
    conn, _, _, _, react, _, _ = await _ensure_connected()
    return await react.get_component_tree(
        conn, max_depth=max_depth, max_children=max_children
    )


@mcp.tool()
async def get_component_at(selector: str) -> dict:
    """Get the React component that owns a specific DOM element.

    Finds the DOM element by CSS selector, then walks up the React fiber
    tree to find the nearest component. Returns the component's name,
    source file, current props, and the chain of parent components above
    it in the tree.

    Useful for answering: "what component renders this element, and what
    props is it receiving?"

    Args:
        selector: CSS selector for the DOM element (e.g., ".quote-details",
                  "#main-content", "[data-testid='login-form']").

    Returns:
        Dict with component name, source location, props, and parent chain.
    """
    conn, _, _, _, react, _, _ = await _ensure_connected()
    return await react.get_component_at(conn, selector)


@mcp.tool()
async def get_elements_grouped_by_component(selector: str) -> dict:
    """Find elements matching a selector and group them by owning React component.

    Use this when a CSS selector returns more elements than you expect —
    common when the same data is rendered in multiple views simultaneously
    (e.g., a "verified" view and "review" view both rendering the same
    rows). Instead of a flat list of 6 ambiguous elements, this returns
    two groups: {VerifiedSourcesView: 3 rows, ReviewSourcesView: 3 rows}.

    Each element in a group includes: tag, text (first 80 chars), visible
    state, bounding rect, data-testid, and id — so you can identify
    which one to interact with.

    Args:
        selector: CSS selector to match (e.g., "[class*='SourceRow']").

    Returns:
        Dict with total count and groups keyed by component name.
    """
    conn, _, _, _, react, _, _ = await _ensure_connected()
    return await react.get_elements_grouped_by_component(conn, selector)


@mcp.tool()
async def get_redux_state(path: str = "") -> dict:
    """PREFERRED: read Redux store state. Use THIS instead of evaluate_js.

    This tool already handles the hard parts:
      - Checks window.__REDUX_STORE__, __NEXT_REDUX_WRAPPER_STORE__, window.store
      - Walks ALL React fiber roots across ALL renderers (Next.js App Router
        creates multiple roots — the store is often in a non-first root)
      - Looks for Provider.store, Provider.value.store, and stateNode.store
      - Caches the found store on window.__SPECTER_STORE__ for fast subsequent reads
      - Safely serializes nested state with depth capping

    DO NOT manually walk __REACT_DEVTOOLS_GLOBAL_HOOK__ with evaluate_js —
    this tool does it for you in one call. Reaching for evaluate_js first
    is the #1 waste of calls in Specter.

    With no path: summary of top-level state keys and their shapes.
    With a path: the value at that path (e.g., "auth.session").

    Args:
        path: Dot-separated path into the state tree (e.g., "auth.session",
              "quotes.selectedBidId"). Empty string = summary view.

    Returns:
        Dict with the state value or a summary of top-level keys.
    """
    conn, _, _, _, react, _, _ = await _ensure_connected()
    return await react.get_redux_state(conn, path=path)


@mcp.tool()
async def get_redux_actions() -> dict:
    """Get info about Redux action dispatch capabilities.

    Reports whether Redux DevTools is available and lists current
    state keys. Full action history replay requires the Redux DevTools
    browser extension.

    Returns:
        Dict with Redux DevTools status and current state shape.
    """
    conn, _, _, _, react, _, _ = await _ensure_connected()
    return await react.get_redux_actions(conn)


# ─── Browser interaction tools ─────────────────────────────────────────


@mcp.tool()
async def get_interactive_elements(role: str | None = None) -> list[dict]:
    """Get all interactive elements on the page (buttons, links, inputs, etc.).

    Returns every clickable, typeable, and selectable element with:
      - A stable CSS selector (prefers data-testid, then id, then aria-label)
      - The visible label/text
      - The element's role (button, link, textbox, checkbox, etc.)
      - Current state (value, checked, disabled)
      - Bounding box (x, y, width, height)

    Use this to understand the page's interactive surface, then use
    click_element, fill_input, or select_option to interact.

    Args:
        role: Optional filter by role ("button", "link", "textbox", etc.).

    Returns:
        List of interactive element descriptors.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.get_interactive_elements(conn, role_filter=role)


@mcp.tool()
async def get_interactive_elements_grouped(role: str | None = None) -> dict:
    """Get interactive elements grouped by ARIA landmark and owning component.

    Returns a tree:
        landmarks → components → elements

    instead of a flat list. Makes reasoning about a busy page much easier:
    dialog contents stay separate from page contents, nav links from main
    content actions, etc. Use this when the flat `get_interactive_elements`
    returns too many entries to skim.

    Each element carries the same metadata as `get_interactive_elements`,
    including `componentOwner` (nearest named React ancestor), `landmark`,
    `handlers` (if discovered via React fiber walk), and `discoveredVia`
    ("dom" vs "react").

    Args:
        role: Optional filter by role ("button", "link", "textbox", etc.)
              applied before grouping.

    Returns:
        Dict with total count and landmarks tree.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.get_interactive_elements_grouped(conn, role_filter=role)


@mcp.tool()
async def click_element(selector: str) -> dict:
    """Click an element by CSS selector.

    Scrolls the element into view, then dispatches the full mouse event
    sequence (mousedown → mouseup → click) that React expects.

    Use get_interactive_elements first to find the right selector.

    Args:
        selector: CSS selector for the element to click.

    Returns:
        Confirmation of the click or error if element not found.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.click_element(conn, selector)


@mcp.tool()
async def fill_input(selector: str, value: str) -> dict:
    """Type a value into an input field or textarea.

    Handles React controlled inputs by using the native value setter
    and dispatching both input and change events. Clears existing
    content before typing.

    Args:
        selector: CSS selector for the input element.
        value: Text to type into the field.

    Returns:
        Confirmation with the resulting value.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.fill_input(conn, selector, value)


@mcp.tool()
async def set_file_input(selector: str, file_paths: list[str]) -> dict:
    """Attach local files to an `<input type="file">` element.

    The only programmatic way to populate a file input. Three constraints
    stack: browsers gate the native picker behind real user activation,
    JS in the page can't read `file://` URLs from an `http://` origin, and
    `input.click()` from a non-user-activated context is unreliable. This
    tool bypasses all three via CDP's `DOM.setFileInputFiles` — the browser
    process reads the file from local disk and attaches it as if the user
    had picked it. A `change` event is dispatched after to guarantee React
    (and other framework) listeners fire.

    Files must exist on the **same machine** as the browser (Specter is
    local). Use absolute paths only.

    For multi-file uploads, the input must have the `multiple` attribute —
    Chromium silently drops extras otherwise.

    Args:
        selector: CSS selector for the `<input type="file">` element.
        file_paths: Absolute paths to local files (e.g.
            `["/home/user/uploads/sample.pdf"]`).

    Returns:
        Confirmation with file count + names, or error if the element
        wasn't found, isn't a file input, or any path is missing/invalid.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.set_file_input(conn, selector, file_paths)


@mcp.tool()
async def select_option(selector: str, option_value: str) -> dict:
    """Select an option from a dropdown by value or visible text.

    Args:
        selector: CSS selector for the select element.
        option_value: The option's value attribute or visible text.

    Returns:
        Confirmation or list of available options if not found.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.select_option(conn, selector, option_value)


@mcp.tool()
async def wait_for_element(selector: str, timeout_ms: int = 10000) -> dict:
    """Wait for an element to appear and become visible.

    Polls every 200ms until the element exists and has layout. Useful
    after clicking something that triggers navigation, a modal, or
    lazy-loaded content.

    Args:
        selector: CSS selector to wait for.
        timeout_ms: Maximum wait time in milliseconds (default 10000).

    Returns:
        Dict with found status and elapsed time.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.wait_for_element(conn, selector, timeout_ms=timeout_ms)


@mcp.tool()
async def scroll_to_element(selector: str) -> dict:
    """Scroll an element into the viewport.

    Useful when a target is rendered but below the fold (or inside a
    scrollable container). Screenshots only capture the viewport, and
    click_element dispatches events at the element's current screen
    coordinates — scrolling it in first is usually what you want before
    a screenshot or click.

    Args:
        selector: CSS selector for the element to scroll into view.

    Returns:
        Dict with the element's new rect and an `inViewport` flag.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.scroll_to_element(conn, selector)


@mcp.tool()
async def scroll_within(
    scroller_selector: str | None = None,
    direction: str = "down",
    count: int = 1,
) -> dict:
    """Scroll a container (or the window) by viewport-sized steps.

    Use for walking through long lists, virtualized tables, or scrollable
    panels/modals that have their own overflow (window scrolling alone
    won't reach the bottom of a modal's internal list). Each step scrolls
    by one viewport minus a 100px overlap so content stays visible across
    steps.

    Args:
        scroller_selector: CSS selector for a scrollable container. Pass
            None or "" to scroll the main window.
        direction: "up", "down", "left", or "right".
        count: Number of viewport-sized steps (default 1).

    Returns:
        Dict with before/after scroll positions, whether the scroll moved,
        and an `atEnd` object flagging which edges are hit — so you can
        stop scrolling when there's nothing more to reveal.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.scroll_within(
        conn,
        scroller_selector=scroller_selector,
        direction=direction,
        count=count,
    )


# ─── New v0.4 tools ────────────────────────────────────────────────────


@mcp.tool()
async def hover_element(selector: str) -> dict:
    """Hover over an element to reveal hidden UI.

    Many UIs show action buttons, edit controls, dropdown triggers, and
    tooltips only on hover. This dispatches mouseenter + mouseover +
    mousemove events to trigger those states.

    After hovering, call get_interactive_elements() to see the newly-
    revealed elements, or take_screenshot() to see the visual change.

    Args:
        selector: CSS selector for the element to hover over (e.g., a
                  table row, a card, a menu trigger).

    Returns:
        Confirmation of the hover or error if element not found.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.hover_element(conn, selector)


@mcp.tool()
async def press_key(
    key: str,
    modifiers: list[str] | None = None,
    selector: str | None = None,
) -> dict:
    """Press a keyboard key.

    Common uses:
      - press_key("Enter") — submit a form
      - press_key("Escape") — close a modal/dialog
      - press_key("Tab") — move focus to next element
      - press_key("ArrowDown") — navigate a dropdown
      - press_key("a", modifiers=["ctrl"]) — select all
      - press_key("Backspace") — delete character

    Args:
        key: Key name — "Enter", "Escape", "Tab", "ArrowDown", "ArrowUp",
             "Backspace", "Delete", "Space", "Home", "End", or a single
             character like "a".
        modifiers: Optional list of modifier keys: "ctrl", "shift", "alt", "meta".
        selector: Optional CSS selector to focus before pressing the key.

    Returns:
        Confirmation of the key press.
    """
    conn, _, _, _, _, interact, _ = await _ensure_connected()
    return await interact.press_key(conn, key, modifiers=modifiers, selector=selector)


@mcp.tool()
async def navigate_to(url: str) -> dict:
    """Hard navigate the current tab to a URL (full page reload).

    Uses CDP Page.navigate — equivalent to typing the URL in the address bar.
    Triggers a full page load, resets all in-memory app state, and re-runs
    every bootstrap effect. Use sparingly.

    Prefer `click_element` on a link for intra-app navigation — that uses the
    app's own router and keeps state. Use this tool when:
      - You need to land on an absolute URL that isn't linked from the
        current page.
      - You want to explicitly reset app state (log out, clear a broken
        client cache, start a session fresh).
      - You're navigating to a DIFFERENT origin.

    Caveat: for Next.js App Router apps, programmatic navigation can strip
    query params in some flows. If query state matters, prefer
    `router_navigate` or `click_element`.

    Args:
        url: Absolute URL to navigate to (e.g., "http://localhost:3000/shop").

    Returns:
        Dict with {navigated, url, type: "hard", frame_id} or {error, url}.
    """
    conn, _, _, runtime, _, _, _ = await _ensure_connected()
    return await runtime.navigate_to(conn, url)


@mcp.tool()
async def router_navigate(path: str) -> dict:
    """Client-side navigate using the app's own router.

    Softer alternative to `navigate_to`. Tries in order:
      1. Click an existing `<a href>` link that matches the target path
         (most reliable for Next.js — uses the app's Link behavior).
      2. Fall back to `window.location.href = path` for same-origin paths,
         which most SPA routers intercept as soft transitions.

    Keeps app state (Redux, React context, Zustand, etc.) when the app's
    router handles the transition. The link-click path is preferred because
    it exercises the same code path as a real user click.

    Use this when:
      - You want to deep-link to a URL without breaking app state.
      - There's no visible link to click (e.g., the target only renders
        after a multi-step flow you'd rather skip).

    Known caveats:
      - Next.js App Router may strip query params on some `location.href`
        transitions. If that bites you, `click_element` the link directly.
      - After navigating, call `wait_for_network_idle()` before the next
        interaction — the new route's data fetches are in flight.

    Args:
        path: Path + optional query (e.g., "/shop/quote/6/description"
              or "/shop/quote/6/description?source=9"). Must be same-origin.

    Returns:
        Dict with {navigated, path, router: "link-click"|"location-href"}
        or a "already on this path" no-op.
    """
    conn, _, _, runtime, _, _, _ = await _ensure_connected()
    return await runtime.router_navigate(conn, path)


@mcp.tool()
async def wait_for_network_idle(idle_ms: int = 500, timeout_ms: int = 10000) -> dict:
    """Wait until all network requests have completed.

    Monitors in-flight HTTP requests and waits until none are pending
    for at least idle_ms milliseconds. Use this after navigation or
    clicking something that triggers API calls — ensures the page is
    fully loaded before taking a screenshot or inspecting state.

    Args:
        idle_ms: How long the network must be quiet to count as idle
                 (default 500ms).
        timeout_ms: Maximum wait time (default 10000ms).

    Returns:
        Dict with idle status, remaining in-flight count, and elapsed time.
    """
    _, _, network, _, _, _, _ = await _ensure_connected()
    return await network.wait_for_idle(idle_ms=idle_ms, timeout_ms=timeout_ms)


@mcp.tool()
async def get_page_structure() -> dict | list:
    """Get the semantic structure of the current page.

    Walks the DOM using ARIA landmarks, roles, and semantic HTML to build
    a structural map. Returns a tree showing:

      - Major sections: navigation, main content, sidebars, dialogs
      - Widget state: which tab is selected, what's expanded/collapsed
      - Section contents: headings, interactive element counts, labels
      - Data-testid anchors for stable references

    This is the "what am I looking at?" tool. Use it to understand the
    page layout before deciding what to interact with. Much more useful
    than parsing a screenshot for structural understanding.

    Returns:
        Nested tree of page sections with roles, labels, and states.
    """
    conn, _, _, _, _, _, structure = await _ensure_connected()
    return await structure.get_page_structure(conn)


@mcp.tool()
async def debug_snapshot() -> dict:
    """Capture a complete debugging snapshot in one call.

    Returns everything Claude needs to understand the current page state:
      - Screenshot (file path to PNG)
      - Page URL and title
      - Console errors (last 10)
      - Network errors (last 10)
      - Page structure (semantic layout map)

    This replaces the 5-call sequence of take_screenshot + get_page_info +
    get_console_logs + get_network_errors + get_page_structure with a
    single tool call. Use this as the starting point for any debugging
    session.

    Returns:
        Dict with screenshot path, page info, errors, network errors,
        and page structure.
    """
    conn, console, network, runtime, _, _, structure = await _ensure_connected()

    # Gather everything in parallel where possible
    screenshot = await runtime.take_screenshot(conn)
    page_info = await runtime.get_page_info(conn)
    console_errors = console.get_logs(level="error", limit=10)
    exceptions = console.get_errors(limit=10)
    network_errors = network.get_requests(errors_only=True, limit=10)
    page_struct = await structure.get_page_structure(conn)

    return {
        "screenshot": screenshot.get("file_path"),
        "page": page_info,
        "console_errors": console_errors,
        "exceptions": exceptions,
        "network_errors": network_errors,
        "page_structure": page_struct,
    }


# ---------------------------------------------------------------------------
# SLICE-A tools — render_reasons, diff_component_tree, track_hooks
# ---------------------------------------------------------------------------


@mcp.tool()
async def render_reasons(
    duration_s: float = 5.0,
    component_filter: str | None = None,
) -> list[dict]:
    """Record which React components re-rendered and why during a time window.

    Hooks into React's commitFiberRoot to capture post-commit diffs of
    props and state. Returns a ranked list showing what caused each re-render.

    Requires React running in development mode (production builds strip
    the DevTools hook). Use after triggering the interaction you want to
    analyze — e.g., click a button, then review why components re-rendered.

    Args:
        duration_s: How long to record re-renders (default 5 seconds).
        component_filter: Optional substring to filter component paths
            (e.g., "App" to only see App and its children).

    Returns:
        List of {component_path, reason, prev, next, commit_batch_id}.
        commit_batch_id groups components that re-rendered in one React batch.
    """
    conn, _, _, _, react, _, _ = await _ensure_connected()
    return await react.render_reasons(
        conn, duration_s=duration_s, component_filter=component_filter
    )


@mcp.tool()
async def diff_component_tree(
    snap_a: dict | list,
    snap_b: dict | list,
) -> dict:
    """Diff two React component-tree snapshots from get_component_tree().

    Pure analysis — no Chrome connection required. Pass two snapshots
    you captured before and after a state change to see exactly what
    changed in the component tree.

    Args:
        snap_a: First snapshot (before state) from get_component_tree().
        snap_b: Second snapshot (after state) from get_component_tree().

    Returns:
        {added, removed, unmounted, props_changed} — each entry contains
        the component path and relevant props data.
    """
    return _diff_component_tree_fn(snap_a, snap_b)


@mcp.tool()
async def track_hooks(
    component_selector: str,
    duration_s: float = 5.0,
) -> list[dict]:
    """Record useState/useReducer/useRef values for one component over time.

    Polls the component's fiber hook state at regular intervals to build
    a timeline of how local state evolves during a user interaction.

    Requires React running in development mode.

    Args:
        component_selector: CSS selector for a DOM element inside the component
            you want to track (e.g., "#counter", ".my-component").
        duration_s: How long to record (default 5 seconds).

    Returns:
        Timeline: [{ts, hook_index, hook_type, value}, ...] sorted by time.
        hook_type is one of: useState, useReducer, useRef, useEffect, unknown.
    """
    conn, _, _, _, react, _, _ = await _ensure_connected()
    return await react.track_hooks(
        conn, component_selector=component_selector, duration_s=duration_s
    )


# ---------------------------------------------------------------------------
# SLICE-C tools — record_interaction, replay_interaction
# ---------------------------------------------------------------------------


@mcp.tool()
async def record_interaction(
    label: str,
    duration_s: float = 60.0,
) -> dict:
    """Record user interactions in the active browser tab for later replay.

    Injects a lightweight DOM listener that captures clicks, key presses,
    input changes, and page navigations. Events are written to a versioned
    JSONL file at ~/.local/state/khimaira/specter/recordings/<label>.jsonl.

    Captured event kinds: click, keydown (special keys only: Enter, Escape,
    Tab, Arrow keys, Backspace, Delete, Space, Home, End), input_change,
    select_change, navigate.

    Determinism limits — replay may not be identical when the UI uses:
      - CSS animations / transitions (timing-dependent state)
      - Debounced input handlers (timing-dependent dispatch)
      - Date.now() or Math.random() in business logic
      - Network latency-dependent state (API responses during interaction)
    Use this tool for deterministic flows (counter clicks, static form fills).
    See specter.browser.record_replay module docstring for details.

    Args:
        label: Recording name (alphanumeric + hyphens recommended). Becomes
            the filename: <label>.jsonl. Overwrites any prior recording with
            the same label.
        duration_s: Recording window in seconds (default 60). Recording stops
            automatically after this timeout.

    Returns:
        Dict with label, recorded_event_count, file_path, duration_s_actual.
    """
    conn, _, _, _, _, _, _ = await _ensure_connected()
    return await _recorder.record_interaction(conn, label=label, duration_s=duration_s)


@mcp.tool()
async def replay_interaction(label: str) -> dict:
    """Replay a previously recorded interaction session.

    Loads the JSONL recording, validates its schema version, then re-issues
    each recorded event through CDP in order — respecting the original
    inter-event timing.

    The page should be in the same initial state as when the recording was
    made (same URL, same data). Replay does not navigate to the recording's
    start URL automatically.

    Determinism limits: see record_interaction for full details.

    Args:
        label: Recording name to replay (must match a prior record_interaction call).

    Returns:
        On success: {label, replayed_event_count, end_state_snapshot}
            end_state_snapshot = {url, title} after the last event fires.
        On error: {error: "<description>", label: ...}
            error cases: missing recording, unknown schema version, corrupted file.
    """
    conn, _, _, _, _, _, _ = await _ensure_connected()
    return await _recorder.replay_interaction(conn, label=label)


# ---------------------------------------------------------------------------
# SLICE-B tools — a11y_audit, extract_state_machine, 3 assertion tools
# ---------------------------------------------------------------------------


@mcp.tool()
async def a11y_audit(selector: str | None = None) -> dict:
    """Run an axe-core accessibility audit against the active page.

    Injects axe-core 4.10.2 (vendored, no CDN dependency) into the page and
    runs a WCAG 2.1 AA audit. Injection is idempotent — calling twice is safe.

    Large pages may take 5-15s. Pass a selector to scope to a subtree (faster
    and reduces noise from irrelevant sections).

    Args:
        selector: Optional CSS selector to scope the audit. If None, audits
            the full document.

    Returns:
        {violations, passes, inapplicable, incomplete, axe_version}
        violations: list of {id, impact, description, nodes, helpUrl}
        On error: {"error": "<reason>"}
    """
    conn, _, _, _, _, _, _ = await _ensure_connected()
    return await _a11y.audit(conn, selector=selector)


@mcp.tool()
async def extract_state_machine(library: str = "redux") -> dict:
    """Extract Redux store state or XState machine shape from the page.

    For Redux: walks the React fiber tree to find the Redux store, then returns
    the current state shape, top-level slice names, and dispatched action count
    from Redux DevTools if available.

    For XState: detects @xstate/inspect integration (initDevtools() must have
    been called); returns machine IDs, known states, and current state per
    service. Returns a structured error — not a Python exception — if XState
    inspect is not present.

    Args:
        library: "redux" (default) or "xstate".

    Returns:
        For redux: {store_shape, slices, actions_history_count}
        For xstate: {services: {<id>: {machine_id, current_state, states}}}
        On error: {"error": "<reason>"}
    """
    conn, _, _, _, _, _, _ = await _ensure_connected()
    return await _state_machine.extract(conn, library=library)  # type: ignore[arg-type]


@mcp.tool()
async def assert_no_console_errors(since_ms: int | None = None) -> dict:
    """Assert no console errors or unhandled JS exceptions are buffered.

    Wraps specter_get_errors + specter_get_console_logs(level='error').
    Use after user interactions or page loads to confirm no regressions.

    Args:
        since_ms: Only check errors after this Unix timestamp in milliseconds.
            If None, checks all buffered errors.

    Returns:
        {ok: True, errors: [], error_count: 0} if no errors.
        {ok: False, errors: [...], error_count: N} if errors found.
    """
    _, console, _, _, _, _, _ = await _ensure_connected()
    return _asserter.assert_no_console_errors(console, since_ms=since_ms)


@mcp.tool()
async def assert_no_network_errors(
    since_ms: int | None = None,
    url_filter: str | None = None,
) -> dict:
    """Assert no failed HTTP requests (4xx, 5xx, network failures) are buffered.

    Wraps specter_get_network_errors. Use after API interactions to confirm
    all requests succeeded.

    Args:
        since_ms: Only check errors after this Unix timestamp in milliseconds.
            If None, checks all buffered errors.
        url_filter: Only requests whose URL contains this substring.

    Returns:
        {ok: True, errors: [], error_count: 0} if no errors.
        {ok: False, errors: [...], error_count: N} if errors found.
    """
    _, _, network, _, _, _, _ = await _ensure_connected()
    return _asserter.assert_no_network_errors(
        network, since_ms=since_ms, url_filter=url_filter
    )


@mcp.tool()
async def assert_element_visible(
    selector: str,
    timeout_ms: int = 5000,
) -> dict:
    """Assert that an element exists and is visually visible within a timeout.

    Uses in-browser JS polling (no asyncio.sleep). Checks: element exists,
    offsetWidth > 0, offsetHeight > 0, visibility != 'hidden', display != 'none'.

    Args:
        selector: CSS selector for the element to check.
        timeout_ms: Maximum time to wait in milliseconds (default 5000).

    Returns:
        {visible: True, found_after_ms: N} on success.
        {visible: False, found_after_ms: N, element_exists: bool} on timeout.
        {error: "..."} if CDP evaluation fails.
    """
    conn, _, _, _, _, _, _ = await _ensure_connected()
    return await _asserter.assert_element_visible(conn, selector=selector, timeout_ms=timeout_ms)
