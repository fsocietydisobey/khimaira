import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

export interface Connection {
  var: string;
  host: string;
  database: string;
}

export interface Project {
  name: string;
  path: string;
  detected_via: "pyproject" | "source-scan";
  has_pyproject: boolean;
  connections: Connection[];
}

export interface TopologyEdge {
  source: string;
  target: string;
}

export interface NodeMeta {
  role: string;     // empty when not classified
  label: string;    // empty → use raw node name
  summary: string;  // empty → no description available yet
}

export interface TopologyGraph {
  name: string;
  label?: string;
  summary?: string;
  role?: "orchestrator" | "subgraph" | "leaf" | "";
  source: "introspection" | "ast";
  approximate: boolean;
  error: string | null;
  layout?: "TB" | "BT" | "LR" | "RL";
  invokes?: Record<string, string>;
  nodes: string[];
  node_meta?: Record<string, NodeMeta>;
  edges: TopologyEdge[];
  mermaid: string;
}

export interface TopologyResponse {
  project: string;
  graphs: TopologyGraph[];
  combined_mermaid?: string;
  scan_status?: "enriched" | "stale" | "none";
  summary?: string;
}

export type ThreadStatus = "paused" | "running" | "starting" | "idle";

export interface ThreadSummary {
  thread_id: string;
  latest_checkpoint_id: string;
  last_updated: string | null;
  step: number | null;
  status: ThreadStatus;
  current_node: string | null;
  recent_nodes: string[];
  agent_profile: string | null;
  phase: string | null;
  // Generic grouping fields — see backend's parse_grouping +
  // metadata.thread_grouping. Always present.
  scope_kind: string;
  scope_id: string;
  stage: string;
  stage_detail: string;
}

/**
 * Project-specific run-clustering rule, derived by the LLM scan from
 * sample thread_ids and codebase context. Null when no scan has landed
 * yet — UI falls back to a trailing-UUID + 5min heuristic.
 *
 * Pattern is JavaScript regex; first capture group is the cluster key.
 * Threads where the pattern matches and produces the same key cluster
 * together. Misses fall into time-proximity grouping.
 */
export interface RunClustering {
  source_field: "thread_id" | "scope_id" | "stage" | "stage_detail";
  pattern: string | null;
  time_window_seconds: number;
  run_label: string;
}

export interface ThreadsResponse {
  project: string;
  limit: number;
  offset: number;
  since: string | null;
  scope_label: string;             // "Deliverable", "Run", "Chain", ...
  run_clustering: RunClustering | null;
  /**
   * Per-project running threshold in seconds. Frontend's stale/stuck
   * classifier scales its thresholds against this — projects with slow
   * nodes get correspondingly later "stale"/"stuck" flags so the badges
   * don't false-fire during legitimately long node executions.
   * Default 300 when no metadata scan has provided a value.
   */
  running_threshold_seconds: number;
  threads: ThreadSummary[];
}

export interface CheckpointDetail {
  checkpoint_id: string;
  parent_checkpoint_id: string | null;
  created_at: string | null;
  step: number | null;
  node: string | null;
  state: unknown;
  metadata: unknown;
}

export interface ThreadDetailResponse {
  project: string;
  thread_id: string;
  checkpoints: CheckpointDetail[];
}

// ---------------------------------------------------------------------------
// Observer / heartbeat API — populated by chimera_observer (v0.4.0+) on the
// app side, served by chimera-monitor. Wraps three new endpoints landed in
// commit 77156cf:
//   GET /api/heartbeats/{project}/cost
//   GET /api/heartbeats/{project}/slow
//   GET /api/heartbeats/{project}/by-correlation/{cid}
//
// The cost dashboard + trace waterfall views consume these.
// ---------------------------------------------------------------------------

export interface CostByModel {
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
  calls: number;
}

export interface CostSummary {
  project: string;
  run_count: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: number;
  by_model: Record<string, CostByModel>;
  telemetry_calls_langsmith: number;
  note: string;
}

export interface CostBucket {
  ts_start: number;     // unix seconds, inclusive
  ts_end: number;       // unix seconds, exclusive
  cost_usd: number;
  llm_calls: number;
}

export interface CostTimeseries {
  project: string;
  bucket_minutes: number;
  window_minutes: number;
  buckets: CostBucket[];
}

export interface SlowCall {
  kind: "chain" | "llm" | "tool" | "external";
  run_id: string;
  name: string | null;
  started_ts: number;
  ended_ts: number | null;
  in_flight: boolean;
  duration_ms: number;
  threshold_ms: number;
  project: string;
  correlation_id: string | null;
  extra: Record<string, unknown> | null;
}

export interface SlowCallsResponse {
  project: string;
  thresholds: Record<string, number>;
  count: number;
  slow: SlowCall[];
}

export interface ObserverEvent {
  project: string;
  run_id: string | null;
  parent_run_id: string | null;
  name: string | null;
  event: string;            // "chain_start" | "chain_end" | "llm_start" | "llm_end" |
                            // "tool_start" | "tool_end" | "external_start" |
                            // "external_end" | "external_error" | <error variants>
  ts: number;               // unix seconds, fractional
  extra: Record<string, unknown> | null;
  correlation_id: string | null;
}

export interface CorrelationResponse {
  project: string;
  correlation_id: string;
  event_count: number;
  events: ObserverEvent[];
}

// RTK Query slice. Live updates are via `pollingInterval: 2000` per query
// (not SSE — that lands in Phase 2). Two seconds is the spec's "live update
// within 2s" target, met identically by polling at this volume.
export const monitorApi = createApi({
  reducerPath: "monitorApi",
  baseQuery: fetchBaseQuery({ baseUrl: "/api" }),
  tagTypes: ["Projects", "Topology", "Threads"],
  endpoints: (build) => ({
    listProjects: build.query<Project[], void>({
      query: () => "/projects",
      providesTags: ["Projects"],
    }),
    getTopology: build.query<TopologyResponse, string>({
      query: (name) => `/topology/${encodeURIComponent(name)}`,
      providesTags: (_r, _e, name) => [{ type: "Topology", id: name }],
    }),
    listThreads: build.query<
      ThreadsResponse,
      { name: string; limit?: number; offset?: number }
    >({
      query: ({ name, limit = 50, offset = 0 }) =>
        `/threads/${encodeURIComponent(name)}?limit=${limit}&offset=${offset}`,
      providesTags: (_r, _e, { name }) => [{ type: "Threads", id: name }],
    }),
    getThreadDetail: build.query<
      ThreadDetailResponse,
      { name: string; threadId: string; limit?: number }
    >({
      query: ({ name, threadId, limit = 20 }) =>
        `/threads/${encodeURIComponent(name)}/${encodeURIComponent(threadId)}?limit=${limit}`,
    }),
    getCostSummary: build.query<CostSummary, string>({
      query: (project) => `/heartbeats/${encodeURIComponent(project)}/cost`,
    }),
    getCostTimeseries: build.query<
      CostTimeseries,
      { project: string; bucketMinutes?: number; windowMinutes?: number }
    >({
      query: ({ project, bucketMinutes = 5, windowMinutes = 60 }) =>
        `/heartbeats/${encodeURIComponent(project)}/cost/timeseries` +
        `?bucket_minutes=${bucketMinutes}&window_minutes=${windowMinutes}`,
    }),
    getSlowCalls: build.query<
      SlowCallsResponse,
      { project: string; chain?: number; llm?: number; tool?: number; external?: number }
    >({
      query: ({ project, chain, llm, tool, external }) => {
        const params = new URLSearchParams();
        if (chain !== undefined) params.set("chain", String(chain));
        if (llm !== undefined) params.set("llm", String(llm));
        if (tool !== undefined) params.set("tool", String(tool));
        if (external !== undefined) params.set("external", String(external));
        const qs = params.toString();
        return `/heartbeats/${encodeURIComponent(project)}/slow${qs ? `?${qs}` : ""}`;
      },
    }),
    getEventsByCorrelation: build.query<
      CorrelationResponse,
      { project: string; correlationId: string }
    >({
      query: ({ project, correlationId }) =>
        `/heartbeats/${encodeURIComponent(project)}/by-correlation/${encodeURIComponent(correlationId)}`,
    }),
  }),
});

export const {
  useListProjectsQuery,
  useGetTopologyQuery,
  useListThreadsQuery,
  useGetThreadDetailQuery,
  useGetCostSummaryQuery,
  useGetCostTimeseriesQuery,
  useGetSlowCallsQuery,
  useGetEventsByCorrelationQuery,
} = monitorApi;
