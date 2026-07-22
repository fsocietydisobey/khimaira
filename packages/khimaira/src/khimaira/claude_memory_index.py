"""Prune + archive a Claude Code native auto-memory index (`MEMORY.md`).

Claude Code's own auto-memory feature (code.claude.com/docs/en/memory)
auto-loads the FIRST 200 lines or FIRST 25KB (whichever hits first) of
`memory/MEMORY.md` into every session's boot context — in full, every
session, no pruning. Every other file in that same `memory/` directory
("topic files") is only read on demand, never auto-injected. That's the
two-tier shape this codebase's own memory already follows (one-line index
bullet + pointer to a same-directory detail file) — but neither khimaira's
nor jeevy's `MEMORY.md` has ever had the other half of that convention:
a hard cap + archive so the index doesn't grow unboundedly and blow past
the native truncation threshold with no legible curation.

This module is that missing half: parse the bullet-list index, rank
entries by a deterministic recency signal (never LLM-judged relevance —
see ~/dotfiles/claude/rules/engineering/ai-engineering.md on load-bearing
determinism), keep the N most recent/relevant under a byte/line budget,
and relocate (never delete) the rest into a sibling archive file that
Claude Code does not auto-load. Detail/topic files are never touched or
moved — only the index bullet line relocates between index and archive.

SAFETY: writes use an atomic replace and abort if the source changed between
read and write. The higher-level ``khimaira.claude_memory_retrieval`` module
wires this operation into Stop, a daemon timer, and a manual CLI; all three use
the same concurrency guard and deterministic policy.

Usage:
    uv run python -m khimaira.claude_memory_index \\
        --index path/to/MEMORY.md \\
        [--archive path/to/MEMORY_ARCHIVE.md] \\
        [--keep-entries N] [--max-bytes N] [--max-lines N] \\
        [--sort {mtime,position}] \\
        [--pin SUBSTRING ...] \\
        [--dry-run] [--force]
"""

from __future__ import annotations

import argparse
import contextlib
import os
import re
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path

# Matches "- [Title](link.md) — free text" style bullet entries. Title, link,
# and body are captured so the same parser can feed both pruning and retrieval.
# Entries whose link can't be resolved fall back to file position for ranking.
_ENTRY_RE = re.compile(r"^-\s*\[([^\]]*)\]\(([^)]+)\)\s*(?:—|--|-)\s*(.*)$")

# Anthropic's own native auto-memory truncation thresholds (docs, 2026):
# first 200 lines OR first 25KB, whichever comes first. Not enforced by
# this tool by default — exposed as opt-in budget knobs (--max-lines /
# --max-bytes) so callers can target the real ceiling directly.
NATIVE_MAX_LINES = 200
NATIVE_MAX_BYTES = 25 * 1024


@dataclass
class Entry:
    """One parsed index bullet line."""

    raw: str
    """The exact original line text (no trailing newline)."""

    link: str | None
    """Resolved link target inside the parenthesis, if the line matched
    the bullet-entry pattern. None for unparseable / non-entry lines."""

    position: int
    """0-based original line index — used as the ranking fallback."""

    title: str | None = None
    """Human-readable link title, when the line parsed as an entry."""

    body: str | None = None
    """Summary text after the entry delimiter, when parsed."""

    @property
    def is_entry(self) -> bool:
        return self.link is not None


def _parse_index(text: str) -> list[Entry]:
    """Parse an index file's lines into `Entry` records.

    Blank lines, headers, and any line that doesn't match the bullet
    pattern are still returned as Entry objects with `link=None` — they
    are never eligible for archival (we can't confidently classify them)
    and are always kept in their original relative order.
    """
    entries: list[Entry] = []
    for i, line in enumerate(text.splitlines()):
        match = _ENTRY_RE.match(line)
        title = match.group(1).strip() if match else None
        link = match.group(2).strip() if match else None
        body = match.group(3).strip() if match else None
        entries.append(Entry(raw=line, link=link, position=i, title=title, body=body))
    return entries


def _entry_key(link: str) -> str:
    """Dedup/identity key for an entry: its link target, normalized."""
    return link.strip()


def _mtime_for(entry: Entry, index_dir: Path) -> float:
    """Recency signal for one entry: linked topic file's mtime.

    Falls back to -1 (oldest possible) when the link doesn't resolve to
    an existing file, so unresolvable links sort as least-recent under
    --sort mtime rather than crashing or silently ranking first.
    """
    if entry.link is None:
        return -1.0
    target = (index_dir / entry.link).resolve()
    try:
        return target.stat().st_mtime
    except OSError:
        return -1.0


def _rank(
    entries: list[Entry],
    *,
    index_dir: Path,
    sort: str,
) -> list[Entry]:
    """Return entry-list entries (only, no blank/header lines) ranked
    most-recent/relevant FIRST, per --sort.

    `position` sort ranks the LAST-appearing entry first (position is a
    proxy for append order); `mtime` sort ranks the newest linked-file
    mtime first, falling back to position for ties/unresolvable links.
    """
    real_entries = [e for e in entries if e.is_entry]
    if sort == "position":
        return sorted(real_entries, key=lambda e: -e.position)
    if sort == "mtime":
        return sorted(
            real_entries,
            key=lambda e: (_mtime_for(e, index_dir), e.position),
            reverse=True,
        )
    raise ValueError(f"Unknown sort mode: {sort!r}")


def _is_pinned(entry: Entry, pins: list[str]) -> bool:
    return any(p in entry.raw for p in pins)


def _rebuild_text(entries: list[Entry], kept_links: set[str]) -> str:
    """Rebuild index text preserving original line order.

    Non-entry lines (blank/header/malformed) are always kept. Entry
    lines are kept only if their link is in `kept_links`.
    """
    lines = [
        e.raw
        for e in entries
        if (not e.is_entry) or (_entry_key(e.link) in kept_links)  # type: ignore[arg-type]
    ]
    text = "\n".join(lines)
    if text and not text.endswith("\n"):
        text += "\n"
    return text


def _atomic_write_text(path: Path, text: str) -> None:
    """Write `text` to `path` atomically (unique tmp file + os.replace).

    Mirrors khimaira.monitor.sessions._atomic_write_json's idiom: unique
    per-call tmp filename (pid + random suffix), then an atomic rename,
    so no concurrent reader ever observes a torn/partial file.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


@dataclass
class PruneResult:
    """Outcome of a `prune()` call, for callers/tests/CLI reporting."""

    kept_count: int
    archived_count: int
    already_archived_count: int
    skipped_malformed: int
    changed: bool
    aborted_concurrent_modification: bool
    index_text: str
    archive_text: str


def prune(
    *,
    index_path: Path,
    archive_path: Path | None = None,
    keep_entries: int | None = None,
    max_bytes: int | None = None,
    max_lines: int | None = None,
    sort: str = "mtime",
    pins: list[str] | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> PruneResult:
    """Prune `index_path` down to a budget, archiving the rest.

    At least one of keep_entries/max_bytes/max_lines must be given; the
    most restrictive constraint wins (i.e. all supplied constraints are
    enforced simultaneously — kept entries are trimmed until every
    constraint that was supplied is satisfied).

    Never deletes content: every original entry ends up in exactly one
    of {pruned index, archive}. Pinned entries (substring match against
    the raw line) are never archived, regardless of rank or budget.

    Idempotent: a no-op on an already-under-budget index; a second
    consecutive run after a real prune also produces no further change
    (archive dedups by link — already-archived entries are skipped, not
    re-appended).

    Concurrency guard (default ON, bypass with force=True): the index
    file's (mtime, size) is captured before ranking and re-checked right
    before the write. If it changed — a different writer (Claude Code's
    own native auto-memory writer, or a sibling session) touched the
    file in between — the prune aborts with NO writes to either file.
    """
    if keep_entries is None and max_bytes is None and max_lines is None:
        raise ValueError("At least one of --keep-entries/--max-bytes/--max-lines is required.")

    pins = pins or []
    index_path = Path(index_path)
    index_dir = index_path.parent
    if archive_path is None:
        archive_path = index_dir / f"{index_path.stem}_ARCHIVE{index_path.suffix}"
    archive_path = Path(archive_path)

    original_text = index_path.read_text(encoding="utf-8")
    stat_before = index_path.stat()

    entries = _parse_index(original_text)
    real_entries = [e for e in entries if e.is_entry]
    skipped_malformed = sum(1 for e in entries if not e.is_entry and e.raw.strip().startswith("-"))

    ranked = _rank(entries, index_dir=index_dir, sort=sort)

    # Walk ranked (most-recent-first) entries, keeping the prefix that
    # satisfies every supplied budget. Pinned entries are always kept
    # and don't consume budget slots (they're evergreen by design).
    pinned_links = {
        _entry_key(e.link)  # type: ignore[arg-type]
        for e in real_entries
        if _is_pinned(e, pins)
    }

    kept_links: set[str] = set(pinned_links)

    def _within_budget(candidate_links: set[str]) -> bool:
        candidate_entries = [e for e in real_entries if _entry_key(e.link) in candidate_links]  # type: ignore[arg-type]
        if keep_entries is not None and len(candidate_entries) > keep_entries:
            return False
        rebuilt = _rebuild_text(entries, candidate_links)
        if max_bytes is not None and len(rebuilt.encode("utf-8")) > max_bytes:
            return False
        return not (max_lines is not None and len(rebuilt.splitlines()) > max_lines)

    for entry in ranked:
        key = _entry_key(entry.link)  # type: ignore[arg-type]
        if key in kept_links:
            continue
        candidate = kept_links | {key}
        if _within_budget(candidate):
            kept_links.add(key)
        # Deliberately no `break` when a candidate doesn't fit: entries
        # vary in byte length (3-4x in the real files), so a lower-
        # priority (older) entry can still be SMALLER and fit under a
        # byte/line budget even after a higher-priority entry didn't.
        # Only --keep-entries alone would make an early break valid;
        # scanning the remainder keeps byte/line budgets correct too and
        # the extra cost is negligible for index files this size.

    archived_entries = [e for e in real_entries if _entry_key(e.link) not in kept_links]  # type: ignore[arg-type]

    new_index_text = _rebuild_text(entries, kept_links)

    # Build the archive addition, deduping against what's already archived.
    existing_archive_text = (
        archive_path.read_text(encoding="utf-8") if archive_path.exists() else ""
    )
    existing_archive_links = {
        _entry_key(e.link)  # type: ignore[arg-type]
        for e in _parse_index(existing_archive_text)
        if e.is_entry
    }
    to_append = [e for e in archived_entries if _entry_key(e.link) not in existing_archive_links]
    already_archived_count = len(archived_entries) - len(to_append)

    if to_append:
        addition = "\n".join(e.raw for e in to_append) + "\n"
        if existing_archive_text and not existing_archive_text.endswith("\n"):
            existing_archive_text += "\n"
        new_archive_text = existing_archive_text + addition
    else:
        new_archive_text = existing_archive_text

    # Self-check: every original entry must land in exactly one of
    # {kept, archived (new or pre-existing)} before any write happens.
    all_links = {_entry_key(e.link) for e in real_entries}  # type: ignore[arg-type]
    accounted_for = kept_links | {_entry_key(e.link) for e in archived_entries}  # type: ignore[arg-type]
    if all_links != accounted_for:
        missing = all_links - accounted_for
        raise AssertionError(
            f"Prune invariant violated — entries lost: {missing!r}. Aborting, no writes made."
        )

    changed = new_index_text != original_text
    archive_changed = bool(to_append)

    if dry_run or not (changed or archive_changed):
        return PruneResult(
            kept_count=len(kept_links),
            archived_count=len(archived_entries),
            already_archived_count=already_archived_count,
            skipped_malformed=skipped_malformed,
            changed=changed,
            aborted_concurrent_modification=False,
            index_text=new_index_text,
            archive_text=new_archive_text,
        )

    if not force:
        stat_now = index_path.stat()
        if (
            stat_now.st_mtime_ns != stat_before.st_mtime_ns
            or stat_now.st_size != stat_before.st_size
        ):
            return PruneResult(
                kept_count=len(kept_links),
                archived_count=len(archived_entries),
                already_archived_count=already_archived_count,
                skipped_malformed=skipped_malformed,
                changed=False,
                aborted_concurrent_modification=True,
                index_text=original_text,
                archive_text=existing_archive_text,
            )

    if archive_changed:
        _atomic_write_text(archive_path, new_archive_text)
    if changed:
        _atomic_write_text(index_path, new_index_text)

    return PruneResult(
        kept_count=len(kept_links),
        archived_count=len(archived_entries),
        already_archived_count=already_archived_count,
        skipped_malformed=skipped_malformed,
        changed=changed,
        aborted_concurrent_modification=False,
        index_text=new_index_text,
        archive_text=new_archive_text,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="khimaira.claude_memory_index",
        description=(
            "Prune a Claude Code native auto-memory index (MEMORY.md) down "
            "to a budget, archiving relocated entries to a sibling file "
            "Claude Code does NOT auto-load. Never deletes content."
        ),
    )
    parser.add_argument("--index", required=True, type=Path, help="Path to MEMORY.md.")
    parser.add_argument(
        "--archive",
        type=Path,
        default=None,
        help="Path to the archive file (default: <index-stem>_ARCHIVE.md next to --index).",
    )
    parser.add_argument("--keep-entries", type=int, default=None, help="Max entries to keep.")
    parser.add_argument("--max-bytes", type=int, default=None, help="Max index file size in bytes.")
    parser.add_argument("--max-lines", type=int, default=None, help="Max index file line count.")
    parser.add_argument(
        "--sort",
        choices=["mtime", "position"],
        default="mtime",
        help=(
            "Recency signal: 'mtime' (default) uses each entry's linked topic "
            "file's mtime — recommended, since list position is NOT reliably "
            "chronological in observed real files. 'position' ranks by last-"
            "appearing-first in the index instead."
        ),
    )
    parser.add_argument(
        "--pin",
        action="append",
        default=[],
        dest="pins",
        help="Substring match — entries containing it are never archived. Repeatable.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report without writing.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Bypass the concurrent-modification abort guard.",
    )
    args = parser.parse_args(argv)

    if args.keep_entries is None and args.max_bytes is None and args.max_lines is None:
        parser.error("At least one of --keep-entries/--max-bytes/--max-lines is required.")

    if not args.index.is_file():
        print(f"error: --index does not exist or is not a file: {args.index}", file=sys.stderr)
        return 1

    result = prune(
        index_path=args.index,
        archive_path=args.archive,
        keep_entries=args.keep_entries,
        max_bytes=args.max_bytes,
        max_lines=args.max_lines,
        sort=args.sort,
        pins=args.pins,
        dry_run=args.dry_run,
        force=args.force,
    )

    if result.aborted_concurrent_modification:
        print(
            "ABORTED: --index was modified by another writer between read and "
            "write. No changes made. Re-run once the file is quiet, or pass "
            "--force to bypass this guard.",
            file=sys.stderr,
        )
        return 1

    mode = "DRY RUN" if args.dry_run else ("APPLIED" if result.changed else "NO-OP")
    print(
        f"[{mode}] kept={result.kept_count} archived_new={result.archived_count - result.already_archived_count} "
        f"already_archived={result.already_archived_count} skipped_malformed_lines={result.skipped_malformed}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
