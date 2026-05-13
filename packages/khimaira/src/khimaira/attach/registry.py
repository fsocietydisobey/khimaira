"""Registry of attached projects — `~/.local/state/khimaira/attached.json`.

The daemon's auto-reattach machinery reads this on startup and watches
the venvs of every entry. Survives daemon restarts.

Schema:
    {
      "version": 1,
      "projects": [
        {
          "project_path": "/abs/path/to/jeevy_portal",
          "venv_path": "/abs/path/to/jeevy_portal/.venv",
          "attached_at": "2026-05-09T22:00:00Z",
          "label": "jeevy_portal"   // optional human-readable
        },
        ...
      ]
    }
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from khimaira.log import get_logger

log = get_logger("attach.registry")

_STATE_DIR = Path(
    os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
) / "khimaira"
REGISTRY_FILE = _STATE_DIR / "attached.json"

_VERSION = 1


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load() -> dict:
    if not REGISTRY_FILE.exists():
        return {"version": _VERSION, "projects": []}
    try:
        data = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.warning("registry: failed to parse %s (%s) — starting fresh", REGISTRY_FILE, exc)
        return {"version": _VERSION, "projects": []}
    if not isinstance(data, dict) or "projects" not in data:
        return {"version": _VERSION, "projects": []}
    return data


def _save(data: dict) -> None:
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    tmp.replace(REGISTRY_FILE)


def list_attached() -> list[dict]:
    """All attached projects, newest-first."""
    data = _load()
    projects = data.get("projects") or []
    if not isinstance(projects, list):
        return []
    projects.sort(key=lambda p: p.get("attached_at") or "", reverse=True)
    return projects


def record_attach(project_path: Path, venv_path: Path, label: str = "") -> dict:
    """Add or update a project entry. Idempotent on (project_path, venv_path)."""
    data = _load()
    projects: list = data.setdefault("projects", [])
    target = str(project_path.resolve())
    venv_str = str(venv_path.resolve())

    # Replace any prior entry for this project
    projects = [p for p in projects if p.get("project_path") != target]
    entry = {
        "project_path": target,
        "venv_path": venv_str,
        "attached_at": _now_iso(),
        "label": label or project_path.name,
    }
    projects.insert(0, entry)
    data["projects"] = projects
    data["version"] = _VERSION
    _save(data)
    return entry


def record_detach(project_path: Path) -> bool:
    """Remove a project entry. Returns True if it was present."""
    data = _load()
    projects = data.get("projects") or []
    target = str(project_path.resolve())
    new_projects = [p for p in projects if p.get("project_path") != target]
    if len(new_projects) == len(projects):
        return False
    data["projects"] = new_projects
    _save(data)
    return True
