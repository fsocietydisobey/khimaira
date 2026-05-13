"""Detect a project's dev server command. Pure heuristics — no AI.

Strategy:
  1. Look for a 'dev' script in package.json (npm/pnpm/yarn/bun)
  2. Look for FastAPI/uvicorn entries in pyproject.toml
  3. Look for `manage.py` (Django) / `bin/rails` / `app.py`
  4. Last resort: report none and let the user pass --command

The goal is "detect the obvious thing 80% of the time and let the user
override the rest." Don't try to be clever about deciding *between* multiple
candidates — surface them and let the user pick.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path


@dataclass
class DevCommand:
    """A detected dev-server invocation."""

    cmd: list[str]
    label: str          # human-readable name ("npm run dev", "uvicorn app:app")
    working_dir: str
    framework: str      # "vite", "next", "fastapi", "django", "unknown"
    expected_url_pattern: str = r"https?://(?:localhost|127\.0\.0\.1)[\w:/.\-]*"
    """Regex matched against output to detect "ready". Used by khimaira dev's
    wait_for_process call so we know when to launch the browser at the URL."""


def detect(project_path: str | Path) -> list[DevCommand]:
    """Return all plausible dev commands, ordered by confidence.

    Empty list = no detection; user must pass --command explicitly.
    """
    project = Path(project_path).resolve()
    out: list[DevCommand] = []

    out.extend(_detect_node(project))
    out.extend(_detect_python(project))

    return out


def _detect_node(project: Path) -> list[DevCommand]:
    pkg = project / "package.json"
    if not pkg.is_file():
        return []
    try:
        data = json.loads(pkg.read_text())
    except json.JSONDecodeError:
        return []

    scripts = data.get("scripts", {}) if isinstance(data.get("scripts"), dict) else {}
    if "dev" not in scripts:
        return []

    # Pick the package manager. Prefer pnpm > yarn > npm based on lockfile.
    if (project / "pnpm-lock.yaml").exists() and shutil.which("pnpm"):
        pm = "pnpm"
    elif (project / "yarn.lock").exists() and shutil.which("yarn"):
        pm = "yarn"
    elif (project / "bun.lockb").exists() and shutil.which("bun"):
        pm = "bun"
    else:
        pm = "npm"

    # Heuristic on framework — affects the "ready" URL pattern
    deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
    if "vite" in deps:
        framework = "vite"
        url_pattern = r"Local:\s+(https?://\S+)"
    elif "next" in deps:
        framework = "next"
        url_pattern = r"started server on (https?://\S+)"
    elif "@remix-run/serve" in deps or "remix" in deps:
        framework = "remix"
        url_pattern = r"https?://localhost:\d+"
    else:
        framework = "node"
        url_pattern = r"https?://(?:localhost|127\.0\.0\.1)[\w:/.\-]*"

    return [DevCommand(
        cmd=[pm, "run", "dev"],
        label=f"{pm} run dev",
        working_dir=str(project),
        framework=framework,
        expected_url_pattern=url_pattern,
    )]


def _detect_python(project: Path) -> list[DevCommand]:
    """Detect FastAPI / uvicorn / Django dev servers."""
    out: list[DevCommand] = []

    pyproject = project / "pyproject.toml"
    if pyproject.is_file():
        try:
            text = pyproject.read_text()
        except OSError:
            text = ""
        # Crude — but cheap and effective for the common case.
        if "fastapi" in text.lower() or "uvicorn" in text.lower():
            # Look for an obvious app entry
            for candidate in ("app.py", "main.py", "server.py", "src/app.py", "src/main.py"):
                p = project / candidate
                if p.is_file():
                    module = candidate.replace("/", ".").removesuffix(".py")
                    out.append(DevCommand(
                        cmd=["uvicorn", f"{module}:app", "--reload", "--host", "127.0.0.1"],
                        label=f"uvicorn {module}:app",
                        working_dir=str(project),
                        framework="fastapi",
                        expected_url_pattern=r"Uvicorn running on (https?://\S+)",
                    ))
                    break

    if (project / "manage.py").is_file():
        out.append(DevCommand(
            cmd=["python", "manage.py", "runserver"],
            label="django runserver",
            working_dir=str(project),
            framework="django",
            expected_url_pattern=r"Starting development server at (https?://\S+)",
        ))

    return out
