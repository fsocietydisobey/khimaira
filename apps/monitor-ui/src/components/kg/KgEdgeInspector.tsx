/**
 * KgEdgeInspector — edge-detail (provenance) side panel for the graph viewer.
 *
 * The "why does this edge exist?" surface for an LLM-extracted graph. Fetches
 * the generic GraphEdgeDetail from the daemon proxy
 * (/api/graph/<project>/edge/<id>?scope=…) and renders the opaque provenance
 * `meta` (match method, source doc/page/bbox, confidence, link origin) with no
 * schema knowledge. Endpoints are clickable — jump to the from/to node.
 *
 * Edge ids are an adapter capability: a project whose graph edges don't carry
 * ids simply never opens this panel (the canvas click is a no-op). When ids are
 * present, this is the provenance drill-in.
 */

import { useEffect, useState } from "react";

import { Button } from "@/components/ui/button";
import { typeColor, typeColorAlpha } from "./graphStyle";
import { CopyJsonButton } from "./CopyJsonButton";
import { GRAPH_URL, MOCK_MODE, type GraphEdgeDetail } from "./kgTypes";

interface KgEdgeInspectorProps {
  project: string | undefined;
  scope: string;
  edgeId: string;
  /** Jump the canvas + node panel to one of this edge's endpoints. */
  onNavigateNode: (nodeId: string) => void;
  onClose: () => void;
}

type LoadState =
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ok"; detail: GraphEdgeDetail };

const MOCK_EDGE_DETAIL: GraphEdgeDetail = {
  id: "mock-edge",
  type: "belongs-to",
  from: "task-1",
  to: "ws-1",
  weight: 0.82,
  meta: {
    match_method: "fuzzy",
    source_doc: "quote.pdf",
    page: 3,
    confidence: 0.82,
  },
};

export function KgEdgeInspector({
  project,
  scope,
  edgeId,
  onNavigateNode,
  onClose,
}: KgEdgeInspectorProps) {
  const [state, setState] = useState<LoadState>({ status: "loading" });

  useEffect(() => {
    setState({ status: "loading" });
    let cancelled = false;

    if (MOCK_MODE) {
      const id = setTimeout(() => {
        if (!cancelled) setState({ status: "ok", detail: MOCK_EDGE_DETAIL });
      }, 150);
      return () => {
        cancelled = true;
        clearTimeout(id);
      };
    }

    if (!project) {
      setState({ status: "error", message: "no project in route" });
      return;
    }

    const url =
      `${GRAPH_URL}/${encodeURIComponent(project)}/edge/${encodeURIComponent(edgeId)}` +
      `?scope=${encodeURIComponent(scope)}`;
    fetch(url)
      .then((r) => {
        if (!r.ok) throw new Error(`${r.status} ${r.statusText}`);
        return r.json();
      })
      .then((json) => {
        if (!cancelled)
          setState({ status: "ok", detail: json.data as GraphEdgeDetail });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: String(err) });
      });

    return () => {
      cancelled = true;
    };
  }, [project, scope, edgeId]);

  return (
    <div className="flex h-full flex-col border-l border-border bg-card/40">
      {/* Header */}
      <div className="flex items-start justify-between border-b border-border px-3 py-2 gap-2">
        <div className="min-w-0">
          <p className="text-[10px] uppercase tracking-wider text-muted-foreground">
            graph edge
          </p>
          <p className="text-[10px] text-muted-foreground/70 font-mono break-all mt-0.5">
            {edgeId}
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>
          ×
        </Button>
      </div>

      {/* Body */}
      <div className="flex-1 overflow-auto p-3 space-y-3">
        {state.status === "loading" ? (
          <p className="text-xs text-muted-foreground">loading edge…</p>
        ) : state.status === "error" ? (
          <p className="text-xs text-destructive">
            Failed to load edge: {state.message}
          </p>
        ) : (
          <EdgeDetailBody
            detail={state.detail}
            onNavigateNode={onNavigateNode}
          />
        )}
      </div>
    </div>
  );
}

function EdgeDetailBody({
  detail,
  onNavigateNode,
}: {
  detail: GraphEdgeDetail;
  onNavigateNode: (nodeId: string) => void;
}) {
  const metaEntries = detail.meta ? Object.entries(detail.meta) : [];
  return (
    <>
      {/* Relation type + weight */}
      <div className="flex items-center gap-2 flex-wrap">
        <span
          className="inline-flex items-center rounded-md border px-1.5 py-0 text-[11px] font-medium"
          style={{
            backgroundColor: typeColorAlpha(detail.type, 0.15),
            color: typeColor(detail.type),
            borderColor: typeColorAlpha(detail.type, 0.4),
          }}
        >
          {detail.type}
        </span>
        {detail.weight !== undefined ? (
          <span className="text-[11px] font-mono text-muted-foreground">
            weight {Math.round(detail.weight * 100)}%
          </span>
        ) : null}
      </div>

      {/* Endpoints — clickable */}
      <section>
        <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">
          endpoints
        </p>
        <div className="space-y-1">
          <EndpointRow
            label="from"
            nodeId={detail.from}
            onNavigateNode={onNavigateNode}
          />
          <EndpointRow
            label="to"
            nodeId={detail.to}
            onNavigateNode={onNavigateNode}
          />
        </div>
      </section>

      {/* Provenance */}
      <section>
        <p className="text-[10px] uppercase tracking-wider text-muted-foreground mb-1.5">
          provenance ({metaEntries.length})
        </p>
        {metaEntries.length > 0 ? (
          <div className="space-y-1">
            {metaEntries.map(([k, v]) => (
              <div
                key={k}
                className="flex items-baseline justify-between gap-2 rounded-md border border-border bg-card/60 px-2.5 py-1 text-[11px]"
              >
                <span className="font-mono text-muted-foreground">{k}</span>
                <span className="font-mono text-foreground/90 break-all text-right">
                  {v === null ? (
                    <span className="italic">null</span>
                  ) : (
                    String(v)
                  )}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <p className="text-[11px] text-muted-foreground italic">
            No provenance recorded for this edge.
          </p>
        )}
      </section>

      <div className="pt-1">
        <CopyJsonButton value={detail} />
      </div>
    </>
  );
}

function EndpointRow({
  label,
  nodeId,
  onNavigateNode,
}: {
  label: string;
  nodeId: string;
  onNavigateNode: (nodeId: string) => void;
}) {
  return (
    <button
      type="button"
      onClick={() => onNavigateNode(nodeId)}
      title={`focus ${nodeId}`}
      className="flex w-full items-center gap-2 rounded-md border border-border bg-card/60 px-2.5 py-1 text-[11px] text-left hover:bg-accent/40 transition-colors"
    >
      <span className="text-muted-foreground uppercase tracking-wider text-[9px] w-8 shrink-0">
        {label}
      </span>
      <span className="font-mono text-foreground/90 break-all">{nodeId}</span>
    </button>
  );
}
