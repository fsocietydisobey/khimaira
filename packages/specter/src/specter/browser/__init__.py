"""Browser connection and event capture via Chrome DevTools Protocol (CDP)."""

from __future__ import annotations

import re as _re

# URLs that are known to belong to active user workspaces. Any Specter cleanup
# operation that navigates or reloads a tab MUST call is_safe_to_clean() first
# and abort if the result is False.
_DENYLIST_PATTERNS = (
    _re.compile(r"(^|://)localhost:3000(/|$)", _re.IGNORECASE),
    _re.compile(r"(^|://)127\.0\.0\.1:3000(/|$)", _re.IGNORECASE),
    _re.compile(r"jeevy", _re.IGNORECASE),
)


def is_safe_to_clean(url: str) -> bool:
    """Return True only when a URL is safe to navigate away from or reload.

    Protects active user workspaces from accidental clobber by Specter
    cleanup code. Returns False for any URL that looks like a live app tab
    (jeevy portal, localhost:3000 dev server, etc.).

    Allowed (returns True): about:blank, file:// fixture pages, empty string.
    Denied (returns False): localhost:3000/*, jeevy.*, 127.0.0.1:3000/*.

    Args:
        url: The tab's current URL string.

    Returns:
        True if it is safe to navigate/reload this URL; False otherwise.
    """
    if not url or url.startswith("about:") or url.startswith("file://"):
        return True
    return not any(p.search(url) for p in _DENYLIST_PATTERNS)
