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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal


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

    `meta` is an optional structured payload for ops that need to
    convey more than a status + line of prose to downstream consumers
    (e.g. `git_pull_repo` reports commits_pulled + deps_changed so the
    sync runner can decide whether to fire `uv sync` and how to
    format the final summary). The CLI renderer never reads meta —
    it's runner-internal.
    """

    op: str
    target: str
    status: OpStatus
    detail: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


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


# State file recording which MCP servers khimaira itself has registered.
# Used by reconcile_mcp_drift to safely remove ONLY khimaira-managed
# entries (never touches user-managed servers from outside the profile).
_MANAGED_MCP_FILE = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "managed_mcp.json"
)


def _read_managed_mcp() -> set[str]:
    """Read the managed-MCP state file. Returns empty set on first run
    or any read error (worst case: stale entries linger; sync is
    additive-safe — they get cleaned up next time)."""
    import json

    try:
        if not _MANAGED_MCP_FILE.is_file():
            return set()
        data = json.loads(_MANAGED_MCP_FILE.read_text(encoding="utf-8"))
        names = data.get("registered_by_khimaira", []) if isinstance(data, dict) else []
        return {str(n) for n in names if n}
    except Exception:  # noqa: BLE001 — state read should never break sync
        log.warning("bootstrap: failed reading %s (ignoring)", _MANAGED_MCP_FILE)
        return set()


def _write_managed_mcp(names: set[str]) -> None:
    """Atomic-rename write of the managed-MCP set."""
    import json

    try:
        _MANAGED_MCP_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _MANAGED_MCP_FILE.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"registered_by_khimaira": sorted(names)}, indent=2) + "\n",
            encoding="utf-8",
        )
        tmp.replace(_MANAGED_MCP_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("bootstrap: failed writing %s: %s", _MANAGED_MCP_FILE, exc)


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
    # Track in managed-MCP state so reconcile_mcp_drift can later
    # safely remove this entry if the profile drops it. Read-merge-write
    # rather than blind-overwrite — other concurrent registrations
    # shouldn't clobber each other (rare; safe to assume serial here
    # since bootstrap/sync run register_mcp in a loop on one process).
    managed = _read_managed_mcp()
    if spec.name not in managed:
        managed.add(spec.name)
        _write_managed_mcp(managed)
    return OpResult(
        op="mcp-register",
        target=spec.name,
        status="created",
        detail="registered with Claude Code (user scope)",
    )


def reconcile_mcp_drift(profile_names: set[str]) -> list[OpResult]:
    """Remove MCP servers khimaira registered that are no longer in the profile.

    Inverse of register_mcp. Only removes entries tracked in the
    managed-MCP state file (`~/.local/state/khimaira/managed_mcp.json`)
    — user-managed MCP servers added outside the profile (e.g. via
    `claude mcp add personal-thing` by hand) are NEVER touched.

    Returns one OpResult per removal attempt (`updated` on success,
    `failed` if `claude mcp remove` errored). When nothing's stale,
    returns a single `unchanged` row so the report stays uniform.

    Idempotent: running on an already-converged state is a no-op.
    """
    available, existing = _claude_mcp_list()
    if not available:
        return [
            OpResult(
                op="mcp-reconcile",
                target="all",
                status="skipped",
                detail="`claude` CLI not on PATH",
            )
        ]

    managed = _read_managed_mcp()
    # Stale = khimaira-managed AND still present in claude AND not in profile
    stale = (managed & existing) - profile_names

    if not stale:
        return [
            OpResult(
                op="mcp-reconcile",
                target="all",
                status="unchanged",
                detail="no stale khimaira-managed MCP entries",
            )
        ]

    results: list[OpResult] = []
    new_managed = set(managed)
    for name in sorted(stale):
        proc = _run(["claude", "mcp", "remove", name, "-s", "user"])
        if proc.returncode != 0:
            results.append(
                OpResult(
                    op="mcp-reconcile",
                    target=name,
                    status="failed",
                    detail=(
                        f"claude mcp remove failed: "
                        f"{(proc.stderr or proc.stdout).strip()[:200]}"
                    ),
                )
            )
            continue
        new_managed.discard(name)
        results.append(
            OpResult(
                op="mcp-reconcile",
                target=name,
                status="updated",
                detail="removed stale registration (was in managed state but not in profile)",
            )
        )

    # Persist the trimmed state (only on at least one successful removal)
    if new_managed != managed:
        _write_managed_mcp(new_managed)

    return results


# ---------------------------------------------------------------------------
# Monitor daemon freshness check (task #66 v2.2)
# ---------------------------------------------------------------------------


def _monitor_active_enter_ts() -> str | None:
    """Read the khimaira-monitor systemd unit's ActiveEnterTimestamp.

    Returns the raw timestamp string (e.g. "Wed 2026-05-14 14:32:18 CDT")
    when the unit is loaded + active, None otherwise. Empty string
    (`ActiveEnterTimestamp=`) means the unit exists but isn't active.

    Linux-only — macOS uses launchd and the existing `khimaira monitor
    watch` foreground supervisor pattern is the recommended path there.
    """
    if not shutil.which("systemctl"):
        return None
    proc = _run(
        [
            "systemctl",
            "--user",
            "show",
            "khimaira-monitor",
            "--property=ActiveEnterTimestamp",
        ]
    )
    if proc.returncode != 0:
        return None
    # Output: "ActiveEnterTimestamp=Wed 2026-05-14 14:32:18 CDT"
    for line in (proc.stdout or "").splitlines():
        if line.startswith("ActiveEnterTimestamp="):
            raw = line.split("=", 1)[1].strip()
            return raw or None
    return None


def _systemd_ts_to_epoch(raw: str) -> float | None:
    """Parse systemd's ActiveEnterTimestamp format to epoch float.

    systemd uses locale-formatted timestamps like
    `"Wed 2026-05-14 14:32:18 CDT"`. We delegate parsing to `date -d`
    rather than rolling a strptime that has to know every timezone
    abbreviation. Returns None if parsing fails (skip silently —
    monitor staleness is a hint, not load-bearing).
    """
    if not raw or not shutil.which("date"):
        return None
    proc = _run(["date", "-d", raw, "+%s"])
    if proc.returncode != 0:
        return None
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return None


def check_monitor_freshness(workspace_root: Path | None) -> OpResult:
    """Compare khimaira-monitor daemon boot time to latest khimaira commit.

    If the running daemon predates the latest khimaira commit, it's
    running stale code (most often after `git pull` brings new server
    changes but the daemon hasn't been restarted). Returns:
      - `unchanged` — daemon is fresher than HEAD, no work needed
      - `updated`   — daemon is stale; suggests restart (run_sync
                       passes `auto_restart=True` to flip this into
                       an actual `restart_monitor` call)
      - `skipped`   — systemctl unavailable, unit not loaded, or
                       workspace not on disk (installed-wheel mode)

    workspace_root is the khimaira repo root (or None when running
    from an installed wheel). When None, skip — there's no commit
    timestamp to compare against.
    """
    if workspace_root is None:
        return OpResult(
            op="monitor-fresh",
            target="khimaira-monitor",
            status="skipped",
            detail="installed-wheel mode — no workspace HEAD to compare",
        )
    raw_ts = _monitor_active_enter_ts()
    if not raw_ts:
        return OpResult(
            op="monitor-fresh",
            target="khimaira-monitor",
            status="skipped",
            detail="systemctl unavailable or khimaira-monitor unit not active",
        )
    daemon_epoch = _systemd_ts_to_epoch(raw_ts)
    if daemon_epoch is None:
        return OpResult(
            op="monitor-fresh",
            target="khimaira-monitor",
            status="skipped",
            detail=f"couldn't parse systemd timestamp {raw_ts!r}",
        )

    # Latest khimaira commit timestamp (committer date, ISO8601 strict)
    proc = _run(
        ["git", "-C", str(workspace_root), "log", "-1", "--format=%ct", "HEAD"]
    )
    if proc.returncode != 0:
        return OpResult(
            op="monitor-fresh",
            target="khimaira-monitor",
            status="skipped",
            detail="couldn't read HEAD commit timestamp",
        )
    try:
        head_epoch = float((proc.stdout or "0").strip())
    except ValueError:
        return OpResult(
            op="monitor-fresh",
            target="khimaira-monitor",
            status="skipped",
            detail="HEAD timestamp parse failed",
        )

    if daemon_epoch >= head_epoch:
        return OpResult(
            op="monitor-fresh",
            target="khimaira-monitor",
            status="unchanged",
            detail="daemon was started after latest khimaira commit",
            meta={"daemon_epoch": daemon_epoch, "head_epoch": head_epoch},
        )

    age_minutes = (head_epoch - daemon_epoch) / 60
    return OpResult(
        op="monitor-fresh",
        target="khimaira-monitor",
        status="updated",
        detail=(
            f"daemon predates HEAD by ~{age_minutes:.0f}m — "
            "consider `systemctl --user restart khimaira-monitor` "
            "(or run sync with --auto-restart)"
        ),
        meta={
            "daemon_epoch": daemon_epoch,
            "head_epoch": head_epoch,
            "age_seconds": head_epoch - daemon_epoch,
        },
    )


def restart_monitor() -> OpResult:
    """Run `systemctl --user restart khimaira-monitor`.

    Best-effort: returns `failed` cleanly if systemctl errors. The
    user can still restart manually; sync doesn't gate further ops
    on this.
    """
    if not shutil.which("systemctl"):
        return OpResult(
            op="monitor-restart",
            target="khimaira-monitor",
            status="skipped",
            detail="systemctl unavailable",
        )
    proc = _run(["systemctl", "--user", "restart", "khimaira-monitor"])
    if proc.returncode != 0:
        return OpResult(
            op="monitor-restart",
            target="khimaira-monitor",
            status="failed",
            detail=(
                f"systemctl restart failed: "
                f"{(proc.stderr or proc.stdout).strip()[:200]}"
            ),
        )
    return OpResult(
        op="monitor-restart",
        target="khimaira-monitor",
        status="updated",
        detail="khimaira-monitor restarted",
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


# ---------------------------------------------------------------------------
# Sibling repo sync (git pull + dep-change detection) — task #66
# ---------------------------------------------------------------------------


def _git_head(path: Path) -> str | None:
    """Return current HEAD sha for path, or None if git lookup fails."""
    proc = _run(["git", "-C", str(path), "rev-parse", "HEAD"])
    if proc.returncode != 0:
        return None
    return (proc.stdout or "").strip() or None


def _deps_touched_between(repo_path: Path, prev_head: str, new_head: str) -> bool:
    """Return True iff pyproject.toml or uv.lock changed between two SHAs.

    Used to decide whether `khimaira sync` should fire `uv sync` after a
    pull. False on any git-side error — better to skip the dep refresh
    than to error out the whole sync.
    """
    if prev_head == new_head:
        return False
    proc = _run(
        [
            "git",
            "-C",
            str(repo_path),
            "diff",
            "--name-only",
            f"{prev_head}..{new_head}",
        ]
    )
    if proc.returncode != 0:
        return False
    for name in (proc.stdout or "").splitlines():
        name = name.strip()
        if name.endswith("pyproject.toml") or name.endswith("uv.lock"):
            return True
    return False


def git_pull_repo(spec: RepoSpec) -> OpResult:
    """git fetch + ff-only merge a sibling repo declared in the profile.

    Distinct from `sync_dotfiles`: that's hardcoded to the dotfiles
    spec; this handles any RepoSpec from `profile.repos`. Used by
    `khimaira sync` to keep sibling repos current alongside dotfiles.

    Returns an OpResult with `meta` populated:
      - `commits_pulled`: int — count of new commits merged in
      - `deps_changed`: bool — pyproject.toml or uv.lock touched

    The sync runner reads `meta` to decide whether to fire
    `uv sync --all-packages` after all repo pulls complete. The CLI
    renderer ignores `meta` (only renders status + detail).
    """
    path = spec.resolved_path()
    if not (path / ".git").is_dir():
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="skipped",
            detail=f"no git repo at {path} — run `khimaira bootstrap` first",
        )

    prev_head = _git_head(path)
    if prev_head is None:
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

    merge = _run(["git", "-C", str(path), "merge", "--ff-only", "FETCH_HEAD"])
    new_head = _git_head(path) or prev_head

    # Order matters: check merge exit code BEFORE head-equality. A
    # successful merge with no upstream changes is "Already up to
    # date" (rc=0, head unchanged). A refused ff-only merge is rc!=0,
    # head also unchanged. Both leave HEAD at prev_head, so
    # head-equality alone can't distinguish "no work needed" from
    # "diverged, refused to clobber."
    if merge.returncode != 0:
        # FETCH_HEAD diverged from local — would need a real merge or rebase.
        # Sync's contract is "don't touch local work"; surface + bail.
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="failed",
            detail=(
                "ff-only merge refused — local has commits not on origin. "
                f"Resolve manually: cd {path} && git status"
            ),
        )

    if prev_head == new_head:
        return OpResult(
            op="repo-pull",
            target=spec.name,
            status="unchanged",
            detail="already up to date",
        )

    proc = _run(
        ["git", "-C", str(path), "rev-list", "--count", f"{prev_head}..{new_head}"]
    )
    commits_pulled = 0
    if proc.returncode == 0:
        try:
            commits_pulled = int((proc.stdout or "0").strip())
        except ValueError:
            pass

    deps_changed = _deps_touched_between(path, prev_head, new_head)

    detail = f"pulled {commits_pulled} commit(s)"
    if deps_changed:
        detail += " · pyproject/uv.lock touched"

    return OpResult(
        op="repo-pull",
        target=spec.name,
        status="updated",
        detail=detail,
        meta={"commits_pulled": commits_pulled, "deps_changed": deps_changed},
    )


def check_unpushed(spec: RepoSpec) -> OpResult:
    """Report whether the repo has commits ahead of its tracking branch.

    Informational only — `khimaira sync` never auto-pushes. The user
    sees the count and decides whether to push from this machine
    (e.g. they may want to push from a different machine where the
    commits originated).

    Status semantics:
      - `unchanged` — local in sync with upstream (the common case)
      - `updated` — N commits ahead (would-be-pushed in `--check` view)
      - `skipped` — no upstream tracking, can't determine

    Never `failed` — an ahead-of-upstream state is not a failure of
    sync; it's a signal to the user.
    """
    path = spec.resolved_path()
    if not (path / ".git").is_dir():
        return OpResult(
            op="unpushed-check",
            target=spec.name,
            status="skipped",
            detail="no git repo",
        )
    proc = _run(["git", "-C", str(path), "rev-list", "--count", "@{u}..HEAD"])
    if proc.returncode != 0:
        return OpResult(
            op="unpushed-check",
            target=spec.name,
            status="skipped",
            detail="no upstream tracking branch (or detached HEAD)",
        )
    try:
        ahead = int((proc.stdout or "0").strip())
    except ValueError:
        ahead = 0
    if ahead == 0:
        return OpResult(
            op="unpushed-check",
            target=spec.name,
            status="unchanged",
            detail="in sync with origin",
        )
    return OpResult(
        op="unpushed-check",
        target=spec.name,
        status="updated",
        detail=f"{ahead} unpushed commit(s) — push from this machine if origin",
        meta={"unpushed_count": ahead},
    )


def maybe_run_uv_sync(workspace_root: Path, deps_changed: bool) -> OpResult:
    """Run `uv sync --all-packages` in workspace_root if deps_changed.

    Called by `khimaira sync` after all repo pulls; deps_changed is
    True iff at least one pulled repo had pyproject.toml or uv.lock
    touched.

    Returns `unchanged` (no work) when deps_changed is False — keeps
    the report a single coherent row instead of an asymmetric branch.
    """
    if not deps_changed:
        return OpResult(
            op="uv-sync",
            target="workspace",
            status="unchanged",
            detail="no pyproject/uv.lock changes detected",
        )
    proc = _run(["uv", "sync", "--all-packages"], cwd=workspace_root)
    if proc.returncode != 0:
        return OpResult(
            op="uv-sync",
            target="workspace",
            status="failed",
            detail=f"uv sync failed: {(proc.stderr or proc.stdout).strip()[:300]}",
        )
    return OpResult(
        op="uv-sync",
        target="workspace",
        status="updated",
        detail="workspace dependencies re-resolved",
    )
