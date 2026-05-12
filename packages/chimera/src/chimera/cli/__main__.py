"""Enables `python -m chimera.cli ...` as an alternative to the
`chimera` console script.

Used by bootstrap's self-referential subprocess calls (operations.py)
because `["chimera", ...]` requires the binary to be on PATH, while
`[sys.executable, "-m", "chimera.cli", ...]` works regardless of how
chimera was installed — including a fresh `uv run`-only environment
where there's no PATH-visible binary yet.
"""

import sys

from chimera.cli import main

if __name__ == "__main__":
    sys.exit(main())
