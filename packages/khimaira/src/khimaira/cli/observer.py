"""`khimaira observer` subcommand — query observer/heartbeat data from CLI.

Subcommands:
  trace <project> <correlation_id>  — full chronological event timeline
                                       for one app-level run
  compare <project> <cid-a> <cid-b> — A/B per-node wall-time delta
                                       between two correlated runs
  slow <project>                    — recent slow chain/llm/tool/external
                                       calls, sorted by duration

The observer's HTTP endpoints back all of these — this module is just a
terminal-friendly view layer. Use case: post-deploy "did Phase A actually
speed up?" check; on-demand "what's grinding right now?" pulse.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict


_DEFAULT_BASE = "http://127.0.0.1:8740"


def _get(path: str, base: str = _DEFAULT_BASE) -> dict:
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=10.0) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as e:
        print(f"khimaira-monitor unreachable at {base} ({e}). Start it with `khimaira monitor`.", file=sys.stderr)
        sys.exit(2)


def _summarize_by_node(events: list[dict]) -> dict[str, dict]:
    """Pair start/end events by run_id; return per-name wall-time totals.

    Returns: {name: {count, total_ms, avg_ms, max_ms, calls: [(ms, kind)]}}
    """
    per_run: dict[tuple[str, str], dict] = {}
    for ev in events:
        ekind = ev.get("event") or ""
        rid = ev.get("run_id") or ""
        for k in ("chain", "llm", "tool", "external"):
            if not ekind.startswith(k):
                continue
            suffix = ekind[len(k) + 1:]
            key = (k, rid)
            if suffix == "start":
                per_run[key] = {"start": ev.get("ts", 0.0), "name": ev.get("name") or "?", "kind": k}
            elif suffix in ("end", "error") and key in per_run:
                rec = per_run[key]
                rec["end"] = ev.get("ts", rec["start"])
                rec["duration_ms"] = int((rec["end"] - rec["start"]) * 1000)
            break

    by_name: dict[str, dict] = defaultdict(lambda: {
        "count": 0, "total_ms": 0, "max_ms": 0, "kind": None,
    })
    for rec in per_run.values():
        if "duration_ms" not in rec:
            continue  # in-flight
        agg = by_name[rec["name"]]
        agg["count"] += 1
        agg["total_ms"] += rec["duration_ms"]
        agg["max_ms"] = max(agg["max_ms"], rec["duration_ms"])
        agg["kind"] = rec["kind"]
    for n, agg in by_name.items():
        agg["avg_ms"] = agg["total_ms"] // agg["count"] if agg["count"] else 0
    return dict(by_name)


def _cmd_trace(args: argparse.Namespace) -> int:
    data = _get(
        f"/api/heartbeats/{urllib.parse.quote(args.project)}/by-correlation/"
        f"{urllib.parse.quote(args.correlation_id)}"
    )
    events = data.get("events", [])
    if not events:
        print(f"no events for correlation_id={args.correlation_id!r} in {args.project!r}")
        return 1

    if args.json:
        print(json.dumps(data, indent=2, default=str))
        return 0

    print(f"# Trace — project={args.project} correlation={args.correlation_id}")
    print(f"# {len(events)} events\n")
    t0 = events[0].get("ts", 0)
    for ev in events:
        rel = (ev.get("ts", t0) - t0) * 1000  # ms from start
        kind = ev.get("event", "?")
        name = ev.get("name") or "-"
        extra = ev.get("extra") or {}
        ms_field = extra.get("ms")
        ms_str = f" {ms_field}ms" if ms_field is not None else ""
        print(f"  +{rel:>8.0f}ms  {kind:<16} {name:<35}{ms_str}")
    return 0


def _cmd_compare(args: argparse.Namespace) -> int:
    a = _get(
        f"/api/heartbeats/{urllib.parse.quote(args.project)}/by-correlation/"
        f"{urllib.parse.quote(args.run_a)}"
    )
    b = _get(
        f"/api/heartbeats/{urllib.parse.quote(args.project)}/by-correlation/"
        f"{urllib.parse.quote(args.run_b)}"
    )
    summ_a = _summarize_by_node(a.get("events", []))
    summ_b = _summarize_by_node(b.get("events", []))

    if args.json:
        print(json.dumps({"a": summ_a, "b": summ_b}, indent=2))
        return 0

    print(f"# A/B compare — project={args.project}")
    print(f"# A = {args.run_a} ({len(a.get('events', []))} events)")
    print(f"# B = {args.run_b} ({len(b.get('events', []))} events)\n")

    all_names = sorted(set(summ_a) | set(summ_b))
    print(f"  {'name':<40} {'kind':<10} {'A.total':>10} {'B.total':>10} {'delta':>10} {'pct':>7}")
    print(f"  {'-'*40} {'-'*10} {'-'*10} {'-'*10} {'-'*10} {'-'*7}")
    for name in all_names:
        ra = summ_a.get(name, {})
        rb = summ_b.get(name, {})
        kind = ra.get("kind") or rb.get("kind") or "?"
        ta = ra.get("total_ms", 0)
        tb = rb.get("total_ms", 0)
        delta = tb - ta
        pct = (delta / ta * 100) if ta else (0 if tb == 0 else float("inf"))
        marker = ""
        if abs(pct) > 20 and ta > 100:
            marker = " ⚠️" if delta > 0 else " ✓"
        pct_str = f"{pct:+.0f}%" if abs(pct) != float("inf") else "  new"
        print(f"  {name[:40]:<40} {kind:<10} {ta:>9}ms {tb:>9}ms {delta:>+9}ms {pct_str:>7}{marker}")

    total_a = sum(r.get("total_ms", 0) for r in summ_a.values())
    total_b = sum(r.get("total_ms", 0) for r in summ_b.values())
    overall_delta = total_b - total_a
    print(f"\n  TOTAL: A={total_a}ms  B={total_b}ms  delta={overall_delta:+}ms")
    return 0


def _cmd_slow(args: argparse.Namespace) -> int:
    qs = []
    for k in ("chain", "llm", "tool", "external"):
        v = getattr(args, k, None)
        if v is not None:
            qs.append(f"{k}={v}")
    qstr = "?" + "&".join(qs) if qs else ""
    data = _get(f"/api/heartbeats/{urllib.parse.quote(args.project)}/slow{qstr}")
    slow = data.get("slow", [])
    if not slow:
        print(f"no slow calls in {args.project!r} (thresholds: {data.get('thresholds')})")
        return 0
    if args.json:
        print(json.dumps(data, indent=2))
        return 0
    print(f"# Slow calls — project={args.project}")
    print(f"# Thresholds (ms): {data.get('thresholds')}")
    print(f"# {len(slow)} calls exceeded threshold\n")
    print(f"  {'kind':<10} {'duration':>10} {'thresh':>8} {'name':<40} {'state'}")
    print(f"  {'-'*10} {'-'*10} {'-'*8} {'-'*40} {'-'*8}")
    for s in slow:
        state = "in-flight" if s.get("in_flight") else "done"
        print(
            f"  {s['kind']:<10} {s['duration_ms']:>9}ms {s['threshold_ms']:>7}ms "
            f"{(s.get('name') or '?')[:40]:<40} {state}"
        )
    return 0


def add_subparser(subparsers: argparse._SubParsersAction) -> None:
    p = subparsers.add_parser(
        "observer",
        help="query observer / heartbeat data (trace, compare, slow)",
    )
    sub = p.add_subparsers(dest="observer_cmd", required=True)

    p_trace = sub.add_parser("trace", help="full event timeline for one correlation_id")
    p_trace.add_argument("project")
    p_trace.add_argument("correlation_id")
    p_trace.add_argument("--json", action="store_true")
    p_trace.set_defaults(func=_cmd_trace)

    p_cmp = sub.add_parser("compare", help="A/B per-node wall-time delta between two runs")
    p_cmp.add_argument("project")
    p_cmp.add_argument("run_a")
    p_cmp.add_argument("run_b")
    p_cmp.add_argument("--json", action="store_true")
    p_cmp.set_defaults(func=_cmd_compare)

    p_slow = sub.add_parser("slow", help="recent slow calls in project")
    p_slow.add_argument("project")
    p_slow.add_argument("--chain", type=float, help="chain threshold in seconds")
    p_slow.add_argument("--llm", type=float, help="llm threshold in seconds")
    p_slow.add_argument("--tool", type=float, help="tool threshold in seconds")
    p_slow.add_argument("--external", type=float, help="external threshold in seconds")
    p_slow.add_argument("--json", action="store_true")
    p_slow.set_defaults(func=_cmd_slow)
