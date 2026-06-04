"""khimaira proxy — local Anthropic concurrency-proxy.

Eliminates the 32-session server-throttle by adding the cross-session
rate-management the CLI structurally lacks: a shared concurrency-cap
(Semaphore across all sessions) + adaptive-retry (Retry-After + jitter).

Usage:
    khimaira proxy serve            # foreground
    khimaira proxy watch            # supervised (foreground, auto-restart)
    khimaira proxy install-service  # systemd user unit

Then in every session's .env / settings:
    ANTHROPIC_BASE_URL=http://127.0.0.1:8741
    ENABLE_TOOL_SEARCH=1            # REQUIRED: BASE_URL disables MCP tool-search otherwise
"""
