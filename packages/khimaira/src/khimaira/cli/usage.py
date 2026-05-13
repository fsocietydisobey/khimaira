"""`khimaira usage` — read the usage tracker, surface costs + savings.

Subcommands:
  khimaira usage savings [--days N] [--by mode|runner|day]
  khimaira usage list [--days N] [--mode auto|explicit-tier|manual]

The savings number is THE value-prop: "your $20/month Claude Code
subscription stretched to do work that would have cost $X if every call
hit Opus directly."

Limitation (v1): savings here count only khimaira-routed dispatches.
Direct Claude Code calls (the ones you make without going through
mcp__khimaira__delegate / mcp__khimaira__auto) aren't tracked yet —
that's the Phase 4 transcript scrape on the roadmap. Until then, this
number is the LOWER BOUND of what khimaira saved you; real savings
are higher.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from typing import Iterable

from khimaira.log import get_logger
from khimaira.usage import estimate_cost, log_file_path

log = get_logger("cli.usage")

# Default counterfactual baseline. The assumption is the user, without
# khimaira auto-mode, would be calling Opus directly inside Claude Code.
# That's the dominant pattern for the user we're building for.
#
# Override priority (highest wins):
#   1. KHIMAIRA_USAGE_BASELINE_MODEL env var (per-session / per-invocation)
#   2. baseline_model: top-level key in ~/.khimaira/models.yaml (persistent)
#   3. Hardcoded default below (sane out-of-box)
#
# The chosen model id must be one estimate_cost() recognizes (see _PRICES
# in khimaira.usage). Unknown ids return $0 cost — so the savings number
# silently goes to zero instead of crashing. Doctor check should surface
# this case eventually.
_DEFAULT_COUNTERFACTUAL_MODEL = "claude-opus-4-7"


def _resolve_counterfactual_model() -> str:
    """Look up the savings baseline. Env var > registry > default.

    Resolved at call-time (not import-time) so test fixtures and shell
    env changes take effect without re-importing the module.
    """
    env = os.environ.get("KHIMAIRA_USAGE_BASELINE_MODEL")
    if env:
        return env

    # Registry override: top-level `baseline_model:` key in the user's
    # models.yaml. Reuses the same path resolution as the model registry.
    try:
        import yaml

        from khimaira.dispatch.registry import _user_registry_path

        path = _user_registry_path()
        if path.is_file():
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
            if isinstance(data, dict):
                baseline = data.get("baseline_model")
                if baseline:
                    return str(baseline)
    except Exception as exc:  # noqa: BLE001 — config read should never break savings
        log.warning("usage: failed to read baseline_model from registry: %s", exc)

    return _DEFAULT_COUNTERFACTUAL_MODEL


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    """Register `khimaira usage` and its sub-subcommands."""
    parser = subparsers.add_parser(
        "usage",
        help="Inspect the usage tracker (cost, savings, breakdowns).",
        description=(
            "Reads ~/.local/state/khimaira/usage.jsonl and reports cost + "
            "savings. The savings number compares what khimaira actually "
            "spent in auto mode against what an Opus-direct dispatch "
            "would have cost for the same tokens."
        ),
    )
    sub = parser.add_subparsers(dest="action", required=True)

    p_savings = sub.add_parser(
        "savings",
        help="Estimate USD saved by khimaira auto-mode routing.",
    )
    p_savings.add_argument(
        "--days",
        type=int,
        default=30,
        help="Look back this many days (default: 30).",
    )
    p_savings.add_argument(
        "--by",
        choices=("mode", "runner", "day"),
        default="mode",
        help="Group breakdown by this dimension (default: mode).",
    )
    p_savings.set_defaults(func=_run_savings)

    p_list = sub.add_parser(
        "list",
        help="Print recent usage records (newest first).",
    )
    p_list.add_argument(
        "--days",
        type=int,
        default=7,
        help="Look back this many days (default: 7).",
    )
    p_list.add_argument(
        "--mode",
        choices=("auto", "explicit-tier", "manual", "unknown"),
        help="Filter to records with this mode.",
    )
    p_list.add_argument(
        "--limit",
        type=int,
        default=50,
        help="Cap the number of records printed (default: 50).",
    )
    p_list.set_defaults(func=_run_list)


def _iter_records(days: int) -> Iterable[dict]:
    """Yield usage records newer than `days` days, oldest first.

    JSONL on disk — newest record at the end. We don't load it all into
    memory unless we have to (the file grows unbounded; `khimaira usage
    savings --days 30` should be O(records-in-window), not O(all-records)).
    Lines older than the cutoff are skipped cheaply.
    """
    path = log_file_path()
    if not path.is_file():
        return

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    cutoff_iso = cutoff.isoformat()

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                log.warning("usage: skipping malformed jsonl line")
                continue
            # Cheap lexicographic compare on ISO 8601 — only parse if
            # we're going to keep the record.
            ts = rec.get("ts", "")
            if ts < cutoff_iso:
                continue
            yield rec


def _run_savings(args: argparse.Namespace) -> int:
    records = list(_iter_records(args.days))
    baseline_model = _resolve_counterfactual_model()

    if not records:
        print(
            f"No usage records in the last {args.days} days. "
            f"(Looked at {log_file_path()}.)\n"
            "Run a `mcp__khimaira__auto` or `khimaira task` dispatch to "
            "start collecting data.",
        )
        return 0

    # Per-record math: actual cost vs counterfactual baseline cost
    total_actual = 0.0
    total_counterfactual = 0.0
    total_auto_actual = 0.0
    total_auto_counterfactual = 0.0
    auto_count = 0
    by_dim: dict[str, dict[str, float]] = defaultdict(
        lambda: {"actual": 0.0, "counterfactual": 0.0, "count": 0.0},
    )

    for rec in records:
        in_tok = int(rec.get("input_tokens", 0))
        out_tok = int(rec.get("output_tokens", 0))
        actual = float(rec.get("estimated_cost_usd", 0.0))
        counterfactual = estimate_cost(baseline_model, in_tok, out_tok)
        mode = rec.get("mode", "unknown")

        total_actual += actual
        total_counterfactual += counterfactual

        if mode == "auto":
            auto_count += 1
            total_auto_actual += actual
            total_auto_counterfactual += counterfactual

        key = _bucket_key(rec, args.by)
        bucket = by_dim[key]
        bucket["actual"] += actual
        bucket["counterfactual"] += counterfactual
        bucket["count"] += 1

    auto_savings = total_auto_counterfactual - total_auto_actual

    # Render
    print(f"Window: last {args.days} days  ({len(records)} records)")
    print(f"  auto-mode records: {auto_count}")
    print(f"  baseline model:    {baseline_model}")
    print()
    print(f"Total actual spend:               ${total_actual:>9.4f}")
    print(f"If everything had been baseline:  ${total_counterfactual:>9.4f}")
    print(f"  → savings (auto only):          ${auto_savings:>9.4f}")
    if total_auto_counterfactual > 0:
        pct = (auto_savings / total_auto_counterfactual) * 100
        print(f"  → auto-mode efficiency:         {pct:>9.1f}%  (vs {baseline_model})")
    print()

    print(f"Breakdown by {args.by}:")
    rows = sorted(by_dim.items(), key=lambda kv: -kv[1]["actual"])
    for key, b in rows:
        ratio = ""
        if b["counterfactual"] > 0:
            ratio = f"  ({(1 - b['actual'] / b['counterfactual']) * 100:>5.1f}% saved)"
        print(
            f"  {key:<20}  n={int(b['count']):>4}  "
            f"actual=${b['actual']:>8.4f}  "
            f"baseline=${b['counterfactual']:>8.4f}{ratio}",
        )
    print()
    print(
        f"Note: 'baseline' = {baseline_model} cost for the same tokens. "
        "Override with KHIMAIRA_USAGE_BASELINE_MODEL=<model-id> or set "
        "`baseline_model: <id>` in ~/.khimaira/models.yaml. Savings shown "
        "here count ONLY khimaira-routed dispatches; direct Claude Code "
        "calls aren't tracked yet (Phase 4 on the roadmap). The real "
        "savings number is the floor, not the ceiling.",
    )
    return 0


def _bucket_key(rec: dict, by: str) -> str:
    if by == "mode":
        return rec.get("mode", "unknown")
    if by == "runner":
        return rec.get("runner", "unknown")
    if by == "day":
        ts = rec.get("ts", "")
        return ts[:10] if ts else "unknown"
    return "unknown"


def _run_list(args: argparse.Namespace) -> int:
    records = list(_iter_records(args.days))
    if args.mode:
        records = [r for r in records if r.get("mode") == args.mode]

    if not records:
        print(f"No usage records matching the filter.", file=sys.stderr)
        return 0

    # Newest first
    records.sort(key=lambda r: r.get("ts", ""), reverse=True)
    records = records[: args.limit]

    for r in records:
        ts = r.get("ts", "")[:19].replace("T", " ")
        runner = r.get("runner", "?")
        model = r.get("model", "?")
        mode = r.get("mode", "?")
        in_tok = r.get("input_tokens", 0)
        out_tok = r.get("output_tokens", 0)
        cost = r.get("estimated_cost_usd", 0.0)
        print(
            f"{ts}  {runner:<8} {model:<28} mode={mode:<14} "
            f"{in_tok:>6}→{out_tok:<6}  ${cost:>7.4f}",
        )
    return 0
