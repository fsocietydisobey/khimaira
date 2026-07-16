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

    A "yes" requires every installed hook event, including internal-roster
    PreToolUse governance, to have its expected khimaira-marked module
    command. Legacy command forms (filesystem paths to scripts/hooks/*.py)
    report `would-update` so the user re-runs install-hooks.
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
    required = {
        "PreToolUse": "claude_internal_roster_pretool",
        "PostToolUse": "post_tool_use",
        "SessionStart": "session_start",
        "UserPromptSubmit": "user_prompt_submit",
        "SubagentStop": "subagent_stop",
    }
    missing: list[str] = []
    legacy: list[str] = []
    for event, module_basename in required.items():
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
        expected_module = f"-m khimaira.hooks.{module_basename}"
        if not any(expected_module in command for command in khimaira_cmds):
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
        detail="all 5 events use their current `python -m khimaira.hooks.` form",
    )


def check_codex_mcp_config(khimaira_root: Path, *, config_path: Path | None = None) -> OpResult:
    """Read-only check for the two managed Codex MCP server tables."""
    from khimaira.bootstrap.codex_config import (
        CodexConfigError,
        default_codex_config_path,
        merge_codex_mcp_config,
    )

    target = config_path or default_codex_config_path()
    try:
        outcome = merge_codex_mcp_config(khimaira_root, path=target, apply=False)
    except (CodexConfigError, OSError) as exc:
        return OpResult(
            op="codex-mcp-config",
            target=str(target),
            status="failed",
            detail=str(exc),
        )
    return OpResult(
        op="codex-mcp-config",
        target=str(target),
        status=outcome.status,
        detail=(
            "khimaira MCP entries match"
            if outcome.status == "unchanged"
            else "would merge khimaira and khimaira-chat MCP entries"
        ),
    )


def check_codex_hooks(*, hooks_path: Path | None = None) -> OpResult:
    """Read-only check for the four managed Codex lifecycle hooks."""
    from khimaira.bootstrap.codex_config import (
        CodexConfigError,
        default_codex_hooks_path,
        merge_codex_hooks,
    )

    target = hooks_path or default_codex_hooks_path()
    try:
        outcome = merge_codex_hooks(path=target, apply=False)
    except (CodexConfigError, OSError) as exc:
        return OpResult(
            op="codex-hooks",
            target=str(target),
            status="failed",
            detail=str(exc),
        )
    return OpResult(
        op="codex-hooks",
        target=str(target),
        status=outcome.status,
        detail=(
            "khimaira Codex hooks match"
            if outcome.status == "unchanged"
            else "would merge khimaira Codex lifecycle hooks"
        ),
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


# ---------------------------------------------------------------------------
# Sync-specific drift checks (task #66) — fetches but never merges.
# ---------------------------------------------------------------------------


def check_git_pull_repo(spec: RepoSpec) -> OpResult:
    """`khimaira sync --check` preview: what WOULD git_pull_repo do?

    Runs `git fetch` (network op — keeps origin/main fresh) then
    diffs HEAD against FETCH_HEAD locally. Returns:
      - `unchanged` / "in sync with origin"  — nothing to pull
      - `updated` / "would pull N commits"   — would-pull on apply
      - `failed` / "would refuse ff-only"    — local diverged
      - `skipped`                            — no git dir

    Mirror of `git_pull_repo` from operations.py but with --check
    semantics (no merge, no side effects on the working tree). The
    `meta` dict carries the same commits_pulled + deps_changed flags
    so the sync summary line can aggregate them in --check mode.
    """
    from khimaira.bootstrap.operations import (
        _deps_touched_between,
        _git_head,
        _run,
    )

    path = spec.resolved_path()
    if not (path / ".git").is_dir():
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="skipped",
            detail=f"no git repo at {path} — run `khimaira bootstrap` first",
        )

    current_head = _git_head(path)
    if current_head is None:
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="failed",
            detail="couldn't read current HEAD",
        )

    fetch = _run(["git", "-C", str(path), "fetch", "--quiet"])
    if fetch.returncode != 0:
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="failed",
            detail=f"git fetch failed: {(fetch.stderr or fetch.stdout).strip()[:300]}",
        )

    fetch_head_p = _run(["git", "-C", str(path), "rev-parse", "FETCH_HEAD"])
    if fetch_head_p.returncode != 0:
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="unchanged",
            detail="nothing fetched (already in sync with upstream)",
        )
    fetch_head = (fetch_head_p.stdout or "").strip()

    if current_head == fetch_head:
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="unchanged",
            detail="in sync with origin",
        )

    # Would ff-only succeed? Use merge-base to detect divergence.
    base_p = _run(["git", "-C", str(path), "merge-base", current_head, fetch_head])
    base = (base_p.stdout or "").strip()
    if base_p.returncode != 0 or not base:
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="failed",
            detail="couldn't compute merge-base; manual git inspection needed",
        )

    if base != current_head:
        # Local has commits not on FETCH_HEAD — ff-only would refuse.
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="failed",
            detail=(
                "would refuse ff-only — local has commits not on origin. "
                f"Resolve manually: cd {path} && git status"
            ),
        )

    # ff-only would succeed. Count commits + detect dep changes.
    count_p = _run(["git", "-C", str(path), "rev-list", "--count", f"{current_head}..{fetch_head}"])
    commits_pulled = 0
    if count_p.returncode == 0:
        try:
            commits_pulled = int((count_p.stdout or "0").strip())
        except ValueError:
            pass

    deps_changed = _deps_touched_between(path, current_head, fetch_head)
    detail = f"would pull {commits_pulled} commit(s)"
    if deps_changed:
        detail += " · pyproject/uv.lock would change"

    return OpResult(
        op="repo-pull",
        target=spec.name,
        status="updated",
        detail=detail,
        meta={"commits_pulled": commits_pulled, "deps_changed": deps_changed},
    )
