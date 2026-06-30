/**
 * Generic graph contract for the KG mapper — khimaira-owned, framework-neutral.
 *
 * This is layer 1 of the code-agnostic architecture (mirrors how the LangGraph
 * monitor renders ANY attached project). There are ZERO jeevy schema terms here:
 * `type` is an opaque category string and the renderer styles it deterministically
 * (see graphStyle.ts). A per-project adapter maps its own schema → this contract.
 *
 * Wire contract (served by the khimaira daemon at GET /api/graph/<project>?scope=…):
 *   { data: { nodes: GraphNode[], edges: GraphEdge[] } }
 *
 * Node-detail (provisional — wired at P3):
 *   { data: GraphNodeDetail }
 *
 * Flip MOCK_MODE to false once the daemon endpoint is live (held until P1/P3).
 */

// ---------------------------------------------------------------------------
// API endpoint constants — the single flip point for mock → live.
// The live endpoints are the khimaira DAEMON's generic graph routes (same-origin
// via the vite /api proxy → daemon 8740), NOT a per-project API. Wired at P3.
// ---------------------------------------------------------------------------

export const MOCK_MODE = false;

/** Generic graph endpoint base — append `/<project>?scope=<scope>`. */
export const GRAPH_URL = "/api/graph";

/** Generic node-detail endpoint base — append `/<project>/node/<id>`. Provisional. */
export const NODE_URL = "/api/graph/node";

// ---------------------------------------------------------------------------
// Generic wire shapes — no jeevy terms. `type` is an OPAQUE string.
// ---------------------------------------------------------------------------

export interface GraphNode {
  /** Stable unique id (adapter-defined). */
  id: string;
  /** Opaque category — drives deterministic color (graphStyle.typeColor). */
  type: string;
  /** Human-readable display text. */
  label: string;
  /** Optional corner chip (e.g. a count or short tag). */
  badge?: string | number;
}

export interface GraphEdge {
  /** Opaque stable id (adapter-defined) — addresses the edge for edge-detail. */
  id?: string;
  from: string;
  to: string;
  /** Opaque relation label — drives edge color. */
  type: string;
  /** Optional 0–1 emphasis (opacity/width). Defaults to 1 when absent. */
  weight?: number;
}

/** Edge provenance detail (the edge-debug surface). Jeevy provenance/source
 *  fields fold into the opaque `meta` map — no schema terms here. */
export interface GraphEdgeDetail {
  id: string;
  type: string;
  from: string;
  to: string;
  weight?: number;
  meta?: Record<string, string | number | boolean | null>;
}

/** A (fromType)-[linkType]->(toType) pattern that occurs in the graph. */
export interface GraphSchemaTriple {
  fromType: string;
  linkType: string;
  toType: string;
  count: number;
}

/** Type meta-graph — distinct node/link types + the triples that occur. The
 *  structural-gap finder: a missing triple = a relationship type never extracted. */
export interface GraphSchema {
  nodeTypes: string[];
  linkTypes: string[];
  triples: GraphSchemaTriple[];
}

export interface GraphContract {
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface GraphResponse {
  data: GraphContract;
}

// ---------------------------------------------------------------------------
// Generic node-detail shapes. A "fact" is an opaque key/value with optional
// metadata chips; the adapter folds its domain fields (trust tier, confidence,
// timestamps, …) into `meta`. No jeevy fact semantics leak into the viewer.
// ---------------------------------------------------------------------------

export interface GraphFact {
  /** Fact name (adapter-defined). */
  label: string;
  value: string | number | boolean | null;
  /** Optional small metadata chips rendered next to the fact. */
  meta?: Record<string, string | number | boolean | null>;
  /** When true, rendered dimmed (historical/superseded). */
  deprecated?: boolean;
}

export interface GraphNodeDetail {
  id: string;
  type: string;
  label: string;
  badge?: string | number;
  /** Current facts (rendered prominently). */
  currentFacts: GraphFact[];
  /** Historical/superseded facts (rendered dimmed). */
  historyFacts: GraphFact[];
  /** Outbound + inbound edges for the edge list. */
  edgesFrom: GraphEdge[];
  edgesTo: GraphEdge[];
}

export interface GraphNodeDetailResponse {
  data: GraphNodeDetail;
}

/**
 * Underlying SOURCE DB record behind a projected node — the "DB RECORD" peek.
 * Served by GET /api/graph/<project>/node/<id>/source?scope=… (daemon → adapter).
 *
 * `found:false` is a graceful-empty case (NOT an error): the node is out of
 * scope, OR its type is name/composite-keyed (part, bom-line, organization, …)
 * with no single source PK to resolve. `reason` explains which. Only PK-keyed
 * types (job, task, workstream, line_item, user, personnel) return a `row`.
 * Secrets are redacted server-side, so `row` is safe to render verbatim.
 */
export interface GraphNodeSource {
  found: boolean;
  node_id: string;
  node_type?: string;
  canonical_key?: string;
  table?: string;
  source_id?: string | number;
  row?: Record<string, string | number | boolean | null> | null;
  reason?: string;
}

export interface GraphNodeSourceResponse {
  data: GraphNodeSource;
  meta?: Record<string, string | number | null>;
}

// ---------------------------------------------------------------------------
// Mock fixture — generic-shaped. The sample VALUES are illustrative (a fab-shop
// graph), but the TYPES are open strings: the renderer handles any of them via
// graphStyle, proving the viewer is code-agnostic.
// ---------------------------------------------------------------------------

export const MOCK_GRAPH: GraphResponse = {
  data: {
    nodes: [
      { id: "ws-1", type: "workstream", label: "Structural Steel", badge: 3 },
      { id: "ws-2", type: "workstream", label: "Electrical Conduit", badge: 2 },
      { id: "job-1", type: "job", label: "JOB-2024-001", badge: 5 },
      { id: "task-1", type: "task", label: "Procure angle iron", badge: 4 },
      { id: "task-2", type: "task", label: "Cut to length", badge: 2 },
      { id: "task-3", type: "task", label: "Weld frame", badge: 6 },
      { id: "part-1", type: "part", label: "L2x2x3/16 Angle", badge: 7 },
      { id: "part-2", type: "part", label: '3/16" Plate 4×8', badge: 5 },
      { id: "pt-1", type: "part_type", label: "Angle Iron", badge: 1 },
      { id: "bom-1", type: "bom-line", label: "BOM: L2x2 × 12ft", badge: 3 },
      { id: "bom-2", type: "bom-line", label: "BOM: 3/16 Plate × 2", badge: 2 },
      { id: "vendor-1", type: "vendor", label: "Acme Metals", badge: 2 },
      { id: "doc-1", type: "document", label: "Acme Quote May-2024", badge: 4 },
      { id: "dt-1", type: "document_type", label: "Quote", badge: 1 },
      { id: "org-1", type: "organization", label: "FabCo LLC", badge: 1 },
      { id: "user-1", type: "user", label: "J. Smith", badge: 2 },
      { id: "shop-1", type: "shop", label: "FabCo Main Shop", badge: 1 },
    ],
    edges: [
      { from: "ws-1", to: "job-1", type: "belongs-to", weight: 0.98 },
      { from: "ws-2", to: "job-1", type: "belongs-to", weight: 0.95 },
      { from: "task-1", to: "ws-1", type: "belongs-to", weight: 0.97 },
      { from: "task-2", to: "ws-1", type: "subtask-of", weight: 0.9 },
      { from: "task-3", to: "ws-1", type: "subtask-of", weight: 0.88 },
      { from: "task-2", to: "task-1", type: "depends-on", weight: 0.82 },
      { from: "task-3", to: "task-2", type: "depends-on", weight: 0.79 },
      { from: "bom-1", to: "job-1", type: "belongs-to", weight: 1.0 },
      { from: "bom-2", to: "job-1", type: "belongs-to", weight: 1.0 },
      { from: "bom-1", to: "part-1", type: "for-part", weight: 1.0 },
      { from: "bom-2", to: "part-2", type: "for-part", weight: 1.0 },
      { from: "part-1", to: "pt-1", type: "has-type", weight: 0.95 },
      { from: "vendor-1", to: "part-1", type: "supplies", weight: 0.89 },
      { from: "doc-1", to: "bom-1", type: "appears-on", weight: 0.91 },
      { from: "doc-1", to: "vendor-1", type: "quotes", weight: 0.93 },
      { from: "doc-1", to: "dt-1", type: "has-document-type", weight: 1.0 },
      { from: "org-1", to: "shop-1", type: "owns", weight: 0.99 },
      { from: "user-1", to: "task-1", type: "created-by", weight: 1.0 },
      { from: "shop-1", to: "job-1", type: "owns", weight: 0.97 },
    ],
  },
};

export const MOCK_NODE_DETAIL: GraphNodeDetail = {
  id: "task-1",
  type: "task",
  label: "Procure angle iron",
  badge: 4,
  currentFacts: [
    {
      label: "status",
      value: "open",
      meta: { trust: "high", confidence: "99%", at: "2024-05-01 10:00" },
    },
    {
      label: "assignee",
      value: "jsmith",
      meta: { trust: "high", confidence: "95%", at: "2024-05-01 10:00" },
    },
    {
      label: "due_date",
      value: "2024-05-15",
      meta: { trust: "medium", confidence: "80%", at: "2024-05-02 08:30" },
    },
    {
      label: "priority",
      value: "high",
      meta: { trust: "high", confidence: "92%", at: "2024-05-01 10:00" },
    },
  ],
  historyFacts: [
    {
      label: "status",
      value: "draft",
      deprecated: true,
      meta: { trust: "high", confidence: "99%", at: "2024-04-28 09:00" },
    },
  ],
  edgesFrom: [],
  edgesTo: [],
};
