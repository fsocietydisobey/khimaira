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

from chimera.log import get_logger
from chimera.bootstrap import operations as ops
from chimera.bootstrap.schema import Profile

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


def _resolve_chimera_scripts_dir(profile: Profile) -> str | None:
    """Look up the chimera repo in the profile, return its scripts/hooks
    path if it exists locally.

    Used by install_claude_hooks: when chimera was installed via
    `uv tool install`, install-hooks's auto-detection lands on a path
    that doesn't include scripts/ (the wheel strips it). The profile's
    chimera repo entry knows where the source checkout lives, so we
    derive scripts/hooks from that.

    Returns None when there's no chimera entry — install_claude_hooks
    then falls back to its own auto-detection.
    """
    for spec in profile.repos:
        if spec.name == "chimera":
            candidate = spec.resolved_path() / "scripts" / "hooks"
            if candidate.is_dir():
                return str(candidate)
            # Path declared in profile but not on disk — fall through;
            # the operation will surface a helpful error.
            return None
    return None


def _chimera_repo_root() -> Path | None:
    """Locate the chimera source checkout if we're running from one.

    Used to find apps/monitor-ui/ for the SPA build. Returns None
    when running as an installed wheel (no SPA build available in
    that case — wheel ships pre-built dist via package data).
    """
    # __file__ = .../chimera/packages/chimera/src/chimera/bootstrap/runner.py
    # parents[5] = chimera/ (workspace root)
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
        # Pass the chimera repo's scripts/hooks explicitly so install-hooks
        # works even when chimera CLI was installed via `uv tool install`
        # (the tool wheel doesn't include workspace-level scripts/).
        scripts_override = _resolve_chimera_scripts_dir(profile)
        report.results.append(ops.install_claude_hooks(scripts_dir=scripts_override))

    # --- 6. supervisor ---
    if profile.supervisor.auto_install:
        report.results.append(ops.install_supervisor(force=force))

    # --- 6. SPA build ---
    if profile.spa_build:
        root = _chimera_repo_root()
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
    # — and necessary on sync because a chimera pull may have shipped
    # new hook scripts that need re-pointing.
    if profile.install_claude_hooks:
        scripts_override = _resolve_chimera_scripts_dir(profile)
        report.results.append(ops.install_claude_hooks(scripts_dir=scripts_override))

    return report
