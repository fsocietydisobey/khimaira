"""Runtime observation collector — mines per-project checkpoint history
to compute per-node duration statistics and end-node frequencies.

Output is persisted to a separate YAML file alongside the LLM-derived
metadata cache. Two-file layout (instead of merging into the main
metadata cache) is intentional: observations update on a fast cadence
(every ~5min in production), while metadata is rewritten only when the
LLM scan fires. Splitting them avoids race conditions and lets us
trigger refinement scans by including the observation file as input
to the prompt.

Design notes:
  - Aggregation is per-NODE-NAME (not per-graph-and-node). Tradeoff:
    same node name across multiple graphs gets bucketed together,
    losing some precision. Most LangGraph apps have unique node names
    per project (verified for jeevy + chimera), so this is fine in
    practice. Extend to (graph, node) tuples if collisions become
    a problem.
  - Durations measured between consecutive checkpoints in a thread:
    `checkpoint[i].created_at - checkpoint[i-1].created_at` is how
    long whatever-fired-in-step-i took. This is the SAME signal we
    use for the dashboard's "Ns ago" indicator, just aggregated.
  - Scope: only inspects threads where the latest activity is recent
    enough to matter. Configurable via CHIMERA_MONITOR_OBSERVATION_LIMIT
    (default 200 threads per project).

The collector runs synchronously and is idempotent — you can re-run
it any time and it'll regenerate observations from scratch. No state
is carried between runs.
"""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median
from typing import Any

import yaml

from chimera.log import get_logger

from ..discovery.connections import Connections, discover_all
from .schema import GraphObservations, NodeStats, RuntimeObservations

log = get_logger("monitor.metadata.observations")

# How many threads to inspect per project. Bounded so the collector
# stays fast even on very busy projects. Most-recently-updated threads
# come first — old idle ones rarely add information.
_DEFAULT_LIMIT = int(os.environ.get("CHIMERA_MONITOR_OBSERVATION_LIMIT", "200"))

# Special node names that LangGraph synthesizes; not real work.
_SPECIAL_NODES = frozenset({"__input__", "__start__", "__interrupt__", "__end__"})

# Where observation files live. Same dir as the metadata cache; suffix
# distinguishes them.
_CACHE_DIR = Path(
    os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache"))
) / "chimera" / "monitor"


def observations_path(project_path: Path) -> Path:
    """Return the YAML path where this project's observations are cached."""
    import hashlib

    name = project_path.name or "project"
    digest = hashlib.sha256(str(project_path.resolve()).encode()).hexdigest()[:8]
    return _CACHE_DIR / f"{name}-{digest}-observations.yaml"


def load(project_path: Path) -> RuntimeObservations | None:
    """Read the observations file for a project. Returns None when no
    collection has happened yet (caller falls back to defaults)."""
    path = observations_path(project_path)
    if not path.exists():
        return None
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning("observations: failed to load %s: %s", path, exc)
        return None
    if not isinstance(data, dict):
        return None
    try:
        return RuntimeObservations.model_validate(data)
    except Exception as exc:
        log.warning("observations: schema-invalid %s: %s", path, exc)
        return None


def save(observations: RuntimeObservations, project_path: Path) -> Path:
    """Write the observations YAML, creating dirs as needed."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = observations_path(project_path)
    path.write_text(
        yaml.safe_dump(
            observations.model_dump(),
            sort_keys=True,
            default_flow_style=False,
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    return path


def collect(project_path: Path, limit: int = _DEFAULT_LIMIT) -> RuntimeObservations | None:
    """Run a full observation pass for one project. Returns None when
    the project has no checkpointer connection."""
    try:
        conns = discover_all(project_path)
    except Exception as exc:
        log.warning("observations: discover_all failed for %s: %s", project_path, exc)
        return None
    if not conns.postgres and not conns.sqlite:
        return None

    log.info("observations: collecting for %s (limit=%d threads)", project_path.name, limit)

    node_durations: dict[str, list[float]] = defaultdict(list)
    end_node_counts: Counter[str] = Counter()
    threads_seen = 0

    for thread_id, checkpoints in _iter_threads(conns, limit):
        threads_seen += 1
        if not checkpoints:
            continue
        # Compute inter-checkpoint durations. Each checkpoint's "current
        # node" is the one that fired during the step preceding it; the
        # time delta is that step's duration.
        for prev, curr in zip(checkpoints, checkpoints[1:]):
            try:
                prev_ts = _parse_iso(prev["created_at"])
                curr_ts = _parse_iso(curr["created_at"])
            except Exception:
                continue
            if prev_ts is None or curr_ts is None:
                continue
            duration = (curr_ts - prev_ts).total_seconds()
            if duration < 0 or duration > 86_400:  # sanity bound: 1 day
                continue
            node = curr.get("current_node")
            if node and node not in _SPECIAL_NODES:
                node_durations[node].append(duration)
        # The last checkpoint's current_node is "where this thread
        # ended up." If the thread is settled (idle) this is a strong
        # signal of an empirical end-node.
        last = checkpoints[-1]
        end_node = last.get("current_node")
        if end_node and end_node not in _SPECIAL_NODES:
            end_node_counts[end_node] += 1

    # Roll durations into stats. Single graph bucket for now (per-node
    # aggregation across the project).
    node_stats: dict[str, NodeStats] = {}
    for node, durations in node_durations.items():
        if not durations:
            continue
        node_stats[node] = NodeStats(
            visits=len(durations),
            duration_p50=_pct(durations, 50),
            duration_p95=_pct(durations, 95),
            duration_max=max(durations),
        )

    obs = RuntimeObservations(
        last_collected_at=datetime.now(timezone.utc).isoformat(),
        samples_seen=threads_seen,
        graphs={
            "_aggregate": GraphObservations(
                nodes=node_stats,
                end_node_counts=dict(end_node_counts),
            )
        },
    )
    save(obs, project_path)
    log.info(
        "observations: %s — %d threads, %d nodes with stats, end-nodes=%s",
        project_path.name,
        threads_seen,
        len(node_stats),
        list(end_node_counts.most_common(5)),
    )
    return obs


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _iter_threads(conns: Connections, limit: int):
    """Yield (thread_id, checkpoints[oldest_first]) for the most-recent
    `limit` threads across whichever backends the project uses."""
    seen: set[str] = set()

    for pg in conns.postgres:
        try:
            yield from _pg_iter_threads(pg.url, limit, seen)
        except Exception as exc:
            log.warning("observations: pg fetch failed: %s", exc)
            continue
        if len(seen) >= limit:
            return

    for sl in conns.sqlite:
        try:
            yield from _sqlite_iter_threads(sl.path, limit, seen)
        except Exception as exc:
            log.warning("observations: sqlite fetch failed: %s", exc)
            continue
        if len(seen) >= limit:
            return


def _pg_iter_threads(url: str, limit: int, seen: set[str]):
    import psycopg

    with psycopg.connect(url, connect_timeout=3) as db:
        with db.cursor() as cur:
            # Get the most-recently-active thread_ids first.
            cur.execute(
                """
                SELECT thread_id, MAX(checkpoint_id) AS latest
                FROM checkpoints
                GROUP BY thread_id
                ORDER BY latest DESC
                LIMIT %s
                """,
                (limit,),
            )
            tids = [t for (t, _) in cur.fetchall() if t not in seen]
            for tid in tids:
                cur.execute(
                    """
                    SELECT checkpoint_id,
                           checkpoint->>'ts' AS created_at,
                           metadata->'writes' AS writes,
                           checkpoint->'versions_seen' AS versions_seen
                    FROM checkpoints
                    WHERE thread_id = %s
                    ORDER BY checkpoint_id ASC
                    """,
                    (tid,),
                )
                rows = []
                for cp_id, ts, writes, vers in cur.fetchall():
                    rows.append(
                        {
                            "checkpoint_id": cp_id,
                            "created_at": ts,
                            "current_node": _node_from_writes_versions(writes, vers),
                        }
                    )
                seen.add(tid)
                yield tid, rows


def _sqlite_iter_threads(db_path: str, limit: int, seen: set[str]):
    import sqlite3

    from ..discovery.state_decoder import decode

    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=2.0) as db:
        cur = db.execute(
            """
            SELECT thread_id, MAX(checkpoint_id) AS latest
            FROM checkpoints
            GROUP BY thread_id
            ORDER BY latest DESC
            LIMIT ?
            """,
            (limit,),
        )
        tids = [t for (t, _) in cur.fetchall() if t not in seen]
        for tid in tids:
            cur = db.execute(
                """
                SELECT checkpoint_id, type, checkpoint, metadata
                FROM checkpoints
                WHERE thread_id = ?
                ORDER BY checkpoint_id ASC
                """,
                (tid,),
            )
            rows = []
            for cp_id, type_str, blob, meta_blob in cur.fetchall():
                try:
                    cp = decode(type_str, blob) or {}
                    md = decode(type_str, meta_blob) or {}
                except Exception:
                    continue
                if not isinstance(cp, dict) or not isinstance(md, dict):
                    continue
                rows.append(
                    {
                        "checkpoint_id": cp_id,
                        "created_at": cp.get("ts") if isinstance(cp.get("ts"), str) else None,
                        "current_node": _node_from_writes_versions(
                            md.get("writes"), cp.get("versions_seen")
                        ),
                    }
                )
            seen.add(tid)
            yield tid, rows


def _node_from_writes_versions(writes: Any, versions_seen: Any) -> str | None:
    """Mirror of `_derive_nodes` from threads.py — keep in sync. Picks
    the most-likely-current node from the standard signals."""
    if isinstance(writes, dict) and writes:
        nodes = sorted(k for k in writes.keys() if k not in _SPECIAL_NODES)
        if nodes:
            return nodes[0]
    if isinstance(versions_seen, dict):
        # Take the node with the highest max-channel-version. Matches
        # _derive_nodes' logic in threads.py.
        candidates: list[tuple[str, str]] = []
        for node, channels in versions_seen.items():
            if node in _SPECIAL_NODES or not isinstance(channels, dict):
                continue
            try:
                max_v = max(str(v) for v in channels.values())
            except ValueError:
                continue
            candidates.append((max_v, node))
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0][1]
    return None


def _parse_iso(ts: Any) -> datetime | None:
    if not isinstance(ts, str):
        return None
    try:
        d = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _pct(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    sorted_vs = sorted(values)
    if pct >= 100:
        return sorted_vs[-1]
    if pct <= 0:
        return sorted_vs[0]
    if pct == 50:
        return median(sorted_vs)
    idx = max(0, min(len(sorted_vs) - 1, int(len(sorted_vs) * pct / 100)))
    return sorted_vs[idx]
