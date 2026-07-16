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

from khimaira.bootstrap import checks, install_mode
from khimaira.bootstrap import operations as ops
from khimaira.bootstrap.schema import Profile
from khimaira.log import get_logger

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


def _codex_khimaira_root(profile: Profile) -> Path | None:
    """Resolve the checkout Codex should use for khimaira's MCP server."""
    for repo in profile.repos:
        if repo.name == "khimaira":
            return repo.resolved_path()
    return _khimaira_repo_root()


def _install_codex_adapter(report: RunReport, profile: Profile) -> None:
    root = _codex_khimaira_root(profile)
    if root is None or not root.is_dir():
        report.results.append(
            ops.OpResult(
                op="codex-mcp-config",
                target="~/.codex/config.toml",
                status="failed",
                detail=(
                    "khimaira checkout is unavailable after bootstrap: add a repos "
                    "entry named 'khimaira' or fix its clone/install failure"
                ),
            )
        )
    else:
        report.results.append(ops.install_codex_mcp_config(root))
    report.results.append(ops.install_codex_hooks())


def _check_codex_adapter(report: RunReport, profile: Profile) -> None:
    root = _codex_khimaira_root(profile)
    if root is None:
        report.results.append(
            ops.OpResult(
                op="codex-mcp-config",
                target="~/.codex/config.toml",
                status="failed",
                detail=(
                    "cannot resolve khimaira checkout: add a repos entry named "
                    "'khimaira' to the profile"
                ),
            )
        )
    else:
        report.results.append(checks.check_codex_mcp_config(root))
    report.results.append(checks.check_codex_hooks())


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

    if profile.install_codex_adapter:
        _install_codex_adapter(report, profile)

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


def run_sync(
    profile: Profile,
    *,
    force: bool = False,
    auto_restart_monitor: bool = False,
    auto_upgrade: bool = False,
) -> RunReport:
    """Ongoing sync — mode-aware. See module docstring for the editable path.

    Branches on install mode at the top (task #v1.2):

      - **site-packages mode** (`uvx khimaira` / `pip install khimaira`):
        skip git-pull / uv-sync ops (no workspace to manage); check PyPI
        for a newer release + optionally upgrade in-place; then re-apply
        the profile bits that work the same regardless of mode (symlinks,
        MCP register, hooks).

      - **editable mode** (contributor workflow): existing v1 behavior —
        dotfiles pull, sibling repo pulls, uv sync, install re-run,
        symlinks, MCP, hooks, monitor freshness check.

    `auto_upgrade=True` skips the interactive prompt in site-packages
    mode and applies the upgrade unconditionally (use in cron). Has no
    effect in editable mode.

    All operations are idempotent — re-running on a no-change machine
    produces all-`unchanged` results.
    """
    report = RunReport()
    mode = install_mode.detect_install_mode()

    # --- Site-packages mode: PyPI-based upgrade flow ---
    if mode == "site-packages":
        # No dotfiles git-pull or sibling git-pull in this mode — those
        # ops assume a workspace checkout. Apply only the symmetric
        # parts of the pipeline.
        report.results.append(ops.check_and_upgrade_khimaira(auto_upgrade=auto_upgrade))

        # Apply symlinks IFF the user has a dotfiles repo declared — for
        # community-profile users without dotfiles, this is a no-op.
        if profile.dotfiles:
            r = ops.sync_dotfiles(profile.dotfiles)
            report.results.append(r)
            if r.status != "failed":
                dotfiles_root = Path(os.path.expanduser(profile.dotfiles.path)).resolve()
                if dotfiles_root.is_dir():
                    for entry in profile.dotfiles.symlinks:
                        report.results.append(ops.apply_symlink(entry, dotfiles_root))

        # Re-register MCP servers + reconcile drift — works the same in
        # either mode (operates on Claude Code's settings, not the repo).
        for mcp in profile.mcp_servers:
            report.results.append(ops.register_mcp(mcp, force=force))
        profile_mcp_names = {m.name for m in profile.mcp_servers}
        report.results.extend(ops.reconcile_mcp_drift(profile_mcp_names))

        # Re-apply hooks if requested — the hook commands use
        # `python -m khimaira.hooks.<name>`, which resolves against
        # whichever khimaira is on $PATH (the upgraded one if we just
        # upgraded).
        if profile.install_claude_hooks:
            report.results.append(ops.install_claude_hooks())

        if profile.install_codex_adapter:
            _install_codex_adapter(report, profile)

        # Audit-log this run for future cross-machine comparisons.
        ops.log_sync_event(
            "sync-run",
            "all",
            {
                "mode": "site-packages",
                "had_failures": report.had_failures,
            },
        )
        return report

    # --- Editable mode: existing v1 pipeline (unchanged) ---

    # --- 1. dotfiles pull ---
    if profile.dotfiles:
        r = ops.sync_dotfiles(profile.dotfiles)
        report.results.append(r)
        if r.status == "failed":
            # Don't apply symlinks against a possibly-stale dotfiles
            # tree if the pull itself errored. Surface the one failure
            # and bail — user fixes git state, re-runs.
            return report

    # --- 2. sibling repo pulls ---
    # Track any-deps-changed across the whole fan-out so we know
    # whether to fire `uv sync` once at the end (cheaper than per-repo).
    any_deps_changed = False
    for repo_spec in profile.repos:
        r = ops.git_pull_repo(repo_spec)
        report.results.append(r)
        if r.meta.get("deps_changed"):
            any_deps_changed = True

    # --- 3. uv sync (workspace-level, single shot) ---
    # Only meaningful when running from a checkout (workspace mode).
    # Installed-wheel runs skip — there's no workspace to re-sync.
    workspace_root = _khimaira_repo_root()
    if workspace_root is not None:
        report.results.append(ops.maybe_run_uv_sync(workspace_root, any_deps_changed))

    # --- 3b. sibling install re-run (v2.3) — for each repo, re-run
    #          its `install:` command IFF the command changed in the
    #          profile since last apply. First-time records baseline. ---
    for repo_spec in profile.repos:
        report.results.append(ops.maybe_reinstall_repo(repo_spec))

    # --- 4. apply symlinks (idempotent — picks up new entries) ---
    if profile.dotfiles:
        dotfiles_root = Path(os.path.expanduser(profile.dotfiles.path)).resolve()
        if dotfiles_root.is_dir():
            for entry in profile.dotfiles.symlinks:
                report.results.append(ops.apply_symlink(entry, dotfiles_root))

    # --- 5. re-register MCP servers (idempotent — skips already-registered) ---
    for mcp in profile.mcp_servers:
        report.results.append(ops.register_mcp(mcp, force=force))

    # --- 5b. MCP drift reconcile (v2.1) — remove khimaira-managed entries
    #         that have been dropped from the profile. Never touches
    #         user-managed servers (only entries tracked in managed_mcp.json
    #         state file get considered for removal). ---
    profile_mcp_names = {m.name for m in profile.mcp_servers}
    report.results.extend(ops.reconcile_mcp_drift(profile_mcp_names))

    # --- 6. re-apply Claude Code hooks ---
    # Idempotent at the install-hooks layer — and necessary on sync
    # because a khimaira pull may have shipped new hook modules.
    if profile.install_claude_hooks:
        report.results.append(ops.install_claude_hooks())

    if profile.install_codex_adapter:
        _install_codex_adapter(report, profile)

    # --- 6b. monitor freshness check (v2.2) — surface stale-daemon
    #          warning; with auto_restart=True, run systemctl restart. ---
    freshness = ops.check_monitor_freshness(workspace_root)
    report.results.append(freshness)
    if freshness.status == "updated" and auto_restart_monitor:
        report.results.append(ops.restart_monitor())

    # --- 7. unpushed-commits report (informational; sync never pushes) ---
    # Surface AFTER the pulls + applies so the user sees the
    # "everything else is fine, but you have unpushed work" framing.
    for repo_spec in profile.repos:
        report.results.append(ops.check_unpushed(repo_spec))

    # --- 8. audit-log this sync run (v2.4 — cross-machine awareness) ---
    # Append a "sync-run" event to ~/.local/state/khimaira/sync_meta.jsonl.
    # Per-machine local; no cross-machine sync of the meta file itself.
    # Lets future syncs answer "when did I last sync this machine?".
    repos_pulled = sum(1 for r in report.results if r.op == "repo-pull" and r.status == "updated")
    commits_total = sum(
        r.meta.get("commits_pulled", 0)
        for r in report.results
        if r.op == "repo-pull" and r.status == "updated"
    )
    ops.log_sync_event(
        "sync-run",
        "all",
        {
            "repos_pulled": repos_pulled,
            "commits_total": commits_total,
            "had_failures": report.had_failures,
        },
    )

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

    if profile.install_codex_adapter:
        _check_codex_adapter(report, profile)

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


def check_sync(profile: Profile) -> RunReport:
    """Read-only drift report: what would `run_sync` do right now?

    Mirrors `run_sync`'s order but uses checks that fetch (network)
    + diff locally rather than merging. No side effects on any
    working tree:

      1. dotfiles drift (clone status only — sync_dotfiles itself
         is a `git pull`, which is the apply path)
      2. sibling-repo would-pull preview (git fetch + diff)
      3. uv-sync trigger (predicted, based on any deps_changed flag)
      4. symlink drift
      5. MCP server drift
      6. Claude Code hooks drift
      7. unpushed-commits report (already informational; reuse)

    Distinct from `check_bootstrap`: bootstrap-check covers clone +
    install paths; sync-check covers pull + dep-refresh paths. Both
    fit together via `khimaira doctor` (which calls both).
    """
    report = RunReport()
    mode = install_mode.detect_install_mode()

    # --- Site-packages mode preview: PyPI version comparison only ---
    if mode == "site-packages":
        from khimaira import __version__

        latest = install_mode.check_pypi_version("khimaira")
        if latest is None:
            report.results.append(
                ops.OpResult(
                    op="package-upgrade",
                    target="khimaira",
                    status="skipped",
                    detail="PyPI version check failed (offline?)",
                )
            )
        elif install_mode.is_newer_version(__version__, latest):
            report.results.append(
                ops.OpResult(
                    op="package-upgrade",
                    target="khimaira",
                    status="updated",
                    detail=f"would upgrade {__version__} → {latest}",
                    meta={"current": __version__, "latest": latest},
                )
            )
        else:
            report.results.append(
                ops.OpResult(
                    op="package-upgrade",
                    target="khimaira",
                    status="unchanged",
                    detail=f"current {__version__} matches latest {latest}",
                    meta={"current": __version__, "latest": latest},
                )
            )

        # MCP + hooks drift check still applies in site-packages mode.
        for mcp in profile.mcp_servers:
            report.results.append(checks.check_mcp(mcp))
        if profile.install_claude_hooks:
            report.results.append(checks.check_claude_hooks())
        if profile.install_codex_adapter:
            _check_codex_adapter(report, profile)
        return report

    # --- Editable mode preview (existing path) ---

    # --- 1. dotfiles clone presence ---
    if profile.dotfiles:
        report.results.append(checks.check_dotfiles(profile.dotfiles))

    # --- 2. sibling repos would-pull preview ---
    any_deps_changed = False
    for repo_spec in profile.repos:
        r = checks.check_git_pull_repo(repo_spec)
        report.results.append(r)
        if r.meta.get("deps_changed"):
            any_deps_changed = True

    # --- 3. uv-sync trigger preview ---
    workspace_root = _khimaira_repo_root()
    if workspace_root is not None:
        if any_deps_changed:
            report.results.append(
                ops.OpResult(
                    op="uv-sync",
                    target="workspace",
                    status="updated",
                    detail="would re-resolve workspace deps",
                )
            )
        else:
            report.results.append(
                ops.OpResult(
                    op="uv-sync",
                    target="workspace",
                    status="unchanged",
                    detail="no pyproject/uv.lock changes incoming",
                )
            )

    # --- 4. symlinks ---
    if profile.dotfiles:
        dotfiles_root = Path(os.path.expanduser(profile.dotfiles.path)).resolve()
        if dotfiles_root.is_dir():
            for entry in profile.dotfiles.symlinks:
                report.results.append(checks.check_symlink(entry, dotfiles_root))

    # --- 5. MCP server registrations ---
    for mcp in profile.mcp_servers:
        report.results.append(checks.check_mcp(mcp))

    # --- 6. Claude Code hooks ---
    if profile.install_claude_hooks:
        report.results.append(checks.check_claude_hooks())

    if profile.install_codex_adapter:
        _check_codex_adapter(report, profile)

    # --- 7. unpushed-commits report (reusing the apply-mode op — it's
    #        already read-only / informational) ---
    for repo_spec in profile.repos:
        report.results.append(ops.check_unpushed(repo_spec))

    return report


def summarize_sync(report: RunReport) -> str:
    """Aggregate task #66 metrics from a sync RunReport.

    Reads `meta` from repo-pull + uv-sync + unpushed-check ops to
    produce a single one-line summary suitable for the final report
    tail (and parseable by `--quiet` mode + cron post-processors).

    Format: "X commits pulled across N repo(s), Y deps refreshed,
    Z unpushed commits on M repo(s)".
    """
    commits_pulled = 0
    repos_pulled = 0
    deps_refreshed = False
    unpushed_total = 0
    repos_with_unpushed = 0
    upgrade_summary: str | None = None

    for r in report.results:
        if r.op == "repo-pull" and r.status == "updated":
            commits_pulled += r.meta.get("commits_pulled", 0)
            repos_pulled += 1
        elif r.op == "uv-sync" and r.status == "updated":
            deps_refreshed = True
        elif r.op == "unpushed-check" and r.status == "updated":
            unpushed_total += r.meta.get("unpushed_count", 0)
            repos_with_unpushed += 1
        elif r.op == "package-upgrade":
            current = r.meta.get("current")
            latest = r.meta.get("latest")
            if r.status == "updated" and current and latest:
                upgrade_summary = f"khimaira {current} → {latest}"
            elif r.status == "skipped" and current and latest:
                upgrade_summary = f"upgrade available: {current} → {latest} (skipped)"

    parts: list[str] = []
    if upgrade_summary:
        parts.append(upgrade_summary)
    if commits_pulled or repos_pulled:
        parts.append(f"{commits_pulled} commit(s) across {repos_pulled} repo(s)")
    if deps_refreshed:
        parts.append("workspace deps refreshed")
    if unpushed_total:
        parts.append(f"{unpushed_total} unpushed commit(s) on {repos_with_unpushed} repo(s)")

    return " · ".join(parts) if parts else "no changes"
