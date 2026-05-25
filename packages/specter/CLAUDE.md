# Project: Specter

> **Now part of the khimaira monorepo (NORTH_STAR Phase 0, 2026-05-13).**
> Specter's tools are exposed via khimaira's unified MCP server under
> source-prefixed names: `mcp__khimaira__specter_take_screenshot`,
> `mcp__khimaira__specter_get_console_logs`, etc. The standalone
> `specter serve` command below remains for backward compat and
> isolation testing, but the canonical install path is through
> khimaira (`uvx khimaira mcp`).

Browser debugging MCP server. Connects to Firefox via Chrome DevTools Protocol (CDP), captures console logs, errors, network activity, and screenshots in real time. Gives AI assistants eyes into the browser during local development.

Part of the MCP tooling suite alongside Séance (semantic code search) and Scarlet (codebase cartography) — all three now ship inside the khimaira workspace and surface through khimaira's MCP. Serena (LSP navigation, jeevy-only) remains an independent MCP server.

## Commands

```bash
uv run specter status              # Check if Firefox is reachable
uv run specter logs                # Print recent console output
uv run specter errors              # Print JS exceptions
uv run specter screenshot          # Take a screenshot
uv run specter serve               # Start MCP server (stdio)
```

## Architecture

```
src/specter/
  __init__.py
  cli.py                        # CLI entry point (Click)
  server.py                     # MCP server (FastMCP) — ~25 tools
  config.py                     # Config (debug port, screenshot dir)
  browser/
    __init__.py
    connection.py               # CDP WebSocket connection manager
    console.py                  # Console event buffer + retrieval
    network.py                  # HTTP request/response monitoring
    runtime.py                  # JS evaluation, screenshots, DOM inspection
```

## Prerequisites

Firefox must be launched with remote debugging enabled:

```bash
firefox --remote-debugging-port 9222
```

Specter connects to this port via CDP WebSocket. If Firefox isn't running with this flag, all tools will return a connection error.

## MCP Tools

| Tool | What it does |
|---|---|
| `take_screenshot` | Capture page as PNG — Claude reads it with the Read tool (multimodal) |
| `get_console_logs` | Retrieve buffered console.log/warn/error/info output |
| `get_errors` | Retrieve unhandled JS exceptions with stack traces |
| `get_network_errors` | Retrieve failed HTTP requests (4xx/5xx + network failures) |
| `get_network_log` | Retrieve all HTTP requests (for tracing API flow) |
| `evaluate_js` | Run JavaScript in the page and return the result |
| `get_page_info` | Current URL, title, document state |
| `get_dom_html` | Get rendered HTML of a CSS selector |
| `list_tabs` | List all open browser tabs |
| `clear_logs` | Reset all event buffers |
| `navigate_to` | Hard navigate via CDP Page.navigate (full reload, resets state) |
| `router_navigate` | Client-side navigate via the app's own router (preserves state) |

> The table above is a partial highlight. See `server.py` for the full tool set — includes interactive element discovery (DOM + React fiber), landmark grouping, scroll tools, React component tree inspection, Redux state reads, screenshot capture, etc.

## Conventions

### Python
- Python 3.12+. Modern syntax: `str | None`, `list[str]`, `dict[str, Any]`.
- Async throughout — CDP communication is WebSocket-based.
- Type hints on all signatures.
- Imports: stdlib → third-party → `specter.*` (absolute imports).
- Format with `black` after every change.

### CDP
- Connection is a persistent singleton across tool calls within one MCP session.
- On disconnect, reconnects automatically on next tool call.
- Event buffers are ring buffers (deque with maxlen) — old events fall off when full.
- All timestamps are Unix epoch floats from `time.time()`.

## Things to avoid

- Don't open multiple simultaneous CDP connections to the same tab (Firefox doesn't support it).
- Don't capture screenshots too frequently (each one is a full page render + encode + disk write).
- Don't buffer unlimited events — always use bounded deques.
- Don't send CDP commands without checking `is_connected` first.
- **Don't expect Specter to auto-pick a tab.** The first call in any MCP session MUST be `specter_list_tabs()` followed by `specter_connect_to_tab(<id>)`. There is no auto-pick — Specter never decides which tab to operate on; the caller always does. Any tool call before `specter_connect_to_tab` raises ConnectionError with this guidance.
- **Don't run Specter integration tests against your daily Chrome.** Integration tests require `SPECTER_TEST_PORT=9223` + a dedicated isolated Chrome instance. Without `SPECTER_TEST_PORT`, tests skip. Start the isolated instance: `bin/specter-test-chrome &` then `SPECTER_TEST_PORT=9223 uv run pytest packages/specter/tests/`. The dedicated Chrome uses `--user-data-dir=/tmp/specter-test-profile` — no shared cookies, history, or tabs with your real browser.
