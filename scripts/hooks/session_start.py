#!/usr/bin/env python3
"""Legacy shim — delegates to khimaira.hooks.session_start.

settings.json files written by `khimaira install-hooks` PRE-the-
khimaira.hooks-package migration point at this filesystem path. New
settings.json (post khimaira install-hooks re-run) uses
`python -m khimaira.hooks.session_start` directly and doesn't touch
this file at all.

This shim exists so users who haven't re-run install-hooks since the
migration don't have their SessionStart hook break silently. The
moment they run `khimaira install-hooks` or `khimaira bootstrap` /
`/khimaira-configure`, their settings.json gets rewritten with the
python -m form and this file becomes dead.

Plan: delete this shim after a grace period once all known machines
have migrated. `khimaira doctor` reports drift if settings.json still
references scripts/hooks/*.py — that's the signal to run install-hooks.
"""

from __future__ import annotations

import sys

try:
    from khimaira.hooks.session_start import main
except ImportError:
    # khimaira package not on sys.path for this Python — fail silently,
    # exit 0 to match the hook's never-block-Claude-Code contract.
    sys.exit(0)


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except Exception:
        # Catch-all — hooks must never bubble exceptions back to Claude Code.
        sys.exit(0)
