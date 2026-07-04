"""Daemon code-deployment fingerprint — makes the "edited daemon code but
didn't restart" staleness class DETECTABLE instead of silent.

The monitor daemon is a long-lived process: it loads `khimaira/monitor/**` at
boot and holds it in memory. Editing that source afterward does NOT take effect
until a restart — but a live-daemon test against the edited behavior silently
passes/fails against the OLD code, a false signal (this bit us: a reprocess fix
written at 20:05 against a daemon running since 18:32 was "verified" before the
restart that actually deployed it).

This module lets any verifier ask the daemon "is the code you're running stale
versus the source tree?" and get a mechanical yes/no via `/api/version`. Mirrors
mnemosyne's `/health` staleness fingerprint (commit 68bcac6).

Scope is `khimaira/monitor/**` — the code THIS daemon actually loads. The MCP
stdio server (`khimaira/server/**`) and the built frontend (`apps/monitor-ui`)
are separate runtimes with their own redeploy actions (reconnect / rebuild), so
they're deliberately NOT part of the daemon fingerprint.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

# .../khimaira/monitor/ — the source tree this daemon loads into memory.
_MONITOR_SRC = Path(__file__).resolve().parent
# parents[5] = workspace root (see build.py's identical derivation).
_REPO_ROOT = Path(__file__).resolve().parents[5]


def _git(*args: str) -> str | None:
    """Run a git command in the repo root; None on any failure (not a git
    checkout, git missing, timeout) so the fingerprint degrades gracefully."""
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _newest_source_mtime() -> float:
    """Newest mtime across all daemon `.py` files — catches uncommitted edits
    a commit-SHA comparison alone would miss."""
    newest = 0.0
    for p in _MONITOR_SRC.rglob("*.py"):
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return newest


def code_fingerprint() -> dict[str, Any]:
    """Snapshot of the daemon-code source state: HEAD sha, working-tree-dirty
    flag, and the newest monitor `.py` mtime."""
    porcelain = _git("status", "--porcelain")
    return {
        "git_sha": _git("rev-parse", "HEAD"),
        "git_dirty": bool(porcelain) if porcelain is not None else None,
        "source_mtime": _newest_source_mtime(),
    }


def is_stale(boot: dict[str, Any], current: dict[str, Any]) -> bool:
    """True if the daemon's loaded code (its boot fingerprint) differs from the
    current source tree — i.e. daemon code was committed (SHA moved) or edited
    (a `.py` is newer than boot) since the daemon started, and it hasn't been
    restarted to pick the change up."""
    boot_sha, cur_sha = boot.get("git_sha"), current.get("git_sha")
    if boot_sha and cur_sha and boot_sha != cur_sha:
        return True
    return current.get("source_mtime", 0.0) > boot.get("source_mtime", 0.0)
