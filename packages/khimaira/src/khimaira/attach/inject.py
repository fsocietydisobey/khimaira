"""Venv detection + khimaira_observer file injection.

Pure file-system operations: find a project's venv, locate its
site-packages dir, copy `khimaira_observer/` and `khimaira_observer.pth`
into it. Idempotent — safe to re-run; updates files only if changed.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from khimaira.log import get_logger

log = get_logger("attach.inject")

# Directory containing the observer package source (bundled with khimaira).
_OBSERVER_TEMPLATE = Path(__file__).resolve().parent / "observer_template"
_OBSERVER_PKG = _OBSERVER_TEMPLATE / "khimaira_observer"
_OBSERVER_PTH = _OBSERVER_TEMPLATE / "khimaira_observer.pth"

# Common venv directory names, in priority order.
_VENV_DIRNAMES = (".venv", "venv", ".env", "env")

# When a project supports multiple Python versions, its site-packages dir
# is `lib/python3.X/site-packages`. We glob to find whichever exists.
DEFAULT_PYTHON_VERSION_GLOB = "python3.*"


@dataclass
class AttachResult:
    """Outcome of an attach call."""

    project_path: Path
    venv_path: Path
    site_packages: Path
    pth_written: bool          # True if .pth file was newly written or updated
    package_written: bool      # True if khimaira_observer/ was newly written or updated
    reason: str = ""           # human-readable note (e.g., "already up-to-date")


class VenvNotFound(RuntimeError):
    """No venv discoverable under the given project path."""


def detect_venv(project_path: Path) -> Path:
    """Find a project's venv directory.

    Search order: project_path/.venv → venv → .env → env. First hit wins.
    Honors a `KHIMAIRA_VENV` env override pointing at any directory.
    """
    import os

    override = os.environ.get("KHIMAIRA_VENV")
    if override:
        p = Path(override).expanduser().resolve()
        if _looks_like_venv(p):
            return p
        raise VenvNotFound(
            f"KHIMAIRA_VENV={override} doesn't look like a venv "
            "(no bin/python or pyvenv.cfg)."
        )

    project = project_path.expanduser().resolve()
    for name in _VENV_DIRNAMES:
        candidate = project / name
        if _looks_like_venv(candidate):
            return candidate

    raise VenvNotFound(
        f"No venv found under {project}. Tried {list(_VENV_DIRNAMES)}. "
        "Run your project's package manager once to create the venv "
        "(e.g., `uv sync`, `poetry install`, `python -m venv .venv`), "
        "or set KHIMAIRA_VENV to point at the venv path."
    )


def _looks_like_venv(path: Path) -> bool:
    """Heuristic: directory has a pyvenv.cfg or bin/python (POSIX) or
    Scripts/python.exe (Windows)."""
    if not path.is_dir():
        return False
    if (path / "pyvenv.cfg").is_file():
        return True
    if (path / "bin" / "python").exists():
        return True
    if (path / "Scripts" / "python.exe").exists():
        return True
    return False


def find_site_packages(venv_path: Path) -> Path:
    """Find the `site-packages` directory inside a venv. Errors loudly if
    multiple match — Python version ambiguity should never bite here."""
    candidates = list((venv_path / "lib").glob(f"{DEFAULT_PYTHON_VERSION_GLOB}/site-packages"))
    # Windows fallback
    if not candidates:
        win = venv_path / "Lib" / "site-packages"
        if win.is_dir():
            return win
    if not candidates:
        raise VenvNotFound(f"No site-packages found under {venv_path}/lib/python3.*/")
    if len(candidates) > 1:
        # Pick the newest python-version dir; warn but proceed
        candidates.sort(key=lambda p: p.parent.name, reverse=True)
        log.warning(
            "multiple site-packages in %s, picking %s (others: %s)",
            venv_path, candidates[0], [str(c) for c in candidates[1:]],
        )
    return candidates[0]


def is_attached(venv_path: Path) -> bool:
    """True iff khimaira_observer.pth exists in the venv's site-packages."""
    try:
        sp = find_site_packages(venv_path)
    except VenvNotFound:
        return False
    return (sp / "khimaira_observer.pth").is_file() and (sp / "khimaira_observer").is_dir()


def attach_project(project_path: Path | str, *, force: bool = False) -> AttachResult:
    """Inject khimaira_observer into a project's venv. Idempotent.

    Args:
        project_path: project root (containing the venv).
        force: rewrite files even if they appear up-to-date. Default False
            to avoid pointless writes when re-attaching frequently.

    Returns:
        AttachResult with project_path, venv_path, site_packages, and flags
        indicating whether each file was actually written.
    """
    project = Path(project_path).expanduser().resolve()
    if not project.is_dir():
        raise FileNotFoundError(f"project_path {project} is not a directory")

    venv = detect_venv(project)
    sp = find_site_packages(venv)

    pth_target = sp / "khimaira_observer.pth"
    pkg_target = sp / "khimaira_observer"

    pth_written = _maybe_write_pth(pth_target, force=force)
    package_written = _maybe_copy_package(pkg_target, force=force)

    log.info(
        "attached project=%s venv=%s site_packages=%s pth=%s pkg=%s",
        project, venv, sp,
        "wrote" if pth_written else "unchanged",
        "wrote" if package_written else "unchanged",
    )

    return AttachResult(
        project_path=project,
        venv_path=venv,
        site_packages=sp,
        pth_written=pth_written,
        package_written=package_written,
        reason="" if pth_written or package_written else "already up-to-date",
    )


def detach_project(project_path: Path | str) -> AttachResult:
    """Remove khimaira_observer from a project's venv. Idempotent."""
    project = Path(project_path).expanduser().resolve()
    venv = detect_venv(project)
    sp = find_site_packages(venv)

    pth_target = sp / "khimaira_observer.pth"
    pkg_target = sp / "khimaira_observer"

    pth_written = False
    package_written = False
    if pth_target.is_file():
        pth_target.unlink()
        pth_written = True
    if pkg_target.is_dir():
        shutil.rmtree(pkg_target)
        package_written = True

    log.info("detached project=%s — pth=%s pkg=%s",
             project, "removed" if pth_written else "absent",
             "removed" if package_written else "absent")

    return AttachResult(
        project_path=project,
        venv_path=venv,
        site_packages=sp,
        pth_written=pth_written,
        package_written=package_written,
        reason="" if pth_written or package_written else "nothing to remove",
    )


def _maybe_write_pth(target: Path, *, force: bool) -> bool:
    """Write the .pth file iff content differs (or force=True)."""
    expected = _OBSERVER_PTH.read_text(encoding="utf-8")
    if not force and target.is_file():
        try:
            current = target.read_text(encoding="utf-8")
            if current == expected:
                return False
        except OSError:
            pass
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(expected, encoding="utf-8")
    return True


def _maybe_copy_package(target: Path, *, force: bool) -> bool:
    """Copy khimaira_observer/ iff source has newer __init__.py (or force=True)."""
    src_init = _OBSERVER_PKG / "__init__.py"
    target_init = target / "__init__.py"

    if not force and target_init.is_file():
        try:
            if src_init.read_bytes() == target_init.read_bytes():
                return False
        except OSError:
            pass

    if target.is_dir():
        shutil.rmtree(target)
    shutil.copytree(_OBSERVER_PKG, target)
    return True
