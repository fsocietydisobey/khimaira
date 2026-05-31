#!/usr/bin/env python3
"""Detect terminal rate-limit (529 overload) exhaustion in a CC transcript.

Background — #13b-heavy. Claude Code's SDK auto-retries HTTP 529
("overloaded") responses up to ``CLAUDE_CODE_MAX_RETRIES`` (default 10)
*below* the turn boundary. Two outcomes:

* **Recovered** — a retry eventually succeeds, the turn completes, and a
  normal ``assistant`` / ``turn_duration`` record follows the 529 storm.
  Nothing is wrong; the user never needed to know.
* **Terminal** — every retry fails, CC surfaces the raw overload error to
  the terminal, and the *turn ends*. The session is now idle awaiting user
  input and will NOT resume on its own.

The terminal case is the gap: a roster session silently stops mid-work with
no khimaira-side alert (Guard-4 doesn't fire when the session has no task
obligation). This module is the detector — a pure function over the
transcript tail, called from the Stop hook at turn exit.

THE GATE (load-bearing — empirically grounded on real transcripts):
A 529 *existing* in the transcript does NOT mean the turn failed — a
recovered turn has the exact same mid-loop 529 records PLUS a trailing
success. So the gate is **terminal-outcome**, not "a 529 is present":

    terminal  ⟺  the LAST qualifying 529-overload api_error record has NO
                 success record (assistant / turn_duration / result) after it.

Record shape (audit-grade, observed in real CC 2.1.x transcripts)::

    {"type": "system", "subtype": "api_error", "level": "error",
     "error": {"status": 529,
               "headers": {"x-should-retry": "true", ...},
               "type": "overloaded_error", ...},
     "retryAttempt": 7, "maxRetries": 10, "timestamp": "...", ...}

Only ``status == 529`` with ``x-should-retry == "true"`` counts — an
auth/billing api_error (4xx, no-retry) at turn end is NOT a transient
overload and must NOT trip detection.

Stdlib only; fail-open by design (the caller is a Stop hook that must never
block CC from exiting).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# Default tail budget. The 529 retry storm is ~maxRetries records a few
# seconds apart (~27 records × ~1.5 KB ≈ 40 KB observed). 512 KB covers the
# whole storm plus the surrounding turn with wide margin, and bounds the
# read so a multi-MB transcript doesn't get slurped whole at turn exit.
_DEFAULT_TAIL_BYTES = int(os.environ.get("KHIMAIRA_THROTTLE_TAIL_BYTES", str(512 * 1024)))

# Record types that mean "the model produced output / the turn completed" —
# i.e. CC recovered. Their presence AFTER the last 529 means NOT terminal.
#
# NOT included: "user". CC writes tool-results as type=="user" records, which
# appear mid-turn — a "user" record after a 529 does not imply the turn
# completed successfully, so counting it would mask real exhaustions.
_SUCCESS_TYPES = frozenset({"assistant", "result"})
_SUCCESS_SYSTEM_SUBTYPES = frozenset({"turn_duration"})


def _is_overload_529(rec: dict[str, Any]) -> bool:
    """True iff ``rec`` is a transient 529-overload api_error that CC retries.

    Requires type/subtype == system/api_error, error.status == 529, and the
    ``x-should-retry`` response header == "true". Auth/billing errors (4xx,
    no retry) and non-error records return False.
    """
    if rec.get("type") != "system" or rec.get("subtype") != "api_error":
        return False
    err = rec.get("error")
    if not isinstance(err, dict):
        return False
    if err.get("status") != 529:
        return False
    headers = err.get("headers")
    if not isinstance(headers, dict):
        # Be lenient: status 529 is overload by definition; treat a 529 with
        # no readable headers as retryable rather than dropping a real one.
        return True
    # Header keys are lowercased by CC; value is the string "true".
    return str(headers.get("x-should-retry", "")).lower() == "true"


def _is_success(rec: dict[str, Any]) -> bool:
    """True iff ``rec`` marks the turn making forward progress / completing."""
    t = rec.get("type")
    if t in _SUCCESS_TYPES:
        return True
    if t == "system" and rec.get("subtype") in _SUCCESS_SYSTEM_SUBTYPES:
        return True
    return False


def _read_tail_records(transcript_path: str, tail_bytes: int) -> list[dict[str, Any]]:
    """Parse the last ``tail_bytes`` of a JSONL transcript into record dicts.

    Drops a leading partial line (the byte window almost always starts
    mid-line) and silently skips unparseable lines. Returns records in file
    order. Returns [] on any I/O error.
    """
    try:
        path = Path(transcript_path)
        size = path.stat().st_size
        start = max(0, size - tail_bytes)
        with path.open("rb") as f:
            f.seek(start)
            chunk = f.read()
    except OSError:
        return []

    text = chunk.decode("utf-8", errors="replace")
    lines = text.split("\n")
    # If we started mid-file, the first line is probably a fragment — drop it.
    if start > 0 and lines:
        lines = lines[1:]

    records: list[dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            records.append(rec)
    return records


def detect_terminal_overload(
    transcript_path: str | None,
    tail_bytes: int = _DEFAULT_TAIL_BYTES,
) -> dict[str, Any] | None:
    """Return a verdict dict if the transcript's last turn ended on an
    unrecovered 529 overload, else None.

    The verdict carries enough context for the daemon to alert::

        {"terminal": True,
         "retry_attempt": <int max retryAttempt seen in the storm>,
         "max_retries": <int>,
         "overload_count": <int # of 529 records in the tail>,
         "last_timestamp": <iso str | None>,
         "message": <overload_error message | None>}

    Returns None when:
      * transcript_path is falsy / unreadable,
      * no 529-overload record is in the tail, or
      * a success record (assistant / turn_duration / result) follows the
        last 529 (CC recovered — the critical false-positive guard).
    """
    if not transcript_path:
        return None

    records = _read_tail_records(transcript_path, tail_bytes)
    if not records:
        return None

    last_overload_idx: int | None = None
    last_success_idx: int | None = None
    overload_count = 0
    max_retry_attempt = 0
    max_retries = 0
    last_overload_rec: dict[str, Any] | None = None

    for idx, rec in enumerate(records):
        if _is_overload_529(rec):
            last_overload_idx = idx
            last_overload_rec = rec
            overload_count += 1
            try:
                max_retry_attempt = max(max_retry_attempt, int(rec.get("retryAttempt", 0)))
                max_retries = max(max_retries, int(rec.get("maxRetries", 0)))
            except (TypeError, ValueError):
                pass
        elif _is_success(rec):
            last_success_idx = idx

    if last_overload_idx is None:
        return None  # no overload in the tail at all

    # Recovered: a success record follows the last 529 → NOT terminal.
    if last_success_idx is not None and last_success_idx > last_overload_idx:
        return None

    message: str | None = None
    last_timestamp: str | None = None
    if last_overload_rec is not None:
        last_timestamp = last_overload_rec.get("timestamp")
        err = last_overload_rec.get("error")
        if isinstance(err, dict):
            # Message nests under error.error.error.message in real records;
            # fall back through the layers defensively.
            inner = err.get("error")
            if isinstance(inner, dict):
                inner2 = inner.get("error")
                if isinstance(inner2, dict):
                    message = inner2.get("message")
                if message is None:
                    message = inner.get("message")

    return {
        "terminal": True,
        "retry_attempt": max_retry_attempt,
        "max_retries": max_retries,
        "overload_count": overload_count,
        "last_timestamp": last_timestamp,
        "message": message,
    }
