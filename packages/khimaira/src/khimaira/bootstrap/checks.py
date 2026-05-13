"""Read-only drift checks for `khimaira bootstrap --check` / `khimaira doctor`.

Each function mirrors a bootstrap operation but ONLY inspects local
state — never writes, never clones, never installs. Returns the same
OpResult shape as operations.py but with `current` instead of
`unchanged`, and `would-*` instead of `created` / `updated`.

This lets the user audit profile-vs-machine drift without applying
anything: "what would `khimaira bootstrap` do right now?"
"""

from __future__ import annotations

import json
import os
import shutil
from importlib import resources
from pathlib import Path

from khimaira.bootstrap.operations import OpResult, _claude_mcp_list
from khimaira.bootstrap.schema import (
    DotfilesSpec,
    McpServerSpec,
    RepoSpec,
    SymlinkEntry,
)


def check_dotfiles(spec: DotfilesSpec) -> OpResult:
    """Is the dotfiles repo cloned at the declared path?

    Doesn't check git remote URL — that's brittle (the user may
    legitimately point a different fork at the same path).
    """
    path = Path(os.path.expanduser(spec.path)).resolve()
    if (path / ".git").is_dir():
        return OpResult(
            op="dotfiles-clone",
            target=spec.repo,
            status="unchanged",
            detail=f"present at {path}",
        )
    if path.exists():
        return OpResult(
            op="dotfiles-clone",
            target=spec.repo,
            status="failed",
            detail=f"{path} exists but isn't a git repo",
        )
    return OpResult(
        op="dotfiles-clone",
        target=spec.repo,
        status="created",  # i.e. would-create
        detail=f"would clone → {path}",
    )


def check_symlink(entry: SymlinkEntry, dotfiles_root: Path) -> OpResult:
    """Is the symlink in place pointing at the right source?

    Mirrors apply_symlink's logic but stops before mutating anything.
    """
    src = (dotfiles_root / entry.src).resolve()
    dest = Path(os.path.expanduser(entry.dest))

    if not src.exists():
        return OpResult(
            op="symlink",
            target=entry.dest,
            status="failed",
            detail=f"source missing in dotfiles: {src}",
        )

    if dest.is_symlink():
        existing = os.readlink(dest)
        if Path(existing).resolve() == src:
            return OpResult(
                op="symlink",
                target=entry.dest,
                status="unchanged",
                detail=f"→ {src}",
            )
        return OpResult(
            op="symlink",
            target=entry.dest,
            status="updated",  # i.e. would-update
            detail=f"would re-point: currently → {existing}, should → {src}",
        )

    if dest.exists():
        return OpResult(
            op="symlink",
            target=entry.dest,
            status="updated",
            detail=f"would back up existing file then link → {src}",
        )

    return OpResult(
        op="symlink",
        target=entry.dest,
        status="created",
        detail=f"would create → {src}",
    )


def check_repo(spec: RepoSpec) -> OpResult:
    """Is the repo cloned at its resolved path?"""
    path = spec.resolved_path()
    if (path / ".git").is_dir():
        return OpResult(
            op="clone",
            target=spec.name,
            status="unchanged",
            detail=f"present at {path}",
        )
    if path.exists():
        return OpResult(
            op="clone",
            target=spec.name,
            status="failed",
            detail=f"{path} exists but isn't a git repo (re-run with --force to recover)",
        )
    return OpResult(
        op="clone",
        target=spec.name,
        status="created",
        detail=f"would clone {spec.url} → {path}",
    )


def check_mcp(spec: McpServerSpec) -> OpResult:
    """Is the MCP server registered with Claude Code (user scope)?"""
    available, existing = _claude_mcp_list()
    if not available:
        return OpResult(
            op="mcp-register",
            target=spec.name,
            status="skipped",
            detail="claude CLI not on PATH — can't check registration",
        )
    if spec.name in existing:
        return OpResult(
            op="mcp-register",
            target=spec.name,
            status="unchanged",
            detail="registered with Claude Code",
        )
    return OpResult(
        op="mcp-register",
        target=spec.name,
        status="created",
        detail="would register with Claude Code (user scope)",
    )


def check_claude_hooks() -> OpResult:
    """Are khimaira's hooks present in settings.json and using the
    current `python -m khimaira.hooks.<name>` command form?

    A "yes" requires all three hook events (PostToolUse, SessionStart,
    UserPromptSubmit) to have a khimaira-marked entry whose command
    starts with `<some-python> -m khimaira.hooks.`. Legacy command
    forms (filesystem paths to scripts/hooks/*.py) report `would-update`
    so the user re-runs install-hooks.
    """
    try:
        from khimaira.cli.install_hooks import _KHIMAIRA_MARKER, SETTINGS_PATH
    except ImportError as e:
        return OpResult(
            op="claude-hooks",
            target="settings.json",
            status="failed",
            detail=f"khimaira.cli.install_hooks import failed: {e}",
        )

    settings_path = Path(SETTINGS_PATH)
    if not settings_path.is_file():
        return OpResult(
            op="claude-hooks",
            target=str(settings_path),
            status="created",
            detail="would create settings.json with khimaira hooks",
        )

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        return OpResult(
            op="claude-hooks",
            target=str(settings_path),
            status="failed",
            detail=f"settings.json unreadable: {e}",
        )

    hooks = settings.get("hooks", {}) if isinstance(settings, dict) else {}
    required = ("PostToolUse", "SessionStart", "UserPromptSubmit")
    missing: list[str] = []
    legacy: list[str] = []
    for event in required:
        entries = hooks.get(event) or []
        if not isinstance(entries, list):
            missing.append(event)
            continue
        khimaira_cmds: list[str] = []
        for matcher in entries:
            if not isinstance(matcher, dict):
                continue
            for h in matcher.get("hooks") or []:
                if isinstance(h, dict) and h.get(_KHIMAIRA_MARKER):
                    cmd = h.get("command") or ""
                    khimaira_cmds.append(cmd)
        if not khimaira_cmds:
            missing.append(event)
            continue
        # Look for at least one command using the current form.
        if not any("-m khimaira.hooks." in c for c in khimaira_cmds):
            legacy.append(event)

    if missing:
        return OpResult(
            op="claude-hooks",
            target=str(settings_path),
            status="created",
            detail=f"would add khimaira hooks for: {', '.join(missing)}",
        )
    if legacy:
        return OpResult(
            op="claude-hooks",
            target=str(settings_path),
            status="updated",
            detail=(
                f"would migrate legacy hook command form for: "
                f"{', '.join(legacy)} (currently uses scripts/hooks paths)"
            ),
        )
    return OpResult(
        op="claude-hooks",
        target=str(settings_path),
        status="unchanged",
        detail="all 3 events use the current `python -m khimaira.hooks.` form",
    )


def check_supervisor() -> OpResult:
    """Is the host-native supervisor unit installed AND its content
    current?

    'Active' is intentionally not checked — bootstrap installs +
    enables, but a unit can be intentionally stopped without the
    setup being wrong. Drift is about whether the unit file exists
    and matches what khimaira would write today.
    """
    import sys

    try:
        if sys.platform == "linux":
            from khimaira.monitor.cli import _systemd_unit_content, _systemd_unit_path

            path = _systemd_unit_path()
            label = "systemd user unit"
        elif sys.platform == "darwin":
            from khimaira.monitor.cli import _launchd_plist_content, _launchd_plist_path

            path = _launchd_plist_path()
            label = "launchd LaunchAgent"
        else:
            return OpResult(
                op="supervisor",
                target="khimaira-monitor",
                status="skipped",
                detail=f"no native supervisor for {sys.platform}",
            )
    except ImportError as e:
        return OpResult(
            op="supervisor",
            target="khimaira-monitor",
            status="failed",
            detail=f"khimaira.monitor.cli import failed: {e}",
        )

    if not path.is_file():
        return OpResult(
            op="supervisor",
            target=str(path),
            status="created",
            detail=f"would write {label} → {path}",
        )

    existing = path.read_text(encoding="utf-8")
    if sys.platform == "linux":
        expected = _systemd_unit_content()
    else:
        expected = _launchd_plist_content()

    if existing == expected:
        return OpResult(
            op="supervisor",
            target=str(path),
            status="unchanged",
            detail=f"{label} content matches",
        )
    return OpResult(
        op="supervisor",
        target=str(path),
        status="updated",
        detail=f"would rewrite {label} (content differs from current template)",
    )


def check_spa(khimaira_root: Path) -> OpResult:
    """Has the SPA dashboard been built? Checks for dist/ presence —
    doesn't try to diff against source to determine staleness."""
    spa_dir = khimaira_root / "apps" / "monitor-ui"
    if not spa_dir.is_dir():
        return OpResult(
            op="spa-build",
            target="monitor-ui",
            status="skipped",
            detail=f"no SPA dir at {spa_dir}",
        )
    dist = spa_dir / "dist"
    if dist.is_dir() and (dist / "index.html").is_file():
        return OpResult(
            op="spa-build",
            target=str(dist),
            status="unchanged",
            detail="dist/ present",
        )
    if not shutil.which("npm"):
        return OpResult(
            op="spa-build",
            target=str(spa_dir),
            status="skipped",
            detail="npm not on PATH — install Node.js to build",
        )
    return OpResult(
        op="spa-build",
        target=str(spa_dir),
        status="created",
        detail="would build SPA",
    )
