"""Tests for the daemon deploy-fingerprint staleness guard."""

from __future__ import annotations

from khimaira.monitor import deploy_fingerprint as fp


def test_code_fingerprint_has_expected_keys():
    got = fp.code_fingerprint()
    assert set(got) == {"git_sha", "git_dirty", "source_mtime"}
    assert isinstance(got["source_mtime"], float)


def test_is_stale_identical_is_fresh():
    boot = {"git_sha": "abc", "git_dirty": False, "source_mtime": 100.0}
    assert fp.is_stale(boot, dict(boot)) is False


def test_is_stale_on_committed_sha_change():
    boot = {"git_sha": "abc", "git_dirty": False, "source_mtime": 100.0}
    current = {"git_sha": "def", "git_dirty": False, "source_mtime": 100.0}
    assert fp.is_stale(boot, current) is True


def test_is_stale_on_source_edited_after_boot():
    boot = {"git_sha": "abc", "git_dirty": False, "source_mtime": 100.0}
    current = {"git_sha": "abc", "git_dirty": True, "source_mtime": 200.0}
    assert fp.is_stale(boot, current) is True


def test_not_stale_when_mtime_unchanged_and_sha_same():
    boot = {"git_sha": "abc", "git_dirty": False, "source_mtime": 150.0}
    current = {"git_sha": "abc", "git_dirty": False, "source_mtime": 150.0}
    assert fp.is_stale(boot, current) is False


def test_is_stale_tolerates_missing_git_sha():
    # Non-git checkout: git_sha is None on both sides — fall back to mtime only.
    boot = {"git_sha": None, "git_dirty": None, "source_mtime": 100.0}
    current_same = {"git_sha": None, "git_dirty": None, "source_mtime": 100.0}
    current_newer = {"git_sha": None, "git_dirty": None, "source_mtime": 101.0}
    assert fp.is_stale(boot, current_same) is False
    assert fp.is_stale(boot, current_newer) is True
