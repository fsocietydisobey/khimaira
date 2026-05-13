"""Tests for the profile-driven bootstrap.

Focus areas:
  - schema: validation strictness (unknown keys, missing required fields)
  - loader: source resolution (path, env, default)
  - operations: idempotency contract — re-running yields `unchanged`
  - symlinks: backup-existing-file behavior so we never silently clobber
  - register_mcp: graceful skip when `claude` CLI is missing

Network paths (URL profile fetch, git clone) aren't exercised — they're
thin wrappers around urllib/subprocess where the value-add tests would
mostly assert mock interactions. The repo-clone path IS tested via a
real local "remote" (a fresh `git init --bare` in tmp_path) to catch
realistic shape-mismatch bugs.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from khimaira.bootstrap import (
    Profile,
    ProfileError,
    load_profile,
)
from khimaira.bootstrap import operations as ops
from khimaira.bootstrap.schema import (
    DotfilesSpec,
    McpServerSpec,
    RepoSpec,
    SymlinkEntry,
    _parse_dict,
)
from khimaira.bootstrap.runner import run_bootstrap, run_sync

# ---------------------------------------------------------------------------
# Schema validation
# ---------------------------------------------------------------------------


def test_parse_minimal_profile_is_valid():
    """A bare dict (just `name`) parses without errors."""
    p = _parse_dict({"name": "minimal"})
    assert p.name == "minimal"
    assert p.dotfiles is None
    assert p.repos == []
    assert p.mcp_servers == []
    assert p.supervisor.auto_install is False
    assert p.spa_build is False


def test_parse_rejects_unknown_top_level_keys():
    """Typos in profile top-level keys must fail loud, not silently ignore."""
    with pytest.raises(ProfileError, match="unknown top-level keys"):
        _parse_dict({"name": "x", "dotfile": {}})  # note typo


def test_parse_rejects_dotfiles_without_repo():
    """dotfiles section needs a repo URL — symlinks alone are meaningless."""
    with pytest.raises(ProfileError, match="dotfiles.repo is required"):
        _parse_dict({"name": "x", "dotfiles": {"symlinks": []}})


def test_parse_rejects_malformed_symlink():
    """Each symlink entry needs both src and dest."""
    with pytest.raises(ProfileError, match="each symlink needs"):
        _parse_dict(
            {
                "name": "x",
                "dotfiles": {
                    "repo": "git@example.com:x.git",
                    "symlinks": [{"src": "a"}],
                },
            }
        )


def test_parse_rejects_repo_without_url():
    with pytest.raises(ProfileError, match="each repo needs"):
        _parse_dict({"name": "x", "repos": [{"name": "a"}]})


def test_parse_rejects_mcp_server_without_command():
    with pytest.raises(ProfileError, match="each mcp_server needs"):
        _parse_dict({"name": "x", "mcp_servers": [{"name": "a"}]})


def test_repo_resolved_path_defaults_to_dev_subdir():
    spec = RepoSpec(name="seance", url="git@example.com:seance.git")
    p = spec.resolved_path()
    assert p.name == "seance"
    assert "dev" in p.parts


def test_repo_resolved_path_respects_explicit_path(tmp_path):
    spec = RepoSpec(name="x", url="...", path=str(tmp_path / "custom"))
    assert spec.resolved_path() == (tmp_path / "custom").resolve()


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------


def test_load_profile_defaults_when_nothing_else_set(monkeypatch):
    """No --profile, no env, no ~/.config — built-in default ships."""
    monkeypatch.delenv("KHIMAIRA_PROFILE", raising=False)
    # Force XDG to an empty tmp so the user-path branch can't match
    monkeypatch.setenv("XDG_CONFIG_HOME", "/nonexistent/xdg")
    profile, source = load_profile()
    assert profile.name == "khimaira-default"
    assert "built-in default" in source


def test_load_profile_honors_explicit_path(tmp_path):
    yaml_text = "name: testprofile\nmcp_servers:\n  - name: foo\n    command: echo hi\n"
    p = tmp_path / "profile.yaml"
    p.write_text(yaml_text)
    profile, source = load_profile(str(p))
    assert profile.name == "testprofile"
    assert profile.mcp_servers[0].name == "foo"
    assert source == str(p)


def test_load_profile_errors_on_missing_path(tmp_path):
    with pytest.raises(ProfileError, match="profile file not found"):
        load_profile(str(tmp_path / "nope.yaml"))


# ---------------------------------------------------------------------------
# apply_symlink — idempotency + backup behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_dotfiles(tmp_path):
    """A fake dotfiles dir with one source file ready to link."""
    root = tmp_path / "dotfiles"
    (root / "claude").mkdir(parents=True)
    (root / "claude" / "CLAUDE.md").write_text("# rules")
    return root


def test_apply_symlink_creates_when_dest_missing(fake_dotfiles, tmp_path):
    dest = tmp_path / "home" / ".claude" / "CLAUDE.md"
    result = ops.apply_symlink(
        SymlinkEntry(src="claude/CLAUDE.md", dest=str(dest)),
        fake_dotfiles,
    )
    assert result.status == "created"
    assert dest.is_symlink()
    assert (
        Path(os.readlink(dest)).resolve()
        == (fake_dotfiles / "claude/CLAUDE.md").resolve()
    )


def test_apply_symlink_idempotent_unchanged(fake_dotfiles, tmp_path):
    """Second call with the same args reports unchanged, doesn't churn."""
    entry = SymlinkEntry(src="claude/CLAUDE.md", dest=str(tmp_path / "dest"))
    ops.apply_symlink(entry, fake_dotfiles)  # first call
    r2 = ops.apply_symlink(entry, fake_dotfiles)  # second call
    assert r2.status == "unchanged"


def test_apply_symlink_backs_up_existing_real_file(fake_dotfiles, tmp_path):
    """A real file at the destination must be preserved as .bak.<ts>."""
    dest = tmp_path / "existing.md"
    dest.write_text("user's existing content, do not lose")
    r = ops.apply_symlink(
        SymlinkEntry(src="claude/CLAUDE.md", dest=str(dest)),
        fake_dotfiles,
    )
    assert r.status == "updated"
    assert dest.is_symlink()
    backups = list(tmp_path.glob("existing.md.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text() == "user's existing content, do not lose"


def test_apply_symlink_repoints_wrong_link(fake_dotfiles, tmp_path):
    """An existing symlink pointing elsewhere gets re-pointed cleanly."""
    dest = tmp_path / "link"
    other = tmp_path / "somewhere_else"
    other.write_text("x")
    dest.symlink_to(other)
    r = ops.apply_symlink(
        SymlinkEntry(src="claude/CLAUDE.md", dest=str(dest)),
        fake_dotfiles,
    )
    assert r.status == "updated"
    assert (
        Path(os.readlink(dest)).resolve()
        == (fake_dotfiles / "claude/CLAUDE.md").resolve()
    )


def test_apply_symlink_missing_source_fails(fake_dotfiles, tmp_path):
    r = ops.apply_symlink(
        SymlinkEntry(src="not/here.md", dest=str(tmp_path / "x")),
        fake_dotfiles,
    )
    assert r.status == "failed"
    assert "source missing" in r.detail


# ---------------------------------------------------------------------------
# ensure_repo — uses a real local bare repo as the "remote"
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_remote(tmp_path):
    """git init --bare + one commit so `git clone` has something to fetch.

    Real subprocess git so we exercise the real path. Bare repo lives
    in tmp_path so each test gets a fresh one.

    The `-b main` flag on both inits is load-bearing: without it some
    git versions default the bare repo's HEAD to `master` while our
    push uses `main`, and the clone silently checks out nothing.
    """
    remote = tmp_path / "remote.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    work = tmp_path / "seed"
    work.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(work), "config", "user.name", "t"], check=True)
    (work / "README.md").write_text("hi")
    subprocess.run(["git", "-C", str(work), "add", "."], check=True)
    subprocess.run(["git", "-C", str(work), "commit", "-qm", "init"], check=True)
    subprocess.run(
        ["git", "-C", str(work), "remote", "add", "origin", str(remote)], check=True
    )
    subprocess.run(["git", "-C", str(work), "push", "-q", "origin", "main"], check=True)
    return remote


def test_ensure_repo_clones_when_missing(fake_remote, tmp_path):
    spec = RepoSpec(name="x", url=str(fake_remote), path=str(tmp_path / "clone"))
    r = ops.ensure_repo(spec)
    assert r.status == "created"
    assert (tmp_path / "clone" / ".git").is_dir()


def test_ensure_repo_unchanged_when_already_cloned(fake_remote, tmp_path):
    spec = RepoSpec(name="x", url=str(fake_remote), path=str(tmp_path / "clone"))
    ops.ensure_repo(spec)
    r2 = ops.ensure_repo(spec)
    assert r2.status == "unchanged"


def test_ensure_repo_fails_when_non_git_dir_blocks(tmp_path):
    """A non-git directory at the target path → failed without --force."""
    target = tmp_path / "blocker"
    target.mkdir()
    (target / "junk.txt").write_text("x")
    spec = RepoSpec(name="x", url="file:///nonexistent", path=str(target))
    r = ops.ensure_repo(spec, force=False)
    assert r.status == "failed"
    assert "--force" in r.detail


# ---------------------------------------------------------------------------
# register_mcp — graceful skip on missing CLI
# ---------------------------------------------------------------------------


def test_register_mcp_skips_when_claude_cli_missing():
    """If `claude` isn't on PATH, registration is a graceful skip — not a fail.

    Reasoning: bootstrap is useful even before Claude Code is installed;
    the user might be setting up the substrate first.
    """
    with patch("khimaira.bootstrap.operations.shutil.which", return_value=None):
        r = ops.register_mcp(McpServerSpec(name="x", command="echo"))
    assert r.status == "skipped"
    assert "claude" in r.detail.lower()


# ---------------------------------------------------------------------------
# run_bootstrap / run_sync — full orchestration smoke
# ---------------------------------------------------------------------------


def test_run_bootstrap_full_smoke(fake_remote, fake_dotfiles, tmp_path, monkeypatch):
    """End-to-end on a real bare-remote + real symlink target. No mocks.

    Asserts: dotfiles cloned (we point at our fake_dotfiles via a bare
    remote), one repo cloned, one symlink applied, supervisor + spa
    declared off so we don't shell into systemd. MCP registration
    skipped because no `claude` CLI in the test env.
    """
    # Build a separate bare-remote for the dotfiles repo, seeded with
    # our fake_dotfiles content.
    dotfiles_remote = tmp_path / "dotfiles.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(dotfiles_remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "init", "-q", "-b", "main"], check=True
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "config", "user.name", "t"], check=True
    )
    subprocess.run(["git", "-C", str(fake_dotfiles), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "commit", "-qm", "init"], check=True
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(fake_dotfiles),
            "remote",
            "add",
            "origin",
            str(dotfiles_remote),
        ],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "push", "-q", "origin", "main"], check=True
    )

    profile = Profile(
        name="test",
        dotfiles=DotfilesSpec(
            repo=str(dotfiles_remote),
            path=str(tmp_path / "user-dotfiles"),
            symlinks=[
                SymlinkEntry(
                    src="claude/CLAUDE.md", dest=str(tmp_path / "linked-CLAUDE.md")
                ),
            ],
        ),
        repos=[
            RepoSpec(name="x", url=str(fake_remote), path=str(tmp_path / "sibling")),
        ],
        mcp_servers=[McpServerSpec(name="never-registered", command="echo")],
    )
    # Force the MCP-register path to take the skipped branch.
    with patch("khimaira.bootstrap.operations.shutil.which", return_value=None):
        report = run_bootstrap(profile)

    statuses = [(r.op, r.status) for r in report.results]
    assert ("dotfiles-clone", "created") in statuses
    assert ("symlink", "created") in statuses
    assert ("clone", "created") in statuses
    assert ("mcp-register", "skipped") in statuses
    assert not report.had_failures


def test_run_sync_dotfiles_pull_idempotent(fake_dotfiles, tmp_path):
    """`khimaira sync` against an already-current repo reports unchanged.

    Mirrors the real flow: dev runs bootstrap, then sync some time later
    with no upstream changes; nothing should churn.
    """
    # Make a bare remote, push fake_dotfiles, clone it back into a
    # user-side path, then run sync. No upstream commits between =>
    # "Already up to date" branch.
    remote = tmp_path / "dotfiles.git"
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(remote)],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "init", "-q", "-b", "main"], check=True
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "config", "user.email", "t@t"], check=True
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "config", "user.name", "t"], check=True
    )
    subprocess.run(["git", "-C", str(fake_dotfiles), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "commit", "-qm", "init"], check=True
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "remote", "add", "origin", str(remote)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(fake_dotfiles), "push", "-q", "origin", "main"], check=True
    )

    user_path = tmp_path / "user-side"
    subprocess.run(["git", "clone", "-q", str(remote), str(user_path)], check=True)

    profile = Profile(
        name="t",
        dotfiles=DotfilesSpec(repo=str(remote), path=str(user_path)),
    )
    with patch("khimaira.bootstrap.operations.shutil.which", return_value=None):
        report = run_sync(profile)
    pulled = [r for r in report.results if r.op == "dotfiles-pull"]
    assert len(pulled) == 1
    assert pulled[0].status == "unchanged"
