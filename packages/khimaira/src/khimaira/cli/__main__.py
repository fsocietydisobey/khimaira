"""Enables `python -m khimaira.cli ...` as an alternative to the
`khimaira` console script.

Used by bootstrap's self-referential subprocess calls (operations.py)
because `["khimaira", ...]` requires the binary to be on PATH, while
`[sys.executable, "-m", "khimaira.cli", ...]` works regardless of how
khimaira was installed — including a fresh `uv run`-only environment
where there's no PATH-visible binary yet.
"""

import sys

from khimaira.cli import main

if __name__ == "__main__":
    sys.exit(main())
