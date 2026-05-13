"""Bootstrap + sync runners — orchestrate the per-op helpers into the
two top-level workflows.

`run_bootstrap(profile)` — first-run on a fresh machine. Order:
  1. dotfiles clone (if any)
  2. apply symlinks
  3. repo clones + installs
  4. MCP server registrations
  5. Claude Code hooks (re-writes settings.json with local hook paths)
  6. supervisor install
  7. SPA build

`run_sync(profile)` — ongoing across machines. Order:
  1. dotfiles git pull
  2. apply symlinks (idempotent — picks up new entries added since last bootstrap)
  3. repo pulls + (re-)installs
  4. MCP server registrations (skips already-registered)

Both return a `RunReport` summarizing every operation. Use the report
to compute a return code (`0` if no `failed`; `1` if any failed) and
to render a digest at the end.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from khimaira.log import get_logger
from khimaira.bootstrap import checks
from khimaira.bootstrap import operations as ops
from khimaira.bootstrap.schema import Profile

log = get_logger("bootstrap.runner")


@dataclass
class RunReport:
    """Aggregated outcome of a bootstrap or sync run.

    `results` is in the order operations ran — useful for replaying
    the run mentally when something fails. `had_failures` is a
    derived convenience for the caller's exit code path.
    """

    results: list[ops.OpResult] = field(default_factory=list)

    @property
    def had_failures(self) -> bool:
        return any(r.status == "failed" for r in self.results)

    @property
    def summary(self) -> dict[str, int]:
        """Counts per status. Used by the CLI to render the
        'X created, Y unchanged, Z failed' tail."""
        out: dict[str, int] = {}
        for r in self.results:
            out[r.status] = out.get(r.status, 0) + 1
        return out


def _khimaira_repo_root() -> Path | None:
    """Locate the khimaira source checkout if we're running from one.

    Used to find apps/monitor-ui/ for the SPA build. Returns None
    when running as an installed wheel (no SPA build available in
    that case — wheel ships pre-built dist via package data).
    """
    # __file__ = .../khimaira/packages/khimaira/src/khimaira/bootstrap/runner.py
    # parents[5] = khimaira/ (workspace root)
    here = Path(__file__).resolve()
    candidate = here.parents[5]
    if (candidate / "apps" / "monitor-ui").is_dir():
        return candidate
    return None


def run_bootstrap(profile: Profile, *, force: bool = False) -> RunReport:
    """First-run bootstrap. See module docstring for order."""
    report = RunReport()

    # --- 1. dotfiles clone ---
    dotfiles_root: Path | None = None
    if profile.dotfiles:
        r = ops.ensure_dotfiles(profile.dotfiles)
        report.results.append(r)
        if r.status != "failed":
            dotfiles_root = Path(os.path.expanduser(profile.dotfiles.path)).resolve()

    # --- 2. symlinks ---
    if profile.dotfiles and dotfiles_root and dotfiles_root.is_dir():
        for entry in profile.dotfiles.symlinks:
            report.results.append(ops.apply_symlink(entry, dotfiles_root))

    # --- 3. repos + installs ---
    for spec in profile.repos:
        clone_result = ops.ensure_repo(spec, force=force)
        report.results.append(clone_result)
        # Skip install if clone failed — running install in a non-existent
        # or partial dir wastes time + produces a confusing second error.
        if clone_result.status not in ("failed",):
            report.results.append(ops.run_install(spec))

    # --- 4. MCP servers ---
    for mcp in profile.mcp_servers:
        report.results.append(ops.register_mcp(mcp, force=force))

    # --- 5. Claude Code hooks (settings.json) ---
    if profile.install_claude_hooks:
        # No scripts_dir override needed since the khimaira.hooks
        # subpackage migration — install-hooks now writes
        # `python -m khimaira.hooks.<name>` commands that work for
        # source checkouts AND wheel installs identically.
        report.results.append(ops.install_claude_hooks())

    # --- 6. supervisor ---
    if profile.supervisor.auto_install:
        report.results.append(ops.install_supervisor(force=force))

    # --- 6. SPA build ---
    if profile.spa_build:
        root = _khimaira_repo_root()
        if root is None:
            report.results.append(
                ops.OpResult(
                    op="spa-build",
                    target="monitor-ui",
                    status="skipped",
                    detail="running from installed wheel — SPA ships pre-built",
                )
            )
        else:
            report.results.append(ops.build_spa(root))

    return report


def run_sync(profile: Profile, *, force: bool = False) -> RunReport:
    """Ongoing sync. Pulls dotfiles + re-applies the manifest.

    Differs from bootstrap in two ways:
      - dotfiles: `git pull` (not clone)
      - repos: `git pull` each before re-running install (TODO when needed)

    For now, sync reuses bootstrap's operations after the dotfiles
    pull — they're already idempotent. Repo pulls land in a follow-up
    when the maintainer surfaces a use case for "update siblings on
    every sync" (default: explicit, via `cd ~/dev/<repo> && git pull`).
    """
    report = RunReport()

    # --- dotfiles pull ---
    if profile.dotfiles:
        r = ops.sync_dotfiles(profile.dotfiles)
        report.results.append(r)
        if r.status == "failed":
            # Don't try to apply symlinks against a possibly-stale
            # dotfiles tree if the pull itself errored. Surface that
            # one failure and stop — user fixes git state, re-runs.
            return report

    # Re-apply symlinks (picks up any new entries added since last bootstrap).
    if profile.dotfiles:
        dotfiles_root = Path(os.path.expanduser(profile.dotfiles.path)).resolve()
        if dotfiles_root.is_dir():
            for entry in profile.dotfiles.symlinks:
                report.results.append(ops.apply_symlink(entry, dotfiles_root))

    # Re-register MCP servers (idempotent — skips already-registered).
    for mcp in profile.mcp_servers:
        report.results.append(ops.register_mcp(mcp, force=force))

    # Re-apply Claude Code hooks. Idempotent at the install-hooks layer
    # — and necessary on sync because a khimaira pull may have shipped
    # new hook modules.
    if profile.install_claude_hooks:
        report.results.append(ops.install_claude_hooks())

    return report


def check_bootstrap(profile: Profile) -> RunReport:
    """Read-only drift report: what would `run_bootstrap` do right now?

    Mirrors run_bootstrap's order but uses the no-side-effect checks
    in khimaira.bootstrap.checks. Status semantics:
      - `unchanged` → already in desired state
      - `created` / `updated` → would-create / would-update on apply
      - `skipped` → can't be checked here (e.g. install command,
        claude CLI missing) — defers judgment to apply time
      - `failed` → drift can't be resolved without intervention
        (e.g. non-git dir blocking a clone path)

    No daemon dependency: every check is local fs / settings.json
    inspection. Cheap to run in a tight loop (e.g. `khimaira doctor`).
    """
    report = RunReport()

    # --- 1. dotfiles + symlinks ---
    dotfiles_root: Path | None = None
    if profile.dotfiles:
        r = checks.check_dotfiles(profile.dotfiles)
        report.results.append(r)
        candidate = Path(os.path.expanduser(profile.dotfiles.path)).resolve()
        if candidate.is_dir():
            dotfiles_root = candidate
            for entry in profile.dotfiles.symlinks:
                report.results.append(checks.check_symlink(entry, dotfiles_root))
        else:
            # Can't check symlinks against a non-existent dotfiles dir;
            # the clone itself is the gating drift.
            for entry in profile.dotfiles.symlinks:
                report.results.append(
                    ops.OpResult(
                        op="symlink",
                        target=entry.dest,
                        status="skipped",
                        detail="can't check until dotfiles is cloned",
                    )
                )

    # --- 2. repos ---
    for spec in profile.repos:
        report.results.append(checks.check_repo(spec))
        # Skip install check (uv sync is itself idempotent, no cheap
        # way to dry-check whether anything would change).
        report.results.append(
            ops.OpResult(
                op="install",
                target=spec.name,
                status="skipped",
                detail="install commands not dry-checkable; defers to apply time",
            )
        )

    # --- 3. MCP servers ---
    for mcp in profile.mcp_servers:
        report.results.append(checks.check_mcp(mcp))

    # --- 4. Claude Code hooks ---
    if profile.install_claude_hooks:
        report.results.append(checks.check_claude_hooks())

    # --- 5. supervisor ---
    if profile.supervisor.auto_install:
        report.results.append(checks.check_supervisor())

    # --- 6. SPA build ---
    if profile.spa_build:
        root = _khimaira_repo_root()
        if root is None:
            report.results.append(
                ops.OpResult(
                    op="spa-build",
                    target="monitor-ui",
                    status="skipped",
                    detail="running from installed wheel — SPA ships pre-built",
                )
            )
        else:
            report.results.append(checks.check_spa(root))

    return report
