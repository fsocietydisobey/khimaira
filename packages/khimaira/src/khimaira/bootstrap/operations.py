"""Per-operation idempotent helpers used by `khimaira bootstrap` / `khimaira sync`.

Each helper returns an `OpResult` describing what happened (created /
updated / unchanged / skipped / failed) plus a human-readable detail
line. Bootstrap and sync orchestrate these — they're not directly
invoked by users.

Idempotency contract: re-running any helper with the same args on
the same machine state is safe and produces `unchanged` after the
first successful run. This is what makes `khimaira sync` cheap to run
on a tight loop (e.g., a daily cron) without churning the filesystem.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


from khimaira.log import get_logger
from khimaira.bootstrap.schema import (
    DotfilesSpec,
    McpServerSpec,
    RepoSpec,
    SymlinkEntry,
)

log = get_logger("bootstrap.ops")

OpStatus = Literal["created", "updated", "unchanged", "skipped", "failed"]


@dataclass
class OpResult:
    """Outcome of one bootstrap operation. Status + a short detail.

    `status` drives the CLI's emoji + return code; `detail` is the
    human-readable line that follows. Together they're enough for the
    user to understand what bootstrap did without re-running with -v.
    """

    op: str
    target: str
    status: OpStatus
    detail: str = ""


def _run(
    cmd: list[str] | str, *, cwd: Path | None = None
) -> subprocess.CompletedProcess:
    """Subprocess wrapper that captures output + raises with stderr on failure.

    Uses shell=True for str commands (the install/MCP commands users
    write are shell lines, not exec-style arrays). Captures stderr so
    a failing git/uv/claude command's actual error reaches the user
    rather than being swallowed.
    """
    shell = isinstance(cmd, str)
    return subprocess.run(
        cmd,
        cwd=cwd,
        shell=shell,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# Repo clone
# ---------------------------------------------------------------------------


def ensure_repo(spec: RepoSpec, *, force: bool = False) -> OpResult:
    """Clone the repo if missing; otherwise leave it untouched.

    `force=True` deletes a non-git directory at the same path and
    re-clones (safer than refusing — common reason a path exists but
    isn't a repo is a previous partial clone).
    """
    path = spec.resolved_path()

    if path.exists():
        if (path / ".git").is_dir():
            return OpResult(
                op="clone",
                target=spec.name,
                status="unchanged",
                detail=f"already at {path}",
            )
        if not force:
            return OpResult(
                op="clone",
                target=spec.name,
                status="failed",
                detail=(
                    f"{path} exists but is not a git repo. "
                    f"Re-run with --force to wipe and re-clone."
                ),
            )
        shutil.rmtree(path)

    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone"]
    if spec.branch:
        cmd += ["--branch", spec.branch]
    cmd += [spec.url, str(path)]
    proc = _run(cmd)
    if proc.returncode != 0:
        return OpResult(
            op="clone",
            target=spec.name,
            status="failed",
            detail=f"git clone failed: {(proc.stderr or proc.stdout).strip()[:300]}",
        )
    return OpResult(
        op="clone",
        target=spec.name,
        status="created",
        detail=f"cloned {spec.url} → {path}",
    )


# ---------------------------------------------------------------------------
# Install command (uv sync, npm install, etc.)
# ---------------------------------------------------------------------------


def run_install(spec: RepoSpec) -> OpResult:
    """Run the spec's `install` command in the repo dir.

    No idempotency check at this layer — defer to the install tool
    (e.g. `uv sync` is itself idempotent). Skipping means: if the user
    didn't declare an install command, we don't run anything.
    """
    if not spec.install:
        return OpResult(
            op="install",
            target=spec.name,
            status="skipped",
            detail="no install command declared",
        )
    path = spec.resolved_path()
    if not path.is_dir():
        return OpResult(
            op="install",
            target=spec.name,
            status="failed",
            detail=f"repo path {path} doesn't exist — clone first",
        )
    proc = _run(spec.install, cwd=path)
    if proc.returncode != 0:
        return OpResult(
            op="install",
            target=spec.name,
            status="failed",
            detail=f"`{spec.install}` failed: {(proc.stderr or proc.stdout).strip()[:300]}",
        )
    return OpResult(
        op="install",
        target=spec.name,
        status="updated",
        detail=f"ran `{spec.install}` in {path}",
    )


# ---------------------------------------------------------------------------
# Dotfiles repo + symlinks
# ---------------------------------------------------------------------------


def ensure_dotfiles(spec: DotfilesSpec) -> OpResult:
    """Clone the dotfiles repo if missing.

    Distinct from `ensure_repo` because dotfiles aren't an MCP server
    — they're the source for the symlinks that follow. Same idempotency
    pattern: leave existing clones alone, refuse to clobber non-git
    dirs.
    """
    path = Path(os.path.expanduser(spec.path)).resolve()
    if path.exists():
        if (path / ".git").is_dir():
            return OpResult(
                op="dotfiles-clone",
                target=spec.repo,
                status="unchanged",
                detail=f"already at {path}",
            )
        return OpResult(
            op="dotfiles-clone",
            target=spec.repo,
            status="failed",
            detail=f"{path} exists but is not a git repo",
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "clone"]
    if spec.branch:
        cmd += ["--branch", spec.branch]
    cmd += [spec.repo, str(path)]
    proc = _run(cmd)
    if proc.returncode != 0:
        return OpResult(
            op="dotfiles-clone",
            target=spec.repo,
            status="failed",
            detail=f"git clone failed: {(proc.stderr or proc.stdout).strip()[:300]}",
        )
    return OpResult(
        op="dotfiles-clone",
        target=spec.repo,
        status="created",
        detail=f"cloned {spec.repo} → {path}",
    )


def apply_symlink(entry: SymlinkEntry, dotfiles_root: Path) -> OpResult:
    """Create/update one symlink. Idempotent.

    Behavior:
      - dest doesn't exist → create symlink, status=created
      - dest is a symlink pointing at the right place → status=unchanged
      - dest is a symlink pointing elsewhere → re-point, status=updated
      - dest is a real file/dir → back up to .bak.<ts>, then symlink,
        status=updated (with backup path in detail). Never clobber
        without preserving the original.
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
                detail=f"already → {src}",
            )
        # Re-point — replace the link, no backup needed (symlink is cheap to replace)
        dest.unlink()
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.symlink_to(src)
        return OpResult(
            op="symlink",
            target=entry.dest,
            status="updated",
            detail=f"re-pointed → {src} (was → {existing})",
        )

    if dest.exists():
        # Real file/dir at destination — back it up, then symlink.
        import time

        ts = int(time.time())
        backup = dest.with_suffix(dest.suffix + f".bak.{ts}")
        shutil.move(str(dest), str(backup))
        dest.symlink_to(src)
        return OpResult(
            op="symlink",
            target=entry.dest,
            status="updated",
            detail=f"backed up existing → {backup}, then linked → {src}",
        )

    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.symlink_to(src)
    return OpResult(
        op="symlink",
        target=entry.dest,
        status="created",
        detail=f"→ {src}",
    )


# ---------------------------------------------------------------------------
# MCP server registration with Claude Code
# ---------------------------------------------------------------------------


def _claude_mcp_list() -> tuple[bool, set[str]]:
    """Query `claude mcp list` and return (available, names).

    `available=False` means the `claude` CLI isn't on PATH — we treat
    MCP registration as skipped rather than failed (khimaira bootstrap
    is still useful even before Claude Code is installed).
    """
    if not shutil.which("claude"):
        return False, set()
    proc = _run(["claude", "mcp", "list"])
    if proc.returncode != 0:
        return True, set()
    # Output format (approximate, version-dependent):
    #   name1: ...
    #   name2: ...
    # We just want the bare names; pull the token before the first ":".
    names: set[str] = set()
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Strip leading bullet / dash if present
        if line.startswith(("- ", "* ")):
            line = line[2:]
        if ":" in line:
            names.add(line.split(":", 1)[0].strip())
        else:
            # Some versions just print the name
            tok = line.split()[0]
            names.add(tok)
    return True, names


def register_mcp(spec: McpServerSpec, *, force: bool = False) -> OpResult:
    """Register one MCP server with Claude Code at user scope.

    Idempotent: if `claude mcp list` already shows the name, skip.
    Doesn't try to compare command bodies — too brittle across Claude
    Code versions. Force re-register: pass `force=True` (calls
    `claude mcp remove <name>` first).
    """
    available, existing = _claude_mcp_list()
    if not available:
        return OpResult(
            op="mcp-register",
            target=spec.name,
            status="skipped",
            detail="`claude` CLI not on PATH — install Claude Code first",
        )

    if spec.name in existing and not force:
        return OpResult(
            op="mcp-register",
            target=spec.name,
            status="unchanged",
            detail="already registered (use --force to re-register)",
        )

    if spec.name in existing and force:
        # Best-effort removal; ignore the result so we always try add next.
        _run(["claude", "mcp", "remove", spec.name, "-s", "user"])

    # `claude mcp add <name> -s user -- <shell line>` — the command is
    # treated as a single shell line. We pass it as one argument after
    # the `--` so it survives the user's shell quoting (already done
    # in the YAML).
    proc = _run(
        [
            "claude",
            "mcp",
            "add",
            spec.name,
            "-s",
            "user",
            "--",
            "bash",
            "-lc",
            spec.command,
        ]
    )
    if proc.returncode != 0:
        return OpResult(
            op="mcp-register",
            target=spec.name,
            status="failed",
            detail=f"claude mcp add failed: {(proc.stderr or proc.stdout).strip()[:300]}",
        )
    return OpResult(
        op="mcp-register",
        target=spec.name,
        status="created",
        detail="registered with Claude Code (user scope)",
    )


# ---------------------------------------------------------------------------
# Supervisor + SPA build
# ---------------------------------------------------------------------------


def _capture_stdout(fn, *args, **kwargs) -> tuple[int, str]:
    """Run `fn(...)` and capture its stdout. Used to call khimaira's
    install-hooks / install-service in-process instead of shelling out.

    Subprocess approach was brittle: it required `khimaira` (or `python
    -m khimaira.cli`) to be locatable, which depends on PATH and venv
    install state. On uv workspaces, the khimaira package can be
    importable inside `uv run` but invisible to a fresh subprocess
    Python because the workspace member isn't installed as a
    site-packages entry — it's loaded via the .pth uv-run sets up.

    We're already inside the khimaira package — just call the function.

    Returns (return_code, captured_stdout). SystemExit gets caught and
    mapped to rc so an `argparse` parse-error inside the called command
    doesn't tear the bootstrap process down.
    """
    import io
    from contextlib import redirect_stdout

    buf = io.StringIO()
    try:
        with redirect_stdout(buf):
            rc = fn(*args, **kwargs) or 0
    except SystemExit as e:
        rc = int(e.code) if isinstance(e.code, int) else 1
    return rc, buf.getvalue().strip()


def install_claude_hooks(*, scripts_dir: str | None = None) -> OpResult:
    """(Re-)write ~/.claude/settings.json with khimaira's SessionStart /
    UserPromptSubmit / PostToolUse hooks via direct in-process call.

    Idempotent. Hooks are imported as `khimaira.hooks.<name>` modules
    via `python -m`, so the command works for both source checkout and
    wheel install — no filesystem-path-to-script required.

    Args:
        scripts_dir: legacy filesystem path override. Kept for backward
            compatibility with old khimaira installs whose scripts/hooks
            lived at workspace root. New writes ignore it; install-hooks
            uses sys.executable + module form.
    """
    import argparse

    try:
        from khimaira.cli.install_hooks import (
            SETTINGS_PATH,
            run as run_install_hooks,
        )
    except ImportError as e:
        return OpResult(
            op="claude-hooks",
            target="settings.json",
            status="failed",
            detail=f"khimaira.cli.install_hooks import failed: {e}",
        )

    args = argparse.Namespace(
        uninstall=False,
        settings_path=str(SETTINGS_PATH),
        scripts_dir=scripts_dir,  # legacy; install-hooks ignores when None
        dry_run=False,
    )
    rc, out = _capture_stdout(run_install_hooks, args)
    if rc != 0:
        return OpResult(
            op="claude-hooks",
            target="settings.json",
            status="failed",
            detail=f"install-hooks rc={rc}: {out[:300]}",
        )
    status: OpStatus = "unchanged" if "no changes" in out.lower() else "updated"
    return OpResult(
        op="claude-hooks",
        target="settings.json",
        status=status,
        detail=out.split("\n")[-1] if out else "hooks command completed",
    )


def install_supervisor(*, force: bool = False) -> OpResult:
    """Install the host-native supervisor (systemd user unit on Linux,
    launchd LaunchAgent on macOS, no-op elsewhere) via direct in-process
    call to `_cmd_install_service`. Idempotent on matching content.

    `force=True` rewrites the unit file even when contents differ from
    the existing one — needed when bumping khimaira's unit template
    across machines, or recovering from a partial install. Plumbed
    through from `khimaira bootstrap --force`.
    """
    import argparse

    try:
        from khimaira.monitor.cli import _cmd_install_service
    except ImportError as e:
        return OpResult(
            op="supervisor",
            target="khimaira-monitor",
            status="failed",
            detail=f"khimaira.monitor.cli import failed: {e}",
        )

    args = argparse.Namespace(enable=True, force=force)
    rc, out = _capture_stdout(_cmd_install_service, args)
    if rc != 0:
        return OpResult(
            op="supervisor",
            target="khimaira-monitor",
            status="failed",
            detail=f"install-service rc={rc}: {out[:300]}",
        )
    status: OpStatus = "unchanged" if "already installed" in out else "updated"
    return OpResult(
        op="supervisor",
        target="khimaira-monitor",
        status=status,
        detail=out.split("\n")[-1] if out else "supervisor command completed",
    )


def build_spa(khimaira_root: Path) -> OpResult:
    """Build the khimaira-monitor SPA bundle so the dashboard serves.

    Skips when npm isn't on PATH — caller's daemon will still serve
    the JSON API; the dashboard just 404s until the user installs
    Node and re-runs.
    """
    spa_dir = khimaira_root / "apps" / "monitor-ui"
    if not spa_dir.is_dir():
        return OpResult(
            op="spa-build",
            target="monitor-ui",
            status="skipped",
            detail=f"no SPA dir at {spa_dir}",
        )
    if not shutil.which("npm"):
        return OpResult(
            op="spa-build",
            target="monitor-ui",
            status="skipped",
            detail="npm not on PATH (install Node.js for the dashboard UI)",
        )

    # npm install (idempotent — fast no-op when node_modules is current).
    proc = _run(["npm", "install"], cwd=spa_dir)
    if proc.returncode != 0:
        return OpResult(
            op="spa-build",
            target="monitor-ui",
            status="failed",
            detail=f"npm install failed: {(proc.stderr or proc.stdout).strip()[:300]}",
        )
    proc = _run(["npm", "run", "build"], cwd=spa_dir)
    if proc.returncode != 0:
        return OpResult(
            op="spa-build",
            target="monitor-ui",
            status="failed",
            detail=f"npm run build failed: {(proc.stderr or proc.stdout).strip()[:300]}",
        )
    return OpResult(
        op="spa-build",
        target="monitor-ui",
        status="updated",
        detail=f"built {spa_dir / 'dist'}",
    )


# ---------------------------------------------------------------------------
# Dotfiles sync (git pull) — used by `khimaira sync`
# ---------------------------------------------------------------------------


def sync_dotfiles(spec: DotfilesSpec) -> OpResult:
    """git pull the dotfiles repo. Returns `unchanged` if already up-to-date.

    Distinct from ensure_dotfiles (clone): assumes the repo already
    exists. Use in `khimaira sync`, not first-run bootstrap.
    """
    path = Path(os.path.expanduser(spec.path)).resolve()
    if not (path / ".git").is_dir():
        return OpResult(
            op="dotfiles-pull",
            target=spec.repo,
            status="failed",
            detail=f"no git repo at {path} — run `khimaira bootstrap` first",
        )
    proc = _run(["git", "-C", str(path), "pull", "--ff-only"])
    if proc.returncode != 0:
        return OpResult(
            op="dotfiles-pull",
            target=spec.repo,
            status="failed",
            detail=f"git pull failed: {(proc.stderr or proc.stdout).strip()[:300]}",
        )
    out = (proc.stdout or "").strip()
    status: OpStatus = "unchanged" if "Already up to date" in out else "updated"
    return OpResult(
        op="dotfiles-pull",
        target=spec.repo,
        status=status,
        detail=out.split("\n")[-1] if out else "pull completed",
    )
