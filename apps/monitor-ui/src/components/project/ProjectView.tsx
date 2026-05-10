/**
 * ProjectView — unified single-page view per project.
 *
 * Layout:
 *   ┌──────────────────────────────────────────────────┐
 *   │ status counts header                              │
 *   ├────────┬──────────────────────────┬──────────────┤
 *   │ runs   │ live topology (all       │ node         │
 *   │ side-  │ graphs; focused run's    │ inspector    │
 *   │ bar    │ current node highlighted)│ (when a node │
 *   │        │                          │ is clicked)  │
 *   └────────┴──────────────────────────┴──────────────┘
 *
 * Node click → opens NodeInspector with checkpoints where that node was
 * the current node for the focused run.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";

import { useGetTopologyQuery, useListThreadsQuery, type NodeMeta, type ThreadStatus, type ThreadSummary, type TopologyResponse } from "@/api";
import { ActiveNodeCard } from "@/components/project/ActiveNodeCard";
import { FlowCanvas } from "@/components/project/FlowCanvas";
import { LiveRunsCard } from "@/components/project/LiveRunsCard";
import { NodeInspector } from "@/components/project/NodeInspector";
import { ProjectNavTabs } from "@/components/project/ProjectNavTabs";
import { ReplayController, type ReplayState } from "@/components/project/ReplayController";
import { RunsDrawer } from "@/components/project/RunsDrawer";
import { RunStepsCard } from "@/components/project/RunStepsCard";
import { countStaleness, thresholdsFromRunning } from "@/lib/staleness";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { useRunCheckpoints } from "@/lib/useRunCheckpoints";
import { cn } from "@/lib/utils";

const STATUS_DOT: Record<ThreadStatus, string> = {
  running: "bg-emerald-400 animate-pulse",
  paused: "bg-amber-400",
  starting: "bg-sky-400 animate-pulse",
  idle: "bg-zinc-500",
};

export function ProjectView() {
  const { name } = useParams<{ name: string }>();
  const projectName = name ?? "";

  // `userSelectedThreadId` is the thread the user explicitly clicked.
  // When null, we auto-follow the most-recent running thread so the user
  // can just leave the dashboard open and watch live activity flow.
  const [userSelectedThreadId, setUserSelectedThreadId] = useState<string | null>(null);
  const [selectedNode, setSelectedNode] = useState<{ graph: string; node: string } | null>(null);
  const [selectedGraph, setSelectedGraph] = useState<string>("__all__");
  // When the user manually picks a tab, freeze auto-tab-following so we
  // don't yank them away from what they're inspecting.
  const [tabPinned, setTabPinned] = useState<boolean>(false);
  const [allRunsOpen, setAllRunsOpen] = useState<boolean>(false);
  // "Show all active runs" toggle — when on, every running/paused thread's
  // current node lights up simultaneously across the All canvas. Default off
  // so the focused-run UX stays clean. Persisted to localStorage.
  const [showAllActive, setShowAllActive] = useState<boolean>(() => {
    try {
      return localStorage.getItem("chimera-monitor-show-all-active") === "true";
    } catch {
      return false;
    }
  });
  // Auto-zoom level when focusing on the active node. 0 disables auto-pan
  // entirely (the canvas keeps whatever viewport the user dragged to).
  const [focusZoom, setFocusZoom] = useState<number>(() => {
    try {
      const raw = localStorage.getItem("chimera-monitor-focus-zoom");
      const n = raw ? Number(raw) : NaN;
      return Number.isFinite(n) && n >= 0 ? n : 1.0;
    } catch {
      return 1.0;
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("chimera-monitor-focus-zoom", String(focusZoom));
    } catch { /* ignore */ }
  }, [focusZoom]);
  useEffect(() => {
    try {
      localStorage.setItem("chimera-monitor-show-all-active", String(showAllActive));
    } catch { /* ignore */ }
  }, [showAllActive]);

  // Replay state — null when not in replay mode (live follow). When set,
  // the canvas highlights the replay's current node instead of the
  // focused thread's live current_node. Reset effect lives below
  // focusedThread is defined.
  const [replayState, setReplayState] = useState<ReplayState>({
    index: null,
    playing: false,
    speedMs: 750,
  });
  const [replayActiveNode, setReplayActiveNode] = useState<string | null>(null);
  // Run-mode replay: when the user clicks a cluster header, the sister
  // thread_ids land here and ReplayController merges every checkpoint
  // across them into one chronological timeline.
  const [siblingThreadIds, setSiblingThreadIds] = useState<string[]>([]);

  // Ghost overlay — when on, every node that fired in the run is shown
  // dimmed-out with a numbered step badge in execution order. Toggle
  // lives in the ReplayController toolbar. Persisted across renders
  // but not localStorage — it's a per-session lens.
  const [ghostMode, setGhostMode] = useState<boolean>(false);

  const { data: threadsData, error: threadsError } = useListThreadsQuery(
    { name: projectName, limit: 100, offset: 0 },
    { pollingInterval: 2000 },
  );

  const autoFocusedThreadId = useMemo(() => {
    if (!threadsData) return null;
    // Most-recently-updated running thread wins. Falls back to paused
    // (which still represents an in-flight run waiting on a human).
    const ranked = [...threadsData.threads]
      .filter((t) => t.status === "running" || t.status === "paused" || t.status === "starting")
      .sort((a, b) => (b.last_updated ?? "").localeCompare(a.last_updated ?? ""));
    return ranked[0]?.thread_id ?? null;
  }, [threadsData]);

  // Drop a stale manual selection when fresh activity appears elsewhere.
  //
  // Two release triggers, both about "the user has moved on":
  //
  // 1. Focused thread is idle AND another live thread exists.
  //    Classic case: clicked a run, it finished, a new one started.
  //
  // 2. Focused thread is technically still "running" by the heuristic,
  //    BUT a SISTER THREAD (same scope_id — usually the same logical
  //    multi-stage run, e.g. jeevy's deliverable progressing
  //    ingest→digestion→output) has activity > 30s newer.
  //    Real-world: clicked ingest #17 to inspect, ingest finished but
  //    our running-threshold heuristic still classifies it running for
  //    5min after the last checkpoint. Meanwhile digestion spawned and
  //    is actively writing checkpoints. The user wants the dashboard
  //    to follow the action across stage transitions, not stay
  //    pinned on the finished stage.
  //
  // Don't release if the focused thread is still actively progressing
  // — that would yank users away from runs they're watching.
  useEffect(() => {
    if (!userSelectedThreadId || !threadsData) return;
    const focused = threadsData.threads.find((t) => t.thread_id === userSelectedThreadId);
    if (!focused) return;

    // Trigger 1 — focused is idle, fresh live alternative exists.
    if (focused.status === "idle" && autoFocusedThreadId && autoFocusedThreadId !== userSelectedThreadId) {
      setUserSelectedThreadId(null);
      return;
    }

    // Trigger 2 — sister thread has overtaken focused in activity.
    if (!focused.scope_id || !focused.last_updated) return;
    const focusedTs = new Date(focused.last_updated).getTime();
    if (!Number.isFinite(focusedTs)) return;

    const liveSister = threadsData.threads.find((t) => {
      if (t.thread_id === focused.thread_id) return false;
      if (t.scope_id !== focused.scope_id) return false;
      if (t.status === "idle") return false;
      if (!t.last_updated) return false;
      const ts = new Date(t.last_updated).getTime();
      return Number.isFinite(ts) && ts - focusedTs > 30_000;
    });
    if (liveSister) {
      setUserSelectedThreadId(null);
    }
  }, [userSelectedThreadId, threadsData, autoFocusedThreadId]);

  const effectiveSelectedThreadId = userSelectedThreadId ?? autoFocusedThreadId;

  const focusedThread = useMemo(() => {
    if (!threadsData || !effectiveSelectedThreadId) return null;
    return threadsData.threads.find((t) => t.thread_id === effectiveSelectedThreadId) ?? null;
  }, [threadsData, effectiveSelectedThreadId]);

  const focusedIsLive =
    !!focusedThread &&
    (focusedThread.status === "running" ||
      focusedThread.status === "paused" ||
      focusedThread.status === "starting");

  // Fetch the merged-run checkpoint timeline once — feeds both the
  // ReplayController scrubber and the ghost overlay's fired-nodes map.
  // SSE-streamed while live-tailing (catches sub-2s nodes the old 2s
  // poll would miss); plain one-shot fetch while replay is scrubbing.
  const liveTailing = replayState.index === null && focusedIsLive;
  const { checkpoints: runCheckpoints, isLoading: checkpointsLoading } = useRunCheckpoints(
    projectName,
    focusedThread?.thread_id ?? null,
    siblingThreadIds,
    liveTailing ? 2000 : 0,
    { useStreaming: liveTailing },
  );

  const { data: topology } = useGetTopologyQuery(projectName);

  // Build a map: threadId → graphName, used to resolve which graph each
  // checkpoint's node belongs to. Same node name can appear in multiple
  // graphs; the originating thread disambiguates.
  const threadToGraph = useMemo(() => {
    const m = new Map<string, string>();
    if (!topology || !threadsData) return m;
    for (const t of threadsData.threads) {
      const g = pickGraphName(t, topology.graphs);
      if (g) m.set(t.thread_id, g);
    }
    return m;
  }, [topology, threadsData]);

  // Ghost overlay map: ${graphName}__${nodeName} → 1-based step number
  // (first occurrence wins). Built only when we have data — empty map
  // means nothing renders ghost.
  const ghostNodeSteps = useMemo(() => {
    const m = new Map<string, number>();
    if (runCheckpoints.length === 0 || !topology) return m;
    runCheckpoints.forEach((cp, i) => {
      if (!cp.node) return;
      // Resolve graph: thread → graph; fallback to any graph that contains the node.
      let graphName = threadToGraph.get(cp.thread_id);
      if (!graphName) {
        const matches = topology.graphs.filter((g) => g.nodes.includes(cp.node!));
        graphName = matches[0]?.name;
      }
      if (!graphName) return;
      const id = `${graphName}__${cp.node}`;
      if (!m.has(id)) m.set(id, i + 1); // 1-based
    });
    return m;
  }, [runCheckpoints, topology, threadToGraph]);

  // Trail map: ${graphName}__${nodeName} → trail index (0 = most recent
  // non-current node, max TRAIL_LENGTH-1 = oldest). Drives the fading
  // amber rings behind the focused thread's spotlight so a burst of
  // fast SSE checkpoints visibly trails the run instead of jumping
  // straight to the latest node.
  //
  // Only populated while live-tailing — replay scrubbing has its own
  // step-marker UI (ghostNodeSteps) and shouldn't double-render trail.
  const TRAIL_LENGTH = 5;
  const trailNodeIndices = useMemo(() => {
    const m = new Map<string, number>();
    if (!liveTailing || runCheckpoints.length === 0 || !topology) return m;
    const currentNode = focusedThread?.current_node ?? null;
    // Walk newest → oldest; first hit at each node id wins.
    for (let i = runCheckpoints.length - 1; i >= 0 && m.size < TRAIL_LENGTH; i--) {
      const cp = runCheckpoints[i];
      if (!cp.node) continue;
      let graphName = threadToGraph.get(cp.thread_id);
      if (!graphName) {
        const matches = topology.graphs.filter((g) => g.nodes.includes(cp.node!));
        graphName = matches[0]?.name;
      }
      if (!graphName) continue;
      // Skip the live current node — it's already rendered as the
      // pulsing spotlight, no trail ring needed.
      if (cp.node === currentNode) continue;
      const id = `${graphName}__${cp.node}`;
      if (m.has(id)) continue;
      m.set(id, m.size);
    }
    return m;
  }, [liveTailing, runCheckpoints, topology, threadToGraph, focusedThread]);

  // Reset replay when focused thread changes (declared above; effect
  // placed here so focusedThread is in scope). Also drop sibling
  // threads — they're only valid for the run that was actively played.
  //
  // EXCEPTION: when the focus change came from `handlePlayRun`, the
  // ref below is set so this reset is skipped. Without that guard, the
  // reset would clobber the play-run state set milliseconds earlier
  // (siblings cleared, index reset to null → controller reverts to
  // live mode showing the thread's last-known node).
  const skipNextResetRef = useRef(false);
  useEffect(() => {
    if (skipNextResetRef.current) {
      skipNextResetRef.current = false;
      return;
    }
    setReplayActiveNode(null);
    setReplayState((prev) => ({ index: null, playing: false, speedMs: prev.speedMs }));
    setSiblingThreadIds([]);
  }, [focusedThread?.thread_id]);

  // Click a run row in the sidebar → focus the run (primary + siblings),
  // open the player, but DON'T auto-play. User decides whether to play
  // or just toggle ghost.
  const handleSelectRun = useCallback((threadIds: string[]) => {
    if (threadIds.length === 0) return;
    const [primary, ...rest] = threadIds;
    // Skip the focus-reset effect once — siblings here are intentional.
    skipNextResetRef.current = true;
    setUserSelectedThreadId(primary);
    setSelectedNode(null);
    setSiblingThreadIds(rest);
    setReplayActiveNode(null);
    setReplayState((prev) => ({ index: null, playing: false, speedMs: prev.speedMs }));
  }, []);

  // The node inspector stays open across focus changes — clicking a
  // node is now meaningful even without a focused run (description-only
  // mode). The user closes the panel explicitly via the × button.

  const counts = useMemo(() => {
    const out: Record<ThreadStatus, number> = { running: 0, paused: 0, starting: 0, idle: 0 };
    if (threadsData) for (const t of threadsData.threads) out[t.status] += 1;
    return out;
  }, [threadsData]);

  // Staleness — surfaces "N stuck" / "N stale" chips so the user spots
  // hung runs at a glance even when not focused on a sidebar row.
  // Thresholds scale with the project's running_threshold so apps with
  // slow nodes (jeevy: 900s, chimera-pipeline: 600s+) don't false-flag
  // legitimately long executions as stale.
  const staleCounts = useMemo(
    () => countStaleness(
      threadsData?.threads ?? [],
      thresholdsFromRunning(threadsData?.running_threshold_seconds),
    ),
    [threadsData],
  );

  const noCheckpointer = (threadsError as { status?: number } | undefined)?.status === 404;

  return (
    <div className="flex h-full flex-col">
      <header className="flex shrink-0 items-center justify-between border-b border-border bg-card/40 px-4 py-2">
        <div className="flex items-center gap-3">
          <h2 className="text-sm font-semibold">{projectName}</h2>
          <ProjectNavTabs projectName={projectName} />
          {noCheckpointer ? (
            <span className="text-[11px] text-muted-foreground">topology only · no checkpointer</span>
          ) : (
            <div className="flex items-center gap-2 text-[11px] text-muted-foreground">
              {(["running", "paused", "starting", "idle"] as ThreadStatus[]).map((s) =>
                counts[s] > 0 ? (
                  <span key={s} className="inline-flex items-center gap-1">
                    <span className={cn("h-1.5 w-1.5 rounded-full", STATUS_DOT[s])} />
                    {counts[s]} {s}
                  </span>
                ) : null,
              )}
              {staleCounts.stuck > 0 ? (
                <span
                  className="inline-flex items-center rounded border border-rose-500/50 bg-rose-500/15 px-1.5 py-0 text-[10px] font-mono text-rose-300 animate-pulse"
                  title={`${staleCounts.stuck} thread${staleCounts.stuck === 1 ? "" : "s"} appear stuck (running/starting >15 min since last update)`}
                >
                  {staleCounts.stuck} stuck
                </span>
              ) : null}
              {staleCounts.stale > 0 ? (
                <span
                  className="inline-flex items-center rounded border border-amber-500/50 bg-amber-500/15 px-1.5 py-0 text-[10px] font-mono text-amber-300"
                  title={`${staleCounts.stale} thread${staleCounts.stale === 1 ? "" : "s"} stale (>5 min since last update)`}
                >
                  {staleCounts.stale} stale
                </span>
              ) : null}
              {staleCounts["hitl-idle"] > 0 ? (
                <span
                  className="inline-flex items-center rounded border border-amber-500/40 px-1.5 py-0 text-[10px] font-mono text-amber-300/90"
                  title={`${staleCounts["hitl-idle"]} paused thread${staleCounts["hitl-idle"] === 1 ? "" : "s"} idle >15 min — abandoned HITL?`}
                >
                  {staleCounts["hitl-idle"]} HITL idle
                </span>
              ) : null}
              {Object.values(counts).every((c) => c === 0) ? (
                <span>no runs</span>
              ) : null}
            </div>
          )}
        </div>
        <div className="flex items-center gap-3">
          {focusedThread ? (
            <span className="text-[11px] text-muted-foreground">
              {userSelectedThreadId ? "focused (manual)" : "auto-following live run"}
            </span>
          ) : null}
          {userSelectedThreadId ? (
            <button
              type="button"
              onClick={() => {
                setUserSelectedThreadId(null);
                setSelectedNode(null);
              }}
              className="text-[11px] text-muted-foreground hover:text-foreground"
            >
              clear focus ×
            </button>
          ) : null}
          {tabPinned ? (
            <button
              type="button"
              onClick={() => setTabPinned(false)}
              className="text-[11px] text-muted-foreground hover:text-foreground"
              title="Resume auto-switching tabs to follow the focused run"
            >
              unpin tab
            </button>
          ) : null}
        </div>
      </header>

      <div className="flex flex-1 overflow-hidden">
        <div className="flex-1 relative overflow-hidden">
          {noCheckpointer ? (
            <Card className="absolute top-3 left-3 z-10 max-w-md">
              <CardHeader className="py-2">
                <CardTitle className="text-xs">topology only · no checkpointer</CardTitle>
              </CardHeader>
              <CardContent className="text-[11px] text-muted-foreground pt-0">
                Set a <code>postgres://</code> URL in <code>.env</code> or add
                a SQLite checkpoint DB to the project's data dir, then
                restart the daemon to inspect live runs.
              </CardContent>
            </Card>
          ) : (
            <LiveRunsCard
              projectName={projectName}
              selectedThreadId={effectiveSelectedThreadId}
              onSelectThread={(id) => {
                setUserSelectedThreadId(id);
                setSelectedNode(null);
              }}
              onOpenAllRuns={() => setAllRunsOpen(true)}
            />
          )}
          <RunsDrawer
            open={allRunsOpen && !noCheckpointer}
            projectName={projectName}
            selectedThreadId={effectiveSelectedThreadId}
            onSelectThread={(id) => {
              setUserSelectedThreadId(id);
              setSelectedNode(null);
            }}
            onSelectRun={(ids) => {
              handleSelectRun(ids);
              setAllRunsOpen(false);
            }}
            onClose={() => setAllRunsOpen(false)}
          />
          <ProjectFlow
            projectName={projectName}
            focusedThread={focusedThread}
            replayActiveNode={replayActiveNode}
            replayActive={replayState.index !== null}
            ghostMode={ghostMode}
            ghostNodeSteps={ghostNodeSteps}
            trailNodeIndices={trailNodeIndices}
            selectedGraph={selectedGraph}
            // When the toggle is ON, freeze on the All tab — the user
            // explicitly opted into the multi-run overview, so don't yank
            // them away when a new focused run arrives.
            tabPinned={tabPinned || showAllActive}
            showAllActive={showAllActive}
            focusZoom={focusZoom}
            onChangeFocusZoom={setFocusZoom}
            onToggleShowAllActive={() => {
              setShowAllActive((v) => {
                const next = !v;
                // Flipping ON snaps the view back to the All tab so the
                // multi-active highlights have a place to land.
                if (next) setSelectedGraph("__all__");
                return next;
              });
            }}
            additionalActiveThreads={
              showAllActive && threadsData
                ? threadsData.threads.filter(
                    (t) =>
                      (t.status === "running" || t.status === "paused" || t.status === "starting") &&
                      t.thread_id !== focusedThread?.thread_id,
                  )
                : []
            }
            onSelectGraph={(name) => {
              setSelectedGraph(name);
              setTabPinned(true);
            }}
            onAutoGraph={(name) => setSelectedGraph(name)}
            onSelectNode={(graph, node) => {
              if (!node) {
                setSelectedNode(null);
                return;
              }
              setSelectedNode({ graph, node });
            }}
          />

          {focusedThread ? (
            <>
              <ReplayController
                threadId={focusedThread.thread_id}
                threadIsLive={focusedIsLive}
                state={replayState}
                onState={setReplayState}
                onActiveNodeChange={setReplayActiveNode}
                checkpoints={runCheckpoints}
                isLoading={checkpointsLoading}
                siblingCount={siblingThreadIds.length}
                ghostMode={ghostMode}
                onToggleGhost={() => setGhostMode((v) => !v)}
              />
              <ActiveNodeCard
                visible={ghostMode}
                graphLabel={
                  resolveActiveNodeGraphLabel(
                    topology,
                    replayActiveNode ?? focusedThread.current_node ?? null,
                    focusedThread,
                  )
                }
                nodeName={
                  replayState.index !== null
                    ? replayActiveNode
                    : (replayActiveNode ?? focusedThread.current_node ?? null)
                }
                stepNumber={
                  replayState.index !== null && runCheckpoints.length > 0
                    ? replayState.index + 1
                    : null
                }
                totalSteps={runCheckpoints.length > 0 ? runCheckpoints.length : null}
                inReplay={replayState.index !== null}
                lastUpdated={focusedThread.last_updated}
              />
              <RunStepsCard
                visible={ghostMode}
                checkpoints={runCheckpoints}
                activeIndex={
                  replayState.index !== null
                    ? replayState.index
                    : runCheckpoints.length > 0
                      ? runCheckpoints.length - 1
                      : null
                }
                resolveGraphLabel={(threadId) => {
                  const graphName = threadToGraph.get(threadId);
                  if (!graphName || !topology) return null;
                  const g = topology.graphs.find((gg) => gg.name === graphName);
                  return g ? g.label || g.name : null;
                }}
                onSelectStep={(i) => {
                  // Jump replay to this step + pause. The auto-tab effect
                  // in ProjectFlow will follow the new active node into
                  // its graph; if the step belongs to a sister thread,
                  // its node still resolves via the cross-graph lookup.
                  setReplayState((prev) => ({
                    index: i,
                    playing: false,
                    speedMs: prev.speedMs,
                  }));
                }}
              />
            </>
          ) : null}
        </div>

        {selectedNode ? (
          <div className="w-96 shrink-0">
            <NodeInspectorContainer
              projectName={projectName}
              graphName={selectedNode.graph}
              nodeName={selectedNode.node}
              focusedThreadId={focusedThread?.thread_id ?? null}
              onClose={() => setSelectedNode(null)}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}


/**
 * Resolves the node's metadata from the topology query (which is shared
 * via RTK Query cache, so this call dedupes with the FlowCanvas instance).
 * Renders the inspector once data is available.
 */
function NodeInspectorContainer({
  projectName,
  graphName,
  nodeName,
  focusedThreadId,
  onClose,
}: {
  projectName: string;
  graphName: string;
  nodeName: string;
  focusedThreadId: string | null;
  onClose: () => void;
}) {
  const { data: topology } = useGetTopologyQuery(projectName);
  const graph = topology?.graphs.find((g) => g.name === graphName);
  const meta: NodeMeta | undefined = graph?.node_meta?.[nodeName];
  const graphLabel = graph?.label || graph?.name || graphName;
  return (
    <NodeInspector
      projectName={projectName}
      graphName={graphName}
      graphLabel={graphLabel}
      node={nodeName}
      meta={meta}
      threadId={focusedThreadId}
      onClose={onClose}
    />
  );
}


interface ProjectFlowProps {
  projectName: string;
  focusedThread: ThreadSummary | null;
  replayActiveNode: string | null;
  replayActive: boolean;
  ghostMode: boolean;
  ghostNodeSteps: Map<string, number>;
  trailNodeIndices: Map<string, number>;
  selectedGraph: string;
  tabPinned: boolean;
  showAllActive: boolean;
  focusZoom: number;
  onChangeFocusZoom: (z: number) => void;
  onToggleShowAllActive: () => void;
  additionalActiveThreads: ThreadSummary[];
  onSelectGraph: (name: string) => void;
  onAutoGraph: (name: string) => void;
  onSelectNode: (graphName: string, node: string | null) => void;
}

/**
 * Resolve the graph LABEL for whichever node is currently active.
 * Used by the floating ActiveNodeCard so the user can see which graph
 * they're looking at when ghost mode zooms out.
 */
function resolveActiveNodeGraphLabel(
  topology: TopologyResponse | undefined,
  nodeName: string | null,
  focusedThread: ThreadSummary | null,
): string | null {
  if (!topology) return null;
  if (nodeName) {
    const matches = topology.graphs.filter((g) => g.nodes.includes(nodeName));
    if (matches.length === 1) return matches[0].label || matches[0].name;
    if (matches.length > 1 && focusedThread) {
      const probe: ThreadSummary = { ...focusedThread, current_node: nodeName };
      const g = pickGraphName(probe, topology.graphs);
      const found = topology.graphs.find((gg) => gg.name === g);
      return found ? found.label || found.name : null;
    }
    if (matches.length > 0) return matches[0].label || matches[0].name;
  }
  if (focusedThread) {
    const g = pickGraphName(focusedThread, topology.graphs);
    const found = topology.graphs.find((gg) => gg.name === g);
    return found ? found.label || found.name : null;
  }
  return null;
}

function ProjectFlow({
  projectName,
  focusedThread,
  replayActiveNode,
  replayActive,
  ghostMode,
  ghostNodeSteps,
  trailNodeIndices,
  selectedGraph,
  tabPinned,
  showAllActive,
  focusZoom,
  onChangeFocusZoom,
  onToggleShowAllActive,
  additionalActiveThreads,
  onSelectGraph,
  onAutoGraph,
  onSelectNode,
}: ProjectFlowProps) {
  const { data: topology, isLoading, error } = useGetTopologyQuery(projectName);

  // Auto-switch tab to follow the active node — replay overrides live
  // current_node when scrubbed off-live, and the replay path can span
  // multiple graphs (e.g. chimera's pipeline → implementation subgraph).
  // Without this, the tab gets stuck on the live graph and the replay's
  // node has nowhere to light up.
  useEffect(() => {
    if (tabPinned || !topology) return;
    const node = replayActiveNode ?? focusedThread?.current_node ?? null;
    const probeThread: ThreadSummary | null = node
      ? focusedThread
        ? ({ ...focusedThread, current_node: node } as ThreadSummary)
        : ({
            thread_id: "",
            latest_checkpoint_id: "",
            last_updated: null,
            step: null,
            status: "idle",
            current_node: node,
            recent_nodes: [],
            agent_profile: null,
            phase: null,
            scope_kind: "thread",
            scope_id: "",
            stage: "thread",
            stage_detail: "",
          } as ThreadSummary)
      : null;
    if (!probeThread) return;
    const graphName = pickGraphName(probeThread, topology.graphs);
    if (graphName && graphName !== selectedGraph) {
      onAutoGraph(graphName);
    }
  }, [topology, focusedThread, replayActiveNode, tabPinned, selectedGraph, onAutoGraph]);

  if (isLoading) return <p className="p-4 text-xs text-muted-foreground">loading topology…</p>;
  if (error) return <p className="p-4 text-xs text-destructive">topology failed: {String(error)}</p>;
  if (!topology) return null;
  if (topology.graphs.length === 0) {
    return <p className="p-4 text-xs text-muted-foreground">no compiled graphs discovered.</p>;
  }

  return (
    <div className="flex h-full flex-col">
      <div className="flex items-center gap-3 border-b border-border bg-card/40 pr-3 min-w-0">
        <div className="min-w-0 flex-1">
          <GraphTabs
            graphs={topology.graphs.map((g) => ({ name: g.name, label: g.label || g.name }))}
            selected={selectedGraph}
            onSelect={onSelectGraph}
          />
        </div>
        <div className="flex items-center gap-2 shrink-0">
          <FocusZoomSelect value={focusZoom} onChange={onChangeFocusZoom} />
          <LockButton checked={showAllActive} onChange={onToggleShowAllActive} />
        </div>
      </div>
      <div className="flex-1 min-h-0">
        <FlowCanvas
          topology={topology}
          focusedThread={focusedThread}
          replayActiveNode={replayActiveNode}
          replayActive={replayActive}
          onSelectNode={onSelectNode}
          selectedGraph={selectedGraph}
          additionalActiveThreads={additionalActiveThreads}
          focusZoom={focusZoom}
          ghostMode={ghostMode}
          ghostNodeSteps={ghostNodeSteps}
          trailNodeIndices={trailNodeIndices}
        />
      </div>
    </div>
  );
}


const FOCUS_ZOOM_PRESETS = [
  { label: "no zoom", value: 0 },
  { label: "wide", value: 0.7 },
  { label: "balanced", value: 1.0 },
  { label: "close", value: 1.4 },
];


function FocusZoomSelect({
  value,
  onChange,
}: {
  value: number;
  onChange: (v: number) => void;
}) {
  return (
    <select
      value={value}
      onChange={(e) => onChange(Number(e.target.value))}
      className="h-7 rounded-md border border-input bg-background px-2 text-[11px] text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      title={
        value === 0
          ? "Auto-zoom disabled — canvas keeps your viewport when the active node changes"
          : `Auto-zoom level when the active node changes (${value}×). Lower = more context visible.`
      }
    >
      {FOCUS_ZOOM_PRESETS.map((p) => (
        <option key={p.value} value={p.value}>
          focus: {p.label}
        </option>
      ))}
    </select>
  );
}


function LockButton({
  checked,
  onChange,
}: {
  checked: boolean;
  onChange: () => void;
}) {
  // Lock icon SVG inline so we don't need another lucide import.
  // Locked = pinned to All tab; unlocked = auto-jumps to the focused
  // run's tab as new runs arrive.
  return (
    <button
      type="button"
      onClick={onChange}
      aria-pressed={checked}
      aria-label={checked ? "Unpin All tab" : "Pin All tab"}
      title={
        checked
          ? "Locked to All tab — every active run lights up simultaneously. Click to unlock."
          : "Auto-jumps to the focused run's tab as new runs arrive. Click to lock the All view."
      }
      className={cn(
        "inline-flex h-7 w-7 items-center justify-center rounded-md border transition-colors",
        checked
          ? "border-emerald-500/40 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25"
          : "border-input text-muted-foreground hover:bg-accent hover:text-foreground",
      )}
    >
      {checked ? (
        // Closed lock
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
          <path d="M7 11V7a5 5 0 0 1 10 0v4"/>
        </svg>
      ) : (
        // Open lock
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect width="18" height="11" x="3" y="11" rx="2" ry="2"/>
          <path d="M7 11V7a5 5 0 0 1 9.9-1"/>
        </svg>
      )}
    </button>
  );
}


function pickGraphName(thread: ThreadSummary, graphs: { name: string; nodes: string[] }[]): string | null {
  const node = thread.current_node;
  if (node) {
    const matches = graphs.filter((g) => g.nodes.includes(node));
    if (matches.length === 1) return matches[0].name;
    if (matches.length > 1) {
      const firstSeg = thread.thread_id.split(":")[0];
      const byName = matches.find((g) => g.name.toLowerCase().includes(firstSeg.toLowerCase()));
      if (byName) return byName.name;
      return matches[0].name;
    }
  }
  const firstSeg = thread.thread_id.split(":")[0].toLowerCase();
  const fuzzy = graphs.find((g) => g.name.toLowerCase().includes(firstSeg));
  return fuzzy?.name ?? null;
}


function GraphTabs({
  graphs,
  selected,
  onSelect,
}: {
  graphs: Array<{ name: string; label: string }>;
  selected: string;
  onSelect: (name: string) => void;
}) {
  return (
    <div className="flex items-center gap-0.5 overflow-x-auto px-2 min-w-0 scrollbar-hidden">
      <button
        type="button"
        onClick={() => onSelect("__all__")}
        className={cn(
          "relative flex items-center gap-2 px-3 py-2 text-xs font-medium transition-colors border-b-2 -mb-px whitespace-nowrap",
          selected === "__all__"
            ? "border-primary text-foreground"
            : "border-transparent text-muted-foreground hover:text-foreground",
        )}
        title="Show every graph as one canvas with cluster backgrounds + cross-graph links"
      >
        <span className="text-[10px] uppercase tracking-wider">All</span>
      </button>
      {graphs.map((g) => (
        <button
          key={g.name}
          type="button"
          onClick={() => onSelect(g.name)}
          className={cn(
            "relative flex items-center gap-2 px-3 py-2 text-xs font-medium transition-colors border-b-2 -mb-px whitespace-nowrap",
            selected === g.name
              ? "border-primary text-foreground"
              : "border-transparent text-muted-foreground hover:text-foreground",
          )}
          title={g.name}
        >
          {g.label}
        </button>
      ))}
    </div>
  );
}
