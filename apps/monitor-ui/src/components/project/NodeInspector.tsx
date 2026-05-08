/**
 * NodeInspector — opens when a node is clicked.
 *
 * Always renders the node's intent description (role + label + summary
 * from the metadata cache). When a thread is also focused, additionally
 * lists the checkpoints in that thread where this node was the current
 * node, with full deserialized state per visit.
 */

import { useState } from "react";

import { useGetThreadDetailQuery, type CheckpointDetail, type NodeMeta } from "@/api";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { JsonTree } from "@/components/threads/JsonTree";
import { cn } from "@/lib/utils";

interface NodeInspectorProps {
  projectName: string;
  graphName: string;     // kept in props for caller convenience; not rendered directly
  graphLabel: string;
  node: string;
  meta: NodeMeta | undefined;
  // When set, additionally show the per-checkpoint history at this node
  // for the focused thread.
  threadId: string | null;
  onClose: () => void;
}

const ROLE_BADGE: Record<string, { label: string; className: string }> = {
  entry: { label: "entry", className: "bg-amber-500/15 text-amber-300 border-amber-500/40" },
  exit: { label: "exit", className: "bg-emerald-500/15 text-emerald-300 border-emerald-500/40" },
  router: { label: "router", className: "bg-violet-500/15 text-violet-300 border-violet-500/40" },
  gate: { label: "gate", className: "bg-orange-500/15 text-orange-300 border-orange-500/40" },
  critic: { label: "critic", className: "bg-rose-500/15 text-rose-300 border-rose-500/40" },
  synthesis: { label: "synthesis", className: "bg-emerald-600/15 text-emerald-300 border-emerald-600/40" },
  executor: { label: "executor", className: "bg-zinc-500/15 text-zinc-300 border-zinc-500/40" },
};

export function NodeInspector({
  projectName,
  graphLabel,
  node,
  meta,
  threadId,
  onClose,
}: NodeInspectorProps) {
  const displayLabel = (meta?.label && meta.label.trim()) || node;
  const roleBadge = meta?.role ? ROLE_BADGE[meta.role] : null;

  return (
    <div className="flex h-full flex-col border-l border-border bg-card/40">
      <div className="flex items-start justify-between border-b border-border px-3 py-2 gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">node</p>
            {roleBadge ? (
              <span className={cn(
                "inline-flex items-center rounded-md border px-1.5 py-0 text-[10px] font-medium",
                roleBadge.className,
              )}>
                {roleBadge.label}
              </span>
            ) : null}
          </div>
          <h3 className="font-mono text-sm break-all mt-0.5">{displayLabel}</h3>
          {displayLabel !== node ? (
            <p className="text-[10px] text-muted-foreground/70 font-mono break-all mt-0.5">
              {node}
            </p>
          ) : null}
          <p className="text-[10px] text-muted-foreground mt-0.5">
            in <span className="font-mono">{graphLabel}</span>
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>×</Button>
      </div>

      <div className="flex-1 overflow-auto p-3 space-y-3">
        {meta?.summary ? (
          <div className="rounded-md border border-border bg-card/60 px-3 py-2 text-xs leading-relaxed text-foreground/90">
            {meta.summary}
          </div>
        ) : (
          <div className="rounded-md border border-dashed border-border/60 px-3 py-2 text-[11px] text-muted-foreground italic">
            No description yet — re-run <code>chimera monitor rescan {projectName}</code> to
            populate node intent from the codebase.
          </div>
        )}

        {threadId ? (
          <NodeCheckpoints projectName={projectName} threadId={threadId} node={node} />
        ) : (
          <p className="text-[11px] text-muted-foreground italic">
            Focus a run in the sidebar to see this node's state at each visit.
          </p>
        )}
      </div>
    </div>
  );
}

function NodeCheckpoints({
  projectName,
  threadId,
  node,
}: {
  projectName: string;
  threadId: string;
  node: string;
}) {
  const { data, isLoading, error } = useGetThreadDetailQuery(
    { name: projectName, threadId, limit: 50 },
    { pollingInterval: 2000 },
  );

  if (isLoading) return <p className="text-xs text-muted-foreground">loading checkpoints…</p>;
  if (error) return <p className="text-xs text-destructive">{String(error)}</p>;
  if (!data) return null;

  // Backend returns checkpoints newest-first; reverse for chronological
  // order so we can find each checkpoint's predecessor by index.
  const chronological = [...data.checkpoints].reverse();
  const visits = chronological
    .map((cp, i) => ({ cp, prev: i > 0 ? chronological[i - 1] : null }))
    .filter((v) => v.cp.node === node);

  if (visits.length === 0) {
    return (
      <p className="text-[11px] text-muted-foreground italic">
        No checkpoint in this run had <code className="font-mono">{node}</code> as
        its current node yet.
      </p>
    );
  }

  return (
    <>
      <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
        {visits.length} visit{visits.length === 1 ? "" : "s"} in focused run
      </p>
      {visits.map(({ cp, prev }) => (
        <CheckpointVisitCard key={cp.checkpoint_id} cp={cp} prev={prev} />
      ))}
    </>
  );
}

type ViewMode = "diff" | "full";

/**
 * One visit card. Defaults to "diff" mode — shows what changed since
 * the previous checkpoint in the run (the input THIS node received).
 * Toggle to "full" to inspect the entire state tree.
 */
function CheckpointVisitCard({
  cp,
  prev,
}: {
  cp: CheckpointDetail;
  prev: CheckpointDetail | null;
}) {
  const [mode, setMode] = useState<ViewMode>("diff");
  const canDiff = !!prev;

  return (
    <Card>
      <CardHeader className="space-y-1 py-2">
        <div className="flex items-center justify-between gap-2">
          <CardTitle className="text-xs font-mono">step {cp.step ?? "?"}</CardTitle>
          <div className="flex items-center gap-1">
            <Badge variant="outline" className="text-[10px]">
              {cp.created_at?.slice(0, 19) ?? "?"}
            </Badge>
            <div className="inline-flex rounded-md border border-input overflow-hidden">
              <button
                type="button"
                disabled={!canDiff}
                onClick={() => setMode("diff")}
                className={cn(
                  "px-1.5 py-0.5 text-[10px] transition-colors",
                  mode === "diff"
                    ? "bg-emerald-500/15 text-emerald-300"
                    : "text-muted-foreground hover:bg-accent",
                  !canDiff && "opacity-40 cursor-not-allowed",
                )}
                title={canDiff ? "Show what changed since the previous step" : "No previous step to diff against"}
              >
                diff
              </button>
              <button
                type="button"
                onClick={() => setMode("full")}
                className={cn(
                  "px-1.5 py-0.5 text-[10px] transition-colors border-l border-input",
                  mode === "full"
                    ? "bg-emerald-500/15 text-emerald-300"
                    : "text-muted-foreground hover:bg-accent",
                )}
                title="Show the complete state tree"
              >
                full
              </button>
            </div>
          </div>
        </div>
        <p className="text-[10px] text-muted-foreground font-mono">{cp.checkpoint_id}</p>
      </CardHeader>
      <CardContent className="pt-0">
        {mode === "diff" && canDiff ? (
          <StateDiff prev={prev.state} curr={cp.state} prevStep={prev.step} />
        ) : (
          <JsonTree data={cp.state} />
        )}
      </CardContent>
    </Card>
  );
}

/**
 * Top-level shallow diff between two LangGraph state snapshots.
 * LangGraph state is typically a flat dict matching the StateGraph
 * schema, so a shallow key-by-key diff captures what reducers wrote
 * during the step. Deep diffs (per-list-item changes for messages,
 * etc.) are out of scope — toggle to "full" for those.
 *
 * Equality: JSON-stringify. Fine for state — no Dates/Functions/etc.
 * Slow on huge values but state objects rarely exceed a few hundred KB.
 */
function StateDiff({
  prev,
  curr,
  prevStep,
}: {
  prev: unknown;
  curr: unknown;
  prevStep: number | null;
}) {
  if (!isPlainObject(prev) || !isPlainObject(curr)) {
    return (
      <p className="text-[11px] text-muted-foreground italic">
        Diff unavailable — state isn't a top-level object. Use "full" view.
      </p>
    );
  }

  const added: Array<[string, unknown]> = [];
  const removed: Array<[string, unknown]> = [];
  const changed: Array<[string, unknown, unknown]> = [];

  for (const k of Object.keys(curr).sort()) {
    if (!(k in prev)) {
      added.push([k, curr[k]]);
    } else if (jsonEqual(prev[k], curr[k])) {
      // unchanged — skip
    } else {
      changed.push([k, prev[k], curr[k]]);
    }
  }
  for (const k of Object.keys(prev).sort()) {
    if (!(k in curr)) removed.push([k, prev[k]]);
  }

  const noChange = added.length === 0 && removed.length === 0 && changed.length === 0;
  if (noChange) {
    return (
      <p className="text-[11px] text-muted-foreground italic">
        No top-level state keys changed{prevStep != null ? ` since step ${prevStep}` : ""}.
      </p>
    );
  }

  return (
    <div className="space-y-2 text-xs">
      {added.length > 0 ? (
        <DiffSection
          title={`+ added (${added.length})`}
          tone="added"
          entries={added.map(([k, v]) => ({ key: k, value: v }))}
        />
      ) : null}
      {changed.length > 0 ? (
        <DiffSection
          title={`± changed (${changed.length})`}
          tone="changed"
          entries={changed.map(([k, oldV, newV]) => ({ key: k, value: newV, oldValue: oldV }))}
        />
      ) : null}
      {removed.length > 0 ? (
        <DiffSection
          title={`− removed (${removed.length})`}
          tone="removed"
          entries={removed.map(([k, v]) => ({ key: k, value: v }))}
        />
      ) : null}
    </div>
  );
}

function DiffSection({
  title,
  tone,
  entries,
}: {
  title: string;
  tone: "added" | "changed" | "removed";
  entries: Array<{ key: string; value: unknown; oldValue?: unknown }>;
}) {
  const toneClass = {
    added: "border-emerald-500/40 bg-emerald-500/5",
    changed: "border-amber-500/40 bg-amber-500/5",
    removed: "border-rose-500/40 bg-rose-500/5",
  }[tone];
  const toneLabel = {
    added: "text-emerald-300",
    changed: "text-amber-300",
    removed: "text-rose-300",
  }[tone];
  return (
    <div className={cn("rounded-md border px-2 py-1.5", toneClass)}>
      <p className={cn("text-[10px] uppercase tracking-wider mb-1 font-semibold", toneLabel)}>
        {title}
      </p>
      <div className="space-y-2">
        {entries.map((e) => (
          <div key={e.key} className="space-y-0.5">
            <p className="font-mono text-[11px] text-foreground/90">{e.key}</p>
            {tone === "changed" ? (
              <div className="grid grid-cols-1 gap-1 pl-3">
                <div className="flex gap-1 items-baseline">
                  <span className="text-[9px] uppercase text-rose-400/70 font-mono w-8 shrink-0">old</span>
                  <div className="min-w-0 flex-1"><JsonTree data={e.oldValue} /></div>
                </div>
                <div className="flex gap-1 items-baseline">
                  <span className="text-[9px] uppercase text-emerald-400/70 font-mono w-8 shrink-0">new</span>
                  <div className="min-w-0 flex-1"><JsonTree data={e.value} /></div>
                </div>
              </div>
            ) : (
              <div className="pl-3"><JsonTree data={e.value} /></div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

function isPlainObject(v: unknown): v is Record<string, unknown> {
  return v !== null && typeof v === "object" && !Array.isArray(v);
}

function jsonEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true;
  try {
    return JSON.stringify(a) === JSON.stringify(b);
  } catch {
    return false;
  }
}
