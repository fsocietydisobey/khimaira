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
          "label": "jeevy_portal",   // optional human-readable
          "kg_adapter": {            // optional — per-project KG graph adapter
            "url": "http://127.0.0.1:8001/internal/kg/graph",
            "token_env": "JEEVY_KG_ADAPTER_TOKEN"  // env-var NAME, never the secret
          }
        },
        ...
      ]
    }

The `kg_adapter` block opts a project into the generic graph viewer
(GET /api/graph/<project>). The daemon proxies to `url` with a Bearer
resolved from the env var named by `token_env` — the secret itself is
NEVER stored at rest in this file (security rule: load_dotenv +
env-var name only).
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
    """Add or update a project entry. Idempotent on (project_path, venv_path).

    Preserves an existing `kg_adapter` block across re-attach so a manually
    configured adapter survives `khimaira detach`/`attach` cycles.
    """
    data = _load()
    projects: list = data.setdefault("projects", [])
    target = str(project_path.resolve())
    venv_str = str(venv_path.resolve())

    # Preserve a prior kg_adapter (set out-of-band) when re-attaching.
    prior = next((p for p in projects if p.get("project_path") == target), None)
    prior_adapter = prior.get("kg_adapter") if prior else None

    # Replace any prior entry for this project
    projects = [p for p in projects if p.get("project_path") != target]
    entry = {
        "project_path": target,
        "venv_path": venv_str,
        "attached_at": _now_iso(),
        "label": label or project_path.name,
    }
    if prior_adapter:
        entry["kg_adapter"] = prior_adapter
    projects.insert(0, entry)
    data["projects"] = projects
    data["version"] = _VERSION
    _save(data)
    return entry


def set_kg_adapter(
    name_or_path: str, url: str, token_env: str = "", auth_header: str = ""
) -> bool:
    """Attach a `kg_adapter` block to an existing project entry.

    `name_or_path` matches against `label`, the project-path basename, or the
    full resolved project path. Returns True if a matching entry was updated.
    The `token_env` is the NAME of the env var holding the token — the secret
    itself is never written here. `auth_header` overrides the request header the
    daemon sends the token under (default `Authorization` as `Bearer <token>`;
    e.g. set `X-Internal-Key` for a project whose service-auth expects the raw
    token under a custom header).
    """
    data = _load()
    projects = data.get("projects") or []
    matched = False
    for p in projects:
        if _matches_project(p, name_or_path):
            adapter: dict[str, str] = {"url": url}
            if token_env:
                adapter["token_env"] = token_env
            if auth_header:
                adapter["auth_header"] = auth_header
            p["kg_adapter"] = adapter
            matched = True
            break
    if matched:
        data["projects"] = projects
        _save(data)
    return matched


def set_virtual_kg_adapter(
    label: str, url: str, token_env: str = "", auth_header: str = ""
) -> None:
    """Upsert a VIRTUAL registry entry that exists only to carry a kg_adapter.

    `set_kg_adapter` deliberately refuses to create entries — it only annotates
    projects that were actually attached. A virtual adapter (e.g. the daemon's
    own memory-kg routes, label `khimaira-memory`) has no project/venv on disk,
    so this helper creates a placeholder entry marked `"virtual": true` (which
    the attach supervisor skips) and then sets the adapter block on it.
    Idempotent — re-running updates the existing entry's adapter in place.
    """
    if set_kg_adapter(label, url, token_env=token_env, auth_header=auth_header):
        return
    data = _load()
    projects: list = data.setdefault("projects", [])
    projects.insert(
        0,
        {
            "project_path": "",
            "venv_path": "",
            "attached_at": _now_iso(),
            "label": label,
            "virtual": True,
        },
    )
    data["version"] = _VERSION
    _save(data)
    if not set_kg_adapter(label, url, token_env=token_env, auth_header=auth_header):
        raise RuntimeError(f"virtual kg_adapter entry for {label!r} was created but not matched")


def get_kg_adapter(name_or_path: str) -> dict | None:
    """Return the `kg_adapter` block for a project, or None if unset/unknown.

    Matches against `label`, the project-path basename, or the full path —
    the same identity the daemon's /api/graph/<project> route receives.
    """
    for p in list_attached():
        if _matches_project(p, name_or_path) and p.get("kg_adapter"):
            return p["kg_adapter"]
    return None


def _matches_project(entry: dict, name_or_path: str) -> bool:
    """True if `entry` is identified by `name_or_path` (label / basename / path)."""
    if not name_or_path:
        return False
    proj_path = entry.get("project_path") or ""
    return name_or_path in (
        entry.get("label") or "",
        Path(proj_path).name if proj_path else "",
        proj_path,
    )


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
