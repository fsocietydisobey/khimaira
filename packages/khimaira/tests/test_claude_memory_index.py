"""Tests for khimaira.claude_memory_index — prune/archive tool.

Covers the acceptance criteria from the design consult:
1. Round-trip completeness (no entry lost).
2. No-op below budget.
3. Idempotent re-run.
4. mtime-driven ordering beats position.
5. Pin protection.
6. Malformed-line safety.
7. Archive append-only + dedup.
8. Dry-run purity.
9. Budget-driven, not just count-driven.
10. Concurrent-modification abort.
11. Detail-file immutability.
12. Realistic-format fixture (synthetic content, real bullet shape).

All fixtures are constructed programmatically (mirrors
test_backfill_member_roles.py's `_write_chat` helper pattern) — no real
memory content from the live khimaira/jeevy files is copied into this
file, and this suite never touches those live files.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
from khimaira.claude_memory_index import prune


def _write_memory_index(
    tmp_path: Path,
    entries: list[tuple[str, str, str]],
    *,
    filename: str = "MEMORY.md",
    make_topic_files: bool = True,
) -> Path:
    """Build a synthetic index file + its linked topic files.

    `entries` is a list of (title, link, summary) tuples. Returns the
    path to the written index file. Topic files are created with
    distinct, increasing mtimes matching entry order (entry 0 oldest)
    unless `make_topic_files=False`.
    """
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    lines = []
    for i, (title, link, summary) in enumerate(entries):
        lines.append(f"- [{title}]({link}) — {summary}")
        if make_topic_files:
            topic_path = memory_dir / link
            topic_path.write_text(f"# {title}\n\n{summary}\n", encoding="utf-8")
            # Force distinct, ordered mtimes (filesystem mtime resolution
            # can be coarse; bump explicitly rather than sleeping).
            mtime = time.time() - (len(entries) - i) * 10
            os.utime(topic_path, (mtime, mtime))

    index_path = memory_dir / filename
    index_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return index_path


# ---------------------------------------------------------------------------
# AC 1 — Round-trip completeness
# ---------------------------------------------------------------------------


def test_round_trip_completeness(tmp_path: Path):
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(10)]
    index_path = _write_memory_index(tmp_path, entries)

    result = prune(index_path=index_path, keep_entries=4)

    kept_links = {
        line.split("](")[1].split(")")[0]
        for line in result.index_text.splitlines()
        if line.startswith("-")
    }
    archived_links = {
        line.split("](")[1].split(")")[0]
        for line in result.archive_text.splitlines()
        if line.startswith("-")
    }
    all_links = {f"topic_{i}.md" for i in range(10)}

    assert kept_links | archived_links == all_links
    assert kept_links & archived_links == set(), "entry present in both index and archive"
    assert len(kept_links) == 4
    assert len(archived_links) == 6


# ---------------------------------------------------------------------------
# AC 2 — No-op below budget
# ---------------------------------------------------------------------------


def test_noop_below_budget(tmp_path: Path):
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(3)]
    index_path = _write_memory_index(tmp_path, entries)
    original_text = index_path.read_text(encoding="utf-8")

    result = prune(index_path=index_path, keep_entries=10)

    assert result.changed is False
    assert index_path.read_text(encoding="utf-8") == original_text
    archive_path = tmp_path / "memory" / "MEMORY_ARCHIVE.md"
    assert not archive_path.exists()


# ---------------------------------------------------------------------------
# AC 3 — Idempotent re-run
# ---------------------------------------------------------------------------


def test_idempotent_rerun(tmp_path: Path):
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(10)]
    index_path = _write_memory_index(tmp_path, entries)

    prune(index_path=index_path, keep_entries=4)
    index_after_first = index_path.read_text(encoding="utf-8")
    archive_path = tmp_path / "memory" / "MEMORY_ARCHIVE.md"
    archive_after_first = archive_path.read_text(encoding="utf-8")

    result_second = prune(index_path=index_path, keep_entries=4)

    assert result_second.changed is False
    assert index_path.read_text(encoding="utf-8") == index_after_first
    assert archive_path.read_text(encoding="utf-8") == archive_after_first


# ---------------------------------------------------------------------------
# AC 4 — mtime-driven ordering beats position
# ---------------------------------------------------------------------------


def test_mtime_ordering_beats_position(tmp_path: Path):
    """Entry positioned FIRST but with the NEWEST mtime must survive
    pruning over an entry positioned LATER with an OLDER mtime — models
    the real anomaly observed in khimaira's own live memory file."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    # Entry A: positioned first, but touched most recently (newest mtime).
    a_path = memory_dir / "topic_a.md"
    a_path.write_text("A\n", encoding="utf-8")
    # Entry B: positioned second, touched long ago (oldest mtime).
    b_path = memory_dir / "topic_b.md"
    b_path.write_text("B\n", encoding="utf-8")

    old_mtime = time.time() - 100_000
    new_mtime = time.time() - 10
    os.utime(b_path, (old_mtime, old_mtime))
    os.utime(a_path, (new_mtime, new_mtime))

    index_path = memory_dir / "MEMORY.md"
    index_path.write_text(
        "- [Entry A](topic_a.md) — newest mtime, first position\n"
        "- [Entry B](topic_b.md) — oldest mtime, second position\n",
        encoding="utf-8",
    )

    result = prune(index_path=index_path, keep_entries=1, sort="mtime")

    assert "topic_a.md" in result.index_text
    assert "topic_b.md" not in result.index_text
    assert "topic_b.md" in result.archive_text


def test_position_sort_keeps_last_appearing(tmp_path: Path):
    """With --sort position, ranking ignores mtime and uses line order
    (last-appearing = highest priority)."""
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(3)]
    index_path = _write_memory_index(tmp_path, entries)

    result = prune(index_path=index_path, keep_entries=1, sort="position")

    assert "topic_2.md" in result.index_text
    assert "topic_0.md" in result.archive_text
    assert "topic_1.md" in result.archive_text


# ---------------------------------------------------------------------------
# AC 5 — Pin protection
# ---------------------------------------------------------------------------


def test_pin_protection(tmp_path: Path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    profile_path = memory_dir / "user_profile.md"
    profile_path.write_text("profile\n", encoding="utf-8")
    old_mtime = time.time() - 500_000
    os.utime(profile_path, (old_mtime, old_mtime))

    recent_entries = []
    for i in range(5):
        p = memory_dir / f"topic_{i}.md"
        p.write_text(f"topic {i}\n", encoding="utf-8")
        mtime = time.time() - i
        os.utime(p, (mtime, mtime))
        recent_entries.append(f"- [Topic {i}](topic_{i}.md) — recent entry {i}\n")

    index_path = memory_dir / "MEMORY.md"
    index_path.write_text(
        "- [User profile](user_profile.md) — evergreen identity fact, rarely touched\n"
        + "".join(recent_entries),
        encoding="utf-8",
    )

    result = prune(
        index_path=index_path,
        keep_entries=2,
        sort="mtime",
        pins=["user_profile.md"],
    )

    assert "user_profile.md" in result.index_text
    assert "user_profile.md" not in result.archive_text


# ---------------------------------------------------------------------------
# AC 6 — Malformed-line safety
# ---------------------------------------------------------------------------


def test_malformed_line_never_dropped_or_archived(tmp_path: Path):
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    for i in range(3):
        (memory_dir / f"topic_{i}.md").write_text(f"topic {i}\n", encoding="utf-8")

    index_path = memory_dir / "MEMORY.md"
    index_path.write_text(
        "- [Topic 0](topic_0.md) — entry zero\n"
        "- this line has no link at all, malformed\n"
        "\n"
        "- [Topic 1](topic_1.md) — entry one\n"
        "- [Topic 2](topic_2.md) — entry two\n",
        encoding="utf-8",
    )

    result = prune(index_path=index_path, keep_entries=1)

    assert "this line has no link at all, malformed" in result.index_text
    assert "this line has no link at all, malformed" not in result.archive_text
    assert result.skipped_malformed == 1


# ---------------------------------------------------------------------------
# AC 7 — Archive append-only + dedup
# ---------------------------------------------------------------------------


def test_archive_append_only_dedup(tmp_path: Path):
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(6)]
    index_path = _write_memory_index(tmp_path, entries)
    archive_path = tmp_path / "memory" / "MEMORY_ARCHIVE.md"

    prune(index_path=index_path, keep_entries=4)
    first_archive = archive_path.read_text(encoding="utf-8")
    assert first_archive.count("topic_0.md") == 1
    assert first_archive.count("topic_1.md") == 1

    # Add two NEW entries, then prune again with the same budget — the
    # newly-archived pair should append, not duplicate the first two.
    with index_path.open("a", encoding="utf-8") as f:
        for i in (6, 7):
            f.write(f"- [Title {i}](topic_{i}.md) — summary {i}\n")
    memory_dir = tmp_path / "memory"
    for i in (6, 7):
        p = memory_dir / f"topic_{i}.md"
        p.write_text(f"topic {i}\n", encoding="utf-8")
        os.utime(p, (time.time() - 500 + i, time.time() - 500 + i))

    prune(index_path=index_path, keep_entries=4)
    second_archive = archive_path.read_text(encoding="utf-8")

    assert second_archive.count("topic_0.md") == 1, "duplicate archive entry for topic_0"
    assert second_archive.count("topic_1.md") == 1, "duplicate archive entry for topic_1"
    assert "topic_6.md" in second_archive or "topic_7.md" in second_archive


# ---------------------------------------------------------------------------
# AC 8 — Dry-run purity
# ---------------------------------------------------------------------------


def test_dry_run_purity(tmp_path: Path):
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(10)]
    index_path = _write_memory_index(tmp_path, entries)
    original_index_text = index_path.read_text(encoding="utf-8")
    archive_path = tmp_path / "memory" / "MEMORY_ARCHIVE.md"

    result = prune(index_path=index_path, keep_entries=4, dry_run=True)

    assert result.changed is True  # would-change is still reported
    assert index_path.read_text(encoding="utf-8") == original_index_text
    assert not archive_path.exists()


# ---------------------------------------------------------------------------
# AC 9 — Budget-driven, not just count-driven
# ---------------------------------------------------------------------------


def test_byte_budget_prunes_even_under_count_budget(tmp_path: Path):
    long_summary = "x" * 500
    entries = [(f"Title {i}", f"topic_{i}.md", long_summary) for i in range(5)]
    index_path = _write_memory_index(tmp_path, entries)

    # keep_entries=10 would keep everything; max_bytes forces a prune.
    result = prune(index_path=index_path, keep_entries=10, max_bytes=600)

    assert result.kept_count < 5
    assert len(result.index_text.encode("utf-8")) <= 600


# ---------------------------------------------------------------------------
# AC 10 — Concurrent-modification abort
# ---------------------------------------------------------------------------


def test_concurrent_modification_aborts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(10)]
    index_path = _write_memory_index(tmp_path, entries)
    original_text = index_path.read_text(encoding="utf-8")

    from khimaira import claude_memory_index as cmi

    real_stat = Path.stat
    call_count = {"n": 0}

    def _flaky_stat(self, *args, **kwargs):
        call_count["n"] += 1
        # First stat() call is the pre-read capture: return the REAL
        # (pre-mutation) stat, then mutate the file afterward so the
        # second (pre-write) stat() call observes a genuine change.
        stat_result = real_stat(self, *args, **kwargs)
        if call_count["n"] == 1 and self == index_path:
            index_path.write_text(original_text + "\n<concurrent writer edit>\n", encoding="utf-8")
        return stat_result

    monkeypatch.setattr(Path, "stat", _flaky_stat)

    result = cmi.prune(index_path=index_path, keep_entries=4)

    assert result.aborted_concurrent_modification is True
    # No archive should have been created.
    archive_path = tmp_path / "memory" / "MEMORY_ARCHIVE.md"
    assert not archive_path.exists()


def test_force_bypasses_concurrent_guard(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(10)]
    index_path = _write_memory_index(tmp_path, entries)
    original_text = index_path.read_text(encoding="utf-8")

    from khimaira import claude_memory_index as cmi

    real_stat = Path.stat
    call_count = {"n": 0}

    def _flaky_stat(self, *args, **kwargs):
        call_count["n"] += 1
        stat_result = real_stat(self, *args, **kwargs)
        if call_count["n"] == 1 and self == index_path:
            index_path.write_text(original_text + "\n<concurrent writer edit>\n", encoding="utf-8")
        return stat_result

    monkeypatch.setattr(Path, "stat", _flaky_stat)

    result = cmi.prune(index_path=index_path, keep_entries=4, force=True)

    assert result.aborted_concurrent_modification is False


# ---------------------------------------------------------------------------
# AC 11 — Detail-file immutability
# ---------------------------------------------------------------------------


def test_detail_files_untouched(tmp_path: Path):
    entries = [(f"Title {i}", f"topic_{i}.md", f"summary {i}") for i in range(8)]
    index_path = _write_memory_index(tmp_path, entries)
    memory_dir = tmp_path / "memory"

    before = {
        p.name: (p.stat().st_mtime_ns, p.read_text(encoding="utf-8"))
        for p in memory_dir.glob("topic_*.md")
    }

    prune(index_path=index_path, keep_entries=3)

    after = {
        p.name: (p.stat().st_mtime_ns, p.read_text(encoding="utf-8"))
        for p in memory_dir.glob("topic_*.md")
    }

    assert before == after
    assert set(before.keys()) == {f"topic_{i}.md" for i in range(8)}


# ---------------------------------------------------------------------------
# AC 12 — Realistic-format fixture
# ---------------------------------------------------------------------------


def test_realistic_format_fixture(tmp_path: Path):
    """Synthetic content mirroring the real files' actual shape: backticks,
    em dashes, commit hashes, #NNN refs, varied lengths. No real note text
    copied — content invented for this test only."""
    memory_dir = tmp_path / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)

    realistic_entries = [
        (
            "user_profile.md",
            "- [User profile](user_profile.md) — Alex: backend-leaning, "
            "expects depth and directness in review feedback",
        ),
        (
            "project_widget_cache_fix.md",
            "- [Widget cache invalidation fixed](project_widget_cache_fix.md) — "
            "`WidgetCache.invalidate()` wasn't firing on delete (commit a1b2c3d, "
            "closes #142); root cause was a missing signal handler registration",
        ),
        (
            "feedback_prefer_typed_configs.md",
            "- [Prefer typed configs](feedback_prefer_typed_configs.md) — "
            "use `pydantic.BaseSettings` over raw `os.environ` reads; "
            "corrected 2026-03-11 after a silent misconfiguration shipped",
        ),
        (
            "reference_deploy_pipeline.md",
            "- [Deploy pipeline map](reference_deploy_pipeline.md) — "
            "CI → staging (auto) → prod (manual approval gate); rollback via "
            "`./deploy.sh --rollback <sha>`; see #98 for the incident that "
            "added the manual gate",
        ),
    ]

    for filename, _ in realistic_entries:
        (memory_dir / filename).write_text(f"# {filename}\ndetail body\n", encoding="utf-8")
    for i, (filename, _) in enumerate(realistic_entries):
        mtime = time.time() - (len(realistic_entries) - i) * 1000
        os.utime(memory_dir / filename, (mtime, mtime))

    index_path = memory_dir / "MEMORY.md"
    index_path.write_text("\n".join(line for _, line in realistic_entries) + "\n", encoding="utf-8")

    result = prune(
        index_path=index_path,
        keep_entries=2,
        pins=["user_profile.md"],
    )

    kept_links = {
        line.split("](")[1].split(")")[0]
        for line in result.index_text.splitlines()
        if line.startswith("-")
    }
    all_links = {f for f, _ in realistic_entries}
    archived_links = {
        line.split("](")[1].split(")")[0]
        for line in result.archive_text.splitlines()
        if line.startswith("-")
    }

    assert "user_profile.md" in kept_links, "pinned entry must survive"
    assert kept_links | archived_links == all_links
    assert len(kept_links) == 2


# ---------------------------------------------------------------------------
# Extra: missing budget arg raises
# ---------------------------------------------------------------------------


def test_no_budget_raises(tmp_path: Path):
    entries = [("Title", "topic.md", "summary")]
    index_path = _write_memory_index(tmp_path, entries)

    with pytest.raises(ValueError):
        prune(index_path=index_path)
