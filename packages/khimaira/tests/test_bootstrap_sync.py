"""Tests for the new sync operations introduced in task #66.

Covers `git_pull_repo`, `check_unpushed`, and `maybe_run_uv_sync` —
the per-op helpers that `run_sync` orchestrates. Each test uses a
tmp git repo (no real network) so the suite stays hermetic +
deterministic.

Strategy: build a "remote" bare repo in tmp + a "local" clone that
tracks it. Push commits to the bare repo to simulate updates; the
local clone tests fetch + ff-only merge behavior + dep-change
detection.

The runner-level test (run_sync end-to-end with a real profile) is
deferred to integration tests once the profile fixture is stabilized
— the per-op tests below catch every behavior change without
needing a full profile shim.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from khimaira.bootstrap.operations import (
    OpResult,
    check_unpushed,
    git_pull_repo,
    maybe_run_uv_sync,
)
from khimaira.bootstrap.schema import RepoSpec


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Helper: run git with quiet output, raise on non-zero."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"git {args} failed: {proc.stderr}")
    return proc


def _seed_commit(repo: Path, filename: str, content: str, message: str) -> None:
    (repo / filename).write_text(content)
    _git(repo, "add", filename)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", message)


@pytest.fixture
def remote_with_clone(tmp_path: Path):
    """Build a bare 'remote' repo + a local clone tracking it.

    Returns (remote_path, local_path) tuple. The local clone has one
    initial commit and is in sync with origin.
    """
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    local = tmp_path / "local"

    # Create the bare remote
    subprocess.run(
        ["git", "init", "--bare", "--initial-branch=main", str(remote)],
        check=True,
        capture_output=True,
    )

    # Seed with one initial commit via a throwaway working tree
    subprocess.run(
        ["git", "clone", str(remote), str(seed)],
        check=True,
        capture_output=True,
    )
    _seed_commit(seed, "README.md", "initial\n", "initial commit")
    _git(seed, "push", "origin", "main")
    shutil.rmtree(seed)

    # Clone into the local path that the test will operate on
    subprocess.run(
        ["git", "clone", str(remote), str(local)],
        check=True,
        capture_output=True,
    )
    return remote, local


def _spec_for(local_path: Path, name: str = "test-repo") -> RepoSpec:
    """Build a RepoSpec pointing at a tmp local clone."""
    return RepoSpec(name=name, url="file://unused", path=str(local_path))


# -------------------- git_pull_repo -------------------- #


def test_git_pull_repo_unchanged_when_in_sync(remote_with_clone):
    """No new commits on remote → pull is a no-op, status=unchanged."""
    _, local = remote_with_clone
    result = git_pull_repo(_spec_for(local))

    assert result.status == "unchanged"
    assert "up to date" in result.detail.lower()
    assert result.meta == {}


def test_git_pull_repo_picks_up_new_commits(remote_with_clone, tmp_path):
    """A commit on remote is pulled in via ff-only merge."""
    remote, local = remote_with_clone

    # Push a new commit via a throwaway clone of the bare remote
    pusher = tmp_path / "pusher"
    subprocess.run(["git", "clone", str(remote), str(pusher)], check=True, capture_output=True)
    _seed_commit(pusher, "feature.py", "x = 1\n", "add feature")
    _git(pusher, "push", "origin", "main")

    result = git_pull_repo(_spec_for(local))

    assert result.status == "updated"
    assert result.meta["commits_pulled"] == 1
    assert result.meta["deps_changed"] is False
    assert "1 commit" in result.detail
    # The pulled file should now exist locally
    assert (local / "feature.py").is_file()


def test_git_pull_repo_detects_deps_changed_pyproject(remote_with_clone, tmp_path):
    """A commit touching pyproject.toml flips meta.deps_changed=True."""
    remote, local = remote_with_clone

    pusher = tmp_path / "pusher"
    subprocess.run(["git", "clone", str(remote), str(pusher)], check=True, capture_output=True)
    _seed_commit(
        pusher,
        "pyproject.toml",
        '[project]\nname = "demo"\nversion = "0.1.0"\n',
        "add pyproject.toml",
    )
    _git(pusher, "push", "origin", "main")

    result = git_pull_repo(_spec_for(local))

    assert result.status == "updated"
    assert result.meta["deps_changed"] is True
    assert "pyproject/uv.lock touched" in result.detail


def test_git_pull_repo_detects_deps_changed_uv_lock(remote_with_clone, tmp_path):
    """A commit touching uv.lock flips meta.deps_changed=True (same path as pyproject)."""
    remote, local = remote_with_clone

    pusher = tmp_path / "pusher"
    subprocess.run(["git", "clone", str(remote), str(pusher)], check=True, capture_output=True)
    _seed_commit(pusher, "uv.lock", "# lockfile\n", "add uv.lock")
    _git(pusher, "push", "origin", "main")

    result = git_pull_repo(_spec_for(local))

    assert result.meta["deps_changed"] is True


def test_git_pull_repo_skipped_when_no_git_dir(tmp_path):
    """A RepoSpec pointing at a non-git dir is skipped (not failed) —
    bootstrap hasn't run yet, sync surfaces that without erroring out."""
    not_a_repo = tmp_path / "empty"
    not_a_repo.mkdir()

    result = git_pull_repo(_spec_for(not_a_repo))

    assert result.status == "skipped"
    assert "bootstrap" in result.detail.lower()


def test_git_pull_repo_refuses_ff_merge_when_local_diverged(
    remote_with_clone, tmp_path
):
    """Local has its own commits AND remote has new commits → ff-only
    refuses. Sync surfaces this as `failed` with a "resolve manually"
    hint — never silently rewrites local work."""
    remote, local = remote_with_clone

    # Local-only commit
    _seed_commit(local, "local-only.txt", "local\n", "local commit")

    # Remote-only commit
    pusher = tmp_path / "pusher"
    subprocess.run(["git", "clone", str(remote), str(pusher)], check=True, capture_output=True)
    _seed_commit(pusher, "remote-only.txt", "remote\n", "remote commit")
    _git(pusher, "push", "origin", "main")

    result = git_pull_repo(_spec_for(local))

    assert result.status == "failed"
    assert "resolve manually" in result.detail.lower()
    # Local commit must not be lost
    assert (local / "local-only.txt").is_file()


# -------------------- check_unpushed -------------------- #


def test_check_unpushed_zero_when_in_sync(remote_with_clone):
    """A freshly cloned repo with no local commits → 0 ahead."""
    _, local = remote_with_clone

    result = check_unpushed(_spec_for(local))

    assert result.status == "unchanged"
    assert "in sync" in result.detail.lower()
    assert result.meta == {}


def test_check_unpushed_reports_local_commits_ahead(remote_with_clone):
    """Two local commits with no push → report 2 commits ahead."""
    _, local = remote_with_clone

    _seed_commit(local, "a.txt", "a\n", "first local")
    _seed_commit(local, "b.txt", "b\n", "second local")

    result = check_unpushed(_spec_for(local))

    assert result.status == "updated"
    assert result.meta["unpushed_count"] == 2
    assert "2 unpushed" in result.detail


def test_check_unpushed_skipped_without_upstream(tmp_path):
    """A repo with no upstream tracking → skipped, not failed."""
    no_upstream = tmp_path / "no_upstream"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(no_upstream)],
        check=True,
        capture_output=True,
    )
    _seed_commit(no_upstream, "x.txt", "x\n", "init")

    result = check_unpushed(_spec_for(no_upstream))

    assert result.status == "skipped"
    assert "upstream" in result.detail.lower()


def test_check_unpushed_skipped_without_git_dir(tmp_path):
    """A non-git path is skipped (informational op never fails)."""
    not_a_repo = tmp_path / "nogit"
    not_a_repo.mkdir()

    result = check_unpushed(_spec_for(not_a_repo))

    assert result.status == "skipped"
    assert "no git" in result.detail.lower()


# -------------------- maybe_run_uv_sync -------------------- #


def test_maybe_run_uv_sync_skipped_when_no_deps_changed(tmp_path):
    """deps_changed=False → no-op, status=unchanged. Does NOT invoke uv."""
    result = maybe_run_uv_sync(tmp_path, deps_changed=False)

    assert result.status == "unchanged"
    assert "no pyproject/uv.lock changes" in result.detail


def test_maybe_run_uv_sync_failed_status_when_uv_errors(tmp_path):
    """If uv sync errors (e.g. broken pyproject), status=failed with stderr.

    Using an empty tmp dir as the workspace — uv sync there fails
    because there's no pyproject. The point is to exercise the
    failure path, not validate uv's behavior.
    """
    result = maybe_run_uv_sync(tmp_path, deps_changed=True)

    assert result.status == "failed"
    assert "uv sync failed" in result.detail.lower()


# -------------------- OpResult.meta backward-compat -------------------- #


def test_opresult_meta_defaults_to_empty_dict():
    """The new `meta` field on OpResult defaults to {} — existing callers
    don't break because they never pass it. The CLI renderer never reads
    meta (it's runner-internal), so no display regression."""
    r = OpResult(op="x", target="y", status="unchanged")

    assert r.meta == {}
    assert isinstance(r.meta, dict)


def test_opresult_meta_carries_arbitrary_payload():
    """meta is a plain dict — ops can stuff whatever the runner reads."""
    r = OpResult(
        op="x",
        target="y",
        status="updated",
        meta={"commits_pulled": 5, "deps_changed": True, "extra": "ok"},
    )

    assert r.meta["commits_pulled"] == 5
    assert r.meta["deps_changed"] is True
    assert r.meta["extra"] == "ok"


# -------------------- check_git_pull_repo (--check semantics) -------------------- #


def test_check_git_pull_repo_unchanged_when_in_sync(remote_with_clone):
    """No new commits on remote → would-pull-0, status=unchanged."""
    from khimaira.bootstrap.checks import check_git_pull_repo

    _, local = remote_with_clone
    result = check_git_pull_repo(_spec_for(local))

    assert result.status == "unchanged"
    assert "in sync" in result.detail.lower()


def test_check_git_pull_repo_previews_pull_without_merging(remote_with_clone, tmp_path):
    """A commit on remote shows as `updated` with would-pull detail —
    BUT the working tree is NOT touched (no merge happens)."""
    from khimaira.bootstrap.checks import check_git_pull_repo

    remote, local = remote_with_clone
    pre_head = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()

    pusher = tmp_path / "pusher"
    subprocess.run(["git", "clone", str(remote), str(pusher)], check=True, capture_output=True)
    _seed_commit(pusher, "feature.py", "x = 1\n", "add feature")
    _git(pusher, "push", "origin", "main")

    result = check_git_pull_repo(_spec_for(local))

    assert result.status == "updated"
    assert result.meta["commits_pulled"] == 1
    assert "would pull" in result.detail.lower()
    # The working tree must NOT have moved — check pinned local HEAD
    post_head = subprocess.run(
        ["git", "-C", str(local), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert post_head == pre_head, "check mode should not move HEAD"


def test_check_git_pull_repo_flags_dep_changes(remote_with_clone, tmp_path):
    """Pending pyproject change is surfaced in --check meta + detail."""
    from khimaira.bootstrap.checks import check_git_pull_repo

    remote, local = remote_with_clone
    pusher = tmp_path / "pusher"
    subprocess.run(["git", "clone", str(remote), str(pusher)], check=True, capture_output=True)
    _seed_commit(
        pusher,
        "pyproject.toml",
        '[project]\nname = "demo"\nversion = "0.1.0"\n',
        "add pyproject.toml",
    )
    _git(pusher, "push", "origin", "main")

    result = check_git_pull_repo(_spec_for(local))

    assert result.meta["deps_changed"] is True
    assert "pyproject/uv.lock would change" in result.detail


def test_check_git_pull_repo_failed_when_local_diverged(remote_with_clone, tmp_path):
    """Local has commits remote doesn't → would-refuse ff-only, status=failed."""
    from khimaira.bootstrap.checks import check_git_pull_repo

    remote, local = remote_with_clone
    _seed_commit(local, "local-only.txt", "local\n", "local commit")

    pusher = tmp_path / "pusher"
    subprocess.run(["git", "clone", str(remote), str(pusher)], check=True, capture_output=True)
    _seed_commit(pusher, "remote-only.txt", "remote\n", "remote commit")
    _git(pusher, "push", "origin", "main")

    result = check_git_pull_repo(_spec_for(local))

    assert result.status == "failed"
    assert "would refuse ff-only" in result.detail.lower()


# -------------------- summarize_sync (final report tail) -------------------- #


def test_summarize_sync_returns_no_changes_when_empty():
    """Empty report (no ops) → 'no changes'. Quiet mode uses this to
    decide whether to print anything at all."""
    from khimaira.bootstrap.runner import RunReport, summarize_sync

    assert summarize_sync(RunReport()) == "no changes"


def test_summarize_sync_aggregates_commits_pulled():
    """Sums commits_pulled across all updated repo-pull rows; ignores
    unchanged rows."""
    from khimaira.bootstrap.runner import RunReport, summarize_sync

    report = RunReport()
    report.results = [
        OpResult(op="repo-pull", target="a", status="updated", meta={"commits_pulled": 3, "deps_changed": False}),
        OpResult(op="repo-pull", target="b", status="updated", meta={"commits_pulled": 5, "deps_changed": False}),
        OpResult(op="repo-pull", target="c", status="unchanged"),
    ]

    summary = summarize_sync(report)
    assert "8 commit(s)" in summary
    assert "2 repo(s)" in summary  # only the two updated ones


def test_summarize_sync_includes_deps_refreshed():
    """If uv-sync ran (status=updated), 'workspace deps refreshed' lands."""
    from khimaira.bootstrap.runner import RunReport, summarize_sync

    report = RunReport()
    report.results = [
        OpResult(op="repo-pull", target="a", status="updated", meta={"commits_pulled": 1, "deps_changed": True}),
        OpResult(op="uv-sync", target="workspace", status="updated"),
    ]

    summary = summarize_sync(report)
    assert "workspace deps refreshed" in summary


def test_summarize_sync_includes_unpushed():
    """Unpushed commits across repos roll up into one phrase."""
    from khimaira.bootstrap.runner import RunReport, summarize_sync

    report = RunReport()
    report.results = [
        OpResult(op="unpushed-check", target="a", status="updated", meta={"unpushed_count": 2}),
        OpResult(op="unpushed-check", target="b", status="updated", meta={"unpushed_count": 3}),
        OpResult(op="unpushed-check", target="c", status="unchanged"),
    ]

    summary = summarize_sync(report)
    assert "5 unpushed" in summary
    assert "2 repo(s)" in summary


# -------------------- v2.1: MCP drift idempotent-remove -------------------- #


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Re-root XDG_STATE_HOME so managed_mcp.json writes go to tmp."""
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    # Re-import operations to pick up the new XDG_STATE_HOME
    import importlib
    from khimaira.bootstrap import operations
    importlib.reload(operations)
    yield state, operations
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    importlib.reload(operations)


def test_reconcile_mcp_drift_skipped_without_claude_cli(isolated_state, monkeypatch):
    """No `claude` CLI on PATH → skip entirely (single OpResult, skipped)."""
    _, operations = isolated_state
    monkeypatch.setattr("shutil.which", lambda cmd: None)

    results = operations.reconcile_mcp_drift({"khimaira"})

    assert len(results) == 1
    assert results[0].status == "skipped"
    assert "claude" in results[0].detail.lower()


def test_reconcile_mcp_drift_unchanged_when_no_stale_entries(isolated_state, monkeypatch):
    """managed=empty + profile={khimaira} → unchanged (nothing to remove)."""
    _, operations = isolated_state
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
    monkeypatch.setattr(
        operations, "_claude_mcp_list", lambda: (True, {"khimaira", "personal-mcp"})
    )

    results = operations.reconcile_mcp_drift({"khimaira"})

    assert len(results) == 1
    assert results[0].status == "unchanged"


def test_reconcile_mcp_drift_removes_only_khimaira_managed(isolated_state, monkeypatch):
    """A server in claude mcp list that's user-added (NOT in managed state)
    must NOT be removed. Profile says only khimaira; user has personal-mcp;
    both are present in claude. managed_mcp.json only lists khimaira (the
    one khimaira ever registered). Result: nothing removed.
    """
    _, operations = isolated_state
    # State: managed_mcp.json tracks ONLY khimaira (user-added personal-mcp
    # was registered outside the profile, never tracked)
    operations._write_managed_mcp({"khimaira"})
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
    monkeypatch.setattr(
        operations,
        "_claude_mcp_list",
        lambda: (True, {"khimaira", "personal-mcp"}),
    )

    # Profile contains khimaira; the user-added personal-mcp is NOT in profile
    results = operations.reconcile_mcp_drift({"khimaira"})

    # Nothing to remove: khimaira is in profile, personal-mcp not in managed state
    assert all(r.status != "updated" for r in results)
    # Managed state file unchanged
    assert operations._read_managed_mcp() == {"khimaira"}


def test_reconcile_mcp_drift_removes_dropped_khimaira_entry(isolated_state, monkeypatch):
    """khimaira state managed=[khimaira, seance]; profile now only has
    khimaira; both still in claude → seance gets removed."""
    _, operations = isolated_state
    operations._write_managed_mcp({"khimaira", "seance"})

    remove_calls = []

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["claude", "mcp", "remove"]:
            remove_calls.append(cmd[3])
            # Return a mock CompletedProcess with returncode=0
            class _Proc:
                returncode = 0
                stdout = ""
                stderr = ""
            return _Proc()
        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""
        return _Proc()

    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/claude")
    monkeypatch.setattr(
        operations,
        "_claude_mcp_list",
        lambda: (True, {"khimaira", "seance"}),
    )
    monkeypatch.setattr(operations, "_run", fake_run)

    results = operations.reconcile_mcp_drift({"khimaira"})

    # Seance was removed (not in profile but was khimaira-managed)
    assert "seance" in remove_calls
    assert "khimaira" not in remove_calls  # still in profile

    updated_results = [r for r in results if r.status == "updated"]
    assert len(updated_results) == 1
    assert updated_results[0].target == "seance"

    # Managed state file updated to drop seance
    assert operations._read_managed_mcp() == {"khimaira"}


# -------------------- v2.2: monitor freshness check + restart -------------------- #


def test_check_monitor_freshness_skipped_when_no_workspace():
    """Installed-wheel mode (workspace_root=None) → skipped."""
    from khimaira.bootstrap.operations import check_monitor_freshness

    result = check_monitor_freshness(None)

    assert result.status == "skipped"
    assert "installed-wheel" in result.detail


def test_check_monitor_freshness_skipped_when_systemctl_unavailable(tmp_path, monkeypatch):
    """No systemctl on PATH → skipped (macOS, minimal containers, etc.)."""
    from khimaira.bootstrap import operations

    monkeypatch.setattr("shutil.which", lambda cmd: None)

    result = operations.check_monitor_freshness(tmp_path)
    assert result.status == "skipped"
    assert "systemctl" in result.detail.lower()


def test_check_monitor_freshness_unchanged_when_daemon_newer_than_head(tmp_path, monkeypatch):
    """Daemon ActiveEnterTimestamp newer than latest commit → unchanged."""
    from khimaira.bootstrap import operations

    # Build a minimal git repo with one commit at a known timestamp
    repo = tmp_path / "khimaira"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    _seed_commit(repo, "x.txt", "x\n", "init")
    # Force commit timestamp to a known epoch
    head_epoch = "1700000000"  # 2023-11-14
    subprocess.run(
        ["git", "-C", str(repo), "commit", "--amend", "--no-edit",
         "--date", head_epoch, "-c", f"user.email=t@t", "-c", "user.name=t"],
        capture_output=True,
    )

    # Mock systemctl to return a future-ish daemon start time
    def fake_run(cmd, **kwargs):
        class _Proc:
            returncode = 0
            stderr = ""
        if cmd[:1] == ["systemctl"]:
            _Proc.stdout = "ActiveEnterTimestamp=Wed 2026-05-14 14:32:18 CDT\n"
        elif cmd[:1] == ["date"]:
            _Proc.stdout = "1747242738\n"  # well after head_epoch
        elif cmd[:1] == ["git"]:
            _Proc.stdout = f"{head_epoch}\n"
        else:
            _Proc.stdout = ""
        return _Proc()

    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(operations, "_run", fake_run)

    result = operations.check_monitor_freshness(repo)
    assert result.status == "unchanged"


def test_check_monitor_freshness_updated_when_daemon_older_than_head(tmp_path, monkeypatch):
    """Daemon older than HEAD → updated, with age + restart suggestion."""
    from khimaira.bootstrap import operations

    repo = tmp_path / "khimaira"
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(repo)],
        check=True,
        capture_output=True,
    )
    _seed_commit(repo, "x.txt", "x\n", "init")

    def fake_run(cmd, **kwargs):
        class _Proc:
            returncode = 0
            stderr = ""
        if cmd[:1] == ["systemctl"]:
            _Proc.stdout = "ActiveEnterTimestamp=Sun 2024-01-01 00:00:00 CDT\n"
        elif cmd[:1] == ["date"]:
            _Proc.stdout = "1704096000\n"  # 2024-01-01
        elif cmd[:1] == ["git"]:
            _Proc.stdout = "1704182400\n"  # 2024-01-02 (24h after daemon)
        else:
            _Proc.stdout = ""
        return _Proc()

    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(operations, "_run", fake_run)

    result = operations.check_monitor_freshness(repo)

    assert result.status == "updated"
    assert "predates HEAD" in result.detail
    assert "--auto-restart" in result.detail
    # 24h = 1440 min
    assert result.meta["age_seconds"] == 1704182400 - 1704096000


def test_restart_monitor_skipped_without_systemctl(monkeypatch):
    """No systemctl → skipped, not failed."""
    from khimaira.bootstrap import operations

    monkeypatch.setattr("shutil.which", lambda cmd: None)

    result = operations.restart_monitor()
    assert result.status == "skipped"


def test_restart_monitor_runs_systemctl(monkeypatch):
    """With systemctl + a healthy unit, restart runs + returns updated."""
    from khimaira.bootstrap import operations

    monkeypatch.setattr("shutil.which", lambda cmd: f"/usr/bin/{cmd}")

    called: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        called.append(cmd)
        class _Proc:
            returncode = 0
            stdout = ""
            stderr = ""
        return _Proc()

    monkeypatch.setattr(operations, "_run", fake_run)

    result = operations.restart_monitor()

    assert result.status == "updated"
    assert called == [["systemctl", "--user", "restart", "khimaira-monitor"]]
