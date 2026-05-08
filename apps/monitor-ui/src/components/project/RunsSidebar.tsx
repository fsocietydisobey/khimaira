/**
 * Runs sidebar — generic over any LangGraph project.
 *
 * Threads come from the backend already parsed into:
 *   scope_kind   — what kind of durable thing this thread belongs to
 *                  ("deliverable", "orchestrator", "thread", ...)
 *   scope_id     — identifier (uuid, name, raw thread_id)
 *   stage        — the role/phase of THIS thread within its scope
 *                  ("ingest", "digestion", "output", ...)
 *   stage_detail — extra discriminator (run-uuid, source-id, ...)
 *
 * The UI groups blindly by these fields — it doesn't know what
 * "deliverable" means. The label shown ("Deliverable abc…", "Run abc…")
 * comes from the backend's `scope_label`, derived during the LLM scan
 * of the project.
 *
 * Layout (top → bottom):
 *   - Search box
 *   - Sort selector (recent / oldest / scope-name / status)
 *   - Date dividers (Today / Yesterday / Earlier / Other)
 *     - Scope group (collapsible) — all threads sharing scope_id
 *       - Stage sub-group
 *         - Thread row — status + label + @current_node + relative + absolute time
 */

import { useMemo, useState } from "react";

import type { RunClustering, ThreadStatus, ThreadSummary } from "@/api";
import { useListThreadsQuery } from "@/api";
import { StalenessBadge } from "@/components/project/StalenessBadge";
import { Badge } from "@/components/ui/badge";
import { getStaleness, thresholdsFromRunning } from "@/lib/staleness";
import { cn } from "@/lib/utils";

const STATUS_DOT: Record<ThreadStatus, string> = {
  running: "bg-emerald-400 animate-pulse",
  paused: "bg-amber-400",
  starting: "bg-sky-400 animate-pulse",
  idle: "bg-zinc-500",
};

type SortMode = "recent" | "oldest" | "scope" | "status";

const SORT_LABELS: Record<SortMode, string> = {
  recent: "most recent",
  oldest: "oldest",
  scope: "by scope id",
  status: "by status",
};

const STATUS_PRIORITY: Record<ThreadStatus, number> = {
  running: 0,
  paused: 1,
  starting: 2,
  idle: 3,
};

export interface DerivedLabel {
  primary: string;
  secondary: string;
}

/** Public so LiveRunsCard can reuse the same labelling. */
export function deriveLabel(t: ThreadSummary): DerivedLabel {
  const labelBits: string[] = [];
  if (t.agent_profile) labelBits.push(t.agent_profile);
  if (t.stage && t.stage !== t.scope_kind) labelBits.push(t.stage);
  if (t.stage_detail) labelBits.push(`#${t.stage_detail.slice(0, 8)}`);
  if (labelBits.length === 0) labelBits.push(t.scope_kind);
  const secondaryBits: string[] = [];
  if (t.scope_id && t.scope_id !== t.thread_id) {
    secondaryBits.push(`${t.scope_kind} ${t.scope_id.slice(0, 8)}…`);
  }
  return {
    primary: labelBits.join(" · "),
    secondary: secondaryBits.join(" · "),
  };
}

function relativeTime(iso: string | null): string {
  if (!iso) return "—";
  const then = new Date(iso).getTime();
  const seconds = Math.max(0, Math.round((Date.now() - then) / 1000));
  if (seconds < 60) return `${seconds}s ago`;
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return new Date(iso).toLocaleDateString();
}

function absoluteTime(iso: string | null): string {
  if (!iso) return "";
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function startOfLocalDay(d: Date): Date {
  const out = new Date(d);
  out.setHours(0, 0, 0, 0);
  return out;
}

interface ScopeGroup {
  scopeKind: string;
  scopeId: string;
  threads: ThreadSummary[];
  latest: string | null;
  oldest: string | null;
  dominantStatus: ThreadStatus;
}

function buildScopeGroups(threads: ThreadSummary[]): ScopeGroup[] {
  const map = new Map<string, ScopeGroup>();
  for (const t of threads) {
    const key = `${t.scope_kind}/${t.scope_id}`;
    let g = map.get(key);
    if (!g) {
      g = {
        scopeKind: t.scope_kind,
        scopeId: t.scope_id,
        threads: [],
        latest: t.last_updated,
        oldest: t.last_updated,
        dominantStatus: t.status,
      };
      map.set(key, g);
    }
    g.threads.push(t);
    if ((t.last_updated ?? "") > (g.latest ?? "")) g.latest = t.last_updated;
    if ((t.last_updated ?? "9") < (g.oldest ?? "9")) g.oldest = t.last_updated;
    if (STATUS_PRIORITY[t.status] < STATUS_PRIORITY[g.dominantStatus]) {
      g.dominantStatus = t.status;
    }
  }
  return [...map.values()];
}

function sortGroups(groups: ScopeGroup[], mode: SortMode): ScopeGroup[] {
  const cmp = {
    recent: (a: ScopeGroup, b: ScopeGroup) => (b.latest ?? "").localeCompare(a.latest ?? ""),
    oldest: (a: ScopeGroup, b: ScopeGroup) => (a.oldest ?? "").localeCompare(b.oldest ?? ""),
    scope: (a: ScopeGroup, b: ScopeGroup) => a.scopeId.localeCompare(b.scopeId),
    status: (a: ScopeGroup, b: ScopeGroup) => {
      const d = STATUS_PRIORITY[a.dominantStatus] - STATUS_PRIORITY[b.dominantStatus];
      return d !== 0 ? d : (b.latest ?? "").localeCompare(a.latest ?? "");
    },
  }[mode];
  return [...groups].sort(cmp);
}

function bucketByDate(groups: ScopeGroup[]): Array<{ label: string; groups: ScopeGroup[] }> {
  const today = startOfLocalDay(new Date()).getTime();
  const yesterday = today - 24 * 60 * 60 * 1000;
  const weekAgo = today - 7 * 24 * 60 * 60 * 1000;
  const buckets: Record<string, ScopeGroup[]> = {
    Today: [], Yesterday: [], "This week": [], Earlier: [],
  };
  for (const g of groups) {
    if (!g.latest) {
      buckets.Earlier.push(g);
      continue;
    }
    const t = new Date(g.latest).getTime();
    if (t >= today) buckets.Today.push(g);
    else if (t >= yesterday) buckets.Yesterday.push(g);
    else if (t >= weekAgo) buckets["This week"].push(g);
    else buckets.Earlier.push(g);
  }
  return (["Today", "Yesterday", "This week", "Earlier"] as const)
    .map((label) => ({ label, groups: buckets[label] }))
    .filter((b) => b.groups.length > 0);
}

export interface RunsSidebarProps {
  projectName: string;
  selectedThreadId: string | null;
  onSelectThread: (threadId: string | null) => void;
  /**
   * Click on a run cluster row → opens the player with siblings loaded.
   * The list passed is `[primary, ...siblings]` in chronological order
   * (oldest first) so replay can walk the merged run timeline. If the
   * cluster has only one thread, siblings is empty and behavior is the
   * same as `onSelectThread`.
   */
  onSelectRun?: (threadIds: string[]) => void;
}

export function RunsSidebar({ projectName, selectedThreadId, onSelectThread, onSelectRun }: RunsSidebarProps) {
  const [query, setQuery] = useState("");
  const [sort, setSort] = useState<SortMode>("recent");
  const { data, isLoading } = useListThreadsQuery(
    { name: projectName, limit: 100, offset: 0 },
    { pollingInterval: 2000 },
  );

  const buckets = useMemo(() => {
    if (!data) return null;
    const q = query.trim().toLowerCase();
    const filtered = q
      ? data.threads.filter((t) => {
          const label = deriveLabel(t);
          return [
            t.thread_id,
            t.scope_kind,
            t.scope_id,
            t.stage,
            t.stage_detail,
            t.agent_profile ?? "",
            t.current_node ?? "",
            label.primary,
            label.secondary,
          ]
            .join(" ")
            .toLowerCase()
            .includes(q);
        })
      : data.threads;
    const groups = sortGroups(buildScopeGroups(filtered), sort);
    // Sort-by-recent uses date dividers; other sorts get one flat bucket.
    if (sort === "recent" || sort === "oldest") {
      return bucketByDate(groups);
    }
    return [{ label: SORT_LABELS[sort], groups }];
  }, [data, query, sort]);

  const scopeLabel = data?.scope_label || "Run";
  const runClustering = data?.run_clustering ?? null;
  const stalenessThresholds = thresholdsFromRunning(data?.running_threshold_seconds);

  return (
    <div className="flex h-full flex-col border-r border-border bg-card/40">
      <div className="border-b border-border p-3 space-y-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={`search by ${scopeLabel.toLowerCase()}, node, profile…`}
          className="h-7 w-full rounded-md border border-input bg-background px-2 text-xs placeholder:text-muted-foreground/60 focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="flex items-center gap-2">
          <span className="text-[10px] uppercase tracking-wider text-muted-foreground">sort</span>
          <select
            value={sort}
            onChange={(e) => setSort(e.target.value as SortMode)}
            className="h-6 flex-1 rounded-md border border-input bg-background px-1 text-[11px] focus:outline-none focus:ring-1 focus:ring-ring"
          >
            {(Object.keys(SORT_LABELS) as SortMode[]).map((s) => (
              <option key={s} value={s}>{SORT_LABELS[s]}</option>
            ))}
          </select>
        </div>
      </div>

      <div className="flex-1 overflow-auto">
        {isLoading ? (
          <p className="p-3 text-xs text-muted-foreground">loading…</p>
        ) : !buckets || buckets.length === 0 ? (
          <p className="p-3 text-xs text-muted-foreground italic">no runs match.</p>
        ) : (
          buckets.map((bucket, i) => (
            <DateBucket
              key={bucket.label}
              label={bucket.label}
              scopeLabel={scopeLabel}
              runClustering={runClustering}
              stalenessThresholds={stalenessThresholds}
              groups={bucket.groups}
              selectedThreadId={selectedThreadId}
              onSelectThread={onSelectThread}
              onSelectRun={onSelectRun}
              defaultExpanded={i === 0 || bucket.label === "Today"}
            />
          ))
        )}
      </div>
    </div>
  );
}

function DateBucket({
  label,
  scopeLabel,
  runClustering,
  stalenessThresholds,
  groups,
  selectedThreadId,
  onSelectThread,
  onSelectRun,
  defaultExpanded,
}: {
  label: string;
  scopeLabel: string;
  runClustering: RunClustering | null;
  stalenessThresholds: ReturnType<typeof thresholdsFromRunning>;
  groups: ScopeGroup[];
  selectedThreadId: string | null;
  onSelectThread: (threadId: string | null) => void;
  onSelectRun?: (threadIds: string[]) => void;
  defaultExpanded: boolean;
}) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  return (
    <div className="border-b border-border/40 last:border-0">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        className="flex w-full items-center gap-2 px-3 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-muted-foreground hover:bg-accent/30"
      >
        <span className="text-[9px]">{expanded ? "▼" : "▶"}</span>
        <span>{label}</span>
        <Badge variant="outline" className="ml-auto text-[10px]">
          {groups.length}
        </Badge>
      </button>
      {expanded ? (
        <div>
          {groups.map((group) => (
            <ScopeRow
              key={`${group.scopeKind}/${group.scopeId}`}
              scopeLabel={scopeLabel}
              runClustering={runClustering}
              stalenessThresholds={stalenessThresholds}
              group={group}
              selectedThreadId={selectedThreadId}
              onSelectThread={onSelectThread}
              onSelectRun={onSelectRun}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

// Threads within a scope are clustered into "runs" — a logical execution
// pass that may span multiple stages. Two strategies, applied in order:
//   1. Pattern match: extract a cluster key from one of the parsed
//      fields using the project's metadata rule. Threads with matching
//      keys cluster together.
//   2. Time proximity: pattern-misses cluster with the nearest
//      key-having cluster within `time_window_seconds`, else become
//      their own time-bucketed cluster.
//
// The rule comes from the LLM-derived `run_clustering` metadata, with
// a hardcoded fallback (trailing UUID in stage_detail + 5 min) used
// when no scan has landed yet.

const HEURISTIC_FALLBACK: RunClustering = {
  source_field: "stage_detail",
  pattern: "([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$",
  time_window_seconds: 300,
  run_label: "Run",
};

interface RunCluster {
  /** Stable display key — extracted cluster key or synthesized fallback. */
  key: string;
  /** Short label shown to the user. */
  label: string;
  /** All threads in this cluster, newest-first. */
  threads: ThreadSummary[];
  latest: string | null;
}

/** Read the configured field's value off a thread row. */
function fieldValue(thread: ThreadSummary, field: RunClustering["source_field"]): string {
  switch (field) {
    case "thread_id": return thread.thread_id;
    case "scope_id": return thread.scope_id;
    case "stage": return thread.stage;
    case "stage_detail": return thread.stage_detail;
  }
}

function clusterThreadsIntoRuns(
  threads: ThreadSummary[],
  rule: RunClustering | null,
): RunCluster[] {
  const effective = rule ?? HEURISTIC_FALLBACK;
  // Compile pattern once. Bad regex from the LLM shouldn't break the
  // sidebar — fall back to time-only bucketing when compilation fails.
  let regex: RegExp | null = null;
  if (effective.pattern) {
    try {
      regex = new RegExp(effective.pattern);
    } catch {
      regex = null;
    }
  }
  const windowMs = Math.max(0, effective.time_window_seconds) * 1000;

  // Sort once newest-first; everything below assumes that order.
  const sorted = [...threads].sort(
    (a, b) => (b.last_updated ?? "").localeCompare(a.last_updated ?? ""),
  );

  // Step 1 — bucket by extracted cluster key.
  const byKey = new Map<string, ThreadSummary[]>();
  const noKey: ThreadSummary[] = [];
  for (const t of sorted) {
    const value = fieldValue(t, effective.source_field) || "";
    const m = regex ? regex.exec(value) : null;
    const key = m ? (m[1] ?? m[0]) : null;
    if (key) {
      if (!byKey.has(key)) byKey.set(key, []);
      byKey.get(key)!.push(t);
    } else {
      noKey.push(t);
    }
  }

  const clusters: RunCluster[] = [];
  for (const [key, ts] of byKey.entries()) {
    // Truncate to 8 chars for display when key looks UUID-ish, else
    // keep the key as-is up to a sensible width.
    const display = key.length > 12 ? `${key.slice(0, 8)}…` : key;
    clusters.push({
      key: `key:${key}`,
      label: `${effective.run_label} ${display}`,
      threads: ts,
      latest: ts[0]?.last_updated ?? null,
    });
  }

  // Step 2 — pattern-misses attach to nearest key-cluster within the
  // configured window, else become their own time-bucketed cluster.
  for (const t of noKey) {
    const tTime = t.last_updated ? new Date(t.last_updated).getTime() : null;
    if (tTime === null || windowMs === 0) {
      clusters.push({
        key: `solo:${t.thread_id}`,
        label: effective.run_label,
        threads: [t],
        latest: t.last_updated,
      });
      continue;
    }
    let attached = false;
    for (const c of clusters) {
      const cTime = c.latest ? new Date(c.latest).getTime() : null;
      if (cTime === null) continue;
      if (Math.abs(cTime - tTime) <= windowMs) {
        c.threads.push(t);
        c.threads.sort((a, b) => (b.last_updated ?? "").localeCompare(a.last_updated ?? ""));
        if ((t.last_updated ?? "") > (c.latest ?? "")) c.latest = t.last_updated;
        attached = true;
        break;
      }
    }
    if (!attached) {
      clusters.push({
        key: `time:${t.thread_id}`,
        label: `${effective.run_label} @ ${absoluteTime(t.last_updated)}`,
        threads: [t],
        latest: t.last_updated,
      });
    }
  }

  // Newest cluster first.
  clusters.sort((a, b) => (b.latest ?? "").localeCompare(a.latest ?? ""));
  return clusters;
}

function ScopeRow({
  scopeLabel,
  runClustering,
  stalenessThresholds,
  group,
  selectedThreadId,
  onSelectThread,
  onSelectRun,
}: {
  scopeLabel: string;
  runClustering: RunClustering | null;
  stalenessThresholds: ReturnType<typeof thresholdsFromRunning>;
  group: ScopeGroup;
  selectedThreadId: string | null;
  onSelectThread: (threadId: string | null) => void;
  onSelectRun?: (threadIds: string[]) => void;
}) {
  const [expanded, setExpanded] = useState(false);

  const clusters = useMemo(
    () => clusterThreadsIntoRuns(group.threads, runClustering),
    [group.threads, runClustering],
  );
  const stagesOverall = useMemo(() => {
    const m = new Map<string, number>();
    for (const t of group.threads) m.set(t.stage || "(direct)", (m.get(t.stage || "(direct)") ?? 0) + 1);
    return [...m.entries()];
  }, [group.threads]);

  const focusLatest = () => {
    const latest = clusters[0]?.threads[0];
    if (latest) onSelectThread(selectedThreadId === latest.thread_id ? null : latest.thread_id);
  };

  const headerLabel = `${scopeLabel} ${group.scopeId.slice(0, 8)}…`;
  const isSelected = group.threads.some((t) => t.thread_id === selectedThreadId);

  return (
    <div className="border-t border-border/30 first:border-t-0">
      <div
        className={cn(
          "flex w-full items-start gap-2 px-3 py-1.5 text-xs hover:bg-accent/30 cursor-pointer",
          isSelected && "bg-accent/50",
        )}
        onClick={focusLatest}
      >
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
          className="text-muted-foreground hover:text-foreground text-[10px] w-3 shrink-0 mt-0.5"
          aria-label={expanded ? "collapse" : "expand"}
        >
          {expanded ? "▼" : "▶"}
        </button>
        <span className={cn("h-1.5 w-1.5 rounded-full shrink-0 mt-1.5", STATUS_DOT[group.dominantStatus])} />
        <div className="flex-1 min-w-0">
          <div className="flex items-baseline gap-2">
            <span className="font-medium truncate">{headerLabel}</span>
            <span className="text-[10px] text-muted-foreground/70 shrink-0">
              {relativeTime(group.latest)}
            </span>
          </div>
          <div className="flex items-center gap-1 text-[10px] text-muted-foreground/80">
            <span>{absoluteTime(group.latest)}</span>
            <span>·</span>
            <span>{clusters.length} run{clusters.length === 1 ? "" : "s"}</span>
            <span>·</span>
            <span>{group.threads.length} thread{group.threads.length === 1 ? "" : "s"}</span>
          </div>
          <div className="mt-0.5 flex flex-wrap items-center gap-1">
            {stagesOverall.map(([stage, count]) => (
              <span
                key={stage}
                className="inline-flex items-center rounded border border-border/60 px-1 text-[9px] font-mono text-muted-foreground/80"
              >
                {stage}{count > 1 ? ` ×${count}` : ""}
              </span>
            ))}
          </div>
        </div>
      </div>
      {expanded ? (
        <div className="bg-background/30">
          {clusters.map((cluster) => (
            <RunClusterRow
              key={cluster.key}
              cluster={cluster}
              stalenessThresholds={stalenessThresholds}
              selectedThreadId={selectedThreadId}
              onSelectThread={onSelectThread}
              onSelectRun={onSelectRun}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}


function RunClusterRow({
  cluster,
  stalenessThresholds,
  selectedThreadId,
  onSelectThread,
  onSelectRun,
}: {
  cluster: RunCluster;
  stalenessThresholds: ReturnType<typeof thresholdsFromRunning>;
  selectedThreadId: string | null;
  onSelectThread: (threadId: string | null) => void;
  onSelectRun?: (threadIds: string[]) => void;
}) {
  const [expanded, setExpanded] = useState(true); // runs default open — usually only a few threads each
  const stages = useMemo(() => {
    const m = new Map<string, ThreadSummary[]>();
    for (const t of cluster.threads) {
      const key = t.stage || "(direct)";
      if (!m.has(key)) m.set(key, []);
      m.get(key)!.push(t);
    }
    return [...m.entries()].sort((a, b) => {
      const aT = a[1][0]?.last_updated ?? "";
      const bT = b[1][0]?.last_updated ?? "";
      return bT.localeCompare(aT);
    });
  }, [cluster.threads]);

  // Click anywhere on the cluster header → focus this run in the player
  // (primary = oldest thread, rest = siblings, chronological order).
  // Chevron remains a separate button for expand/collapse so users can
  // browse without committing to a focus change.
  const handleHeaderClick = () => {
    if (!onSelectRun) return;
    const sorted = [...cluster.threads]
      .sort((a, b) => (a.last_updated ?? "").localeCompare(b.last_updated ?? ""))
      .map((t) => t.thread_id);
    onSelectRun(sorted);
  };

  const isSelected = cluster.threads.some((t) => t.thread_id === selectedThreadId);

  return (
    <div className="border-t border-border/30 first:border-t-0">
      <div
        className={cn(
          "flex w-full items-center gap-2 px-3 py-1 text-[10px] text-muted-foreground hover:bg-accent/20 cursor-pointer",
          isSelected && "bg-accent/30",
        )}
        onClick={handleHeaderClick}
        title="Open this run in the player"
      >
        <button
          type="button"
          onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v); }}
          className="ml-1 text-[9px] hover:text-foreground"
          aria-label={expanded ? "collapse" : "expand"}
        >
          {expanded ? "▼" : "▶"}
        </button>
        <span className="font-mono uppercase tracking-wider">{cluster.label}</span>
        <span className="text-muted-foreground/70">·</span>
        <span>{cluster.threads.length} thread{cluster.threads.length === 1 ? "" : "s"}</span>
        <span className="ml-auto">{relativeTime(cluster.latest)}</span>
      </div>
      {expanded ? (
        <div>
          {stages.map(([stage, threads]) => (
            <StageBlock
              key={stage}
              stage={stage}
              threads={threads}
              stalenessThresholds={stalenessThresholds}
              selectedThreadId={selectedThreadId}
              onSelectThread={onSelectThread}
            />
          ))}
        </div>
      ) : null}
    </div>
  );
}

function StageBlock({
  stage,
  threads,
  stalenessThresholds,
  selectedThreadId,
  onSelectThread,
}: {
  stage: string;
  threads: ThreadSummary[];
  stalenessThresholds: ReturnType<typeof thresholdsFromRunning>;
  selectedThreadId: string | null;
  onSelectThread: (threadId: string | null) => void;
}) {
  return (
    <div>
      <div className="px-3 pt-1 pb-0.5 text-[9px] uppercase tracking-wider text-muted-foreground/70 font-mono">
        {stage}
      </div>
      <ul>
        {threads.map((t) => (
          <ThreadRow
            key={t.thread_id}
            thread={t}
            active={selectedThreadId === t.thread_id}
            stalenessThresholds={stalenessThresholds}
            onClick={() =>
              onSelectThread(selectedThreadId === t.thread_id ? null : t.thread_id)
            }
          />
        ))}
      </ul>
    </div>
  );
}

function ThreadRow({
  thread,
  active,
  stalenessThresholds,
  onClick,
}: {
  thread: ThreadSummary;
  active: boolean;
  stalenessThresholds: ReturnType<typeof thresholdsFromRunning>;
  onClick: () => void;
}) {
  const detail = thread.stage_detail || "(direct)";
  const staleness = getStaleness(thread, stalenessThresholds);
  return (
    <li>
      <button
        type="button"
        onClick={onClick}
        className={cn(
          "block w-full pl-7 pr-3 py-1.5 text-left text-[11px] hover:bg-accent/40 transition-colors",
          active && "bg-accent text-accent-foreground",
          staleness === "stuck" && "border-l-2 border-l-rose-500",
          staleness === "stale" && "border-l-2 border-l-amber-500",
          staleness === "hitl-idle" && "border-l-2 border-l-amber-500/60",
        )}
      >
        <div className="flex items-center gap-1.5">
          <span className={cn("h-1.5 w-1.5 rounded-full shrink-0", STATUS_DOT[thread.status])} />
          <span className="font-mono truncate flex-1 min-w-0">{detail}</span>
          <StalenessBadge staleness={staleness} lastUpdated={thread.last_updated} />
          <span
            className="text-[10px] text-muted-foreground/70 shrink-0"
            title={absoluteTime(thread.last_updated)}
          >
            {relativeTime(thread.last_updated)}
          </span>
        </div>
        <div className="mt-0.5 ml-3 flex items-center gap-1.5 text-[10px] text-muted-foreground">
          {thread.current_node ? (
            <span className="font-mono text-foreground/80 truncate">@{thread.current_node}</span>
          ) : null}
          {thread.step != null ? <span>· step {thread.step}</span> : null}
          {thread.agent_profile ? <span>· {thread.agent_profile}</span> : null}
          <span className="ml-auto text-muted-foreground/60">{absoluteTime(thread.last_updated)}</span>
        </div>
      </button>
    </li>
  );
}
