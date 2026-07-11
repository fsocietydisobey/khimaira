/**
 * TicketsView — browse-and-manage surface for the local Linear-issue mirror
 * (kind="ticket", packages/khimaira/.../monitor/notes.py "Public API —
 * tickets", commits a9ed277 + c3f0ccf). Structural sibling to LibraryView:
 * a browse state (filters + list, grouped by state) and a detail state
 * (full description, comments thread, read-only-synced-fields banner for a
 * linear-pulled ticket), same SidePanelShell-adjacent conventions
 * (usePersistedBoolean, relativeTime) as the rest of the notebook.
 *
 * Deliberately NO resync button. The daemon has no Linear API access (see
 * a9ed277's commit message + task-0ee33e7500c0) — there is no single
 * backend endpoint that "does" a resync. A resync is agent-orchestrated:
 * an agent calls mcp__linear__list_issues itself, maps the shape, then
 * calls ticket_bulk_upsert. The toolbar's "resync" affordance is a
 * copyable prompt telling Joseph what to ask an agent for, not a button
 * that implies infra we explicitly chose not to build.
 */

import { useMemo, useState } from "react";
import { ChevronLeft, Plus, RefreshCw } from "lucide-react";

import {
  useAddTicketCommentMutation,
  useCreateTicketMutation,
  useGetTicketQuery,
  useListTicketsQuery,
  useUpdateTicketMutation,
} from "@/api";
import { IdChip } from "@/components/notebook/IdChip";
import { CopyRawMarkdownButton } from "@/components/notebook/CopyRawMarkdownButton";
import { relativeTime } from "@/components/notebook/Notebook";
import type { Ticket, TicketState } from "@/components/notebook/notebookTypes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";

const TICKET_STATES: TicketState[] = [
  "Backlog",
  "Todo",
  "In Progress",
  "In Review",
  "Done",
  "Cancelled",
];

const STATE_EMOJI: Record<TicketState, string> = {
  Backlog: "📋",
  Todo: "📝",
  "In Progress": "🚧",
  "In Review": "👀",
  Done: "✅",
  Cancelled: "🚫",
};

const LINEAR_PRIORITY_LABEL: Record<number, string> = {
  0: "no priority",
  1: "🔴 urgent",
  2: "🟠 high",
  3: "🟡 medium",
  4: "⚪ low",
};

function priorityLabel(p: number): string {
  return LINEAR_PRIORITY_LABEL[p] ?? `priority ${p}`;
}

function assigneeLabel(t: Ticket): string {
  return t.assignee?.name || t.assignee?.id || "unassigned";
}

/** Best-effort extraction of an RTK-Query 422's `detail` string — same
 *  convention as api.ts's updateTab comment: the backend's HTTPException
 *  detail lands on `err.data.detail` for a rejected `.unwrap()` promise. */
function errorDetail(err: unknown): string {
  const data = (err as { data?: { detail?: string } } | undefined)?.data;
  return data?.detail || "Something went wrong.";
}

export function TicketsView() {
  const [selectedTicketId, setSelectedTicketId] = useState<string | null>(null);

  return selectedTicketId ? (
    <TicketDetail
      ticketId={selectedTicketId}
      onBack={() => setSelectedTicketId(null)}
    />
  ) : (
    <TicketBrowser onOpenTicket={setSelectedTicketId} />
  );
}

// ---------------------------------------------------------------------------
// Browse — filters + a state-grouped list.
// ---------------------------------------------------------------------------

function TicketBrowser({
  onOpenTicket,
}: {
  onOpenTicket: (id: string) => void;
}) {
  const [project, setProject] = useState("");
  const [state, setState] = useState<TicketState | "">("");
  const [assignee, setAssignee] = useState("");
  const [label, setLabel] = useState("");
  const [showNewTicket, setShowNewTicket] = useState(false);
  const [showResyncPrompt, setShowResyncPrompt] = useState(false);

  const { data, isLoading } = useListTicketsQuery(
    {
      project: project.trim() || undefined,
      state: state || undefined,
      assignee: assignee.trim() || undefined,
      label: label.trim() || undefined,
    },
    { pollingInterval: 5000 },
  );
  const tickets = data?.tickets ?? [];

  const grouped = useMemo(() => {
    const byState = new Map<TicketState, Ticket[]>();
    for (const s of TICKET_STATES) byState.set(s, []);
    for (const t of tickets) {
      const bucket = byState.get(t.state);
      if (bucket) bucket.push(t);
      else byState.set(t.state, [t]); // defensive: an unrecognized state still shows up
    }
    return byState;
  }, [tickets]);

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="flex shrink-0 flex-col gap-2 border-b border-border/70 px-4 py-2.5">
        <div className="flex items-center justify-between gap-2">
          <h3 className="text-sm font-medium">Tickets</h3>
          <div className="flex shrink-0 items-center gap-2">
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-7 gap-1 text-[11px]"
              onClick={() => setShowResyncPrompt((v) => !v)}
            >
              <RefreshCw className="h-3.5 w-3.5" />
              resync
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              className="h-7 gap-1 text-[11px]"
              onClick={() => setShowNewTicket((v) => !v)}
            >
              <Plus className="h-3.5 w-3.5" />
              new ticket
            </Button>
          </div>
        </div>

        {showResyncPrompt ? (
          <ResyncPromptBox
            defaultProject={project}
            onClose={() => setShowResyncPrompt(false)}
          />
        ) : null}

        {showNewTicket ? (
          <NewTicketForm
            defaultProject={project}
            onDone={() => setShowNewTicket(false)}
          />
        ) : null}

        <div className="flex flex-wrap items-center gap-2">
          <input
            value={project}
            onChange={(e) => setProject(e.target.value)}
            placeholder="filter by project…"
            className="h-7 min-w-[9rem] flex-1 rounded-md border border-border bg-card/40 px-2 text-[11px] outline-none focus:border-ring"
          />
          <select
            value={state}
            onChange={(e) => setState(e.target.value as TicketState | "")}
            className="h-7 shrink-0 rounded-md border border-input bg-background px-1.5 text-[11px] text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          >
            <option value="">any state</option>
            {TICKET_STATES.map((s) => (
              <option key={s} value={s}>
                {STATE_EMOJI[s]} {s}
              </option>
            ))}
          </select>
          <input
            value={assignee}
            onChange={(e) => setAssignee(e.target.value)}
            placeholder="filter by assignee…"
            className="h-7 min-w-[9rem] flex-1 rounded-md border border-border bg-card/40 px-2 text-[11px] outline-none focus:border-ring"
          />
          <input
            value={label}
            onChange={(e) => setLabel(e.target.value)}
            placeholder="filter by label…"
            className="h-7 min-w-[9rem] flex-1 rounded-md border border-border bg-card/40 px-2 text-[11px] outline-none focus:border-ring"
          />
        </div>
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {isLoading ? (
          <p className="text-xs text-muted-foreground">loading…</p>
        ) : tickets.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            no tickets{project ? ` for project "${project}"` : ""}. Create one
            locally, or resync from Linear (see the resync button above).
          </p>
        ) : (
          <div className="space-y-4">
            {TICKET_STATES.filter((s) => (grouped.get(s)?.length ?? 0) > 0).map(
              (s) => (
                <div key={s}>
                  <h4 className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium text-muted-foreground">
                    <span>{STATE_EMOJI[s]}</span>
                    {s}
                    <span className="text-muted-foreground/50">
                      ({grouped.get(s)?.length ?? 0})
                    </span>
                  </h4>
                  <div className="divide-y divide-border/50 rounded-md border border-border/50">
                    {(grouped.get(s) ?? []).map((t) => (
                      <TicketRow
                        key={t.id}
                        ticket={t}
                        onOpen={() => onOpenTicket(t.id)}
                      />
                    ))}
                  </div>
                </div>
              ),
            )}
          </div>
        )}
      </div>
    </div>
  );
}

function TicketRow({
  ticket,
  onOpen,
}: {
  ticket: Ticket;
  onOpen: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onOpen}
      className="flex w-full min-w-0 items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-accent/40"
    >
      {ticket.origin === "linear-pulled" ? (
        <span title="Synced from Linear — read-only mirror" className="shrink-0">
          🔗
        </span>
      ) : (
        <span title="Local draft" className="shrink-0">
          📝
        </span>
      )}
      <span className="min-w-0 flex-1 truncate font-medium" title={ticket.title}>
        {ticket.title}
      </span>
      {ticket.labels.length > 0 ? (
        <div className="hidden shrink-0 items-center gap-1 sm:flex">
          {ticket.labels.slice(0, 2).map((l) => (
            <Badge key={l} variant="outline" className="text-[9px]">
              {l}
            </Badge>
          ))}
        </div>
      ) : null}
      <span className="hidden shrink-0 text-[10px] text-muted-foreground md:inline">
        {assigneeLabel(ticket)}
      </span>
      <span className="hidden shrink-0 text-[10px] text-muted-foreground lg:inline">
        {priorityLabel(ticket.linear_priority)}
      </span>
      <span className="shrink-0 text-[10px] text-muted-foreground">
        {relativeTime(ticket.updated_at)}
      </span>
      <IdChip id={ticket.id} />
    </button>
  );
}

// ---------------------------------------------------------------------------
// New-ticket form — local-created only (no origin/linear_ref exposed).
// ---------------------------------------------------------------------------

function NewTicketForm({
  defaultProject,
  onDone,
}: {
  defaultProject: string;
  onDone: () => void;
}) {
  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [state, setState] = useState<TicketState>("Backlog");
  const [project, setProject] = useState(defaultProject);
  const [labels, setLabels] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [createTicket, { isLoading: creating }] = useCreateTicketMutation();

  const handleSubmit = async () => {
    const trimmed = title.trim();
    if (!trimmed || creating) return;
    setError(null);
    try {
      await createTicket({
        title: trimmed,
        description,
        state,
        project: project.trim() || undefined,
        labels: labels
          .split(",")
          .map((l) => l.trim())
          .filter(Boolean),
        // A fresh key per creation attempt — reused automatically if the
        // user hits "create" again on the SAME uncommitted draft below,
        // since this closure is re-created; retrying a genuinely failed
        // submit re-runs handleSubmit with a new key, which is correct
        // (a NEW attempt, not a retry of an in-flight one) — the backend's
        // dedup exists to protect against a client-side timeout landing
        // the write anyway, not against a user re-clicking after a clean
        // rejection.
        idempotency_key: crypto.randomUUID(),
      }).unwrap();
      setTitle("");
      setDescription("");
      setLabels("");
      onDone();
    } catch (err) {
      setError(errorDetail(err));
    }
  };

  return (
    <div className="rounded-md border border-border bg-card/30 p-2.5">
      <input
        autoFocus
        value={title}
        onChange={(e) => setTitle(e.target.value)}
        placeholder="Ticket title…"
        className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-[11px] outline-none focus:ring-1 focus:ring-ring"
      />
      <textarea
        value={description}
        onChange={(e) => setDescription(e.target.value)}
        placeholder="Description (markdown)…"
        rows={3}
        className="mt-1.5 w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-[11px] font-mono outline-none focus:ring-1 focus:ring-ring"
      />
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <select
          value={state}
          onChange={(e) => setState(e.target.value as TicketState)}
          className="h-7 shrink-0 rounded-md border border-input bg-background px-1.5 text-[11px] text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        >
          {TICKET_STATES.map((s) => (
            <option key={s} value={s}>
              {STATE_EMOJI[s]} {s}
            </option>
          ))}
        </select>
        <input
          value={project}
          onChange={(e) => setProject(e.target.value)}
          placeholder="project…"
          className="h-7 w-32 rounded-md border border-input bg-background px-2 text-[11px] outline-none focus:ring-1 focus:ring-ring"
        />
        <input
          value={labels}
          onChange={(e) => setLabels(e.target.value)}
          placeholder="labels, comma-separated…"
          className="h-7 flex-1 rounded-md border border-input bg-background px-2 text-[11px] outline-none focus:ring-1 focus:ring-ring"
        />
      </div>
      {error ? <p className="mt-1.5 text-[10px] text-red-400">{error}</p> : null}
      <div className="mt-1.5 flex items-center justify-end gap-1">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-[10px]"
          onClick={onDone}
        >
          cancel
        </Button>
        <Button
          type="button"
          size="sm"
          className="h-6 px-2 text-[10px]"
          disabled={!title.trim() || creating}
          onClick={() => void handleSubmit()}
        >
          {creating ? "creating…" : "create"}
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Resync affordance — a copyable prompt, deliberately NOT a button that
// triggers anything server-side (see file header).
// ---------------------------------------------------------------------------

function ResyncPromptBox({
  defaultProject,
  onClose,
}: {
  defaultProject: string;
  onClose: () => void;
}) {
  const [project, setProject] = useState(defaultProject || "Langgraph");

  const prompt = [
    `Resync tickets for Linear project "${project}":`,
    `1. Call mcp__linear__list_issues for project "${project}" to fetch the raw issues.`,
    `2. Map each issue onto: {linear_ref (Linear issue id, REQUIRED), title, description, state (Backlog|Todo|In Progress|In Review|Done|Cancelled), linear_priority (0-4), assignee ({id, name}), labels ([str]), parent_id, links ([str])}.`,
    `3. Call ticket_bulk_upsert(project="${project}", tickets=[...]) with the mapped list.`,
  ].join("\n");

  return (
    <div className="rounded-md border border-border bg-card/30 p-2.5 text-[11px]">
      <div className="flex items-center gap-1.5">
        <span className="text-muted-foreground">
          There's no resync button — a resync is agent-orchestrated (the
          daemon has no Linear API access). Paste this to an agent:
        </span>
      </div>
      <div className="mt-1.5 flex items-center gap-1.5">
        <input
          value={project}
          onChange={(e) => setProject(e.target.value)}
          placeholder="Linear project name…"
          className="h-7 w-40 shrink-0 rounded-md border border-input bg-background px-2 text-[11px] outline-none focus:ring-1 focus:ring-ring"
        />
        <pre className="min-w-0 flex-1 overflow-x-auto whitespace-pre-wrap rounded-md border border-border/50 bg-background/60 p-1.5 font-mono text-[10px] text-muted-foreground">
          {prompt}
        </pre>
        <CopyRawMarkdownButton text={prompt} />
      </div>
      <div className="mt-1.5 flex justify-end">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-[10px]"
          onClick={onClose}
        >
          close
        </Button>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Detail — full description, comments, read-only-synced-fields banner.
// ---------------------------------------------------------------------------

function TicketDetail({
  ticketId,
  onBack,
}: {
  ticketId: string;
  onBack: () => void;
}) {
  const { data: ticket, isLoading } = useGetTicketQuery(ticketId, {
    pollingInterval: 5000,
  });

  if (isLoading || !ticket) {
    return (
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="shrink-0 border-b border-border/70 p-1.5">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 gap-1 px-1.5 text-[10px]"
            onClick={onBack}
          >
            <ChevronLeft className="h-3 w-3" />
            tickets
          </Button>
        </div>
        <p className="p-4 text-xs text-muted-foreground">loading…</p>
      </div>
    );
  }

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/70 px-4 py-2.5">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 gap-1 px-1.5 text-[10px]"
          onClick={onBack}
        >
          <ChevronLeft className="h-3 w-3" />
          tickets
        </Button>
        <IdChip id={ticket.id} />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        <OriginBanner ticket={ticket} />
        <TicketFields ticket={ticket} />
        <TicketComments ticket={ticket} />
      </div>
    </div>
  );
}

function OriginBanner({ ticket }: { ticket: Ticket }) {
  if (ticket.origin === "linear-pulled") {
    return (
      <div className="mb-3 flex items-center gap-1.5 rounded-md border border-sky-500/30 bg-sky-500/10 px-2.5 py-1.5 text-[11px] text-sky-300">
        <span>🔗</span>
        <span>
          Synced from Linear — read-only mirror (
          {ticket.sync_state === "drifted" ? "drifted, resync to refresh" : ticket.sync_state}
          ). Title/description/state/priority/assignee/labels/project/links
          can't be edited locally — use{" "}
          <code className="rounded bg-black/20 px-1">ticket_comment</code> to
          annotate it instead.
        </span>
      </div>
    );
  }
  return (
    <div className="mb-3 flex items-center gap-1.5 rounded-md border border-border/50 bg-card/30 px-2.5 py-1.5 text-[11px] text-muted-foreground">
      <span>📝</span>
      <span>Local draft — not synced to Linear (v1 has no push).</span>
    </div>
  );
}

function TicketFields({ ticket }: { ticket: Ticket }) {
  const [editing, setEditing] = useState(false);
  const [title, setTitle] = useState(ticket.title);
  const [description, setDescription] = useState(ticket.raw_text);
  const [state, setState] = useState<TicketState>(ticket.state);
  const [project, setProject] = useState(ticket.project);
  const [labels, setLabels] = useState(ticket.labels.join(", "));
  const [error, setError] = useState<string | null>(null);
  const [updateTicket, { isLoading: saving }] = useUpdateTicketMutation();

  const editable = ticket.origin === "local-created";

  const handleSave = async () => {
    setError(null);
    try {
      await updateTicket({
        id: ticket.id,
        title: title.trim(),
        raw_text: description,
        state,
        project: project.trim(),
        labels: labels
          .split(",")
          .map((l) => l.trim())
          .filter(Boolean),
      }).unwrap();
      setEditing(false);
    } catch (err) {
      setError(errorDetail(err));
    }
  };

  if (editable && editing) {
    return (
      <div className="mb-4 rounded-md border border-border bg-card/30 p-2.5">
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          className="w-full rounded-md border border-input bg-background px-2 py-1.5 text-sm font-medium outline-none focus:ring-1 focus:ring-ring"
        />
        <textarea
          value={description}
          onChange={(e) => setDescription(e.target.value)}
          rows={6}
          className="mt-1.5 w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-[11px] font-mono outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
          <select
            value={state}
            onChange={(e) => setState(e.target.value as TicketState)}
            className="h-7 shrink-0 rounded-md border border-input bg-background px-1.5 text-[11px] text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
          >
            {TICKET_STATES.map((s) => (
              <option key={s} value={s}>
                {STATE_EMOJI[s]} {s}
              </option>
            ))}
          </select>
          <input
            value={project}
            onChange={(e) => setProject(e.target.value)}
            placeholder="project…"
            className="h-7 w-32 rounded-md border border-input bg-background px-2 text-[11px] outline-none focus:ring-1 focus:ring-ring"
          />
          <input
            value={labels}
            onChange={(e) => setLabels(e.target.value)}
            placeholder="labels, comma-separated…"
            className="h-7 flex-1 rounded-md border border-input bg-background px-2 text-[11px] outline-none focus:ring-1 focus:ring-ring"
          />
        </div>
        {error ? <p className="mt-1.5 text-[10px] text-red-400">{error}</p> : null}
        <div className="mt-1.5 flex justify-end gap-1">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-[10px]"
            onClick={() => {
              setEditing(false);
              setError(null);
              setTitle(ticket.title);
              setDescription(ticket.raw_text);
              setState(ticket.state);
              setProject(ticket.project);
              setLabels(ticket.labels.join(", "));
            }}
          >
            cancel
          </Button>
          <Button
            type="button"
            size="sm"
            className="h-6 px-2 text-[10px]"
            disabled={!title.trim() || saving}
            onClick={() => void handleSave()}
          >
            {saving ? "saving…" : "save"}
          </Button>
        </div>
      </div>
    );
  }

  return (
    <div className="mb-4">
      <div className="flex items-start justify-between gap-2">
        <h2 className="min-w-0 text-base font-semibold">{ticket.title}</h2>
        {editable ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            className="h-6 shrink-0 px-2 text-[10px]"
            onClick={() => setEditing(true)}
          >
            edit
          </Button>
        ) : null}
      </div>
      <div className="mt-1.5 flex flex-wrap items-center gap-1.5">
        <Badge variant="outline" className="text-[10px]">
          {STATE_EMOJI[ticket.state]} {ticket.state}
        </Badge>
        <span className="text-[10px] text-muted-foreground">
          {priorityLabel(ticket.linear_priority)}
        </span>
        <span className="text-[10px] text-muted-foreground">
          {assigneeLabel(ticket)}
        </span>
        {ticket.project ? (
          <span className="text-[10px] text-muted-foreground">
            project={ticket.project}
          </span>
        ) : null}
        {ticket.labels.map((l) => (
          <Badge key={l} variant="outline" className="text-[9px]">
            {l}
          </Badge>
        ))}
      </div>
      <div className="mt-3 whitespace-pre-wrap text-[13px] leading-relaxed">
        {ticket.raw_text || (
          <span className="italic text-muted-foreground/60">
            no description.
          </span>
        )}
      </div>
    </div>
  );
}

function TicketComments({ ticket }: { ticket: Ticket }) {
  const [draft, setDraft] = useState("");
  const [addComment, { isLoading: adding }] = useAddTicketCommentMutation();

  const handleAdd = async () => {
    const text = draft.trim();
    if (!text || adding) return;
    await addComment({ id: ticket.id, text }).unwrap();
    setDraft("");
  };

  return (
    <div className="border-t border-border/50 pt-3">
      <h4 className="mb-2 text-[11px] font-medium text-muted-foreground">
        comments ({ticket.comments.length})
      </h4>
      <div className="space-y-2">
        {ticket.comments.length === 0 ? (
          <p className="text-[11px] italic text-muted-foreground/60">
            no comments yet.
          </p>
        ) : (
          ticket.comments.map((c, i) => (
            <div
              key={i}
              className="rounded-md border border-border/50 bg-card/20 px-2.5 py-1.5 text-[11px]"
            >
              <div className="mb-0.5 flex items-center gap-1.5 text-[10px] text-muted-foreground">
                <span className="font-medium">
                  {c.author || "(unattributed)"}
                </span>
                <span>{relativeTime(c.ts)}</span>
              </div>
              <p className="whitespace-pre-wrap">{c.text}</p>
            </div>
          ))
        )}
      </div>
      <div className="mt-2 flex items-start gap-1.5">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="add a comment…"
          rows={2}
          className="w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-[11px] outline-none focus:ring-1 focus:ring-ring"
        />
        <Button
          type="button"
          size="sm"
          className="h-7 shrink-0 px-2 text-[10px]"
          disabled={!draft.trim() || adding}
          onClick={() => void handleAdd()}
        >
          {adding ? "…" : "add"}
        </Button>
      </div>
    </div>
  );
}
