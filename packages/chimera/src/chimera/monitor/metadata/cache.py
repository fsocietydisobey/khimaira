"""Cache file I/O for project metadata.

Files live at `${XDG_CACHE_HOME:-~/.cache}/chimera/monitor/<slug>.yaml`.
The slug is derived from the project's absolute path so two projects
with the same name in different directories don't collide.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

import yaml

from chimera.log import get_logger

from .schema import ProjectMetadata

log = get_logger("monitor.metadata.cache")

CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
) / "chimera" / "monitor"


def _slug(project_path: Path) -> str:
    """Stable filename per project — name plus short hash of absolute path."""
    name = project_path.name or "project"
    digest = hashlib.sha256(str(project_path.resolve()).encode()).hexdigest()[:8]
    return f"{name}-{digest}"


def cache_path(project_path: Path) -> Path:
    return CACHE_DIR / f"{_slug(project_path)}.yaml"


def newest_source_mtime(project_path: Path, file_globs: tuple[str, ...] = ("**/*.py",)) -> float:
    """Find the newest mtime across the project's source. Used to detect
    when a cached scan is stale.

    We don't walk dependency directories — node_modules, .venv etc. are
    skipped to avoid blowing up the watermark on every dependency reinstall.
    """
    skip = {".venv", "venv", "node_modules", ".git", "__pycache__", "site-packages", ".next", "dist", "build"}
    newest = 0.0
    for glob in file_globs:
        for path in project_path.rglob(glob.split("/")[-1]):
            if any(part in skip for part in path.parts):
                continue
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime > newest:
                newest = mtime
    return newest


def load(project_path: Path) -> ProjectMetadata | None:
    """Return the cached metadata for `project_path`, or None if missing/invalid."""
    path = cache_path(project_path)
    if not path.is_file():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError) as exc:
        log.warning("metadata cache unreadable at %s: %s", path, exc)
        return None
    try:
        return ProjectMetadata.model_validate(data)
    except Exception as exc:  # pydantic ValidationError + anything else
        log.warning("metadata cache schema invalid at %s: %s", path, exc)
        return None


def save(metadata: ProjectMetadata) -> Path:
    """Write metadata to the cache file. Creates parent dirs as needed."""
    path = cache_path(Path(metadata.project_path))
    path.parent.mkdir(parents=True, exist_ok=True)
    serialized = metadata.model_dump(mode="json", exclude_none=True)
    # yaml.safe_dump default flow style is messy; force block style.
    text = yaml.safe_dump(serialized, sort_keys=False, default_flow_style=False)
    path.write_text(text, encoding="utf-8")
    log.info("wrote metadata cache: %s", path)
    return path


def is_stale(metadata: ProjectMetadata | None, project_path: Path) -> bool:
    """True if a fresh scan is needed (no cache, or source has changed since)."""
    if metadata is None:
        return True
    current = newest_source_mtime(project_path)
    # Tiny tolerance for filesystem mtime jitter
    return current > metadata.source_mtime_max + 0.5
