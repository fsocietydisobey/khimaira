#!/usr/bin/env python3
"""Legacy shim — delegates to khimaira.hooks.post_tool_use.

See scripts/hooks/session_start.py for the migration backstory.
This shim keeps pre-migration settings.json files working until the
user re-runs `khimaira install-hooks`, which rewrites the command to
`python -m khimaira.hooks.post_tool_use` and stops touching this
file.
"""

from __future__ import annotations

import sys

try:
    from khimaira.hooks.post_tool_use import main
except ImportError:
    sys.exit(0)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        sys.exit(0)
