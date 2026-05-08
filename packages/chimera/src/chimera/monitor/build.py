"""Auto-build helper for the monitor frontend.

The daemon serves static assets from `monitor_ui/dist/`. If `dist/` is
missing or older than the newest source under `monitor_ui/src/`, run
`npm run build` synchronously before serving.

`dist/` is gitignored — users never invoke npm directly.
"""

import shutil
import subprocess
import sys
from pathlib import Path

_MONITOR_UI = Path(__file__).resolve().parent.parent.parent.parent / "monitor_ui"


def ui_root() -> Path:
    """Absolute path to the monitor_ui workspace."""
    return _MONITOR_UI


def dist_dir() -> Path:
    return _MONITOR_UI / "dist"


def _newest_source_mtime() -> float:
    src = _MONITOR_UI / "src"
    if not src.is_dir():
        return 0.0
    newest = 0.0
    for path in src.rglob("*"):
        if path.is_file():
            mtime = path.stat().st_mtime
            if mtime > newest:
                newest = mtime
    # Also include package.json + vite.config.ts which affect build output
    for filename in ("package.json", "vite.config.ts", "tailwind.config.ts"):
        candidate = _MONITOR_UI / filename
        if candidate.is_file():
            mtime = candidate.stat().st_mtime
            if mtime > newest:
                newest = mtime
    return newest


def _dist_mtime() -> float:
    index = dist_dir() / "index.html"
    if not index.is_file():
        return 0.0
    return index.stat().st_mtime


def needs_build() -> bool:
    """True if `dist/` is missing or older than the newest source."""
    return _dist_mtime() < _newest_source_mtime()


def ensure_built() -> None:
    """Build `dist/` if missing or stale.

    Logs progress to stderr. Exits the process if `npm` is unavailable or
    the build fails — the daemon should not start with a broken UI.
    """
    if not _MONITOR_UI.is_dir():
        # Nothing to build — the frontend hasn't been scaffolded yet.
        # The server will fall back to a "frontend not built" placeholder.
        print(f"chimera monitor: monitor_ui/ not found at {_MONITOR_UI} — skipping build", file=sys.stderr)
        return

    if not needs_build():
        return

    npm = shutil.which("npm")
    if not npm:
        print("chimera monitor: `npm` not found in PATH — install Node.js to build the UI", file=sys.stderr)
        sys.exit(1)

    print(f"chimera monitor: building UI in {_MONITOR_UI}...", file=sys.stderr)

    if not (_MONITOR_UI / "node_modules").is_dir():
        result = subprocess.run([npm, "install"], cwd=_MONITOR_UI, stdin=subprocess.DEVNULL)
        if result.returncode != 0:
            print("chimera monitor: `npm install` failed", file=sys.stderr)
            sys.exit(1)

    result = subprocess.run([npm, "run", "build"], cwd=_MONITOR_UI, stdin=subprocess.DEVNULL)
    if result.returncode != 0:
        print("chimera monitor: `npm run build` failed", file=sys.stderr)
        sys.exit(1)

    print("chimera monitor: UI build complete", file=sys.stderr)
