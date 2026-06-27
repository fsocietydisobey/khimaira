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
import {
  MOCK_MODE,
  MOCK_NODE_DETAIL,
  NODE_URL,
  type GraphEdge,
  type GraphFact,
  type GraphNodeDetail,
} from "./kgTypes";

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

interface KgNodeInspectorProps {
  nodeId: string;
  type: string;
  label: string;
  badge?: string | number;
  onClose: () => void;
}

export function KgNodeInspector({
  nodeId,
  type,
  label,
  badge,
  onClose,
}: KgNodeInspectorProps) {
  return (
    <div className="flex h-full flex-col border-l border-border bg-card/40">
      {/* Header */}
      <div className="flex items-start justify-between border-b border-border px-3 py-2 gap-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <p className="text-[10px] uppercase tracking-wider text-muted-foreground">graph node</p>
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
        <FactsPanel nodeId={nodeId} />
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

function FactsPanel({ nodeId }: { nodeId: string }) {
  const [state, setState] = useState<LoadState>({ status: "idle" });

  useEffect(() => {
    setState({ status: "loading" });
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

    // P3 wiring: same-origin daemon node-detail endpoint.
    const url = `${NODE_URL}/${encodeURIComponent(nodeId)}`;
    fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((json) => {
        if (!cancelled) setState({ status: "ok", detail: json.data as GraphNodeDetail });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: String(err) });
      });

    return () => {
      cancelled = true;
    };
  }, [nodeId]);

  if (state.status === "idle" || state.status === "loading") {
    return <p className="text-xs text-muted-foreground">loading facts…</p>;
  }
  if (state.status === "error") {
    return <p className="text-xs text-destructive">{state.message}</p>;
  }

  const { detail } = state;
  const current = detail.currentFacts.filter((f) => !f.deprecated);
  const historical = detail.historyFacts.filter((f) => f.deprecated);

  return (
    <>
      {current.length > 0 ? (
        <section>
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">
            current facts ({current.length})
          </p>
          <div className="space-y-1.5">
            {current.map((fact, i) => (
              <FactRow key={i} fact={fact} />
            ))}
          </div>
        </section>
      ) : (
        <p className="text-[11px] text-muted-foreground italic">No current facts.</p>
      )}

      {historical.length > 0 ? (
        <section>
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5 mt-3">
            history ({historical.length})
          </p>
          <div className="space-y-1.5">
            {historical.map((fact, i) => (
              <FactRow key={i} fact={fact} dimmed />
            ))}
          </div>
        </section>
      ) : null}

      {detail.edgesFrom.length > 0 || detail.edgesTo.length > 0 ? (
        <EdgeList edgesFrom={detail.edgesFrom} edgesTo={detail.edgesTo} />
      ) : null}
    </>
  );
}

function FactRow({ fact, dimmed = false }: { fact: GraphFact; dimmed?: boolean }) {
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
              <Badge key={k} variant="outline" className="text-[10px] py-0 px-1">
                {k}:{String(v)}
              </Badge>
            ))}
          </div>
        ) : null}
      </div>
      <p className="font-mono text-muted-foreground mt-0.5 break-all">
        {fact.value === null ? <span className="italic">null</span> : String(fact.value)}
      </p>
    </div>
  );
}

function EdgeList({
  edgesFrom,
  edgesTo,
}: {
  edgesFrom: GraphEdge[];
  edgesTo: GraphEdge[];
}) {
  return (
    <section>
      <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5 mt-3">
        active edges
      </p>
      <Card>
        <CardContent className="py-2 px-3 space-y-1">
          {edgesFrom.map((e, i) => (
            <EdgeRow key={`f-${i}`} edge={e} direction="→" />
          ))}
          {edgesTo.map((e, i) => (
            <EdgeRow key={`t-${i}`} edge={e} direction="←" />
          ))}
        </CardContent>
      </Card>
    </section>
  );
}

function EdgeRow({ edge, direction }: { edge: GraphEdge; direction: "→" | "←" }) {
  return (
    <div className="flex items-center gap-2 text-[11px]">
      <span className="text-muted-foreground font-mono">{direction}</span>
      <span className="font-mono font-medium" style={{ color: typeColor(edge.type) }}>
        {edge.type}
      </span>
      {edge.weight !== undefined ? (
        <span className="text-muted-foreground/60 ml-auto font-mono">
          {Math.round(edge.weight * 100)}%
        </span>
      ) : null}
    </div>
  );
}
