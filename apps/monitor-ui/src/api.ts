import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import type {
  AskAnswer,
  ChatMessage,
  Note,
  NotebookTab,
  NoteKind,
  NoteStatus,
} from "@/components/notebook/notebookTypes";

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
// Observer / heartbeat API — populated by khimaira_observer (v0.4.0+) on the
// app side, served by khimaira-monitor. Wraps three new endpoints landed in
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
  tagTypes: ["Projects", "Topology", "Threads", "Notes", "Tabs", "ChatHistory"],
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

    // -----------------------------------------------------------------------
    // Notebook (Phase 1a backend: packages/khimaira/.../monitor/notes.py +
    // api/notebook.py). Global daemon state — not scoped by project, despite
    // the per-project route the frontend mounts it under.
    // -----------------------------------------------------------------------
    listNotes: build.query<
      { notes: Note[] },
      { tabId?: string; repo?: string; kind?: NoteKind } | void
    >({
      query: (arg) => {
        const params = new URLSearchParams();
        if (arg?.tabId) params.set("tab_id", arg.tabId);
        if (arg?.repo) params.set("repo", arg.repo);
        if (arg?.kind) params.set("kind", arg.kind);
        const qs = params.toString();
        return qs ? `/notes?${qs}` : "/notes";
      },
      providesTags: (result) =>
        result
          ? [
              ...result.notes.map((n) => ({ type: "Notes" as const, id: n.id })),
              { type: "Notes" as const, id: "LIST" },
            ]
          : [{ type: "Notes" as const, id: "LIST" }],
    }),
    getNote: build.query<Note, string>({
      query: (id) => `/notes/${encodeURIComponent(id)}`,
      providesTags: (_r, _e, id) => [{ type: "Notes", id }],
    }),
    createNote: build.mutation<
      Note,
      { raw_text: string; tab_id?: string; title?: string; repo?: string }
    >({
      query: (body) => ({ url: "/notes", method: "POST", body }),
      invalidatesTags: [{ type: "Notes", id: "LIST" }, { type: "Tabs", id: "LIST" }],
    }),
    updateNote: build.mutation<
      Note,
      {
        id: string;
        title?: string;
        tab_id?: string;
        status?: NoteStatus;
        repo?: string;
        /** Grimoire (Phase 3): applying a REVISE proposal is a plain raw_text
         *  PATCH — never auto-applied, always the result of an explicit Accept. */
        raw_text?: string;
      }
    >({
      query: ({ id, ...body }) => ({ url: `/notes/${encodeURIComponent(id)}`, method: "PATCH", body }),
      invalidatesTags: (_r, _e, { id }) => [
        { type: "Notes", id },
        { type: "Notes", id: "LIST" },
        { type: "Tabs", id: "LIST" },
      ],
    }),
    promoteNote: build.mutation<Note, string>({
      query: (id) => ({ url: `/notes/${encodeURIComponent(id)}/promote`, method: "POST" }),
      invalidatesTags: (_r, _e, id) => [{ type: "Notes", id }, { type: "Notes", id: "LIST" }],
    }),
    revalidateNote: build.mutation<Note, string>({
      query: (id) => ({ url: `/notes/${encodeURIComponent(id)}/revalidate`, method: "POST" }),
      invalidatesTags: (_r, _e, id) => [{ type: "Notes", id }, { type: "Notes", id: "LIST" }],
    }),
    askNotebook: build.mutation<
      AskAnswer,
      { question: string; repo?: string; note_ids?: string[]; exclusive?: boolean }
    >({
      query: (body) => ({ url: "/notes/ask", method: "POST", body }),
      // A heal during ask() changes the cited notes' pipeline — refresh the list
      // so their cards show the corrected content next render.
      invalidatesTags: (result) =>
        result && result.healed.length > 0
          ? [
              ...result.healed.map((id) => ({ type: "Notes" as const, id })),
              { type: "Notes" as const, id: "LIST" },
            ]
          : [],
    }),
    // Grimoire (Phase 3 chat redesign, tasks/grimoire/CHAT-MODEL.md): a
    // per-guide persistent chat. Sending a turn is async (job+poll, reused
    // from the retired toolbar's engine) since every turn is a slow
    // agentic call — sendChatMessage only kicks off the job; pollChatJob
    // is the caller's own polling loop against it.
    getChatHistory: build.query<{ history: ChatMessage[] }, string>({
      query: (id) => `/notes/${encodeURIComponent(id)}/chat`,
      providesTags: (_r, _e, id) => [{ type: "ChatHistory", id }],
    }),
    sendChatMessage: build.mutation<
      { job_id: string; status: string },
      { id: string; message: string; max_budget_usd?: number }
    >({
      query: ({ id, ...body }) => ({
        url: `/notes/${encodeURIComponent(id)}/chat`,
        method: "POST",
        body,
      }),
    }),
    pollChatJob: build.query<
      {
        status: "pending" | "done" | "error";
        message?: { content: string; edit?: ChatMessage["edit"] };
        grounding?: ChatMessage["grounding"];
        total_cost_usd?: number | null;
        error?: string;
      },
      string
    >({
      query: (jobId) => `/notes/research/${encodeURIComponent(jobId)}`,
    }),
    clearChat: build.mutation<{ cleared: boolean }, string>({
      query: (id) => ({ url: `/notes/${encodeURIComponent(id)}/chat/clear`, method: "POST" }),
      invalidatesTags: (_r, _e, id) => [{ type: "ChatHistory", id }],
    }),
    compactChat: build.mutation<{ compacted: boolean; message_count: number }, string>({
      query: (id) => ({ url: `/notes/${encodeURIComponent(id)}/chat/compact`, method: "POST" }),
      invalidatesTags: (_r, _e, id) => [{ type: "ChatHistory", id }],
    }),
    deleteNote: build.mutation<{ id: string; deleted: boolean }, string>({
      query: (id) => ({ url: `/notes/${encodeURIComponent(id)}`, method: "DELETE" }),
      invalidatesTags: (_r, _e, id) => [
        { type: "Notes", id },
        { type: "Notes", id: "LIST" },
        { type: "Tabs", id: "LIST" },
      ],
    }),
    listTabs: build.query<{ tabs: NotebookTab[] }, void>({
      query: () => "/tabs",
      providesTags: (result) =>
        result
          ? [
              ...result.tabs.map((t) => ({ type: "Tabs" as const, id: t.id })),
              { type: "Tabs" as const, id: "LIST" },
            ]
          : [{ type: "Tabs" as const, id: "LIST" }],
    }),
    createTab: build.mutation<NotebookTab, { title?: string }>({
      query: (body) => ({ url: "/tabs", method: "POST", body }),
      invalidatesTags: [{ type: "Tabs", id: "LIST" }],
    }),
    updateTab: build.mutation<NotebookTab, { id: string; title: string }>({
      query: ({ id, ...body }) => ({ url: `/tabs/${encodeURIComponent(id)}`, method: "PATCH", body }),
      invalidatesTags: (_r, _e, { id }) => [{ type: "Tabs", id }, { type: "Tabs", id: "LIST" }],
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
  useListNotesQuery,
  useGetNoteQuery,
  useCreateNoteMutation,
  useUpdateNoteMutation,
  usePromoteNoteMutation,
  useRevalidateNoteMutation,
  useAskNotebookMutation,
  useGetChatHistoryQuery,
  useSendChatMessageMutation,
  usePollChatJobQuery,
  useClearChatMutation,
  useCompactChatMutation,
  useDeleteNoteMutation,
  useListTabsQuery,
  useCreateTabMutation,
  useUpdateTabMutation,
} = monitorApi;
