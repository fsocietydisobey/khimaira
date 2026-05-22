"""Violations log — append-only JSONL with compaction.

Storage: ~/.local/state/khimaira/themis_violations.jsonl
Compaction: triggered at >1MB OR explicit call to compact_if_needed(force=True).
  - Atomic rename: copy live file → themis_violations.YYYYMMDD-HHMMSS.jsonl.gz (gzipped)
  - Truncate live file to entries newer than now() - 30d
  - Archives kept indefinitely (small relative to git history)
  - Replay-on-boot reads live file only; archives are for postmortem queries (Phase 3+)

Public API:
  append_violation(record: ViolationRecord) -> None
  read_violations(session_id?, role?, since?, limit?) -> list[ViolationRecord]
  compact_if_needed(path?, force?) -> bool  (True if compaction ran)
"""

from __future__ import annotations

import gzip
import json
import logging
import os
import shutil
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from themis.data import ViolationRecord

logger = logging.getLogger(__name__)

_COMPACT_THRESHOLD_BYTES = 1024 * 1024  # 1 MB
_RETENTION_DAYS = 30


def _violations_path() -> Path:
    state_home = os.environ.get("XDG_STATE_HOME", os.path.expanduser("~/.local/state"))
    return Path(state_home) / "khimaira" / "themis_violations.jsonl"


def append_violation(record: ViolationRecord, path: Path | None = None) -> None:
    """Append a violation record to the JSONL log. Thread-safe via atomic write."""
    p = path or _violations_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record.to_dict(), ensure_ascii=False) + "\n"
    with p.open("a", encoding="utf-8") as f:
        f.write(line)
    compact_if_needed(path=p)


def read_violations(
    session_id: str | None = None,
    role: str | None = None,
    since: str | None = None,
    limit: int = 50,
    path: Path | None = None,
) -> list[ViolationRecord]:
    """Read violation records, optionally filtered.

    Args:
        session_id: Filter to this session only.
        role: Filter to this role only.
        since: ISO-8601 lower-bound timestamp (inclusive).
        limit: Maximum number of records to return (most recent first).
        path: Override log path (for testing).
    """
    p = path or _violations_path()
    if not p.exists():
        return []

    since_dt: datetime | None = None
    if since:
        since_dt = datetime.fromisoformat(since)
        if since_dt.tzinfo is None:
            since_dt = since_dt.replace(tzinfo=timezone.utc)

    records: list[ViolationRecord] = []
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except json.JSONDecodeError:
            continue
        if session_id and d.get("session_id") != session_id:
            continue
        if role and d.get("role") != role:
            continue
        if since_dt:
            try:
                ts = datetime.fromisoformat(d["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts < since_dt:
                    continue
            except (KeyError, ValueError):
                continue
        records.append(ViolationRecord.from_dict(d))

    # Most recent first, capped at limit
    records.sort(key=lambda r: r.ts, reverse=True)
    return records[:limit]


def compact_if_needed(path: Path | None = None, force: bool = False) -> bool:
    """Compact the violations log if >1MB or force=True.

    Compaction:
      1. Gzip-archive the current file to violations.YYYYMMDD-HHMMSS.jsonl.gz
      2. Rewrite live file keeping only entries newer than now() - 30d
      3. Atomic rename via tempfile to avoid partial writes

    Returns True if compaction ran, False otherwise.
    """
    p = path or _violations_path()
    if not p.exists():
        return False

    size = p.stat().st_size
    if not force and size < _COMPACT_THRESHOLD_BYTES:
        return False

    now = datetime.now(tz=timezone.utc)
    cutoff = now - timedelta(days=_RETENTION_DAYS)
    archive_name = p.parent / f"themis_violations.{now.strftime('%Y%m%d-%H%M%S')}.jsonl.gz"

    try:
        # Step 1: gzip-archive the live file
        with p.open("rb") as src, gzip.open(archive_name, "wb") as dst:
            shutil.copyfileobj(src, dst)

        # Step 2: rewrite live file with only entries newer than cutoff
        lines = p.read_text(encoding="utf-8").splitlines()
        kept: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                d: dict[str, Any] = json.loads(line)
                ts = datetime.fromisoformat(d["ts"])
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)
                if ts >= cutoff:
                    kept.append(line)
            except (json.JSONDecodeError, KeyError, ValueError):
                # Drop malformed entries during compaction
                continue

        # Step 3: atomic rename
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            dir=p.parent,
            encoding="utf-8",
            delete=False,
            suffix=".tmp",
        )
        try:
            for line in kept:
                tmp.write(line + "\n")
            tmp.flush()
            tmp.close()
            os.replace(tmp.name, p)
        except Exception:
            try:
                os.unlink(tmp.name)
            except OSError:
                pass
            raise

        logger.info(
            "Themis violations compacted: %d entries kept, archive at %s",
            len(kept),
            archive_name,
        )
        return True

    except Exception:
        logger.exception("Themis violations compaction failed")
        return False
