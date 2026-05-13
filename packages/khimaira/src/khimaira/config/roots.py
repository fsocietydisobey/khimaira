"""Project roots registry — `~/.config/khimaira/roots.yaml`.

The list of project paths khimaira-monitor watches. Loaded once at import.
To pick up registry changes, restart the daemon.

Resolution order:
  1. KHIMAIRA_ROOTS_FILE env var (explicit override)
  2. $XDG_CONFIG_HOME/khimaira/roots.yaml
  3. ~/.config/khimaira/roots.yaml

Missing/malformed config is non-fatal — falls back to the khimaira repo
itself so the daemon at least has SOMETHING to watch.
"""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from khimaira.log import get_logger

log = get_logger("config.roots")

# The khimaira repo's own root — derived from this file's location.
# packages/khimaira/src/khimaira/config/roots.py → ../../../../..
_KHIMAIRA_REPO_ROOT = str(Path(__file__).resolve().parents[5])

_DEFAULT_ROOTS_FILE = (
    Path(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")))
    / "khimaira"
    / "roots.yaml"
)
ROOTS_FILE = Path(os.environ.get("KHIMAIRA_ROOTS_FILE", _DEFAULT_ROOTS_FILE))


def _load_roots(
    roots_file: Path = ROOTS_FILE,
    *,
    khimaira_repo: str = _KHIMAIRA_REPO_ROOT,
) -> list[str]:
    """Load and dedupe the project-roots registry.

    Returns absolute paths that exist on disk. Always includes the khimaira
    repo. Missing/malformed config logs a warning, then falls back to just
    the khimaira repo.
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(path_str: str) -> None:
        resolved = str(Path(os.path.expanduser(path_str)).resolve())
        if resolved in seen:
            return
        if not Path(resolved).is_dir():
            log.warning("roots: skipping %s — not a directory", path_str)
            return
        seen.add(resolved)
        out.append(resolved)

    _add(khimaira_repo)

    if not roots_file.exists():
        log.info("roots: no registry at %s — using khimaira repo only", roots_file)
        return out

    try:
        data = yaml.safe_load(roots_file.read_text()) or {}
    except yaml.YAMLError as e:
        log.warning("roots: failed to parse %s (%s) — using khimaira repo only", roots_file, e)
        return out

    raw_roots = data.get("roots") if isinstance(data, dict) else None
    if not isinstance(raw_roots, list):
        log.warning("roots: %s missing top-level `roots:` list — using khimaira repo only", roots_file)
        return out

    for entry in raw_roots:
        if isinstance(entry, str) and entry.strip():
            _add(entry.strip())

    return out


# Loaded once at import time. To pick up registry changes, restart khimaira.
ROOTS: list[str] = _load_roots()
