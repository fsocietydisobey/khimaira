"""Prune a Claude Code native auto-memory index (MEMORY.md) to a budget.

Thin CLI entry point over the reusable, tested logic in
`khimaira.claude_memory_index` (packages/khimaira/src/khimaira/). See that
module's docstring for the full design rationale.

Usage (from khimaira repo root):
    .venv/bin/python3 packages/khimaira/scripts/prune_claude_memory_index.py \\
        --index path/to/MEMORY.md --keep-entries 60 --dry-run

Equivalent, preferred form (once the package is installed/on PYTHONPATH):
    uv run python -m khimaira.claude_memory_index --index ... --keep-entries 60

SAFETY — READ BEFORE RUNNING AGAINST ANY REAL FILE:
    Never point --index at a live, shared, multi-session Claude Code
    memory file (e.g. a jeevy-roster or khimaira-roster
    `~/.claude*/projects/*/memory/MEMORY.md`) without EXPLICIT human
    confirmation first. Those files are auto-loaded by every active
    session at boot and may be concurrently written by Claude Code's
    own native auto-memory feature or by a sibling session. The
    concurrent-modification guard (on by default; see --force) reduces
    but does not eliminate the risk of racing a live writer — it is not
    a substitute for confirming no other session depends on the file
    mid-run. Test and validate this tool only against synthetic
    fixtures or throwaway copies.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add src to path so this script runs standalone, matching the existing
# backfill_member_roles.py one-off-script convention in this directory.
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from khimaira.claude_memory_index import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
