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
import dagre from "@dagrejs/dagre";
import { createNodeBorderProgram } from "@sigma/node-border";
import { drawDiscNodeLabel, type NodeHoverDrawingFunction } from "sigma/rendering";

// Custom hover/highlight renderer. Sigma's default (drawDiscNodeHover) fills a
// WHITE box behind the label AND — for label-less nodes — paints a solid WHITE
// circle over the node. That white blob is what appears on hover + on the
// selected node (we set highlighted). This replacement draws ONLY a dark
// themed label box (when there's a label) and never repaints the node, so the
// node keeps its ring/glow (drawn on the WebGL layer) and there's no white.
const drawNodeHover: NodeHoverDrawingFunction = (context, data, settings) => {
  const size = settings.labelSize;
  context.font = `${settings.labelWeight} ${size}px ${settings.labelFont}`;
  const PADDING = 2;
  if (typeof data.label === "string" && data.label) {
    context.fillStyle = "rgba(15,17,21,0.94)"; // dark, theme-neutral — not white
    context.shadowOffsetX = 0;
    context.shadowOffsetY = 0;
    context.shadowBlur = 8;
    context.shadowColor = "#000";
    const textWidth = context.measureText(data.label).width;
    const boxWidth = Math.round(textWidth + 5);
    const boxHeight = Math.round(size + 2 * PADDING);
    const radius = Math.max(data.size, size / 2) + PADDING;
    const angleRadian = Math.asin(boxHeight / 2 / radius);
    const xDelta = Math.sqrt(Math.abs(radius ** 2 - (boxHeight / 2) ** 2));
    context.beginPath();
    context.moveTo(data.x + xDelta, data.y + boxHeight / 2);
    context.lineTo(data.x + radius + boxWidth, data.y + boxHeight / 2);
    context.lineTo(data.x + radius + boxWidth, data.y - boxHeight / 2);
    context.lineTo(data.x + xDelta, data.y - boxHeight / 2);
    context.arc(data.x, data.y, radius, angleRadian, -angleRadian);
    context.closePath();
    context.fill();
    context.shadowBlur = 0;
  }
  // Label text only (no node repaint). Label-less nodes get nothing extra.
  drawDiscNodeLabel(context, data, settings);
};

// Node program — fully ATTRIBUTE-DRIVEN so one program renders both looks
// (toggled globally, no per-theme branching or renderer rebuild):
//   • "ring"  — transparent fill + a crisp colored ring + faint outer halo
//               (the hollow neuron look; edges show through the center).
//   • "glow"  — colored fill + NO ring (ringSize 0) + a wider soft halo
//               (a filled, softly-radiating dot; no hard border).
// Each node carries fillColor / ringColor / ringSize / haloColor / haloSize;
// `nodeDrawAttrs` computes them from the type + the global borderless flag.
//
// Sizes are PIXEL mode (constant on screen regardless of node size / zoom) so
// the thousands of tiny leaf nodes stay visible at the zoomed-out whole-graph
// view — a relative ring shrinks sub-pixel and the leaves vanish until zoom-in.
const NODE_PROGRAM = createNodeBorderProgram({
  borders: [
    {
      color: { attribute: "haloColor" },
      size: { attribute: "haloSize", defaultValue: 0.7, mode: "pixels" },
    },
    {
      color: { attribute: "ringColor" },
      size: { attribute: "ringSize", defaultValue: 1.5, mode: "pixels" },
    },
    { color: { attribute: "fillColor" }, size: { fill: true } },
  ],
  // Dark themed hover box, never a white node repaint (see drawNodeHover).
  drawHover: drawNodeHover,
});

const TRANSPARENT = "#00000000";

/** Draw attributes for a node — ring (hollow) vs glow (soft radial bloom).
 *  `color` is kept (full hue) for picking + label color regardless of look.
 *
 *  The 3 borders are (outer → in): halo, ring, fill. We exploit that as a
 *  3-stop alpha RAMP for glow: a wide faint outer band → a brighter mid band →
 *  a bright (but not fully opaque) center, which reads as a soft radiating
 *  bloom rather than a hard "plastic" disc. Ring mode uses the same 3 slots as
 *  a thin outer halo + a crisp full-alpha ring + a hollow (transparent) center. */
function nodeDrawAttrs(type: string, borderless: boolean) {
  const c = typeColor(type);
  return borderless
    ? {
        color: c,
        fillColor: c + "aa", // bright-ish center (soft, not fully opaque)
        ringColor: c + "55", // mid bloom band
        ringSize: 2.4,
        haloColor: c + "1f", // wide faint outer glow
        haloSize: 4.5,
      }
    : {
        color: c,
        fillColor: TRANSPARENT, // hollow center — edges show through
        ringColor: c, // crisp full-alpha ring
        ringSize: 2.3, // a bit thicker so it's easy to see (esp. on hover)
        haloColor: c + "66",
        haloSize: 0.8,
      };
}

import {
  activePaletteName,
  confidenceColor,
  confidenceSize,
  EDGE_ALPHA,
  EDGE_BASE_HUE,
  EDGE_BASE_SIZE,
  PALETTES,
  registerTypes,
  setActivePalette,
  typeColor,
} from "./graphStyle";
import { KgNodeInspector } from "./KgNodeInspector";
import { KgEdgeInspector } from "./KgEdgeInspector";
import {
  GRAPH_URL,
  MOCK_GRAPH,
  MOCK_MODE,
  type GraphEdge,
  type GraphNode,
  type GraphResponse,
} from "./kgTypes";

/** Edge styling mode: plain neutral web (default) or confidence (low-weight POP). */
type EdgeMode = "plain" | "confidence";

/** Confidence-threshold filter options — show only edges below the cut. */
const THRESHOLD_OPTIONS: { label: string; value: number | null }[] = [
  { label: "all", value: null },
  { label: "< 0.9", value: 0.9 },
  { label: "< 0.7", value: 0.7 },
];

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
  /** Opaque adapter edge id (may be absent for adapters without edge ids). */
  id?: string;
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
  hiddenTypes,
  visible,
  onToggle,
  onToggleType,
}: {
  types: string[];
  hiddenTypes: Set<string>;
  visible: boolean;
  onToggle: () => void;
  onToggleType: (t: string) => void;
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
        <div className="rounded-md border border-border bg-card/90 p-2 text-[10px] space-y-0.5 max-h-72 overflow-auto shadow-lg">
          <p className="uppercase tracking-wider text-muted-foreground mb-1">
            node types{" "}
            <span className="normal-case opacity-60">— click to filter</span>
          </p>
          {types.map((t) => {
            const hidden = hiddenTypes.has(t);
            return (
              <button
                key={t}
                type="button"
                onClick={() => onToggleType(t)}
                title={hidden ? `show ${t}` : `hide ${t}`}
                className={`flex w-full items-center gap-1.5 rounded px-1 py-0.5 text-left hover:bg-accent/40 ${
                  hidden ? "opacity-35" : ""
                }`}
              >
                <span
                  className="inline-block h-2.5 w-2.5 rounded-full"
                  style={{ backgroundColor: typeColor(t) }}
                />
                <span className="font-mono">{t}</span>
              </button>
            );
          })}
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
  hiddenTypes,
  edgeMode,
  edgeThreshold,
  isolateId,
  hopDepth,
  layoutMode,
  focusId,
  focusNonce,
  focusRatio,
  themeVersion,
  edgeColor,
  borderless,
  onNodeClick,
  onEdgeClick,
}: {
  data: MapData;
  selectedId: string | null;
  hiddenTypes: Set<string>;
  /** "type" = color edges by relation; "confidence" = low-weight edges POP. */
  edgeMode: EdgeMode;
  /** When set, hide edges whose weight is >= this (show only suspect ones). */
  edgeThreshold: number | null;
  /** When set, show ONLY this node + its neighbors (Obsidian local-graph). */
  isolateId: string | null;
  /** Hops out from the hover/isolate focus node to highlight (1/2/3 or whole component). */
  hopDepth: 1 | 2 | 3 | "all";
  /** Node placement: "force" = ForceAtlas2 (organic); "tree" = dagre layered. */
  layoutMode: "force" | "tree";
  /** Node to center the camera on (search / sidebar navigation). */
  focusId: string | null;
  /** Bumped to retrigger focus even when focusId is unchanged. */
  focusNonce: number;
  /** Camera ratio to zoom to on focus (lower = tighter). */
  focusRatio: number;
  /** Bumped when the color palette changes → triggers an in-place recolor. */
  themeVersion: number;
  /** 6-digit hue for the plain-mode edge web (user-picked). */
  edgeColor: string;
  /** true = filled radiating dots (no ring); false = hollow rings. */
  borderless: boolean;
  onNodeClick: (n: ClickedNode) => void;
  onEdgeClick: (edgeId: string | null) => void;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const sigmaRef = useRef<Sigma | null>(null);
  const graphRef = useRef<Graph | null>(null);
  // keep the latest values for use inside sigma reducers without rebuilding
  const selectedRef = useRef<string | null>(selectedId);
  selectedRef.current = selectedId;
  const hiddenRef = useRef(hiddenTypes);
  hiddenRef.current = hiddenTypes;
  const edgeModeRef = useRef(edgeMode);
  edgeModeRef.current = edgeMode;
  const edgeThresholdRef = useRef(edgeThreshold);
  edgeThresholdRef.current = edgeThreshold;
  const isolateRef = useRef<string | null>(isolateId);
  isolateRef.current = isolateId;
  const edgeColorRef = useRef(edgeColor);
  edgeColorRef.current = edgeColor;
  const borderlessRef = useRef(borderless);
  borderlessRef.current = borderless;
  const clickRef = useRef(onNodeClick);
  clickRef.current = onNodeClick;
  const edgeClickRef = useRef(onEdgeClick);
  edgeClickRef.current = onEdgeClick;

  // Hover-to-highlight: the hovered node id + a precomputed neighbor set so the
  // reducers can dim everything non-adjacent without an O(degree) lookup per
  // element. `focusSourceRef` is the active dim source — hover wins over
  // isolate when both are present.
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const focusNeighborsRef = useRef<Set<string> | null>(null);
  // `hopDepth` (prop, owned by the toolbar) governs how many hops out from the focus
  // node both hover-dim and isolate highlight: 1 = direct neighbors (original), 2/3 =
  // the in-between local graph, "all" = the whole reachable component.

  // Recompute the focus set whenever the dim source (hover or isolate) OR the hop
  // depth changes, then refresh once. Hover takes precedence over isolate. BFS the
  // undirected adjacency (forEachNeighbor covers in+out edges, so inverse links stay
  // included) out to `hopDepth` hops — `"all"` walks the entire reachable component.
  useEffect(() => {
    const graph = graphRef.current;
    const source = hoveredId ?? isolateId;
    if (graph && source && graph.hasNode(source)) {
      const set = new Set<string>([source]);
      const maxHops = hopDepth === "all" ? Infinity : hopDepth;
      let frontier: string[] = [source];
      for (let depth = 0; depth < maxHops && frontier.length > 0; depth += 1) {
        const next: string[] = [];
        for (const node of frontier) {
          graph.forEachNeighbor(node, (nb) => {
            if (!set.has(nb)) {
              set.add(nb);
              next.push(nb);
            }
          });
        }
        frontier = next;
      }
      focusNeighborsRef.current = set;
    } else {
      focusNeighborsRef.current = null;
    }
    sigmaRef.current?.refresh();
  }, [hoveredId, isolateId, hopDepth]);

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
        ...nodeDrawAttrs(node.nodeType, borderlessRef.current),
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
        // Stash the contract edge id + linkType + weight as attributes so the
        // reducer can re-style by mode and the click handler can resolve the
        // adapter's edge id for the detail panel. `edgeId` may be undefined for
        // adapters that don't expose edge ids (the click is then a no-op).
        graph.addEdge(l.source, l.target, {
          edgeId: l.id,
          linkType: l.linkType,
          weight: l.weight ?? 1,
          color: edgeColorRef.current + EDGE_ALPHA,
          size: EDGE_BASE_SIZE,
        });
      }
    });

    if (layoutMode === "tree") {
      // Dagre layered layout — code-agnostic: uses ONLY graph structure + edge
      // DIRECTION to rank nodes (no node-type/schema knowledge). Naturally spreads
      // leaf clusters into clean rows under their hub. dagre is already a repo dep
      // (used by FlowCanvas); this mirrors that pattern.
      const dg = new dagre.graphlib.Graph();
      dg.setGraph({ rankdir: "TB", nodesep: 30, ranksep: 70, marginx: 20, marginy: 20 });
      dg.setDefaultEdgeLabel(() => ({}));
      graph.forEachNode((node, attrs) => {
        const sz = (attrs.size as number) ?? 4;
        dg.setNode(node, { width: sz * 2, height: sz * 2 });
      });
      graph.forEachEdge((_edge, _attrs, src, tgt) => {
        dg.setEdge(src, tgt);
      });
      dagre.layout(dg);
      graph.forEachNode((node) => {
        const pos = dg.node(node);
        if (pos) {
          graph.setNodeAttribute(node, "x", pos.x);
          // Flip Y: dagre ranks downward (rank 0 = smallest y); sigma's y axis points
          // up, so negate to put rank 0 (the roots/hubs) at the TOP of the view.
          graph.setNodeAttribute(node, "y", -pos.y);
        }
      });
    } else {
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
    }

    const renderer = new Sigma(graph, container, {
      renderLabels: true,
      labelColor: { color: "#cbd5e1" },
      labelDensity: 0.6,
      labelGridCellSize: 70,
      // Only label nodes whose rendered size clears the threshold. Set high so
      // the zoomed-out whole-graph view stays clean (a wall of overlapping hub
      // labels obscures the wireframe); labels reappear as you zoom in.
      labelRenderedSizeThreshold: 18,
      defaultEdgeColor: edgeColorRef.current + EDGE_ALPHA,
      enableEdgeEvents: true,
      minCameraRatio: 0.02,
      maxCameraRatio: 12,
      zIndex: true,
      // Attribute-driven node program — ring (hollow) or glow (filled) per the
      // global borderless toggle.
      defaultNodeType: "kgnode",
      nodeProgramClasses: { kgnode: NODE_PROGRAM },
    });

    // Node reducer: type filter → isolate/hover dim → selected highlight.
    // Re-applied on refresh() when any interactive state changes. Overrides the
    // visible color on the right channel (fill when borderless, ring otherwise).
    renderer.setSetting("nodeReducer", (node, attrs) => {
      if (hiddenRef.current.has(attrs.nodeType as string)) {
        return { ...attrs, hidden: true };
      }
      const borderless = borderlessRef.current;
      const focusSet = focusNeighborsRef.current;
      // Isolate mode hard-hides non-neighbors; hover only dims them.
      if (focusSet && !focusSet.has(node)) {
        if (isolateRef.current && !hoveredId) {
          return { ...attrs, hidden: true };
        }
        const dim = "#3f3f46";
        return {
          ...attrs,
          fillColor: borderless ? dim : TRANSPARENT,
          ringColor: borderless ? TRANSPARENT : dim,
          haloColor: "#00000000",
          label: "",
          zIndex: 0,
        };
      }
      if (selectedRef.current && node === selectedRef.current) {
        // Highlight in the node's OWN type color (matches the theme — never a
        // fixed white). Distinguished by full opacity + a strong same-hue halo
        // + a larger size, not by recoloring to white.
        const c = attrs.color as string;
        return {
          ...attrs,
          fillColor: borderless ? c : TRANSPARENT, // glow: bright full center
          ringColor: borderless ? c + "88" : c, // ring stays its hue, full
          haloColor: c + "ee", // strong same-hue glow
          haloSize: borderless ? 7 : 3,
          size: (attrs.size as number) * 1.8,
          zIndex: 2,
          highlighted: true,
        };
      }
      return attrs;
    });

    // Edge reducer: threshold filter → isolate/hover incidence → confidence
    // vs type styling. Runs per edge on every refresh.
    renderer.setSetting("edgeReducer", (edge, attrs) => {
      const weight = (attrs.weight as number) ?? 1;
      const threshold = edgeThresholdRef.current;
      if (threshold !== null && weight >= threshold) {
        return { ...attrs, hidden: true };
      }

      const [src, tgt] = graph.extremities(edge);
      // Hide edges whose endpoint type is filtered out (keeps the canvas honest
      // when a node type is toggled off in the legend).
      if (
        hiddenRef.current.has(
          graph.getNodeAttribute(src, "nodeType") as string,
        ) ||
        hiddenRef.current.has(graph.getNodeAttribute(tgt, "nodeType") as string)
      ) {
        return { ...attrs, hidden: true };
      }

      // Highlight edges INTERNAL to the focused subgraph — both endpoints within
      // the hopDepth focus set (which includes the source). This shows the multi-hop
      // neighborhood's real structure, not just spokes from the source. A boundary
      // edge (one endpoint in focus, one outside) is dimmed/hidden like a non-focus
      // edge, so the boundary doesn't distractingly over-light.
      const focusSet = focusNeighborsRef.current;
      const internalToFocus =
        !focusSet || (focusSet.has(src) && focusSet.has(tgt));
      if (focusSet && !internalToFocus) {
        if (isolateRef.current && !hoveredId) {
          return { ...attrs, hidden: true };
        }
        return { ...attrs, color: "#27272a22", zIndex: 0 };
      }

      if (edgeModeRef.current === "confidence") {
        return {
          ...attrs,
          color: confidenceColor(weight),
          size: confidenceSize(weight),
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

    renderer.on("clickEdge", ({ edge }) => {
      edgeClickRef.current(
        (graph.getEdgeAttribute(edge, "edgeId") as string) ?? null,
      );
    });

    renderer.on("enterNode", ({ node }) => setHoveredId(node));
    renderer.on("leaveNode", () => setHoveredId(null));

    sigmaRef.current = renderer;
    graphRef.current = graph;
    return () => {
      renderer.kill();
      sigmaRef.current = null;
      graphRef.current = null;
    };
    // layoutMode in deps: toggling force↔tree recomputes positions + rebuilds the
    // renderer (which re-frames the camera to the new layout).
  }, [data, layoutMode]);

  // Re-apply highlight + filters + encoding without rebuilding the graph.
  useEffect(() => {
    sigmaRef.current?.refresh();
  }, [selectedId, hiddenTypes, edgeMode, edgeThreshold]);

  // Center the camera on a node when a focus is requested (search / nav).
  useEffect(() => {
    const renderer = sigmaRef.current;
    const graph = graphRef.current;
    if (!renderer || !graph || !focusId || !graph.hasNode(focusId)) return;
    const pos = renderer.getNodeDisplayData(focusId);
    if (!pos) return;
    renderer
      .getCamera()
      .animate({ x: pos.x, y: pos.y, ratio: focusRatio }, { duration: 500 });
  }, [focusId, focusNonce, focusRatio]);

  // Recompute node draw attrs in place when the palette OR the ring/glow toggle
  // changes — no relayout (positions are expensive). One pass rewrites the
  // fill/ring/halo channels from the type + the current borderless flag.
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph) return;
    graph.forEachNode((id, attrs) => {
      const draw = nodeDrawAttrs(attrs.nodeType as string, borderless);
      for (const [k, v] of Object.entries(draw))
        graph.setNodeAttribute(id, k, v);
    });
    sigmaRef.current?.refresh();
  }, [themeVersion, borderless]);

  // Recolor edges in place when the edge hue is picked. Plain-mode only — the
  // confidence overlay re-derives its own colors in the reducer.
  useEffect(() => {
    const graph = graphRef.current;
    if (!graph) return;
    graph.forEachEdge((edge) => {
      graph.setEdgeAttribute(edge, "color", edgeColor + EDGE_ALPHA);
    });
    sigmaRef.current?.refresh();
  }, [edgeColor]);

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
  const initialScope =
    searchParams.get("scope") ?? (MOCK_MODE ? "shop:mock" : "");
  const [scope, setScope] = useState<string>(initialScope);
  const [inputValue, setInputValue] = useState<string>(initialScope);
  const [selectedNode, setSelectedNode] = useState<ClickedNode | null>(null);
  const [selectedEdgeId, setSelectedEdgeId] = useState<string | null>(null);
  const [legendVisible, setLegendVisible] = useState<boolean>(false);
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set());

  // Tier-1 controls. Initial values read from the URL so the entire view is
  // reconstructable from a link — this is what makes a graph screenshot
  // deterministic (a capture tool sets the URL, the page renders exactly that):
  //   ?scope=… &selectNode=<id> &isolate=1 &edgeMode=confidence &conf=0.9 &zoom=0.2
  const [edgeMode, setEdgeMode] = useState<EdgeMode>(
    searchParams.get("edgeMode") === "confidence" ? "confidence" : "plain",
  );
  const [edgeThreshold, setEdgeThreshold] = useState<number | null>(() => {
    const c = searchParams.get("conf");
    return c === "0.9" ? 0.9 : c === "0.7" ? 0.7 : null;
  });
  const [isolateMode, setIsolateMode] = useState<boolean>(
    searchParams.get("isolate") === "1",
  );
  // Hops out from the hover/isolate focus node to highlight (1 = direct neighbors,
  // the original behavior; 2/3 = local graph; "all" = whole reachable component).
  const [hopDepth, setHopDepth] = useState<1 | 2 | 3 | "all">(1);
  // Node placement: "force" = ForceAtlas2 (organic, default); "tree" = dagre layered.
  const [layoutMode, setLayoutMode] = useState<"force" | "tree">(
    searchParams.get("layout") === "tree" ? "tree" : "force",
  );
  const [searchValue, setSearchValue] = useState<string>("");
  const [searchMiss, setSearchMiss] = useState<boolean>(false);

  // Camera zoom for the focused node (lower ratio = more zoomed in). Read once
  // from ?zoom= so a screenshot can frame tight (a single node's neighborhood)
  // or wide. Defaults to a moderate zoom-in when a node is focused.
  const focusRatio = (() => {
    const z = Number.parseFloat(searchParams.get("zoom") ?? "");
    return Number.isFinite(z) && z > 0 ? z : 0.25;
  })();
  // focusId + nonce drive the canvas camera; the nonce lets the same id be
  // re-focused (e.g. searching the same term twice).
  const [focusId, setFocusId] = useState<string | null>(null);
  const [focusNonce, setFocusNonce] = useState<number>(0);

  // Color theme. Persisted to localStorage (per-browser preference). The active
  // palette is a module singleton typeColor reads — sync it here, in render,
  // so the legend + inspectors (which call typeColor) get the right colors on
  // this pass; themeVersion drives the canvas's in-place recolor effect.
  const [theme, setTheme] = useState<string>(() => {
    const saved =
      typeof localStorage !== "undefined"
        ? localStorage.getItem("kg-palette")
        : null;
    return saved && PALETTES.some((p) => p.name === saved)
      ? saved
      : activePaletteName();
  });
  const [themeVersion, setThemeVersion] = useState<number>(0);
  if (activePaletteName() !== theme) setActivePalette(theme);

  // Edge hue (plain-mode web). A 6-digit hex the picker sets; rendering appends
  // EDGE_ALPHA to keep the web faint. Persisted per-browser like the theme.
  const [edgeColor, setEdgeColor] = useState<string>(() => {
    // -v2 key: bumped when the default hue changed so a stale saved value
    // doesn't mask the new near-black default.
    const saved =
      typeof localStorage !== "undefined"
        ? localStorage.getItem("kg-edge-color-v3")
        : null;
    return saved && /^#[0-9a-fA-F]{6}$/.test(saved) ? saved : EDGE_BASE_HUE;
  });
  const handleEdgeColorChange = useCallback((hex: string) => {
    setEdgeColor(hex);
    try {
      localStorage.setItem("kg-edge-color-v3", hex);
    } catch {
      // non-fatal — see handleThemeChange
    }
  }, []);

  // Node look: hollow ring (default) vs filled radiating glow (no border).
  const [borderless, setBorderless] = useState<boolean>(() => {
    return typeof localStorage !== "undefined"
      ? localStorage.getItem("kg-node-glow") === "1"
      : false;
  });
  const handleNodeStyleChange = useCallback((glow: boolean) => {
    setBorderless(glow);
    try {
      localStorage.setItem("kg-node-glow", glow ? "1" : "0");
    } catch {
      // non-fatal — see handleThemeChange
    }
  }, []);

  const handleThemeChange = useCallback(
    (name: string) => {
      setActivePalette(name);
      setTheme(name);
      setThemeVersion((v) => v + 1);
      try {
        localStorage.setItem("kg-palette", name);
      } catch {
        // localStorage can be unavailable (private mode) — preference just
        // won't persist across reloads; the in-session change still applies.
      }
      // A theme may carry its own edge hue (e.g. Wireframe wants white edges
      // for the full look). Applying it also updates the edge picker + storage.
      const pal = PALETTES.find((p) => p.name === name);
      if (pal?.edge) handleEdgeColorChange(pal.edge);
    },
    [handleEdgeColorChange],
  );

  const toggleType = useCallback((t: string) => {
    setHiddenTypes((prev) => {
      const next = new Set(prev);
      if (next.has(t)) next.delete(t);
      else next.add(t);
      return next;
    });
  }, []);

  const fetchState = useKgGraph(scope, project);

  const { rawNodes, rawEdges } = useMemo(() => {
    if (fetchState.status !== "ok")
      return { rawNodes: [] as GraphNode[], rawEdges: [] as GraphEdge[] };
    return {
      rawNodes: fetchState.graph.nodes,
      rawEdges: fetchState.graph.edges,
    };
  }, [fetchState]);

  const presentTypes = useMemo(
    () => Array.from(new Set(rawNodes.map((node) => node.type))).sort(),
    [rawNodes],
  );

  // Hand the full node-type set to the color registry so typeColor assigns each
  // distinct type a COLLISION-FREE color (by sorted ordinal, never a hash-mod
  // wrap). Done in render — after the palette sync above and before the legend +
  // inspectors (which call typeColor) render this pass. Cheap + idempotent
  // (registerTypes no-ops when the set + palette are unchanged).
  registerTypes(presentTypes);

  // How many edges fall below the active confidence threshold — drives honest
  // feedback when a filter hides everything (e.g. a graph whose edges are ALL
  // weight 1.0, where <0.9 and <0.7 are both empty and look identical/broken).
  const belowThresholdCount = useMemo(() => {
    if (edgeThreshold === null) return null;
    return rawEdges.filter((e) => (e.weight ?? 1) < edgeThreshold).length;
  }, [edgeThreshold, rawEdges]);

  // Does this graph even HAVE sub-1.0-confidence edges? If not, the <0.9 / <0.7
  // filters are inert (both empty, hence "no difference") — we disable them and
  // say why, instead of leaving the user to wonder.
  const hasConfidenceSpread = useMemo(
    () => rawEdges.some((e) => (e.weight ?? 1) < 0.9),
    [rawEdges],
  );

  // Type meta-graph (schema) computed from the LIVE graph — the same
  // (fromType, linkType, toType) triples the /schema endpoint produces, but
  // derived client-side so the node panel can show a node TYPE's schema with no
  // deploy gate. The node inspector filters these to the selected node's type.
  const schemaTriples = useMemo(() => {
    const typeById = new Map(rawNodes.map((n) => [n.id, n.type]));
    const counts = new Map<
      string,
      { fromType: string; linkType: string; toType: string; count: number }
    >();
    for (const e of rawEdges) {
      const ft = typeById.get(e.from);
      const tt = typeById.get(e.to);
      if (!ft || !tt) continue;
      const key = `${ft}\x00${e.type}\x00${tt}`;
      const cur = counts.get(key);
      if (cur) cur.count += 1;
      else
        counts.set(key, {
          fromType: ft,
          linkType: e.type,
          toType: tt,
          count: 1,
        });
    }
    return Array.from(counts.values()).sort((a, b) => b.count - a.count);
  }, [rawNodes, rawEdges]);

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
          id: e.id,
          source: e.from,
          target: e.to,
          linkType: e.type,
          weight: e.weight ?? 1,
        })),
    };
  }, [rawNodes, rawEdges]);

  // Opaque id → node, for resolving search hits / edge endpoints / sidebar
  // navigation targets back to a full ClickedNode without re-fetching.
  const nodeById = useMemo(() => {
    const m = new Map<string, GraphNode>();
    for (const node of rawNodes) m.set(node.id, node);
    return m;
  }, [rawNodes]);

  const handleNodeClick = useCallback((n: ClickedNode) => {
    setSelectedNode(n);
    setSelectedEdgeId(null);
  }, []);

  // Select + center on a node by id (search hit, edge endpoint, sidebar edge).
  // No-op if the id isn't in the current graph.
  const focusNode = useCallback(
    (nodeId: string) => {
      const node = nodeById.get(nodeId);
      if (!node) return;
      setSelectedNode({
        nodeId: node.id,
        type: node.type,
        label: node.label,
        badge: node.badge,
      });
      setSelectedEdgeId(null);
      setFocusId(node.id);
      setFocusNonce((k) => k + 1);
    },
    [nodeById],
  );

  // Edge-click on the canvas → open the edge-detail panel. Adapters without
  // edge ids pass null (the click is then inert — there's nothing to fetch).
  const handleEdgeClick = useCallback((edgeId: string | null) => {
    if (!edgeId) return;
    setSelectedEdgeId(edgeId);
  }, []);

  // Search: first node whose label or id contains the query (case-insensitive),
  // ranked label-exact → label-prefix → label-substring → id-substring.
  const handleSearch = useCallback(
    (e: React.FormEvent) => {
      e.preventDefault();
      const q = searchValue.trim().toLowerCase();
      if (!q) return;
      let best: { rank: number; node: GraphNode } | null = null;
      for (const node of rawNodes) {
        const label = node.label.toLowerCase();
        const id = node.id.toLowerCase();
        let rank: number;
        if (label === q) rank = 0;
        else if (label.startsWith(q)) rank = 1;
        else if (label.includes(q)) rank = 2;
        else if (id.includes(q)) rank = 3;
        else continue;
        if (!best || rank < best.rank) best = { rank, node };
        if (rank === 0) break;
      }
      if (best) {
        setSearchMiss(false);
        focusNode(best.node.id);
      } else {
        setSearchMiss(true);
      }
    },
    [searchValue, rawNodes, focusNode],
  );

  // Deep-link / programmatic node selection via ?selectNode=<id> — opens the
  // detail panel for that node once the graph has loaded. Used for shareable
  // links and Specter screenshots (the sigma canvas has no DOM node to click).
  // Generic: matches the opaque node id, no schema knowledge.
  const selectNodeParam = searchParams.get("selectNode");
  useEffect(() => {
    if (!selectNodeParam || rawNodes.length === 0) return;
    if (selectedNode?.nodeId === selectNodeParam) return;
    // focusNode selects AND centers the camera — so a ?selectNode= deep link
    // (used by screenshots) frames the node, not just opens its panel.
    focusNode(selectNodeParam);
  }, [selectNodeParam, rawNodes, selectedNode?.nodeId, focusNode]);

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
          <label
            htmlFor="kg-scope"
            className="text-[11px] text-muted-foreground whitespace-nowrap"
          >
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
          <span className="text-[11px] text-muted-foreground animate-pulse">
            loading graph…
          </span>
        ) : null}
        {fetchState.status === "ok" ? (
          <span className="text-[11px] text-muted-foreground">
            {rawNodes.length} nodes · {rawEdges.length} edges
            {MOCK_MODE ? " (mock)" : ""}
          </span>
        ) : null}
        {fetchState.status === "error" ? (
          <span className="text-[11px] text-destructive">
            {fetchState.message}
          </span>
        ) : null}

        {/* Tier-1 controls — only meaningful once a graph is on screen. */}
        {fetchState.status === "ok" && rawNodes.length > 0 ? (
          <div className="ml-auto flex items-center gap-3 flex-wrap justify-end">
            {/* Node search → center + select */}
            <form onSubmit={handleSearch} className="flex items-center gap-1.5">
              <input
                type="text"
                value={searchValue}
                onChange={(e) => {
                  setSearchValue(e.target.value);
                  setSearchMiss(false);
                }}
                placeholder="search label / id"
                className={`h-7 w-44 rounded-md border bg-background px-2 text-[11px] text-foreground placeholder:text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring font-mono ${
                  searchMiss ? "border-destructive" : "border-input"
                }`}
              />
              <button
                type="submit"
                className="h-7 rounded-md border border-input bg-background px-2 text-[11px] text-muted-foreground hover:text-foreground hover:bg-accent transition-colors"
                title="center + select the first matching node"
              >
                find
              </button>
            </form>

            {/* Edge encoding: plain neutral web vs confidence overlay */}
            <div className="flex items-center gap-1 text-[10px]">
              <span className="text-muted-foreground">edges</span>
              {(["plain", "confidence"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setEdgeMode(m)}
                  className={`h-7 rounded-md border px-2 transition-colors ${
                    edgeMode === m
                      ? "border-ring bg-accent text-foreground"
                      : "border-input bg-background text-muted-foreground hover:text-foreground"
                  }`}
                  title={
                    m === "confidence"
                      ? "overlay confidence — low-weight edges POP red/amber"
                      : "plain neutral structural web (same look in any theme)"
                  }
                >
                  {m}
                </button>
              ))}
            </div>

            {/* Confidence threshold filter. Disabled when the graph has no
                sub-1.0-confidence edges — then <0.9 and <0.7 are both empty
                (identical), which is the "I see no difference" confusion. We
                disable + explain rather than let them look broken. */}
            <div className="flex items-center gap-1 text-[10px]">
              <span className="text-muted-foreground">conf</span>
              {THRESHOLD_OPTIONS.map((opt) => {
                // "all" is always available; the sub-1.0 cuts need spread.
                const disabled = opt.value !== null && !hasConfidenceSpread;
                return (
                  <button
                    key={opt.label}
                    type="button"
                    disabled={disabled}
                    onClick={() => setEdgeThreshold(opt.value)}
                    className={`h-7 rounded-md border px-2 font-mono transition-colors ${
                      edgeThreshold === opt.value
                        ? "border-ring bg-accent text-foreground"
                        : "border-input bg-background text-muted-foreground hover:text-foreground"
                    } ${disabled ? "opacity-30 cursor-not-allowed hover:text-muted-foreground" : ""}`}
                    title={
                      disabled
                        ? "every edge in this graph is confidence 1.0 — there's nothing below this cut, so the filter has no effect"
                        : opt.value === null
                          ? "show all edges"
                          : `show only edges with weight < ${opt.value} (the suspect ones)`
                    }
                  >
                    {opt.label}
                  </button>
                );
              })}
              {!hasConfidenceSpread ? (
                <span
                  className="text-muted-foreground/70"
                  title="the adapter reports the same confidence (1.0) for every edge in this scope, so there's no spread to filter on"
                >
                  all edges conf 1.0
                </span>
              ) : edgeThreshold !== null && belowThresholdCount !== null ? (
                <span className="text-muted-foreground/70 font-mono">
                  {belowThresholdCount} shown
                </span>
              ) : null}
            </div>

            {/* Isolate (local-graph) mode */}
            <button
              type="button"
              onClick={() => setIsolateMode((v) => !v)}
              className={`h-7 rounded-md border px-2 text-[10px] transition-colors ${
                isolateMode
                  ? "border-ring bg-accent text-foreground"
                  : "border-input bg-background text-muted-foreground hover:text-foreground"
              }`}
              title="isolate: show only the selected node + its neighbors"
            >
              isolate
            </button>

            {/* Hop depth: how many hops out the hover/isolate highlight reaches */}
            <div className="flex items-center gap-1 text-[10px]">
              <span className="text-muted-foreground">hops</span>
              {([1, 2, 3, "all"] as const).map((h) => (
                <button
                  key={h}
                  type="button"
                  onClick={() => setHopDepth(h)}
                  className={`h-7 rounded-md border px-2 font-mono transition-colors ${
                    hopDepth === h
                      ? "border-ring bg-accent text-foreground"
                      : "border-input bg-background text-muted-foreground hover:text-foreground"
                  }`}
                  title={
                    h === "all"
                      ? "highlight the whole reachable component on hover/isolate"
                      : `highlight nodes within ${h} hop${h === 1 ? "" : "s"} of the focus node`
                  }
                >
                  {h}
                </button>
              ))}
            </div>

            {/* Layout: organic force-directed vs layered tree (dagre) */}
            <div className="flex items-center gap-1 text-[10px]">
              <span className="text-muted-foreground">layout</span>
              {(["force", "tree"] as const).map((m) => (
                <button
                  key={m}
                  type="button"
                  onClick={() => setLayoutMode(m)}
                  className={`h-7 rounded-md border px-2 transition-colors ${
                    layoutMode === m
                      ? "border-ring bg-accent text-foreground"
                      : "border-input bg-background text-muted-foreground hover:text-foreground"
                  }`}
                  title={
                    m === "tree"
                      ? "layered hierarchy (dagre) — spreads leaf clusters into rows"
                      : "organic force-directed layout (ForceAtlas2)"
                  }
                >
                  {m}
                </button>
              ))}
            </div>

            {/* Color theme picker */}
            <div className="flex items-center gap-1 text-[10px]">
              <span className="text-muted-foreground">theme</span>
              <select
                value={theme}
                onChange={(e) => handleThemeChange(e.target.value)}
                className="h-7 rounded-md border border-input bg-background px-1.5 text-[10px] text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
                title="node + selection color palette"
              >
                {PALETTES.map((p) => (
                  <option key={p.name} value={p.name}>
                    {p.label}
                  </option>
                ))}
              </select>
            </div>

            {/* Edge color picker (plain-mode web hue) */}
            <div className="flex items-center gap-1 text-[10px]">
              <span className="text-muted-foreground">edge</span>
              <input
                type="color"
                value={edgeColor}
                onChange={(e) => handleEdgeColorChange(e.target.value)}
                className="h-7 w-8 cursor-pointer rounded-md border border-input bg-background p-0.5"
                title="edge web color (plain mode)"
              />
            </div>

            {/* Node look: hollow ring vs filled radiating glow */}
            <div className="flex items-center gap-1 text-[10px]">
              <span className="text-muted-foreground">nodes</span>
              {(["ring", "glow"] as const).map((style) => {
                const active = (style === "glow") === borderless;
                return (
                  <button
                    key={style}
                    type="button"
                    onClick={() => handleNodeStyleChange(style === "glow")}
                    className={`h-7 rounded-md border px-2 transition-colors ${
                      active
                        ? "border-ring bg-accent text-foreground"
                        : "border-input bg-background text-muted-foreground hover:text-foreground"
                    }`}
                    title={
                      style === "glow"
                        ? "filled, softly-radiating dots (no border)"
                        : "hollow neuron rings"
                    }
                  >
                    {style}
                  </button>
                );
              })}
            </div>
          </div>
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
              <p className="text-sm text-muted-foreground">
                No nodes found for this scope.
              </p>
            </div>
          ) : fetchState.status === "ok" ? (
            <SigmaCanvas
              data={mapData}
              selectedId={selectedNode?.nodeId ?? null}
              hiddenTypes={hiddenTypes}
              edgeMode={edgeMode}
              edgeThreshold={edgeThreshold}
              isolateId={
                isolateMode && selectedNode ? selectedNode.nodeId : null
              }
              hopDepth={hopDepth}
              layoutMode={layoutMode}
              focusId={focusId}
              focusNonce={focusNonce}
              focusRatio={focusRatio}
              themeVersion={themeVersion}
              edgeColor={edgeColor}
              borderless={borderless}
              onNodeClick={handleNodeClick}
              onEdgeClick={handleEdgeClick}
            />
          ) : null}

          <KgLegend
            types={presentTypes}
            hiddenTypes={hiddenTypes}
            visible={legendVisible}
            onToggle={() => setLegendVisible((v) => !v)}
            onToggleType={toggleType}
          />
        </div>

        {/* Detail panel — edge takes priority (it's the more specific click),
            else node. Both fetch their own data from the daemon proxy. */}
        {selectedEdgeId ? (
          <div className="w-96 shrink-0">
            <KgEdgeInspector
              project={project}
              scope={scope}
              edgeId={selectedEdgeId}
              onNavigateNode={focusNode}
              onClose={() => setSelectedEdgeId(null)}
            />
          </div>
        ) : selectedNode ? (
          <div className="w-96 shrink-0">
            <KgNodeInspector
              project={project}
              scope={scope}
              nodeId={selectedNode.nodeId}
              type={selectedNode.type}
              label={selectedNode.label}
              badge={selectedNode.badge}
              schemaTriples={schemaTriples}
              onNavigateNode={focusNode}
              onOpenEdge={handleEdgeClick}
              onClose={() => setSelectedNode(null)}
            />
          </div>
        ) : null}
      </div>
    </div>
  );
}
