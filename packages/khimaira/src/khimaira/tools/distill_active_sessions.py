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
import re
import sys
import time
from pathlib import Path

from khimaira.hooks.mnemosyne_client import distill as _mnemosyne_distill
from khimaira.hooks.session_end_utils import detect_domain, extract_transcript
from khimaira.monitor import sessions as _sessions

_CLAUDE_PROJECTS = Path("~/.claude/projects").expanduser()

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
) -> dict:
    """Distill settled master/lead sessions for one project. Returns a summary dict."""
    transcript_dir = _CLAUDE_PROJECTS / _encode_cwd(project_root)
    summary = {
        "distilled": [],
        "skipped": {"mid_flight": 0, "stale": 0, "low_value": 0, "no_transcript": 0,
                    "untracked": 0},
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
                print(f"[distill-active] skip {name}: mid-flight (idle {idle:.0f}s < {settle_s:.0f}s)")
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
            print(f"[distill-active] DRY-RUN would distill {name} -> {qualified} "
                  f"(idle {idle / 60:.0f}m, {len(transcript)} chars)")
            summary["distilled"].append({"name": name, "domain": qualified, "dry_run": True})
            continue

        result = _mnemosyne_distill(qualified, transcript, name)
        if result is None:
            print(f"[distill-active] WARN distill returned None for {name} -> {qualified} "
                  "(mnemosyne unreachable?) — continuing")
            summary["skipped"]["no_transcript"] += 0  # not a transcript issue; just note
        else:
            pairs = result.get("pairs_extracted", result.get("count", "?"))
            print(f"[distill-active] distilled {name} -> {qualified} ({pairs} pairs)")
            summary["distilled"].append({"name": name, "domain": qualified, "pairs": pairs})

    d = summary["distilled"]
    sk = summary["skipped"]
    print(f"[distill-active] done: {len(d)} distilled, "
          f"skipped(mid-flight={sk['mid_flight']}, stale={sk['stale']}, "
          f"low-value={sk['low_value']}, no-transcript={sk['no_transcript']}, "
          f"untracked={sk['untracked']})")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser(description="Pre-bake distill of settled master/lead sessions.")
    ap.add_argument("--project-root", required=True,
                    help="Project cwd root, e.g. /home/_3ntropy/dev/khimaira")
    ap.add_argument("--project", required=True,
                    help="Project name for the qualified domain key, e.g. khimaira")
    ap.add_argument("--settle-min", type=float, default=30.0,
                    help="Min idle minutes for a session to count as SETTLED (default 30)")
    ap.add_argument("--recent-days", type=float, default=8.0,
                    help="Max idle days — older sessions are stale, not this cycle (default 8)")
    ap.add_argument("--max-chars", type=int, default=50_000,
                    help="Transcript truncation cap passed to extract_transcript")
    ap.add_argument("--master-names", default="",
                    help="Comma-separated session names to treat as masters "
                         "(-> orchestration) when they aren't *-0/*master* shaped, "
                         "e.g. 'muther,janice' for the jeevy roster.")
    ap.add_argument("--dry-run", action="store_true",
                    help="List what would be distilled without writing to the store")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    master_names = frozenset(
        n.strip() for n in args.master_names.split(",") if n.strip()
    )
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
        )
    except Exception as exc:  # fail-open: never break the bake
        print(f"[distill-active] non-fatal error: {exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
