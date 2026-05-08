/**
 * FlowCanvas — single React Flow canvas rendering every graph in the project
 * as a visual cluster. n8n-style UX: pan, zoom, draggable nodes, mini-map.
 *
 * When a thread is focused, its current node pulses and incoming edges
 * animate, so you can see exactly where the run is at a glance.
 *
 * Node IDs are namespaced by graph (`<graph>__<node>`) so two graphs
 * with a "router" node don't collide. Cross-graph "invokes" relationships
 * are rendered as dashed purple edges between the relevant nodes.
 */

import {
  Background,
  BackgroundVariant,
  Controls,
  Edge,
  Handle,
  MiniMap,
  Node,
  NodeProps,
  Position,
  ReactFlow,
  ReactFlowProvider,
  useEdgesState,
  useNodesState,
  useReactFlow,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";

import dagre from "@dagrejs/dagre";
import { useCallback, useEffect, useMemo, useRef } from "react";

import type { ThreadSummary, TopologyGraph, TopologyResponse } from "@/api";
import { cn } from "@/lib/utils";

interface FlowCanvasProps {
  topology: TopologyResponse;
  focusedThread: ThreadSummary | null;
  onSelectNode: (graphName: string, node: string | null) => void;
  /**
   * Which graph to render. "__all__" shows every graph with cluster
   * backgrounds + cross-graph edges. A specific graph name shows only
   * that graph (no clusters, no cross-edges).
   */
  selectedGraph: string;
  /**
   * Additional threads whose current_node should also light up. Used by
   * the "show all active" toggle in the All view — every running/paused
   * thread's current node pulses simultaneously, not just the focused one.
   * Empty list = single-focus mode.
   */
  additionalActiveThreads: ThreadSummary[];
  /**
   * When the replay controller is scrubbed off live, this overrides the
   * focused thread's `current_node` so the canvas highlights the
   * historical node instead. May be null even when replay is active
   * (e.g. the __input__ checkpoints have no real node).
   */
  replayActiveNode: string | null;
  /**
   * True when replay is in control. Causes the canvas to honor
   * `replayActiveNode` literally — no fallback to the focused thread's
   * live current_node. Without this, replay steps with node=null would
   * incorrectly show the thread's last-seen idle node.
   */
  replayActive: boolean;
  /**
   * How far to zoom when auto-focusing on the active node. Smaller =
   * more surrounding context visible. 1.4 = tight, 1.0 = balanced,
   * 0.7 = wide, 0 = no auto-zoom (just keep the current viewport).
   */
  focusZoom: number;
  /**
   * Ghost overlay — when on, every node that fired in the run is shown
   * with a dim emerald outline + numbered step badge. Keyed by full
   * node id (`<graph>__<node>`); value is the 1-based step number in
   * the merged-run timeline. Numbers are global across graphs so the
   * user can read execution order at a glance even in the All view.
   */
  ghostMode: boolean;
  ghostNodeSteps: Map<string, number>;
  /**
   * Recent-fired-nodes trail behind the focused thread's spotlight.
   * Keyed by `${graphName}__${nodeName}`; value is the trail position
   * (0 = most recent non-current node, N = oldest). Renders as fading
   * amber rings so a burst of fast SSE checkpoints visibly trails the
   * run instead of jumping straight to the latest. Empty when not
   * live-tailing.
   */
  trailNodeIndices: Map<string, number>;
}

// Node data we attach so the custom renderer can style by role.
interface NodeData extends Record<string, unknown> {
  label: string;
  role: NodeRole;
  graphName: string;
  nodeName: string;
  /** True when this node is the focused thread's current node. Driver
   *  for the strong active-node ring. */
  isActive: boolean;
  /** True when this node is the current node of an OTHER live thread
   *  (lock-mode "show all active"). Gets a softer secondary highlight
   *  so the focused vs sister-thread distinction is unmistakable. */
  isSecondaryActive: boolean;
  isFocusedGraph: boolean;
  /** 1-based step number in the run's timeline if this node fired, else null. */
  ghostStep: number | null;
  /** True when ghost mode is active — drives the visual treatment. */
  ghostMode: boolean;
  /** Trail position (0 = most recent non-current, larger = older). null = not in trail. */
  trailIndex: number | null;
}

type NodeRole = "entry" | "exit" | "router" | "gate" | "critic" | "synthesis" | "executor" | "default";

const ROLE_KEYWORDS: Array<[RegExp, NodeRole]> = [
  [/^(__start__|start|entry|begin)$/i, "entry"],
  [/^(__end__|end|done|finish|exit)$/i, "exit"],
  [/(router|_route|gate|decision|dispatch|branch|switch)/i, "router"],
  [/(critic|validate|validator|stress|review|check|compliance)/i, "critic"],
  [/(aggregat|merge|synth|compile|commit|format)/i, "synthesis"],
];

function roleFor(name: string, override?: string | null): NodeRole {
  if (override === "entry" || override === "exit" || override === "router" || override === "gate" ||
      override === "critic" || override === "synthesis" || override === "executor") {
    return override;
  }
  for (const [re, role] of ROLE_KEYWORDS) {
    if (re.test(name)) return role;
  }
  return "default";
}

function pickGraphForThread(thread: ThreadSummary, graphs: TopologyGraph[]): string | null {
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
  return fuzzy ? fuzzy.name : null;
}

function buildLayout(
  topology: TopologyResponse,
  focusedGraphName: string | null,
  focusedNodeId: string | null,
  activeNodeIds: Set<string>,
  selectedGraph: string,
  ghostMode: boolean,
  ghostNodeSteps: Map<string, number>,
  trailNodeIndices: Map<string, number>,
): { nodes: Node<NodeData>[]; edges: Edge[] } {
  const allGraphs = topology.graphs.filter((g) => !(g.error && g.nodes.length === 0));
  if (allGraphs.length === 0) return { nodes: [], edges: [] };

  const isAll = selectedGraph === "__all__";
  const graphs = isAll ? allGraphs : allGraphs.filter((g) => g.name === selectedGraph);
  if (graphs.length === 0) return { nodes: [], edges: [] };

  // Build the dagre graph for layout. nodesep/ranksep tuned to feel like
  // n8n — generous spacing so clusters don't visually fuse together.
  const g = new dagre.graphlib.Graph({ compound: true });
  g.setGraph({ rankdir: "TB", nodesep: 60, ranksep: 80, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(() => ({}));

  const nsId = (graphName: string, nodeName: string) =>
    `${graphName}__${nodeName}`;

  // Per-graph "cluster" nodes
  for (const graph of graphs) {
    g.setNode(graph.name, { label: graph.label || graph.name, clusterLabelPos: "top" });
    for (const node of graph.nodes) {
      g.setNode(nsId(graph.name, node), { width: 180, height: 56 });
      g.setParent(nsId(graph.name, node), graph.name);
    }
    for (const edge of graph.edges) {
      g.setEdge(nsId(graph.name, edge.source), nsId(graph.name, edge.target));
    }
  }

  // Cross-graph "invokes" edges — dotted, drawn after layout so dagre
  // accounts for them. Only meaningful in the "All" view.
  const crossEdges: Array<[string, string, string, string]> = []; // [src_graph, src_node, dst_graph, dst_node]
  if (isAll) {
    for (const graph of graphs) {
      const invokes = graph.invokes || {};
      for (const [sourceNode, targetGraph] of Object.entries(invokes)) {
        const target = graphs.find((gg) => gg.name === targetGraph);
        if (!target) continue;
        const targetAnchor = target.nodes.find((n) => n !== "__start__" && n !== "__end__") ?? target.nodes[0];
        if (!targetAnchor) continue;
        g.setEdge(nsId(graph.name, sourceNode), nsId(target.name, targetAnchor));
        crossEdges.push([graph.name, sourceNode, target.name, targetAnchor]);
      }
    }
  }

  dagre.layout(g);

  // Build React Flow nodes
  const nodes: Node<NodeData>[] = [];
  // Track per-graph bounding boxes so we can render cluster backgrounds
  // in the All view.
  const graphBounds: Record<string, { minX: number; minY: number; maxX: number; maxY: number }> = {};

  for (const graph of graphs) {
    const isFocusedGraph = !isAll || focusedGraphName === graph.name;
    for (const node of graph.nodes) {
      const id = nsId(graph.name, node);
      const pos = g.node(id);
      if (!pos) continue;
      const x = pos.x - pos.width / 2;
      const y = pos.y - pos.height / 2;
      // Track bbox
      const bb = graphBounds[graph.name] ?? { minX: x, minY: y, maxX: x + pos.width, maxY: y + pos.height };
      bb.minX = Math.min(bb.minX, x);
      bb.minY = Math.min(bb.minY, y);
      bb.maxX = Math.max(bb.maxX, x + pos.width);
      bb.maxY = Math.max(bb.maxY, y + pos.height);
      graphBounds[graph.name] = bb;

      const isFocused = focusedNodeId !== null && id === focusedNodeId;
      const isAnyActive = activeNodeIds.has(id);
      nodes.push({
        id,
        position: { x, y },
        type: "roleNode",
        data: {
          label: node,
          role: roleFor(node),
          graphName: graph.name,
          nodeName: node,
          // The focused thread's current node gets the primary "active"
          // treatment; other threads' nodes (lock-mode) become "secondary"
          // — visually present but visibly subordinate to the focused one.
          isActive: isFocused,
          isSecondaryActive: isAnyActive && !isFocused,
          isFocusedGraph,
          ghostStep: ghostNodeSteps.get(id) ?? null,
          ghostMode,
          trailIndex: trailNodeIndices.get(id) ?? null,
        },
        draggable: true,
      });
    }
  }

  // Cluster backgrounds — only in the All view, only when there's more
  // than one graph. Inserted at the front of the array so React Flow
  // renders them behind everything else.
  if (isAll && graphs.length > 1) {
    const clusterPadding = 28;
    const headerHeight = 24;
    const clusterNodes: Node<NodeData>[] = [];
    for (const graph of graphs) {
      const bb = graphBounds[graph.name];
      if (!bb) continue;
      const width = bb.maxX - bb.minX + clusterPadding * 2;
      const height = bb.maxY - bb.minY + clusterPadding * 2 + headerHeight;
      clusterNodes.push({
        id: `cluster-${graph.name}`,
        position: { x: bb.minX - clusterPadding, y: bb.minY - clusterPadding - headerHeight },
        type: "clusterNode",
        data: {
          label: graph.label || graph.name,
          role: "default",
          graphName: graph.name,
          nodeName: "",
          isActive: false,
          isSecondaryActive: false,
          isFocusedGraph: !!focusedGraphName && focusedGraphName === graph.name,
          ghostStep: null,
          ghostMode: false,
          trailIndex: null,
        },
        style: { width, height, zIndex: -1 },
        selectable: false,
        draggable: false,
      });
    }
    nodes.unshift(...clusterNodes);
  }

  // Build React Flow edges
  const edges: Edge[] = [];
  for (const graph of graphs) {
    for (const edge of graph.edges) {
      const sourceId = nsId(graph.name, edge.source);
      const targetId = nsId(graph.name, edge.target);
      // Animate any edge whose target is currently active. In multi-active
      // mode this lights up multiple in-flight transitions at once.
      const isActiveEdge = activeNodeIds.has(targetId);
      edges.push({
        id: `${sourceId}-${targetId}`,
        source: sourceId,
        target: targetId,
        type: "smoothstep",
        animated: isActiveEdge,
        style: {
          stroke: isActiveEdge ? "#10b981" : "#4a5568",
          strokeWidth: isActiveEdge ? 2.5 : 1.5,
        },
      });
    }
  }
  for (const [srcGraph, srcNode, dstGraph, dstNode] of crossEdges) {
    edges.push({
      id: `cross-${srcGraph}.${srcNode}->${dstGraph}.${dstNode}`,
      source: nsId(srcGraph, srcNode),
      target: nsId(dstGraph, dstNode),
      type: "smoothstep",
      animated: false,
      style: { stroke: "#9f7aea", strokeWidth: 2, strokeDasharray: "6 4" },
      label: "invokes",
      labelStyle: { fontSize: 10, fill: "#a78bfa" },
    });
  }

  return { nodes, edges };
}

// Custom node component — role drives shape + color.
function RoleNode({ data }: NodeProps) {
  const d = data as NodeData;
  const styleByRole: Record<NodeRole, string> = {
    entry: "bg-amber-400 border-amber-600 text-amber-950 rounded-full px-4",
    exit: "bg-emerald-500 border-emerald-700 text-emerald-50 rounded-full px-4",
    router: "bg-violet-500 border-violet-700 text-violet-50 rounded-md rotate-45",
    gate: "bg-orange-500 border-orange-700 text-orange-50 rounded-md",
    critic: "bg-rose-500 border-rose-700 text-rose-50 rounded-md",
    synthesis: "bg-emerald-600 border-emerald-800 text-emerald-50 rounded-md",
    executor: "bg-zinc-700 border-zinc-500 text-zinc-100 rounded-md",
    default: "bg-zinc-700 border-zinc-500 text-zinc-100 rounded-md",
  };

  // Ghost overlay layers over the base styling. Fired nodes get a soft
  // emerald outline; un-fired nodes are dimmed so the run's path stands
  // out. The active-node ring still wins visually because it pulses.
  const ghostFired = d.ghostMode && d.ghostStep != null;
  const ghostUnfired = d.ghostMode && d.ghostStep == null;

  // Trail rings — amber, fading by trail index. Class strings must be
  // statically present in the source so Tailwind's JIT picks them up;
  // mapping by index avoids dynamic class construction.
  // index 0 = newest non-current node; trails out to TRAIL_LENGTH-1 = 4.
  const TRAIL_RING_BY_INDEX: Record<number, string> = {
    0: "ring-2 ring-amber-300/80 ring-offset-1 ring-offset-background",
    1: "ring-2 ring-amber-400/60 ring-offset-1 ring-offset-background",
    2: "ring-2 ring-amber-500/45 ring-offset-1 ring-offset-background",
    3: "ring-2 ring-amber-600/30 ring-offset-1 ring-offset-background",
    4: "ring-2 ring-amber-700/20 ring-offset-1 ring-offset-background",
  };
  const trailRingClass =
    d.trailIndex != null && !d.isActive && !d.isSecondaryActive
      ? TRAIL_RING_BY_INDEX[d.trailIndex] ?? null
      : null;

  return (
    <div
      className={cn(
        "relative border-2 px-3 py-2 text-xs font-medium shadow-md min-w-[140px] text-center transition-all",
        styleByRole[d.role],
        !d.isFocusedGraph && "opacity-50",
        ghostUnfired && "opacity-30",
        ghostFired && !d.isActive && !d.isSecondaryActive && !trailRingClass &&
          "ring-2 ring-emerald-400/70 ring-offset-1 ring-offset-background",
        // Trail ring — only when no stronger highlight applies. Wins
        // over the static ghost ring during live tail because the trail
        // is the eye candy that says "the run just came through here".
        trailRingClass,
        // Focused thread's lit node — full emerald pulse, the eye-magnet
        d.isActive &&
          "ring-4 ring-emerald-300 ring-offset-2 ring-offset-background animate-pulse",
        // Sister thread's lit node — sky-blue static ring, distinct
        // chroma so users can tell focused vs other-active at a glance
        d.isSecondaryActive &&
          "ring-2 ring-sky-400/80 ring-offset-1 ring-offset-background",
      )}
      title={
        d.isActive
          ? `${d.graphName}.${d.nodeName} · focused thread is here${d.ghostStep != null ? ` · step ${d.ghostStep}` : ""}`
          : d.isSecondaryActive
            ? `${d.graphName}.${d.nodeName} · sister thread is here${d.ghostStep != null ? ` · step ${d.ghostStep}` : ""}`
            : `${d.graphName}.${d.nodeName}${d.ghostStep != null ? ` · step ${d.ghostStep}` : ""}`
      }
    >
      <Handle type="target" position={Position.Top} className="!bg-zinc-400" />
      <span className={cn(d.role === "router" && "block -rotate-45")}>{d.label}</span>
      <Handle type="source" position={Position.Bottom} className="!bg-zinc-400" />
      {ghostFired ? (
        <span
          className={cn(
            "absolute -top-2 -left-2 z-10 inline-flex h-5 min-w-[1.25rem] items-center justify-center rounded-full border-2 border-background bg-emerald-500 px-1 text-[10px] font-bold text-emerald-950 shadow",
            d.role === "router" && "-rotate-45",
          )}
        >
          {d.ghostStep}
        </span>
      ) : null}
    </div>
  );
}

function ClusterNode({ data }: NodeProps) {
  const d = data as NodeData;
  return (
    <div
      className={cn(
        "h-full w-full rounded-lg border border-dashed pointer-events-none",
        d.isFocusedGraph
          ? "border-emerald-500/60 bg-emerald-500/5"
          : "border-zinc-600/40 bg-zinc-900/30",
      )}
    >
      <div className="px-3 py-1 text-[11px] font-mono uppercase tracking-wider text-zinc-400">
        {d.label}
      </div>
    </div>
  );
}

const nodeTypes = { roleNode: RoleNode, clusterNode: ClusterNode };

export function FlowCanvas({
  topology,
  focusedThread,
  onSelectNode,
  selectedGraph,
  additionalActiveThreads,
  replayActiveNode,
  replayActive,
  focusZoom,
  ghostMode,
  ghostNodeSteps,
  trailNodeIndices,
}: FlowCanvasProps) {
  // The "effective" node to focus on. When replay is in control, use
  // its node literally (may be null on input/start checkpoints — that's
  // correct, no node was active at that step). Otherwise follow the
  // focused thread's live current_node.
  const focusedNode = replayActive
    ? replayActiveNode
    : (replayActiveNode ?? focusedThread?.current_node ?? null);

  // Pick the graph that contains the effective focused node. Falls back
  // to the live thread's graph when no node is set.
  const focusedGraphName = useMemo(() => {
    if (focusedNode) {
      const synthetic: ThreadSummary = focusedThread
        ? { ...focusedThread, current_node: focusedNode }
        : ({
            thread_id: "",
            latest_checkpoint_id: "",
            last_updated: null,
            step: null,
            status: "idle",
            current_node: focusedNode,
            recent_nodes: [],
            agent_profile: null,
            phase: null,
            scope_kind: "thread",
            scope_id: "",
            stage: "thread",
            stage_detail: "",
          } as ThreadSummary);
      return pickGraphForThread(synthetic, topology.graphs);
    }
    return focusedThread ? pickGraphForThread(focusedThread, topology.graphs) : null;
  }, [topology, focusedThread, focusedNode]);

  const activeNodeIds = useMemo(() => {
    const set = new Set<string>();
    if (focusedNode && focusedGraphName) {
      set.add(`${focusedGraphName}__${focusedNode}`);
    }
    for (const t of additionalActiveThreads) {
      if (!t.current_node) continue;
      const g = pickGraphForThread(t, topology.graphs);
      if (g) set.add(`${g}__${t.current_node}`);
    }
    return set;
  }, [focusedNode, focusedGraphName, additionalActiveThreads, topology]);

  // Auto-pan target. Always the FOCUSED thread's current node when one
  // is set — even if other sister/active threads also pulse on the
  // canvas (lock-mode "show all active"). Without this, lock-mode
  // bailed out of panning entirely because there were multiple lit
  // nodes; the user lost camera-follow as their focused run progressed.
  const panTargetNodeId = useMemo(
    () => (focusedNode && focusedGraphName ? `${focusedGraphName}__${focusedNode}` : null),
    [focusedNode, focusedGraphName],
  );

  const { nodes: initialNodes, edges: initialEdges } = useMemo(
    () => buildLayout(topology, focusedGraphName, panTargetNodeId, activeNodeIds, selectedGraph, ghostMode, ghostNodeSteps, trailNodeIndices),
    [topology, focusedGraphName, panTargetNodeId, activeNodeIds, selectedGraph, ghostMode, ghostNodeSteps, trailNodeIndices],
  );

  const [nodes, setNodes, onNodesChange] = useNodesState(initialNodes);
  const [edges, setEdges, onEdgesChange] = useEdgesState(initialEdges);
  const lastLayoutSig = useRef<string>("");

  // Re-lay out when the topology STRUCTURE or selected tab changes. For
  // polling-only updates that just change active/focused state, patch the
  // existing nodes in place to preserve user-driven drag positions.
  useEffect(() => {
    const sig = JSON.stringify([
      selectedGraph,
      topology.graphs.map((g) => [g.name, g.nodes.length, g.edges.length, g.invokes]),
    ]);
    if (sig !== lastLayoutSig.current) {
      lastLayoutSig.current = sig;
      setNodes(initialNodes);
      setEdges(initialEdges);
      return;
    }
    setNodes((curr) =>
      curr.map((n) => {
        const fresh = initialNodes.find((nn) => nn.id === n.id);
        if (!fresh) return n;
        return { ...n, data: { ...n.data, ...fresh.data } };
      }),
    );
    setEdges(initialEdges);
  }, [initialNodes, initialEdges, topology, selectedGraph, setNodes, setEdges]);

  const handleNodeClick = useCallback(
    (_e: React.MouseEvent, node: Node) => {
      const data = node.data as NodeData;
      if (!data.nodeName) return; // cluster node
      onSelectNode(data.graphName, data.nodeName);
    },
    [onSelectNode],
  );

  return (
    <div className="h-full w-full chimera-flow">
      <ReactFlowProvider>
        <FlowInner
          nodes={nodes}
          edges={edges}
          onNodesChange={onNodesChange}
          onEdgesChange={onEdgesChange}
          onNodeClick={handleNodeClick}
          selectedGraph={selectedGraph}
          activeNodeId={panTargetNodeId}
          focusZoom={focusZoom}
          ghostMode={ghostMode}
        />
      </ReactFlowProvider>
    </div>
  );
}


/**
 * Inner component that has access to useReactFlow. Calls fitView whenever
 * the active tab changes or the layout structure changes — keeps "the
 * whole diagram visible" as the default behavior so users never land on
 * a half-zoomed view.
 */
function FlowInner({
  nodes,
  edges,
  onNodesChange,
  onEdgesChange,
  onNodeClick,
  selectedGraph,
  activeNodeId,
  focusZoom,
  ghostMode,
}: {
  nodes: Node<NodeData>[];
  edges: Edge[];
  onNodesChange: (changes: any) => void;
  onEdgesChange: (changes: any) => void;
  onNodeClick: (e: React.MouseEvent, node: Node) => void;
  selectedGraph: string;
  activeNodeId: string | null;
  focusZoom: number;
  ghostMode: boolean;
}) {
  const { fitView, setCenter } = useReactFlow();
  const wrapperRef = useRef<HTMLDivElement | null>(null);

  // Re-fit on container resize. React Flow captures viewport dimensions
  // on mount; without this, resizing the browser leaves the diagram
  // anchored at its initial size and the user has to manually re-fit.
  // Debounced via rAF so a continuous resize drag doesn't thrash.
  useEffect(() => {
    const node = wrapperRef.current;
    if (!node || nodes.length === 0) return;
    let frame: number | null = null;
    const observer = new ResizeObserver(() => {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => {
        fitView({ padding: 0.2, duration: 200 });
      });
    });
    observer.observe(node);
    return () => {
      observer.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [fitView, nodes.length]);

  // Re-fit whenever the tab changes or the node-count signature changes.
  // rAF lets React Flow measure its viewport first.
  const nodeCount = nodes.length;
  useEffect(() => {
    if (nodeCount === 0) return;
    const id = requestAnimationFrame(() => {
      fitView({ padding: 0.2, duration: 300 });
    });
    return () => cancelAnimationFrame(id);
  }, [selectedGraph, nodeCount, fitView]);

  // When the active node changes, pan + zoom into it. Ghost mode no
  // longer suppresses this — clicking a step in RunStepsCard should
  // take the user to that node, and continuous play should follow the
  // lit node through the run. focusZoom = 0 still opts out entirely.
  useEffect(() => {
    if (!activeNodeId || focusZoom === 0) return;
    const target = nodes.find((n) => n.id === activeNodeId);
    if (!target) return;
    const id = requestAnimationFrame(() => {
      const cx = target.position.x + 90;
      const cy = target.position.y + 28;
      setCenter(cx, cy, { zoom: focusZoom, duration: 600 });
    });
    return () => cancelAnimationFrame(id);
  }, [activeNodeId, nodes, setCenter, focusZoom]);

  // When ghost mode FIRST flips on (and we're not actively scrubbing),
  // fitView once so the user sees the whole numbered map. Subsequent
  // step interactions take over via the auto-pan above.
  const ghostJustActivatedRef = useRef(ghostMode);
  useEffect(() => {
    if (!ghostMode || nodes.length === 0) return;
    if (ghostJustActivatedRef.current) return; // already fit
    ghostJustActivatedRef.current = true;
    const id = requestAnimationFrame(() => {
      fitView({ padding: 0.2, duration: 400 });
    });
    return () => cancelAnimationFrame(id);
  }, [ghostMode, fitView, nodes.length]);
  useEffect(() => {
    if (!ghostMode) ghostJustActivatedRef.current = false;
  }, [ghostMode]);

  return (
    <div ref={wrapperRef} className="h-full w-full">
    <ReactFlow
      nodes={nodes}
      edges={edges}
      onNodesChange={onNodesChange}
      onEdgesChange={onEdgesChange}
      onNodeClick={onNodeClick}
      nodeTypes={nodeTypes}
      fitView
      fitViewOptions={{ padding: 0.2 }}
      minZoom={0.1}
      maxZoom={2}
      proOptions={{ hideAttribution: true }}
    >
      <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#2d3748" />
      <Controls />
      <MiniMap
        nodeColor={(n) => {
          const role = (n.data as NodeData).role;
          return ({
            entry: "#fbbf24",
            exit: "#10b981",
            router: "#8b5cf6",
            gate: "#f97316",
            critic: "#f43f5e",
            synthesis: "#059669",
            executor: "#52525b",
            default: "#52525b",
          } as Record<NodeRole, string>)[role];
        }}
        maskColor="rgba(0, 0, 0, 0.6)"
        pannable
        zoomable
      />
    </ReactFlow>
    </div>
  );
}
