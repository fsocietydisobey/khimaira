import { createApi, fetchBaseQuery } from "@reduxjs/toolkit/query/react";

import type {
  ChatMessage,
  CodeSource,
  Note,
  NotebookTab,
  NotebookTabKind,
  NoteKind,
  NotePriority,
  NoteStatus,
  NoteTestStatus,
  Ticket,
  TicketAssignee,
  TicketState,
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
  tagTypes: ["Projects", "Topology", "Threads", "Notes", "Tabs", "ChatHistory", "Tickets"],
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
      | {
          tabId?: string;
          repo?: string;
          kind?: NoteKind;
          priority?: NotePriority;
          sort?: string;
          /** File-manager Starred rail. */
          starred?: boolean;
          /** Human testing-workflow status (notes only). */
          testStatus?: NoteTestStatus;
        }
      | void
    >({
      query: (arg) => {
        const params = new URLSearchParams();
        if (arg?.tabId) params.set("tab_id", arg.tabId);
        if (arg?.repo) params.set("repo", arg.repo);
        if (arg?.kind) params.set("kind", arg.kind);
        if (arg?.priority) params.set("priority", arg.priority);
        if (arg?.sort) params.set("sort", arg.sort);
        if (arg?.starred) params.set("starred", "true");
        if (arg?.testStatus) params.set("test_status", arg.testStatus);
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
      // The single-note fetch returns `history` (full version bodies), not
      // the list endpoint's pre-computed `history_count` — normalize here so
      // every consumer sees the same `Note` shape regardless of which
      // endpoint it came from (caught via live verification: this is the
      // single-note endpoint's real payload, not a guess).
      transformResponse: (raw: Note & { history?: unknown[] }): Note => ({
        ...raw,
        history_count: raw.history?.length ?? raw.history_count ?? 0,
      }),
      providesTags: (_r, _e, id) => [{ type: "Notes", id }],
    }),
    createNote: build.mutation<
      Note,
      {
        raw_text: string;
        tab_id?: string;
        title?: string;
        repo?: string;
        /** Grimoire — marks the note credential-safe: the backend redacts a
         *  `llm_text` twin at write time; every LLM egress path routes
         *  through it, `raw_text` stays the real, human-readable text. */
        sensitive?: boolean;
        /** Default "note" server-side — pass "study_guide" for a manually-
         *  authored guide (FILE-MANAGER "+ New guide"); the backend routes
         *  to the guide pipeline (abstract/tags/entities only, raw_text
         *  never re-expressed) instead of the note structuring pipeline. */
        kind?: NoteKind;
      }
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
        /** Grimoire — user-set importance, independent of `status`. */
        priority?: NotePriority;
        /** File-manager — a manual drag-move sends `tab_id` + this together. */
        pinned_placement?: boolean;
        /** File-manager — the Starred rail toggle. */
        starred?: boolean;
        /** Human testing-workflow status, independent of `status`. Notes only. */
        test_status?: NoteTestStatus;
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
    // Grimoire (Phase 3 chat redesign + CHAT-UNIFY): one persistent per-
    // record chat (guide OR note — the guide-only 404 was lifted) + one
    // one-shot notebook-wide chat (nothing open). Both are async (job+poll)
    // since every turn is a slow agentic/retrieval call — the send
    // mutations only kick off the job; pollChatJob is the shared poll loop.
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
    // Notebook-wide (nothing open) — one-shot, no server-side history
    // (MVP per CHAT-UNIFY.md); `refs` = @-referenced note ids.
    sendNotebookChat: build.mutation<
      { job_id: string; status: string },
      { message: string; refs?: string[]; max_budget_usd?: number }
    >({
      query: (body) => ({ url: "/notes/chat", method: "POST", body }),
    }),
    // Same poll route for both kinds ("chat" = per-record, "notebook_chat" =
    // notebook-wide) — the two response shapes are genuinely different
    // (per-record nests under `message`/`grounding`; notebook-wide is
    // answer_question's flat {answer, sources, ...}), so every field below
    // is optional and each caller destructures only the fields its own kind
    // produces.
    pollChatJob: build.query<
      {
        status: "pending" | "done" | "error";
        // per-record (guide/note) chat shape:
        message?: { content: string; edit?: ChatMessage["edit"] };
        grounding?: ChatMessage["grounding"];
        total_cost_usd?: number | null;
        // notebook-wide shape (answer_question's):
        answer?: string;
        sources?: string[];
        healed?: string[];
        code_sources?: CodeSource[];
        code_unavailable?: string[];
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
    createTab: build.mutation<
      NotebookTab,
      { title?: string; kind?: NotebookTabKind; parent_id?: string | null }
    >({
      query: (body) => ({ url: "/tabs", method: "POST", body }),
      invalidatesTags: [{ type: "Tabs", id: "LIST" }],
    }),
    // File-manager (2026-07-04): `parent_id` reparents (undefined = don't
    // touch, null = move to root — the backend distinguishes via
    // exclude_unset, so never send `parent_id: undefined` as a literal key).
    // 422 (cycle / cross-kind-nesting / sibling-name collision) surfaces as
    // `err.data.detail` on the rejected `.unwrap()` promise — callers must
    // catch and show it, RTK Query won't do that for you.
    updateTab: build.mutation<
      NotebookTab,
      { id: string; title?: string; kind?: NotebookTabKind; parent_id?: string | null }
    >({
      query: ({ id, ...body }) => ({ url: `/tabs/${encodeURIComponent(id)}`, method: "PATCH", body }),
      invalidatesTags: (_r, _e, { id }) => [
        { type: "Tabs", id },
        { type: "Tabs", id: "LIST" },
        { type: "Notes", id: "LIST" },
      ],
    }),
    // Re-files child tabs + direct member notes server-side (never loses a
    // guide) — invalidate both Tabs and Notes lists, not just Tabs.
    deleteTab: build.mutation<{ id: string; deleted: boolean }, string>({
      query: (id) => ({ url: `/tabs/${encodeURIComponent(id)}`, method: "DELETE" }),
      invalidatesTags: [{ type: "Tabs", id: "LIST" }, { type: "Notes", id: "LIST" }],
    }),

    // -----------------------------------------------------------------------
    // Tickets (2026-07-11) — local mirror of Linear issues (kind="ticket"),
    // its own resource surface (/api/tickets*, not folded into /notes*) since
    // its filters and read-only-synced-field semantics don't apply to notes/
    // guides. See notebookTypes.ts's Ticket section for the field contract.
    // -----------------------------------------------------------------------
    listTickets: build.query<
      { tickets: Ticket[] },
      { project?: string; state?: TicketState; assignee?: string; label?: string } | void
    >({
      query: (arg) => {
        const params = new URLSearchParams();
        if (arg?.project) params.set("project", arg.project);
        if (arg?.state) params.set("state", arg.state);
        if (arg?.assignee) params.set("assignee", arg.assignee);
        if (arg?.label) params.set("label", arg.label);
        const qs = params.toString();
        return qs ? `/tickets?${qs}` : "/tickets";
      },
      providesTags: (result) =>
        result
          ? [
              ...result.tickets.map((t) => ({ type: "Tickets" as const, id: t.id })),
              { type: "Tickets" as const, id: "LIST" },
            ]
          : [{ type: "Tickets" as const, id: "LIST" }],
    }),
    getTicket: build.query<Ticket, string>({
      query: (id) => `/tickets/${encodeURIComponent(id)}`,
      providesTags: (_r, _e, id) => [{ type: "Tickets", id }],
    }),
    // `idempotency_key` (2026-07-11, c3f0ccf): always sent — the daemon
    // dedups a retry with the same key instead of creating a duplicate (the
    // exact bug chimera-0 hit on notebook_create_study_guide). Callers pass
    // a fresh key per logical create attempt (e.g. crypto.randomUUID()) and
    // MUST reuse the same key if they retry after a failed/timed-out call.
    createTicket: build.mutation<
      Ticket,
      {
        title: string;
        description?: string;
        state?: TicketState;
        project?: string;
        labels?: string[];
        idempotency_key: string;
      }
    >({
      query: (body) => ({ url: "/tickets", method: "POST", body }),
      invalidatesTags: [{ type: "Tickets", id: "LIST" }],
    }),
    // 422 (linear-pulled synced-field is read-only) surfaces as
    // `err.data.detail` on the rejected `.unwrap()` promise, same convention
    // as updateTab's cycle/kind/sibling-uniqueness errors — callers must
    // catch and show it, RTK Query won't do that for you.
    updateTicket: build.mutation<
      Ticket,
      {
        id: string;
        title?: string;
        raw_text?: string;
        state?: TicketState;
        linear_priority?: number;
        assignee?: TicketAssignee | null;
        labels?: string[];
        project?: string;
        parent_id?: string | null;
        links?: string[];
        tab_id?: string;
      }
    >({
      query: ({ id, ...body }) => ({ url: `/tickets/${encodeURIComponent(id)}`, method: "PATCH", body }),
      invalidatesTags: (_r, _e, { id }) => [
        { type: "Tickets", id },
        { type: "Tickets", id: "LIST" },
      ],
    }),
    addTicketComment: build.mutation<Ticket, { id: string; text: string; author?: string }>({
      query: ({ id, ...body }) => ({
        url: `/tickets/${encodeURIComponent(id)}/comments`,
        method: "POST",
        body,
      }),
      invalidatesTags: (_r, _e, { id }) => [{ type: "Tickets", id }],
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
  useGetChatHistoryQuery,
  useSendChatMessageMutation,
  useSendNotebookChatMutation,
  usePollChatJobQuery,
  useClearChatMutation,
  useCompactChatMutation,
  useDeleteNoteMutation,
  useListTabsQuery,
  useCreateTabMutation,
  useUpdateTabMutation,
  useDeleteTabMutation,
  useListTicketsQuery,
  useGetTicketQuery,
  useCreateTicketMutation,
  useUpdateTicketMutation,
  useAddTicketCommentMutation,
} = monitorApi;
