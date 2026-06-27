/**
 * KgMapper — code-agnostic knowledge-graph viewer.
 *
 * Renders ANY khimaira-attached project's graph via the generic {nodes,edges}
 * contract served by the khimaira daemon at /api/graph/<project>?scope=… (the
 * daemon proxies to that project's KG adapter). The view never knows the
 * project's schema — node/edge `type` are opaque strings, colored by a hash.
 *
 * Rendering: sigma.js (WebGL) over a graphology graph, laid out once with
 * ForceAtlas2 (synchronous — positions are computed a single time, not a
 * per-frame force sim). WebGL handles thousands of nodes smoothly (shop 10 ≈
 * 5.7k), which is why sigma is used instead of a canvas force-graph for graphs
 * at this scale.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams, useSearchParams } from "react-router-dom";
import Graph from "graphology";
import Sigma from "sigma";
import forceAtlas2 from "graphology-layout-forceatlas2";

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
// Graph data shape (mapped from the generic contract)
// ---------------------------------------------------------------------------

interface MapNode {
  id: string;
  nodeType: string;
  label: string;
  badge?: string | number;
}
interface MapLink {
  source: string;
  target: string;
  linkType: string;
  weight: number;
}
interface MapData {
  nodes: MapNode[];
  links: MapLink[];
}

interface ClickedNode {
  nodeId: string;
  type: string;
  label: string;
  badge?: string | number;
}

// ---------------------------------------------------------------------------
// Legend (distinct node types present in the data — no fixed enum)
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
                className="inline-block h-2.5 w-2.5 rounded-full"
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
// Data fetch hook — daemon proxy /api/graph/<project>?scope=…
// ---------------------------------------------------------------------------

type FetchState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; graph: GraphResponse["data"] };

function useKgGraph(scope: string, project: string | undefined): FetchState {
  const [state, setState] = useState<FetchState>({ status: "idle" });

  useEffect(() => {
    if (!scope || !project) {
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

    // Same-origin daemon proxy /api/graph/<project>?scope=… (vite /api → daemon).
    const url = `${GRAPH_URL}/${encodeURIComponent(project)}?scope=${encodeURIComponent(scope)}`;
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
  }, [scope, project]);

  return state;
}

// ---------------------------------------------------------------------------
// Sigma (WebGL) canvas — graphology graph, ForceAtlas2 layout, WebGL render
// ---------------------------------------------------------------------------

function SigmaCanvas({
  data,
  selectedId,
  onNodeClick,
}: {
  data: MapData;
  selectedId: string | null;
  onNodeClick: (n: ClickedNode) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  // keep the latest values for use inside sigma callbacks without rebuilding
  const selectedRef = useRef<string | null>(selectedId);
  selectedRef.current = selectedId;
  const clickRef = useRef(onNodeClick);
  clickRef.current = onNodeClick;

  // Build graph + layout + render whenever the data changes.
  useEffect(() => {
    const container = containerRef.current;
    if (!container) return;

    const graph = new Graph({ multi: false, type: "directed" });

    // Degree → node size (hubs render larger).
    const degree = new Map<string, number>();
    for (const l of data.links) {
      degree.set(l.source, (degree.get(l.source) ?? 0) + 1);
      degree.set(l.target, (degree.get(l.target) ?? 0) + 1);
    }

    const n = Math.max(data.nodes.length, 1);
    data.nodes.forEach((node, i) => {
      // Defensive dedup — the adapter's keyset pagination can occasionally
      // surface a duplicate id; graphology throws on a repeat addNode.
      if (graph.hasNode(node.id)) return;
      const deg = degree.get(node.id) ?? 0;
      const angle = (2 * Math.PI * i) / n;
      graph.addNode(node.id, {
        label: node.label,
        nodeType: node.nodeType,
        badge: node.badge,
        color: typeColor(node.nodeType),
        size: Math.min(2 + Math.sqrt(deg) * 1.1, 16),
        x: Math.cos(angle) + (Math.random() - 0.5) * 0.01,
        y: Math.sin(angle) + (Math.random() - 0.5) * 0.01,
      });
    });

    data.links.forEach((l) => {
      if (
        graph.hasNode(l.source) &&
        graph.hasNode(l.target) &&
        !graph.hasEdge(l.source, l.target)
      ) {
        graph.addEdge(l.source, l.target, {
          color: typeColor(l.linkType) + "44",
          size: 0.4 + (l.weight ?? 1) * 0.6,
        });
      }
    });

    // ForceAtlas2 — synchronous, computed ONCE. Scale iterations down for big
    // graphs (barnes-hut keeps it fast); this is not a per-frame simulation.
    const iterations = n > 3000 ? 180 : n > 800 ? 260 : 400;
    const settings = forceAtlas2.inferSettings(graph);
    forceAtlas2.assign(graph, {
      iterations,
      settings: {
        ...settings,
        barnesHutOptimize: n > 800,
        gravity: 0.6,
        scalingRatio: 12,
        slowDown: 1 + Math.log(n),
      },
    });

    const renderer = new Sigma(graph, container, {
      renderLabels: true,
      labelColor: { color: "#cbd5e1" },
      labelDensity: 0.6,
      labelGridCellSize: 70,
      // Only label nodes whose rendered size clears the threshold — declutters
      // the big graph (hubs are labeled; the leaf tail is not until you zoom).
      labelRenderedSizeThreshold: 9,
      defaultEdgeColor: "#33415544",
      minCameraRatio: 0.02,
      maxCameraRatio: 12,
      zIndex: true,
    });

    // Highlight the selected node via a reducer (re-applied on refresh()).
    renderer.setSetting("nodeReducer", (node, attrs) => {
      if (selectedRef.current && node === selectedRef.current) {
        return {
          ...attrs,
          color: "#ffffff",
          size: (attrs.size ?? 4) * 1.6,
          zIndex: 2,
          highlighted: true,
        };
      }
      return attrs;
    });

    renderer.on("clickNode", ({ node }) => {
      const a = graph.getNodeAttributes(node);
      clickRef.current({
        nodeId: node,
        type: a.nodeType as string,
        label: a.label as string,
        badge: a.badge as string | number | undefined,
      });
    });

    sigmaRef.current = renderer;
    return () => {
      renderer.kill();
      sigmaRef.current = null;
    };
  }, [data]);

  // Re-apply the selection highlight without rebuilding the graph.
  useEffect(() => {
    sigmaRef.current?.refresh();
  }, [selectedId]);

  return <div ref={containerRef} className="h-full w-full" />;
}

// ---------------------------------------------------------------------------
// Main view — KgMapper
// ---------------------------------------------------------------------------

export function KgMapper() {
  // Project = the route's :name param (the khimaira-attached project label,
  // e.g. "backend"); the daemon proxies /api/graph/<project> to its KG adapter.
  const { name: project } = useParams<{ name: string }>();
  const [searchParams, setSearchParams] = useSearchParams();

  // Persist the scope in the URL (?scope=…) so a reload restores the same graph
  // instead of dropping it. Falls back to the mock scope in MOCK_MODE.
  const initialScope = searchParams.get("scope") ?? (MOCK_MODE ? "shop:mock" : "");
  const [scope, setScope] = useState<string>(initialScope);
  const [inputValue, setInputValue] = useState<string>(initialScope);
  const [selectedNode, setSelectedNode] = useState<ClickedNode | null>(null);
  const [legendVisible, setLegendVisible] = useState<boolean>(false);

  const fetchState = useKgGraph(scope, project);

  const { rawNodes, rawEdges } = useMemo(() => {
    if (fetchState.status !== "ok")
      return { rawNodes: [] as GraphNode[], rawEdges: [] as GraphEdge[] };
    return { rawNodes: fetchState.graph.nodes, rawEdges: fetchState.graph.edges };
  }, [fetchState]);

  const presentTypes = useMemo(
    () => Array.from(new Set(rawNodes.map((node) => node.type))).sort(),
    [rawNodes],
  );

  // Generic contract → graph shape. Drop edges with missing endpoints.
  const mapData = useMemo<MapData>(() => {
    const ids = new Set(rawNodes.map((node) => node.id));
    return {
      nodes: rawNodes.map((node) => ({
        id: node.id,
        nodeType: node.type,
        label: node.label,
        badge: node.badge,
      })),
      links: rawEdges
        .filter((e) => ids.has(e.from) && ids.has(e.to))
        .map((e) => ({
          source: e.from,
          target: e.to,
          linkType: e.type,
          weight: e.weight ?? 1,
        })),
    };
  }, [rawNodes, rawEdges]);

  const handleNodeClick = useCallback((n: ClickedNode) => {
    setSelectedNode(n);
  }, []);

  const handleSubmitScope = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const next = inputValue.trim();
      setScope(next);
      setSelectedNode(null);
      setSearchParams(
        (prev) => {
          if (next) prev.set("scope", next);
          else prev.delete("scope");
          return prev;
        },
        { replace: true },
      );
    },
    [inputValue, setSearchParams],
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
          ) : fetchState.status === "ok" ? (
            <SigmaCanvas
              data={mapData}
              selectedId={selectedNode?.nodeId ?? null}
              onNodeClick={handleNodeClick}
            />
          ) : null}

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
