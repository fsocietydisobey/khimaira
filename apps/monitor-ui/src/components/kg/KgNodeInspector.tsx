/**
 * KgNodeInspector — node-detail side panel for the generic graph viewer.
 *
 * Code-agnostic: renders a GraphNodeDetail (opaque facts + edges) for ANY
 * project's node. No jeevy schema terms — domain fields (trust tier,
 * confidence, timestamps) arrive folded into each fact's generic `meta` map,
 * which the adapter populates. Styling is by opaque `type` string via
 * graphStyle, not a hardcoded type→class map.
 */

import { useEffect, useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { cn } from "@/lib/utils";
import { typeColor, typeColorAlpha } from "./graphStyle";
import { CopyJsonButton } from "./CopyJsonButton";
import {
  GRAPH_URL,
  MOCK_MODE,
  MOCK_NODE_DETAIL,
  type GraphEdge,
  type GraphFact,
  type GraphNodeDetail,
  type GraphNodeSource,
  type GraphSchemaTriple,
} from "./kgTypes";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface KgNodeInspectorProps {
  /** Khimaira-attached project (route :name) — daemon proxies to its KG adapter. */
  project: string | undefined;
  /** Graph scope (e.g. "shop:10") — forwarded to the node-detail endpoint. */
  scope: string;
  nodeId: string;
  type: string;
  label: string;
  badge?: string | number;
  /** Type meta-graph (whole graph) — used to show THIS node type's schema. */
  schemaTriples?: GraphSchemaTriple[];
  /** Jump the canvas + panel to another node (a clicked edge's other end). */
  onNavigateNode?: (nodeId: string) => void;
  /** Open the edge-detail (provenance) panel for an edge id, if present. */
  onOpenEdge?: (edgeId: string | null) => void;
  onClose: () => void;
}

export function KgNodeInspector({
  project,
  scope,
  nodeId,
  type,
  label,
  badge,
  schemaTriples,
  onNavigateNode,
  onOpenEdge,
  onClose,
}: KgNodeInspectorProps) {
  return (
    <div className="flex h-full flex-col border-l border-border bg-card/40">
      {/* Header */}
      <div className="flex items-start justify-between border-b border-border px-3 py-2 gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
              graph node
            </p>
            <span
              className="inline-flex items-center rounded-md border px-1.5 py-0 text-[10px] font-medium"
              style={{
                backgroundColor: typeColorAlpha(type, 0.15),
                color: typeColor(type),
                borderColor: typeColorAlpha(type, 0.4),
              }}
            >
              {type}
            </span>
          </div>
          <h3 className="font-mono text-sm break-all mt-0.5">{label}</h3>
          <p className="text-[10px] text-muted-foreground/70 font-mono break-all mt-0.5">
            {nodeId}
          </p>
          {badge !== undefined && badge !== "" ? (
            <p className="text-[10px] text-muted-foreground mt-0.5">{badge}</p>
          ) : null}
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>
          ×
        </Button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto p-3 space-y-3">
        <FactsPanel
          project={project}
          scope={scope}
          nodeId={nodeId}
          onNavigateNode={onNavigateNode}
          onOpenEdge={onOpenEdge}
        />
        <SourceRecordSection project={project} scope={scope} nodeId={nodeId} />
        <SchemaSection type={type} triples={schemaTriples} />
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Facts fetch + render
// ---------------------------------------------------------------------------

type LoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; detail: GraphNodeDetail };

function FactsPanel({
  project,
  scope,
  nodeId,
  onNavigateNode,
  onOpenEdge,
}: {
  project: string | undefined;
  scope: string;
  nodeId: string;
  onNavigateNode?: (nodeId: string) => void;
  onOpenEdge?: (edgeId: string | null) => void;
}) {
  const [state, setState] = useState<LoadState>({ status: "idle" });
  // History is collapsed by default — dense debug nodes can have dozens of
  // superseded facts; the current values are what you usually want first.
  const [showHistory, setShowHistory] = useState(false);

  useEffect(() => {
    setState({ status: "loading" });
    setShowHistory(false);
    let cancelled = false;

    if (MOCK_MODE) {
      // Simulate a brief fetch delay so loading state is visible
      const id = setTimeout(() => {
        if (!cancelled) setState({ status: "ok", detail: MOCK_NODE_DETAIL });
      }, 200);
      return () => {
        cancelled = true;
        clearTimeout(id);
      };
    }

    if (!project) {
      setState({ status: "error", message: "no project in route" });
      return;
    }

    // Same-origin daemon node-detail proxy (vite /api → daemon → KG adapter):
    //   /api/graph/<project>/node/<uuid>?scope=<scope>
    const url =
      `${GRAPH_URL}/${encodeURIComponent(project)}/node/${encodeURIComponent(nodeId)}` +
      `?scope=${encodeURIComponent(scope)}`;
    fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((json) => {
        if (!cancelled)
          setState({ status: "ok", detail: json.data as GraphNodeDetail });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: String(err) });
      });

    return () => {
      cancelled = true;
    };
  }, [project, scope, nodeId]);

  if (state.status === "idle" || state.status === "loading") {
    return <p className="text-xs text-muted-foreground">loading facts…</p>;
  }
  if (state.status === "error") {
    return (
      <p className="text-xs text-destructive">
        Failed to load facts: {state.message}
      </p>
    );
  }

  const { detail } = state;
  const current = detail.currentFacts.filter((f) => !f.deprecated);
  const historical = detail.historyFacts.filter((f) => f.deprecated);

  // Disambiguation identity: canonical_key + edge count surfaced prominently
  // so same-label nodes (e.g. two "t-13" jobs) can be told apart at a glance.
  const canonicalKey = current.find(
    (f) => f.label === "canonical_key" || f.label === "key",
  )?.value;
  const edgeCount = detail.edgesFrom.length + detail.edgesTo.length;

  return (
    <>
      {/* Identity row — canonical key + edge count. Helps distinguish nodes
          that share a human label (e.g. two jobs both labeled "t-13"). */}
      <div className="rounded-md border border-border/40 bg-muted/20 px-2.5 py-1.5 text-[10px] font-mono space-y-0.5 mb-1">
        {canonicalKey !== undefined && canonicalKey !== null ? (
          <p className="text-foreground/80 font-medium">{String(canonicalKey)}</p>
        ) : null}
        <p className="text-muted-foreground">{edgeCount} edges in scope</p>
      </div>

      {current.length > 0 ? (
        <section>
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">
            attributes ({current.length})
          </p>
          <div className="space-y-1.5">
            {current.map((fact, i) => (
              <FactRow key={i} fact={fact} />
            ))}
          </div>
        </section>
      ) : (
        <p className="text-[11px] text-muted-foreground italic">
          No attributes.
        </p>
      )}

      {historical.length > 0 ? (
        <section>
          <button
            type="button"
            onClick={() => setShowHistory((v) => !v)}
            className="flex w-full items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5 mt-3 hover:text-foreground transition-colors"
          >
            <span>{showHistory ? "▾" : "▸"}</span>
            <span>history ({historical.length})</span>
          </button>
          {showHistory ? (
            <div className="space-y-1.5">
              {historical.map((fact, i) => (
                <FactRow key={i} fact={fact} dimmed />
              ))}
            </div>
          ) : null}
        </section>
      ) : null}

      {detail.edgesFrom.length > 0 || detail.edgesTo.length > 0 ? (
        <EdgeList
          currentNodeId={nodeId}
          edgesFrom={detail.edgesFrom}
          edgesTo={detail.edgesTo}
          onNavigateNode={onNavigateNode}
          onOpenEdge={onOpenEdge}
        />
      ) : null}

      <div className="pt-1">
        <CopyJsonButton value={detail} />
      </div>
    </>
  );
}

function FactRow({
  fact,
  dimmed = false,
}: {
  fact: GraphFact;
  dimmed?: boolean;
}) {
  const metaEntries = fact.meta ? Object.entries(fact.meta) : [];
  return (
    <div
      className={cn(
        "rounded-md border border-border bg-card/60 px-2.5 py-1.5 text-xs",
        dimmed && "opacity-50",
      )}
    >
      <div className="flex items-baseline justify-between gap-2">
        <span className="font-mono text-foreground/90">{fact.label}</span>
        {metaEntries.length > 0 ? (
          <div className="flex items-center gap-1.5 shrink-0 flex-wrap justify-end">
            {metaEntries.map(([k, v]) => (
              <Badge
                key={k}
                variant="outline"
                className="text-[10px] py-0 px-1"
              >
                {k}:{String(v)}
              </Badge>
            ))}
          </div>
        ) : null}
      </div>
      <p className="font-mono text-muted-foreground mt-0.5 break-all">
        {fact.value === null ? (
          <span className="italic">null</span>
        ) : (
          String(fact.value)
        )}
      </p>
    </div>
  );
}

const EDGE_CAP = 12;

function EdgeList({
  currentNodeId,
  edgesFrom,
  edgesTo,
  onNavigateNode,
  onOpenEdge,
}: {
  currentNodeId: string;
  edgesFrom: GraphEdge[];
  edgesTo: GraphEdge[];
  onNavigateNode?: (nodeId: string) => void;
  onOpenEdge?: (edgeId: string | null) => void;
}) {
  const [showAll, setShowAll] = useState(false);
  // Each row's "other endpoint" is the end that ISN'T this node: outbound
  // edges (→) point at `to`, inbound (←) come from `from`.
  const rows = [
    ...edgesFrom.map((e) => ({
      edge: e,
      direction: "→" as const,
      other: e.to,
    })),
    ...edgesTo.map((e) => ({
      edge: e,
      direction: "←" as const,
      other: e.from,
    })),
  ];
  const shown = showAll ? rows : rows.slice(0, EDGE_CAP);
  return (
    <section>
      <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5 mt-3">
        active edges ({rows.length})
        <span className="normal-case opacity-60"> — click to follow</span>
      </p>
      <Card>
        <CardContent className="py-2 px-3 space-y-1">
          {shown.map(({ edge, direction, other }, i) => (
            <EdgeRow
              key={edge.id ?? `${currentNodeId}-${other}-${i}`}
              edge={edge}
              direction={direction}
              other={other}
              onNavigateNode={onNavigateNode}
              onOpenEdge={onOpenEdge}
            />
          ))}
          {rows.length > EDGE_CAP ? (
            <button
              type="button"
              onClick={() => setShowAll((v) => !v)}
              className="mt-1 text-[10px] text-muted-foreground hover:text-foreground transition-colors"
            >
              {showAll ? "show fewer" : `show all ${rows.length}`}
            </button>
          ) : null}
        </CardContent>
      </Card>
    </section>
  );
}

function EdgeRow({
  edge,
  direction,
  other,
  onNavigateNode,
  onOpenEdge,
}: {
  edge: GraphEdge;
  direction: "→" | "←";
  other: string;
  onNavigateNode?: (nodeId: string) => void;
  onOpenEdge?: (edgeId: string | null) => void;
}) {
  return (
    <div className="flex items-center gap-2 text-[11px]">
      {/* Row click → follow the edge to its other endpoint. */}
      <button
        type="button"
        onClick={() => onNavigateNode?.(other)}
        title={`follow to ${other}`}
        className="flex flex-1 items-center gap-2 rounded px-1 py-0.5 text-left hover:bg-accent/40 transition-colors min-w-0"
      >
        <span className="text-muted-foreground font-mono">{direction}</span>
        <span
          className="font-mono font-medium"
          style={{ color: typeColor(edge.type) }}
        >
          {edge.type}
        </span>
        <span className="font-mono text-muted-foreground/60 truncate">
          {other}
        </span>
        {edge.weight !== undefined ? (
          <span className="text-muted-foreground/60 ml-auto font-mono shrink-0">
            {Math.round(edge.weight * 100)}%
          </span>
        ) : null}
      </button>
      {/* Provenance affordance — only when the adapter exposes an edge id. */}
      {edge.id && onOpenEdge ? (
        <button
          type="button"
          onClick={() => onOpenEdge(edge.id ?? null)}
          title="edge provenance"
          className="shrink-0 rounded px-1 text-muted-foreground/70 hover:text-foreground hover:bg-accent/40 transition-colors"
        >
          ⓘ
        </button>
      ) : null}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Source-record section — the underlying jeevy DB row behind this node ("DB
// RECORD" peek). The projection is lossy (drops owner_kind, status, timestamps);
// this shows ground truth + doubles as a projection-QA surface (projected facts
// above vs source row here). On-demand: fetches only when expanded, so rapid
// node-clicking during exploration doesn't fire a request per click.
// ---------------------------------------------------------------------------

type SourceLoadState =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; source: GraphNodeSource };

function SourceRecordSection({
  project,
  scope,
  nodeId,
}: {
  project: string | undefined;
  scope: string;
  nodeId: string;
}) {
  const [open, setOpen] = useState(false);
  const [state, setState] = useState<SourceLoadState>({ status: "idle" });

  // Fetch whenever the section is open, and re-fetch if the node/scope changes
  // while it stays open. Keyed on open/nodeId/scope/project ONLY — deliberately
  // NOT on state.status: including it made the setState(loading) below re-run
  // this effect, whose cleanup set cancelled=true and blocked the fetch's own
  // state update → the panel hung on "loading…" forever. Collapsed-by-default
  // keeps it on-demand (no fetch until the user expands it).
  useEffect(() => {
    if (!open) return;
    if (!project) {
      setState({ status: "error", message: "no project in route" });
      return;
    }
    setState({ status: "loading" });
    let cancelled = false;

    const url =
      `${GRAPH_URL}/${encodeURIComponent(project)}/node/${encodeURIComponent(nodeId)}/source` +
      `?scope=${encodeURIComponent(scope)}`;
    fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((json) => {
        if (!cancelled)
          setState({ status: "ok", source: json.data as GraphNodeSource });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: String(err) });
      });

    return () => {
      cancelled = true;
    };
  }, [open, project, scope, nodeId]);

  return (
    <section className="mt-4 pt-3 border-t border-border/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5 hover:text-foreground transition-colors"
        title="Fetch the underlying source DB row behind this node — ground truth, including fields the KG projection drops (owner_kind, status, timestamps)"
      >
        <span>{open ? "▾" : "▸"}</span>
        <span>db record</span>
        <span className="normal-case opacity-50"> — source of truth</span>
      </button>
      {open ? <SourceRecordBody state={state} /> : null}
    </section>
  );
}

function SourceRecordBody({ state }: { state: SourceLoadState }) {
  if (state.status === "idle" || state.status === "loading") {
    return (
      <p className="text-xs text-muted-foreground">loading source record…</p>
    );
  }
  if (state.status === "error") {
    return (
      <p className="text-xs text-destructive">
        Failed to load source record: {state.message}
      </p>
    );
  }

  const { source } = state;
  // Graceful-empty: out-of-scope, or a name/composite-keyed type with no PK row.
  if (!source.found) {
    return (
      <p className="text-[11px] text-muted-foreground italic">
        {source.reason || "No single source row for this node type."}
      </p>
    );
  }

  const row = source.row ?? {};
  const entries = Object.entries(row);
  return (
    <div className="space-y-1.5">
      {/* Identity header — which table + PK this row came from. */}
      <div className="rounded-md border border-border/40 bg-muted/20 px-2.5 py-1.5 text-[10px] font-mono text-muted-foreground space-y-0.5">
        {source.table ? (
          <p>
            <span className="text-foreground/80">{source.table}</span>
            {source.source_id !== undefined ? `  #${source.source_id}` : ""}
          </p>
        ) : null}
        {source.canonical_key ? <p>{source.canonical_key}</p> : null}
      </div>

      {entries.length > 0 ? (
        <div className="space-y-1">
          {entries.map(([k, v]) => (
            <div
              key={k}
              className="rounded-md border border-border bg-card/60 px-2.5 py-1 text-xs"
            >
              <div className="flex items-baseline justify-between gap-2">
                <span className="font-mono text-muted-foreground shrink-0">
                  {k}
                </span>
                <span className="font-mono text-foreground/90 break-all text-right">
                  {v === null ? (
                    <span className="italic text-muted-foreground">null</span>
                  ) : (
                    String(v)
                  )}
                </span>
              </div>
            </div>
          ))}
        </div>
      ) : (
        <p className="text-[11px] text-muted-foreground italic">Empty row.</p>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Schema section — this node TYPE's relationship schema, derived from the live
// graph (the (fromType, linkType, toType) patterns it participates in). Answers
// "what shape is a node of this type?" without a schema-endpoint round trip.
// ---------------------------------------------------------------------------

function SchemaSection({
  type,
  triples,
}: {
  type: string;
  triples?: GraphSchemaTriple[];
}) {
  const [open, setOpen] = useState(true);
  if (!triples || triples.length === 0) return null;

  const outgoing = triples.filter((t) => t.fromType === type);
  const incoming = triples.filter((t) => t.toType === type);
  if (outgoing.length === 0 && incoming.length === 0) return null;

  return (
    <section className="mt-4 pt-3 border-t border-border/30">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-1 text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5 hover:text-foreground transition-colors"
        title="Aggregate relationship patterns for ALL nodes of this type in the loaded graph — not just this node's own edges"
      >
        <span>{open ? "▾" : "▸"}</span>
        <span>
          all{" "}
          <span
            className="normal-case font-mono"
            style={{ color: typeColor(type) }}
          >
            {type}
          </span>{" "}
          nodes · type schema
        </span>
      </button>
      {open ? (
        <div className="space-y-2">
          <p className="text-[9px] text-muted-foreground/60 italic mb-1">
            Aggregate patterns across <em>all</em> {type} nodes in this scope — not this node's own edges.
          </p>
          {outgoing.length > 0 ? (
            <div>
              <p className="text-[9px] uppercase tracking-wider text-muted-foreground/70 mb-1">
                outgoing ({outgoing.length})
              </p>
              <div className="space-y-0.5">
                {outgoing.map((t, i) => (
                  <SchemaTripleRow
                    key={`o${i}`}
                    dir="→"
                    link={t.linkType}
                    other={t.toType}
                    count={t.count}
                  />
                ))}
              </div>
            </div>
          ) : null}
          {incoming.length > 0 ? (
            <div>
              <p className="text-[9px] uppercase tracking-wider text-muted-foreground/70 mb-1">
                incoming ({incoming.length})
              </p>
              <div className="space-y-0.5">
                {incoming.map((t, i) => (
                  <SchemaTripleRow
                    key={`i${i}`}
                    dir="←"
                    link={t.linkType}
                    other={t.fromType}
                    count={t.count}
                  />
                ))}
              </div>
            </div>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

function SchemaTripleRow({
  dir,
  link,
  other,
  count,
}: {
  dir: "→" | "←";
  link: string;
  other: string;
  count: number;
}) {
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className="text-muted-foreground font-mono">{dir}</span>
      <span
        className="font-mono font-medium"
        style={{ color: typeColor(link) }}
      >
        {link}
      </span>
      <span className="font-mono" style={{ color: typeColor(other) }}>
        {other}
      </span>
      <span className="ml-auto text-muted-foreground/60 font-mono shrink-0">
        ×{count}
      </span>
    </div>
  );
}
