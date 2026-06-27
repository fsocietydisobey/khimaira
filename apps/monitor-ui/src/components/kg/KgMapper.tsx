/**
 * KgMapper — interactive React Flow canvas for the generic graph contract.
 *
 * Code-agnostic, like FlowCanvas (LangGraph mapper): renders ANY attached
 * project's graph via the generic {nodes,edges} contract. Same stack —
 * React Flow (@xyflow/react) + @dagrejs/dagre + ReactFlowProvider.
 *
 * Key points:
 * - Nodes/edges typed by an OPAQUE `type` string (no fixed enum); color is
 *   derived deterministically per type via graphStyle.typeColor.
 * - Edge `weight` (0–1, optional) modulates opacity/width.
 * - Node click → KgNodeInspector (fact side panel).
 * - Scope selector in the toolbar (e.g. shop:<id>); live data is served by the
 *   khimaira daemon at /api/graph/<project>?scope=… (wired at P3).
 */

import {
  Background,
  BackgroundVariant,
  Controls,
  Edge,
  EdgeChange,
  Handle,
  MiniMap,
  Node,
  NodeChange,
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
import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { cn } from "@/lib/utils";
import { typeColor } from "./graphStyle";
import { KgNodeInspector } from "./KgNodeInspector";
import {
  GRAPH_URL,
  MOCK_GRAPH,
  MOCK_MODE,
  type GraphEdge,
  type GraphNode,
  type GraphResponse,
} from "./kgTypes";

// ---------------------------------------------------------------------------
// Node data shape attached to React Flow nodes
// ---------------------------------------------------------------------------

interface KgNodeData extends Record<string, unknown> {
  label: string;
  nodeType: string;
  badge?: string | number;
  isSelected: boolean;
}

// ---------------------------------------------------------------------------
// Dagre layout
// ---------------------------------------------------------------------------

function buildKgLayout(
  nodes: GraphNode[],
  edges: GraphEdge[],
  selectedNodeId: string | null,
): { rfNodes: Node<KgNodeData>[]; rfEdges: Edge[] } {
  if (nodes.length === 0) return { rfNodes: [], rfEdges: [] };

  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: "TB", nodesep: 60, ranksep: 80, marginx: 40, marginy: 40 });
  g.setDefaultEdgeLabel(() => ({}));

  for (const n of nodes) {
    g.setNode(n.id, { width: 180, height: 52 });
  }

  // Only add edges between nodes that exist in the set (avoid dagre crash
  // on dangling references when the deliverable scope trims the node list).
  const nodeIdSet = new Set(nodes.map((n) => n.id));
  for (const e of edges) {
    if (nodeIdSet.has(e.from) && nodeIdSet.has(e.to)) {
      g.setEdge(e.from, e.to);
    }
  }

  dagre.layout(g);

  const rfNodes: Node<KgNodeData>[] = nodes.map((n) => {
    const pos = g.node(n.id);
    return {
      id: n.id,
      position: { x: pos.x - pos.width / 2, y: pos.y - pos.height / 2 },
      type: "kgNode",
      data: {
        label: n.label,
        nodeType: n.type,
        badge: n.badge,
        isSelected: n.id === selectedNodeId,
      },
      draggable: true,
    };
  });

  const rfEdges: Edge[] = edges
    .filter((e) => nodeIdSet.has(e.from) && nodeIdSet.has(e.to))
    .map((e) => {
      const color = typeColor(e.type);
      // Weight (default 1) modulates opacity (0.4–1.0) and stroke width (1–2.5).
      const weight = e.weight ?? 1;
      const opacity = 0.4 + weight * 0.6;
      const strokeWidth = 1 + weight * 1.5;
      return {
        id: `${e.from}->${e.to}:${e.type}`,
        source: e.from,
        target: e.to,
        type: "smoothstep",
        animated: false,
        label: e.type,
        labelStyle: { fontSize: 9, fill: color, opacity },
        style: { stroke: color, strokeWidth, opacity },
      };
    });

  return { rfNodes, rfEdges };
}

// ---------------------------------------------------------------------------
// Custom node component
// ---------------------------------------------------------------------------

function KgNodeComponent({ data }: NodeProps) {
  const d = data as KgNodeData;
  const color = typeColor(d.nodeType);
  const hasBadge = d.badge !== undefined && d.badge !== "" && d.badge !== 0;
  return (
    <div
      className={cn(
        "relative rounded-md border-2 px-3 py-2 text-xs font-medium text-white shadow-md min-w-[150px] text-center transition-all select-none",
        d.isSelected && "ring-4 ring-white/70 ring-offset-2 ring-offset-background",
      )}
      style={{ backgroundColor: color, borderColor: color }}
      title={`${d.nodeType}${hasBadge ? ` · ${d.badge}` : ""}`}
    >
      <Handle type="target" position={Position.Top} className="!bg-zinc-300/60" />

      {/* optional badge chip */}
      {hasBadge ? (
        <span className="absolute -top-2 -right-2 z-10 inline-flex h-4 min-w-[1rem] items-center justify-center rounded-full bg-zinc-900/80 border border-zinc-600 text-[9px] font-bold text-zinc-200 px-1">
          {d.badge}
        </span>
      ) : null}

      <span className="block truncate max-w-[160px]" title={d.label}>
        {d.label}
      </span>
      <span className="block text-[9px] opacity-70 mt-0.5 uppercase tracking-wider">
        {d.nodeType}
      </span>

      <Handle type="source" position={Position.Bottom} className="!bg-zinc-300/60" />
    </div>
  );
}

const KG_NODE_TYPES = { kgNode: KgNodeComponent };

// ---------------------------------------------------------------------------
// Inner canvas (needs useReactFlow — must be inside ReactFlowProvider)
// ---------------------------------------------------------------------------

function KgFlowInner({
  rfNodes,
  rfEdges,
  onNodesChange,
  onEdgesChange,
  onNodeClick,
}: {
  rfNodes: Node<KgNodeData>[];
  rfEdges: Edge[];
  onNodesChange: (changes: NodeChange<Node<KgNodeData>>[]) => void;
  onEdgesChange: (changes: EdgeChange<Edge>[]) => void;
  onNodeClick: (e: React.MouseEvent, node: Node) => void;
}) {
  const { fitView } = useReactFlow();
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const nodeCount = rfNodes.length;

  // Fit view on resize
  useEffect(() => {
    const el = wrapperRef.current;
    if (!el || nodeCount === 0) return;
    let frame: number | null = null;
    const obs = new ResizeObserver(() => {
      if (frame !== null) cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() => fitView({ padding: 0.15, duration: 200 }));
    });
    obs.observe(el);
    return () => {
      obs.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [fitView, nodeCount]);

  // Fit view when node set changes
  useEffect(() => {
    if (nodeCount === 0) return;
    const id = requestAnimationFrame(() => fitView({ padding: 0.15, duration: 300 }));
    return () => cancelAnimationFrame(id);
  }, [nodeCount, fitView]);

  return (
    <div ref={wrapperRef} className="h-full w-full">
      <ReactFlow
        nodes={rfNodes}
        edges={rfEdges}
        onNodesChange={onNodesChange}
        onEdgesChange={onEdgesChange}
        onNodeClick={onNodeClick}
        nodeTypes={KG_NODE_TYPES}
        fitView
        fitViewOptions={{ padding: 0.15 }}
        minZoom={0.05}
        maxZoom={2.5}
        proOptions={{ hideAttribution: true }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#2d3748" />
        <Controls />
        <MiniMap
          nodeColor={(n) => typeColor((n.data as KgNodeData).nodeType)}
          maskColor="rgba(0,0,0,0.6)"
          pannable
          zoomable
        />
      </ReactFlow>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Legend
// ---------------------------------------------------------------------------

function KgLegend({
  types,
  visible,
  onToggle,
}: {
  types: string[];
  visible: boolean;
  onToggle: () => void;
}) {
  return (
    <div className="absolute bottom-4 left-4 z-10">
      <button
        type="button"
        onClick={onToggle}
        className="mb-1 px-2 py-1 text-[10px] rounded-md border border-border bg-card/80 text-muted-foreground hover:text-foreground transition-colors"
      >
        {visible ? "hide legend" : "show legend"}
      </button>
      {visible && types.length > 0 ? (
        <div className="rounded-md border border-border bg-card/90 p-2 text-[10px] space-y-0.5 max-h-64 overflow-auto shadow-lg">
          <p className="uppercase tracking-wider text-muted-foreground mb-1">node types</p>
          {types.map((t) => (
            <div key={t} className="flex items-center gap-1.5">
              <span
                className="inline-block h-2.5 w-2.5 rounded-sm border"
                style={{ backgroundColor: typeColor(t) }}
              />
              <span className="font-mono">{t}</span>
            </div>
          ))}
        </div>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Data fetch hook
// ---------------------------------------------------------------------------

type FetchState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; graph: GraphResponse["data"] };

function useKgGraph(scope: string): FetchState {
  const [state, setState] = useState<FetchState>({ status: "idle" });

  useEffect(() => {
    if (!scope) {
      setState({ status: "idle" });
      return;
    }

    setState({ status: "loading" });
    let cancelled = false;

    if (MOCK_MODE) {
      const id = setTimeout(() => {
        if (!cancelled) setState({ status: "ok", graph: MOCK_GRAPH.data });
      }, 300);
      return () => {
        cancelled = true;
        clearTimeout(id);
      };
    }

    // P3 wiring: same-origin daemon endpoint. The `/<project>` segment is
    // prepended once this view is mounted with a project from routing.
    const url = `${GRAPH_URL}?scope=${encodeURIComponent(scope)}`;
    fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((json: GraphResponse) => {
        if (!cancelled) setState({ status: "ok", graph: json.data });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: String(err) });
      });

    return () => {
      cancelled = true;
    };
  }, [scope]);

  return state;
}

// ---------------------------------------------------------------------------
// Main view — KgMapper
// ---------------------------------------------------------------------------

interface SelectedKgNode {
  nodeId: string;
  type: string;
  label: string;
  badge?: string | number;
}

export function KgMapper() {
  // In mock mode the scope is pre-filled so the canvas renders immediately
  // without requiring user input.
  const [scope, setScope] = useState<string>(MOCK_MODE ? "shop:mock" : "");
  const [inputValue, setInputValue] = useState<string>(MOCK_MODE ? "shop:mock" : "");
  const [selectedNode, setSelectedNode] = useState<SelectedKgNode | null>(null);
  const [legendVisible, setLegendVisible] = useState<boolean>(false);

  const fetchState = useKgGraph(scope);

  const { nodes: rawNodes, edges: rawEdges } = useMemo(() => {
    if (fetchState.status !== "ok") return { nodes: [] as GraphNode[], edges: [] as GraphEdge[] };
    return { nodes: fetchState.graph.nodes, edges: fetchState.graph.edges };
  }, [fetchState]);

  // Distinct node types present in the data → drives the legend (no fixed enum).
  const presentTypes = useMemo(
    () => Array.from(new Set(rawNodes.map((n) => n.type))).sort(),
    [rawNodes],
  );

  const { rfNodes: initialRfNodes, rfEdges: initialRfEdges } = useMemo(
    () => buildKgLayout(rawNodes, rawEdges, selectedNode?.nodeId ?? null),
    [rawNodes, rawEdges, selectedNode?.nodeId],
  );

  const [rfNodes, setRfNodes, onNodesChange] = useNodesState(initialRfNodes);
  const [rfEdges, setRfEdges, onEdgesChange] = useEdgesState(initialRfEdges);

  // Re-layout when graph data changes; patch selection-state in place
  // to preserve user-dragged positions when only the selected node flips.
  const lastLayoutSig = useRef<string>("");
  useEffect(() => {
    const sig = `${rawNodes.length}:${rawEdges.length}`;
    if (sig !== lastLayoutSig.current) {
      lastLayoutSig.current = sig;
      setRfNodes(initialRfNodes);
      setRfEdges(initialRfEdges);
      return;
    }
    // Only selection changed — patch isSelected without re-laying out.
    setRfNodes((curr) =>
      curr.map((n) => {
        const fresh = initialRfNodes.find((nn) => nn.id === n.id);
        if (!fresh) return n;
        return { ...n, data: { ...n.data, isSelected: fresh.data.isSelected } };
      }),
    );
  }, [initialRfNodes, initialRfEdges, rawNodes.length, rawEdges.length, setRfNodes, setRfEdges]);

  const handleNodeClick = useCallback(
    (_e: React.MouseEvent, node: Node) => {
      const d = node.data as KgNodeData;
      setSelectedNode({
        nodeId: node.id,
        type: d.nodeType,
        label: d.label,
        badge: d.badge,
      });
    },
    [],
  );

  const handleSubmitScope = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      setScope(inputValue.trim());
      setSelectedNode(null);
    },
    [inputValue],
  );

  return (
    <div className="flex h-full flex-col">
      {/* Toolbar */}
      <div className="flex shrink-0 items-center gap-3 border-b border-border bg-card/40 px-4 py-2">
        <h2 className="text-sm font-semibold text-foreground">KG Mapper</h2>

        <form onSubmit={handleSubmitScope} className="flex items-center gap-2">
          <label htmlFor="kg-scope" className="text-[11px] text-muted-foreground whitespace-nowrap">
            scope
          </label>
          <input
            id="kg-scope"
            type="text"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            placeholder="e.g. shop:10"
            className="h-7 w-72 rounded-md border border-input bg-background px-2 text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring font-mono"
          />
          <button
            type="submit"
            className="h-7 rounded-md border border-input bg-background px-3 text-[11px] text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
          >
            load
          </button>
        </form>

        {fetchState.status === "loading" ? (
          <span className="text-[11px] text-muted-foreground animate-pulse">loading graph…</span>
        ) : null}
        {fetchState.status === "ok" ? (
          <span className="text-[11px] text-muted-foreground">
            {rawNodes.length} nodes · {rawEdges.length} edges
            {MOCK_MODE ? " (mock)" : ""}
          </span>
        ) : null}
        {fetchState.status === "error" ? (
          <span className="text-[11px] text-destructive">{fetchState.message}</span>
        ) : null}
      </div>

      {/* Body */}
      <div className="flex flex-1 overflow-hidden">
        {/* Canvas */}
        <div className="relative flex-1 overflow-hidden">
          {fetchState.status === "idle" ? (
            <div className="flex h-full items-center justify-center">
              <p className="text-sm text-muted-foreground">
                Enter a scope and click load to render the graph.
              </p>
            </div>
          ) : fetchState.status === "ok" && rawNodes.length === 0 ? (
            <div className="flex h-full items-center justify-center">
              <p className="text-sm text-muted-foreground">No nodes found for this scope.</p>
            </div>
          ) : (
            <div className="h-full w-full khimaira-flow">
              <ReactFlowProvider>
                <KgFlowInner
                  rfNodes={rfNodes}
                  rfEdges={rfEdges}
                  onNodesChange={onNodesChange}
                  onEdgesChange={onEdgesChange}
                  onNodeClick={handleNodeClick}
                />
              </ReactFlowProvider>
            </div>
          )}

          <KgLegend
            types={presentTypes}
            visible={legendVisible}
            onToggle={() => setLegendVisible((v) => !v)}
          />
        </div>

        {/* Node-detail panel */}
        {selectedNode ? (
          <div className="w-96 shrink-0">
            <KgNodeInspector
              nodeId={selectedNode.nodeId}
              type={selectedNode.type}
              label={selectedNode.label}
              badge={selectedNode.badge}
              onClose={() => setSelectedNode(null)}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}
