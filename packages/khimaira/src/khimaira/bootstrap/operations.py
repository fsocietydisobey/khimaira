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
from datetime import UTC
from pathlib import Path
from typing import Any, Literal

from khimaira.bootstrap.schema import (
    DotfilesSpec,
    McpServerSpec,
    RepoSpec,
    SymlinkEntry,
)
from khimaira.log import get_logger

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


def _run(cmd: list[str] | str, *, cwd: Path | None = None) -> subprocess.CompletedProcess:
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
                        f"claude mcp remove failed: {(proc.stderr or proc.stdout).strip()[:200]}"
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
# Cross-machine sync audit (task #66 v2.4)
# ---------------------------------------------------------------------------

_SYNC_META_FILE = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "sync_meta.jsonl"
)


def _machine_id() -> str:
    """Stable per-machine identifier for sync audit logs.

    `socket.gethostname()` works for most setups. Anonymized via
    truncation isn't useful here — the data stays local on each
    machine's disk, never crosses the wire.
    """
    import socket

    try:
        return socket.gethostname() or "unknown"
    except Exception:  # noqa: BLE001
        return "unknown"


def log_sync_event(action: str, target: str, payload: dict[str, Any] | None = None) -> None:
    """Append one event to ~/.local/state/khimaira/sync_meta.jsonl.

    Schema:
      ts        — ISO8601 UTC of the event
      machine   — hostname (per-machine; not synced anywhere)
      action    — "sync-run" | "repo-pull" | "monitor-restart" | "install-rerun"
      target    — repo name / "all" / "khimaira-monitor"
      payload   — free-form dict (commits_pulled, etc.)

    Append-only. No daemon sync — each machine keeps its own log.
    Useful for retrospective "when did I last sync this repo on
    this machine" queries.
    """
    import json
    from datetime import datetime

    try:
        _SYNC_META_FILE.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "machine": _machine_id(),
            "action": action,
            "target": target,
            "payload": payload or {},
        }
        with _SYNC_META_FILE.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.warning("bootstrap: failed appending to %s: %s", _SYNC_META_FILE, exc)


def last_sync_event(action: str, target: str) -> dict[str, Any] | None:
    """Return the most-recent sync_meta event matching (action, target).

    None if no events recorded yet. Reads the whole jsonl (cheap;
    typical file < a few thousand lines). For larger histories we'd
    add a tail-reader; not needed in practice — sync runs aren't
    that frequent.
    """
    import json

    if not _SYNC_META_FILE.is_file():
        return None
    try:
        latest: dict[str, Any] | None = None
        with _SYNC_META_FILE.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("action") == action and rec.get("target") == target:
                    latest = rec
        return latest
    except Exception:  # noqa: BLE001
        return None


def _humanize_age(seconds: float) -> str:
    """Compact human-readable age. 30s / 5m / 2h / 3d / 2w cadence."""
    if seconds < 60:
        return f"{int(seconds)}s ago"
    if seconds < 3600:
        return f"{int(seconds / 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds / 3600)}h ago"
    if seconds < 86400 * 14:
        return f"{int(seconds / 86400)}d ago"
    return f"{int(seconds / (86400 * 7))}w ago"


# ---------------------------------------------------------------------------
# Sibling install re-run on profile-cmd change (task #66 v2.3)
# ---------------------------------------------------------------------------

_INSTALL_HASHES_FILE = (
    Path(os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state")))
    / "khimaira"
    / "install_hashes.json"
)


def _hash_install(command: str) -> str:
    """sha256-hex of the install command string. Stable across versions."""
    import hashlib

    return hashlib.sha256(command.encode("utf-8")).hexdigest()


def _read_install_hashes() -> dict[str, str]:
    """Map of repo-name → last-applied install-command hash."""
    import json

    try:
        if not _INSTALL_HASHES_FILE.is_file():
            return {}
        data = json.loads(_INSTALL_HASHES_FILE.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return {}
        return {str(k): str(v) for k, v in data.items()}
    except Exception:  # noqa: BLE001
        log.warning("bootstrap: failed reading %s (ignoring)", _INSTALL_HASHES_FILE)
        return {}


def _write_install_hashes(hashes: dict[str, str]) -> None:
    """Atomic-rename write of the install-hashes map."""
    import json

    try:
        _INSTALL_HASHES_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _INSTALL_HASHES_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(hashes, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        tmp.replace(_INSTALL_HASHES_FILE)
    except Exception as exc:  # noqa: BLE001
        log.warning("bootstrap: failed writing %s: %s", _INSTALL_HASHES_FILE, exc)


def maybe_reinstall_repo(spec: RepoSpec) -> OpResult:
    """Re-run a sibling repo's install command IF the command changed.

    Records a hash of the most recently applied install command per
    repo at `~/.local/state/khimaira/install_hashes.json`. On each
    sync:
      - empty `install:` → skipped (nothing to compare)
      - no recorded hash → BASELINE-only (record current, don't run).
        Preserves the upgrade path from pre-v2.3 khimaira where
        bootstrap already ran the install; we trust it succeeded.
      - hash matches recorded → unchanged
      - hash differs → run install, update state, return updated

    Idempotent across consecutive syncs: once installed, repeated
    syncs are no-ops unless the profile's install command is edited.
    """
    if not spec.install:
        return OpResult(
            op="repo-install",
            target=spec.name,
            status="skipped",
            detail="no install command declared in profile",
        )

    path = spec.resolved_path()
    if not path.is_dir():
        return OpResult(
            op="repo-install",
            target=spec.name,
            status="skipped",
            detail=f"repo not cloned yet at {path} — run `khimaira bootstrap` first",
        )

    current_hash = _hash_install(spec.install)
    hashes = _read_install_hashes()
    recorded = hashes.get(spec.name)

    if recorded is None:
        # First time we've seen this repo's install command at the
        # sync layer. Trust that bootstrap's earlier run was correct;
        # record the current hash as the baseline without re-running.
        hashes[spec.name] = current_hash
        _write_install_hashes(hashes)
        return OpResult(
            op="repo-install",
            target=spec.name,
            status="unchanged",
            detail="recorded install baseline (no re-run)",
        )

    if recorded == current_hash:
        return OpResult(
            op="repo-install",
            target=spec.name,
            status="unchanged",
            detail="install command unchanged since last apply",
        )

    # Hash drifted — install command was edited in the profile. Re-run.
    proc = _run(spec.install, cwd=path)
    if proc.returncode != 0:
        return OpResult(
            op="repo-install",
            target=spec.name,
            status="failed",
            detail=(f"install command failed: {(proc.stderr or proc.stdout).strip()[:300]}"),
        )
    hashes[spec.name] = current_hash
    _write_install_hashes(hashes)
    return OpResult(
        op="repo-install",
        target=spec.name,
        status="updated",
        detail="install command changed in profile — re-ran",
    )


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
    proc = _run(["git", "-C", str(workspace_root), "log", "-1", "--format=%ct", "HEAD"])
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
            detail=(f"systemctl restart failed: {(proc.stderr or proc.stdout).strip()[:200]}"),
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
    """(Re-)write ~/.claude/settings.json with khimaira's lifecycle hooks
    and internal-roster PreToolUse governance via direct in-process call.

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
        from khimaira.cli.install_hooks import SETTINGS_PATH
        from khimaira.cli.install_hooks import run as run_install_hooks
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


def install_codex_mcp_config(khimaira_root: Path, *, config_path: Path | None = None) -> OpResult:
    """Merge Codex MCP config without disturbing unrelated TOML content."""
    from khimaira.bootstrap.codex_config import (
        CodexConfigError,
        default_codex_config_path,
        merge_codex_mcp_config,
    )

    target = config_path or default_codex_config_path()
    try:
        outcome = merge_codex_mcp_config(khimaira_root, path=target)
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
            "khimaira MCP entries already match"
            if outcome.status == "unchanged"
            else "merged khimaira and khimaira-chat MCP entries"
        ),
    )


def install_codex_hooks(*, hooks_path: Path | None = None) -> OpResult:
    """Merge Codex lifecycle hooks without disturbing unrelated events."""
    from khimaira.bootstrap.codex_config import (
        CodexConfigError,
        default_codex_hooks_path,
        merge_codex_hooks,
    )

    target = hooks_path or default_codex_hooks_path()
    try:
        outcome = merge_codex_hooks(path=target)
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
            "khimaira Codex hooks already match"
            if outcome.status == "unchanged"
            else "merged khimaira Codex lifecycle hooks"
        ),
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

    proc = _run(["git", "-C", str(path), "rev-list", "--count", f"{prev_head}..{new_head}"])
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

    Informational only — `khimaira sync` never auto-pushes. v2.4
    enriches the detail with:
      - age of the oldest unpushed commit (so user can tell "is this
        a 5min WIP or did I forget to push this last week?")
      - identity of THIS machine via _machine_id() (helps disambiguate
        "is this from desktop or laptop?" when reviewing multi-
        machine state)

    Status semantics unchanged:
      - `unchanged` — local in sync with upstream
      - `updated` — N commits ahead
      - `skipped` — no upstream tracking, can't determine

    Never `failed` — an ahead-of-upstream state is a signal, not an error.
    """
    import time as _time

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

    # v2.4 — surface age of OLDEST unpushed commit + this machine's id
    oldest_age_s: float | None = None
    ts_proc = _run(
        [
            "git",
            "-C",
            str(path),
            "log",
            "@{u}..HEAD",
            "--format=%ct",
            "--reverse",
        ]
    )
    if ts_proc.returncode == 0:
        lines = (ts_proc.stdout or "").strip().splitlines()
        if lines:
            try:
                oldest_ts = int(lines[0].strip())
                oldest_age_s = _time.time() - oldest_ts
            except ValueError:
                pass

    machine = _machine_id()
    detail_parts = [f"{ahead} unpushed commit(s)"]
    if oldest_age_s is not None and oldest_age_s > 0:
        detail_parts.append(f"oldest {_humanize_age(oldest_age_s)}")
    detail_parts.append(f"on `{machine}`")
    detail = " · ".join(detail_parts)

    meta: dict[str, Any] = {"unpushed_count": ahead, "machine": machine}
    if oldest_age_s is not None:
        meta["oldest_age_seconds"] = oldest_age_s

    return OpResult(
        op="unpushed-check",
        target=spec.name,
        status="updated",
        detail=detail,
        meta=meta,
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


# ---------------------------------------------------------------------------
# PyPI upgrade — site-packages-mode sync
# ---------------------------------------------------------------------------


def check_and_upgrade_khimaira(
    *,
    auto_upgrade: bool = False,
    prompt_fn=None,
) -> OpResult:
    """Site-packages branch of `khimaira sync`: check PyPI, upgrade if newer.

    Behavior:
      1. Read installed `khimaira.__version__`.
      2. Fetch latest from PyPI.
      3. If PyPI returns nothing (network error, etc.) → skipped.
      4. If same or older → unchanged.
      5. If newer:
         - `auto_upgrade=True` → run the upgrade subprocess.
         - `auto_upgrade=False` + interactive tty → prompt; user can
           accept (run upgrade) or decline (skipped with suggestion).
         - `auto_upgrade=False` + non-interactive (cron/pipe) →
           skipped with explicit "rerun with --auto-upgrade" hint.

    `prompt_fn` is injectable for tests (defaults to `input`).

    Returns an OpResult with op="package-upgrade", target="khimaira".
    """
    import sys as _sys

    from khimaira import __version__
    from khimaira.bootstrap import install_mode

    if prompt_fn is None:
        prompt_fn = input

    latest = install_mode.check_pypi_version("khimaira")
    if latest is None:
        return OpResult(
            op="package-upgrade",
            target="khimaira",
            status="skipped",
            detail="PyPI version check failed (offline or rate-limited?)",
        )

    if not install_mode.is_newer_version(__version__, latest):
        return OpResult(
            op="package-upgrade",
            target="khimaira",
            status="unchanged",
            detail=f"already on {__version__} (latest: {latest})",
            meta={"current": __version__, "latest": latest},
        )

    # Newer release available — decide whether to upgrade.
    tool = install_mode.detect_upgrade_tool()
    siblings = install_mode.discover_installed_siblings()
    packages = ["khimaira", *siblings]

    if not auto_upgrade:
        # Only prompt when stdin is an actual tty — cron/pipe should
        # skip rather than block forever waiting on input.
        if not _sys.stdin.isatty():
            return OpResult(
                op="package-upgrade",
                target="khimaira",
                status="skipped",
                detail=(
                    f"newer release available: {__version__} → {latest}. "
                    f"Rerun with `khimaira sync --auto-upgrade` to apply."
                ),
                meta={"current": __version__, "latest": latest, "tool": tool},
            )
        try:
            reply = (
                prompt_fn(f"khimaira {__version__} → {latest} available. Upgrade now? [Y/n] ")
                .strip()
                .lower()
            )
        except (EOFError, KeyboardInterrupt):
            reply = "n"
        if reply and reply not in ("y", "yes", ""):
            return OpResult(
                op="package-upgrade",
                target="khimaira",
                status="skipped",
                detail=f"upgrade declined by user (latest: {latest})",
                meta={"current": __version__, "latest": latest},
            )

    ok, output = install_mode.run_upgrade(tool, packages)
    if not ok:
        return OpResult(
            op="package-upgrade",
            target="khimaira",
            status="failed",
            detail=f"upgrade subprocess failed: {output[:300]}",
            meta={"current": __version__, "latest": latest, "tool": tool},
        )
    return OpResult(
        op="package-upgrade",
        target="khimaira",
        status="updated",
        detail=f"upgraded {__version__} → {latest} via {tool}",
        meta={
            "current": __version__,
            "latest": latest,
            "tool": tool,
            "packages_upgraded": packages,
        },
    )
