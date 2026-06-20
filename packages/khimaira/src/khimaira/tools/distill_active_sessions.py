"""Pre-bake distill of ACTIVE, SETTLED roster sessions into mnemosyne.

The mnemosyne oracle bakes whatever is in the store at bake time. A session's
knowledge normally reaches the store only on Stop (session_end hook) or via a
manual ``/khimaira-distill``. Long-lived sessions — masters, leads — hold hours
of high-value context that never gets baked until they end. This tool runs as a
PRE-BAKE step (before ``build_corpus`` in ``refresh_oracle.sh``) and distills the
in-flight knowledge of currently-active sessions so the next weekly oracle
internalises it.

Two guards keep the oracle from learning noise or wrong intermediate reasoning:

* **SETTLED-only** (idle >= ``--settle-min``): an ACTIVE session is full of
  hypotheses that turned out false (e.g. a debugging session's discarded
  theories). Distilling mid-flight would bake confidently-wrong intermediate
  conclusions. Idle-for-a-while is a cheap proxy for "done thinking, conclusions
  settled" — the same point at which the Stop hook would naturally fire.
* **High-value roles only**: only masters (-> ``orchestration``) and domain leads
  (-> their domain) are distilled. Agents / critics / verifiers / analysts /
  intake / tracker produce thin durable knowledge, and ``detect_domain`` marks
  them ``general`` — folding that in risks the same contamination the bake's
  ``general``-domain exclusion already guards against.

Scoped to ONE project's transcript directory (the bake is project-specific).
Fail-open throughout: any error skips that session; the tool always exits 0 so a
distill hiccup never breaks the bake.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from khimaira.hooks.mnemosyne_client import distill as _mnemosyne_distill
from khimaira.hooks.session_end_utils import detect_domain, extract_transcript
from khimaira.monitor import sessions as _sessions

_CLAUDE_PROJECTS = Path("~/.claude/projects").expanduser()
_STATE_DIR = Path("~/.local/state/khimaira").expanduser()

# A name is master-shaped if it ends in "-0" (the roster master slot) or carries
# an explicit "master" token. Masters distill to the `orchestration` domain.
_MASTER_RE = re.compile(r"(?:^|[-_])master(?:[-_]\d+)?$|-0$", re.IGNORECASE)


def _encode_cwd(project_root: str) -> str:
    """Claude Code's transcript-dir encoding: each '/' and '_' becomes '-'.

    /home/_3ntropy/dev/khimaira -> -home--3ntropy-dev-khimaira
    """
    return re.sub(r"[/_]", "-", project_root.rstrip("/"))


def _resolve_domain(name: str, master_names: frozenset[str] = frozenset()) -> str | None:
    """Map a session name to its mnemosyne domain, or None to skip.

    Lead names (``backend-lead-1``) -> their domain via detect_domain.
    Master-shaped names (``*-0``, ``*master*``, or an explicit ``master_names``
    entry like ``muther``/``janice``) -> ``orchestration``.
    Everything else (agent/critic/verifier/analyst/intake/tracker/observer) -> None.
    """
    domain = detect_domain(name)
    if domain != "general":
        return domain  # a real domain lead
    if name in master_names or _MASTER_RE.search(name):
        return "orchestration"
    return None  # non-lead, non-master — thin durable value, skip


# --- backfill (historical transcripts) -------------------------------------
#
# The active path above only distills sessions that are STILL in the live
# session_list (settled + recent). Historical transcripts — sessions reaped from
# the registry, or older than --recent-days — never reach the oracle. ``--backfill``
# is a MANUAL one-shot catch-up over those: it inverts the recency cap, recovers
# names from transcript content for untracked sessions, and is LEDGER-GUARDED so
# each frozen transcript distills exactly once (content-hash → a changed transcript
# re-runs). The ledger is backfill-only; the weekly active path intentionally
# re-distills live sessions (latest-wins) and never touches it.


def _name_from_transcript(path: Path) -> str | None:
    """Recover a session's self-assigned name from its transcript JSONL.

    Scans for the LAST ``session_set_name`` tool_use (the most recent self-naming)
    and returns its ``name`` argument. Targets that specific tool — a blind "name"
    scan would conflate with Skill invocations (e.g. ``khimaira-bootstrap-roster``)
    and other tool args. Returns None if the session never named itself.
    """
    found: str | None = None
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                # Cheap pre-filter before the JSON parse — most lines won't match.
                if "session_set_name" not in line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = (rec.get("message") or {}).get("content") or rec.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if (
                        isinstance(block, dict)
                        and block.get("type") == "tool_use"
                        and str(block.get("name", "")).endswith("session_set_name")
                    ):
                        nm = (block.get("input") or {}).get("name")
                        if isinstance(nm, str) and nm.strip():
                            found = nm.strip()  # keep the last self-naming
    except OSError:
        return None
    return found


def _content_hash(path: Path) -> str:
    """16-hex-char sha256 of the transcript bytes — ledger key suffix. A changed
    transcript yields a new hash, so backfill re-distills it; an unchanged one is
    skipped."""
    h = hashlib.sha256()
    try:
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
    except OSError:
        return ""
    return h.hexdigest()[:16]


def _default_ledger_path() -> Path:
    return _STATE_DIR / "backfill_ledger.json"


def _load_ledger(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_ledger(path: Path, ledger: dict) -> None:
    """Atomic write (tmp + rename) so a crash mid-backfill never corrupts the ledger."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(ledger, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        print(f"[backfill] WARN could not persist ledger {path}: {exc}")


def _parse_since(date_str: str) -> float | None:
    """Parse YYYY-MM-DD (UTC midnight) → epoch seconds, or None if empty."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=UTC).timestamp()
    except ValueError:
        raise SystemExit(f"--backfill-since must be YYYY-MM-DD, got {date_str!r}") from None


def _backfill(
    *,
    project: str,
    transcript_dir: Path,
    by_id: dict,
    max_chars: int,
    master_names: frozenset[str],
    backfill_since: str,
    max_sessions: int,
    confirm: bool,
    ledger_path: Path | None,
    settle_min: float,
    verbose: bool,
) -> dict:
    """Backfill historical transcripts. Dry-run unless ``confirm``. See module note."""
    since_ts = _parse_since(backfill_since)
    settle_s = settle_min * 60.0
    ledger_path = ledger_path or _default_ledger_path()
    ledger = _load_ledger(ledger_path)
    proj_ledger = ledger.setdefault(project, {})

    summary: dict = {
        "distilled": [],
        "skipped": {
            "mid_flight": 0,
            "too_old": 0,
            "low_value": 0,
            "no_transcript": 0,
            "no_name": 0,
            "already_done": 0,
            "capped": 0,
        },
        "transcript_dir": str(transcript_dir),
        "ledger_path": str(ledger_path),
        "dry_run": not confirm,
    }

    now = time.time()
    distilled_count = 0
    for path in sorted(transcript_dir.glob("*.jsonl")):
        sid = path.stem
        if max_sessions and distilled_count >= max_sessions:
            summary["skipped"]["capped"] += 1
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            summary["skipped"]["no_transcript"] += 1
            continue
        # Lower bound: a transcript older than --backfill-since is out of window.
        if since_ts is not None and mtime < since_ts:
            summary["skipped"]["too_old"] += 1
            continue
        # Live-session guard: a transcript touched within the settle window may still
        # be mid-flight (e.g. this very session) — same contamination risk the active
        # path's settle-floor guards. mtime is the activity proxy (untracked sids have
        # no session_list idle).
        if (now - mtime) < settle_s:
            summary["skipped"]["mid_flight"] += 1
            continue
        # Name: prefer the live registry; fall back to the transcript's self-naming.
        name = (by_id.get(sid) or {}).get("name") or _name_from_transcript(path)
        if not name:
            summary["skipped"]["no_name"] += 1
            if verbose:
                print(
                    f"[backfill] skip {sid}: no resolvable name (untracked + no session_set_name)"
                )
            continue
        domain = _resolve_domain(name, master_names)
        if domain is None:
            summary["skipped"]["low_value"] += 1
            continue
        key = f"{sid}:{_content_hash(path)}"
        if key in proj_ledger:
            summary["skipped"]["already_done"] += 1
            if verbose:
                print(f"[backfill] skip {name}: already backfilled (ledger {key})")
            continue
        transcript = extract_transcript(sid, max_chars=max_chars, transcript_path=str(path))
        if not transcript:
            summary["skipped"]["no_transcript"] += 1
            continue
        qualified = f"{project}:{domain}"
        when = datetime.fromtimestamp(mtime, UTC).strftime("%F")
        if not confirm:
            print(
                f"[backfill] DRY-RUN would distill {name} -> {qualified} "
                f"(mtime {when}, {len(transcript)} chars, key {key})"
            )
            summary["distilled"].append(
                {"name": name, "domain": qualified, "dry_run": True, "key": key}
            )
            distilled_count += 1
            continue
        result = _mnemosyne_distill(qualified, transcript, name)
        if result is None:
            print(
                f"[backfill] WARN distill returned None for {name} -> {qualified} "
                "(mnemosyne unreachable?) — NOT ledgering, will retry next run"
            )
            continue
        pairs = result.get("pairs_extracted", result.get("count", "?"))
        proj_ledger[key] = {"name": name, "domain": qualified, "pairs": pairs, "mtime": mtime}
        _save_ledger(ledger_path, ledger)  # persist after each success (crash-safe)
        print(f"[backfill] distilled {name} -> {qualified} ({pairs} pairs)")
        summary["distilled"].append({"name": name, "domain": qualified, "pairs": pairs, "key": key})
        distilled_count += 1

    d = summary["distilled"]
    sk = summary["skipped"]
    print(
        f"[backfill] done ({'DRY-RUN' if not confirm else 'WROTE'}): "
        f"{len(d)} distilled, skipped(mid-flight={sk['mid_flight']}, "
        f"too-old={sk['too_old']}, low-value={sk['low_value']}, no-name={sk['no_name']}, "
        f"already-done={sk['already_done']}, no-transcript={sk['no_transcript']}, "
        f"capped={sk['capped']})"
    )
    return summary


def distill_active_sessions(
    *,
    project_root: str,
    project: str,
    settle_min: float,
    recent_days: float,
    max_chars: int,
    dry_run: bool,
    verbose: bool,
    master_names: frozenset[str] = frozenset(),
    backfill: bool = False,
    backfill_since: str = "",
    max_sessions: int = 0,
    confirm: bool = False,
    ledger_path: Path | None = None,
) -> dict:
    """Distill settled master/lead sessions for one project. Returns a summary dict.

    When ``backfill`` is set, processes HISTORICAL transcripts instead of the active
    settled set (ledger-guarded, dry-run unless ``confirm``) — see ``_backfill``.
    """
    transcript_dir = _CLAUDE_PROJECTS / _encode_cwd(project_root)
    summary = {
        "distilled": [],
        "skipped": {
            "mid_flight": 0,
            "stale": 0,
            "low_value": 0,
            "no_transcript": 0,
            "untracked": 0,
        },
        "transcript_dir": str(transcript_dir),
    }
    if not transcript_dir.is_dir():
        print(f"[distill-active] no transcript dir {transcript_dir} — nothing to do")
        return summary

    # session_id -> record (name + last_active_age_s). Fresh read, not cached.
    try:
        rows = _sessions.list_sessions(use_cache=False)
    except Exception as exc:
        print(f"[distill-active] list_sessions failed: {exc} — nothing to do")
        return summary
    by_id = {r.get("session_id"): r for r in rows if r.get("session_id")}

    if backfill:
        return _backfill(
            project=project,
            transcript_dir=transcript_dir,
            by_id=by_id,
            max_chars=max_chars,
            master_names=master_names,
            backfill_since=backfill_since,
            max_sessions=max_sessions,
            confirm=confirm,
            ledger_path=ledger_path,
            settle_min=settle_min,
            verbose=verbose,
        )

    settle_s = settle_min * 60.0
    recent_s = recent_days * 86400.0

    for path in sorted(transcript_dir.glob("*.jsonl")):
        sid = path.stem
        rec = by_id.get(sid)
        name = (rec or {}).get("name")
        if not rec or not name:
            summary["skipped"]["untracked"] += 1
            continue
        idle = float(rec.get("last_active_age_s") or 0.0)
        if idle < settle_s:
            summary["skipped"]["mid_flight"] += 1
            if verbose:
                print(
                    f"[distill-active] skip {name}: mid-flight (idle {idle:.0f}s < {settle_s:.0f}s)"
                )
            continue
        if idle > recent_s:
            summary["skipped"]["stale"] += 1
            continue
        domain = _resolve_domain(name, master_names)
        if domain is None:
            summary["skipped"]["low_value"] += 1
            if verbose:
                print(f"[distill-active] skip {name}: non-lead/non-master")
            continue

        transcript = extract_transcript(sid, max_chars=max_chars, transcript_path=str(path))
        if not transcript:
            summary["skipped"]["no_transcript"] += 1
            continue

        qualified = f"{project}:{domain}"
        if dry_run:
            print(
                f"[distill-active] DRY-RUN would distill {name} -> {qualified} "
                f"(idle {idle / 60:.0f}m, {len(transcript)} chars)"
            )
            summary["distilled"].append({"name": name, "domain": qualified, "dry_run": True})
            continue

        result = _mnemosyne_distill(qualified, transcript, name)
        if result is None:
            print(
                f"[distill-active] WARN distill returned None for {name} -> {qualified} "
                "(mnemosyne unreachable?) — continuing"
            )
            summary["skipped"]["no_transcript"] += 0  # not a transcript issue; just note
        else:
            pairs = result.get("pairs_extracted", result.get("count", "?"))
            print(f"[distill-active] distilled {name} -> {qualified} ({pairs} pairs)")
            summary["distilled"].append({"name": name, "domain": qualified, "pairs": pairs})

    d = summary["distilled"]
    sk = summary["skipped"]
    print(
        f"[distill-active] done: {len(d)} distilled, "
        f"skipped(mid-flight={sk['mid_flight']}, stale={sk['stale']}, "
        f"low-value={sk['low_value']}, no-transcript={sk['no_transcript']}, "
        f"untracked={sk['untracked']})"
    )
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-bake distill of settled master/lead sessions.")
    ap.add_argument(
        "--project-root", required=True, help="Project cwd root, e.g. /home/_3ntropy/dev/khimaira"
    )
    ap.add_argument(
        "--project", required=True, help="Project name for the qualified domain key, e.g. khimaira"
    )
    ap.add_argument(
        "--settle-min",
        type=float,
        default=30.0,
        help="Min idle minutes for a session to count as SETTLED (default 30)",
    )
    ap.add_argument(
        "--recent-days",
        type=float,
        default=8.0,
        help="Max idle days — older sessions are stale, not this cycle (default 8)",
    )
    ap.add_argument(
        "--max-chars",
        type=int,
        default=600_000,
        help="Transcript window passed to extract_transcript (~150k tok; Haiku 200k "
             "headroom). Over-budget → decision-dense whole-session selection.",
    )
    ap.add_argument(
        "--master-names",
        default="",
        help="Comma-separated session names to treat as masters "
        "(-> orchestration) when they aren't *-0/*master* shaped, "
        "e.g. 'muther,janice' for the jeevy roster.",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="List what would be distilled without writing to the store",
    )
    ap.add_argument("--verbose", action="store_true")
    # --- backfill mode (historical transcripts; manual one-shot) ---
    ap.add_argument(
        "--backfill",
        action="store_true",
        help="Backfill HISTORICAL transcripts (older than --recent-days, "
        "or untracked/reaped sessions) instead of the active settled "
        "set. Ledger-guarded + DRY-RUN by default — pass --confirm to write.",
    )
    ap.add_argument(
        "--backfill-since",
        default="",
        help="Backfill lower bound, YYYY-MM-DD (by transcript mtime). "
        "Empty = no lower bound (full history).",
    )
    ap.add_argument(
        "--max-sessions",
        type=int,
        default=0,
        help="Cap distills per backfill run (0 = no cap). Use for a small smoke run.",
    )
    ap.add_argument(
        "--confirm",
        action="store_true",
        help="Actually write during --backfill (default is dry-run). No effect outside --backfill.",
    )
    args = ap.parse_args()

    master_names = frozenset(n.strip() for n in args.master_names.split(",") if n.strip())
    try:
        distill_active_sessions(
            project_root=args.project_root,
            project=args.project,
            settle_min=args.settle_min,
            recent_days=args.recent_days,
            max_chars=args.max_chars,
            dry_run=args.dry_run,
            verbose=args.verbose,
            master_names=master_names,
            backfill=args.backfill,
            backfill_since=args.backfill_since,
            max_sessions=args.max_sessions,
            confirm=args.confirm,
        )
    except Exception as exc:  # fail-open: never break the bake
        print(f"[distill-active] non-fatal error: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
