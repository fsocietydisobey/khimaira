/**
 * Notebook — self-healing, code-grounded knowledge base (Phase 1-2c backend,
 * NotebookLM-style 3-panel reader UI per Joseph's direction 2026-07-03).
 *
 * Layout: a persistent ask bar at the top, then three panels —
 *   LEFT   — the notes list (title + status), tab filter, collapsible add-note.
 *            Click a note to load it into center + right.
 *   CENTER — the selected note's STRUCTURED read (summary/technical/plain,
 *            rendered markdown+mermaid) — OR the latest ask answer (an
 *            answer is just a generated note: rendered markdown + cited
 *            sources + a "healed N" badge). Clicking a cited source loads
 *            that note into center+right, same as a left-panel click.
 *   RIGHT  — the selected note's ORIGINAL (raw_text), rendered the same way.
 *            Empty until a note is loaded (a raw answer has no "original").
 *
 * Global daemon state (not project-scoped) mounted under a per-project route
 * for nav consistency with the other observability views.
 */

import { useState } from "react";
import { useParams } from "react-router-dom";
import { Plus, RefreshCw, Search, Trash2, Upload } from "lucide-react";

import {
  useAskNotebookMutation,
  useCreateNoteMutation,
  useCreateTabMutation,
  useDeleteNoteMutation,
  useListNotesQuery,
  useListTabsQuery,
  usePromoteNoteMutation,
  useRevalidateNoteMutation,
} from "@/api";
import { MarkdownView } from "@/components/notebook/MarkdownView";
import type { AskAnswer, Note } from "@/components/notebook/notebookTypes";
import { ProjectNavTabs } from "@/components/project/ProjectNavTabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

const ALL_TABS = "__all__";

type CenterView = { kind: "note"; noteId: string } | { kind: "answer"; data: AskAnswer } | null;

export function Notebook() {
  const { name } = useParams<{ name: string }>();
  const projectName = name ?? "";

  const [selectedTab, setSelectedTab] = useState<string>(ALL_TABS);
  const [showAddNote, setShowAddNote] = useState(false);
  const [draft, setDraft] = useState("");
  const [creatingTab, setCreatingTab] = useState(false);
  const [newTabTitle, setNewTabTitle] = useState("");
  const [centerView, setCenterView] = useState<CenterView>(null);

  const { data: tabsData } = useListTabsQuery();
  const { data: notesData, isLoading: notesLoading } = useListNotesQuery(
    selectedTab === ALL_TABS ? undefined : { tabId: selectedTab },
    { pollingInterval: 3000 },
  );
  const [createNote, { isLoading: creatingNote }] = useCreateNoteMutation();
  const [createTab] = useCreateTabMutation();
  const [promoteNote] = usePromoteNoteMutation();
  const [deleteNote] = useDeleteNoteMutation();
  const [revalidateNote, { isLoading: revalidating }] = useRevalidateNoteMutation();
  const [askNotebook, { isLoading: asking }] = useAskNotebookMutation();

  const tabs = tabsData?.tabs ?? [];
  const notes = notesData?.notes ?? [];
  const selectedNote =
    centerView?.kind === "note" ? (notes.find((n) => n.id === centerView.noteId) ?? null) : null;

  const handleSelectNote = (id: string) => setCenterView({ kind: "note", noteId: id });

  const handleAsk = async (question: string) => {
    const result = await askNotebook({ question }).unwrap();
    setCenterView({ kind: "answer", data: result });
  };

  const handleAddNote = async () => {
    const text = draft.trim();
    if (!text) return;
    await createNote({
      raw_text: text,
      tab_id: selectedTab === ALL_TABS ? undefined : selectedTab,
    }).unwrap();
    setDraft("");
    setShowAddNote(false);
  };

  const handleCreateTab = async () => {
    const title = newTabTitle.trim();
    const created = await createTab({ title }).unwrap();
    setNewTabTitle("");
    setCreatingTab(false);
    setSelectedTab(created.id);
  };

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <header className="shrink-0 border-b border-border bg-card/40 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">notebook — {projectName}</h2>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              paste → auto-structure → ask → self-heal against the live code
            </p>
          </div>
          <ProjectNavTabs projectName={projectName} />
        </div>
      </header>

      <AskBar onAsk={handleAsk} asking={asking} />

      <div className="flex flex-1 overflow-hidden">
        <NotesListPanel
          tabs={tabs}
          notes={notes}
          notesLoading={notesLoading}
          selectedTab={selectedTab}
          onSelectTab={setSelectedTab}
          selectedNoteId={centerView?.kind === "note" ? centerView.noteId : null}
          onSelectNote={handleSelectNote}
          showAddNote={showAddNote}
          onToggleAddNote={() => setShowAddNote((v) => !v)}
          draft={draft}
          onDraftChange={setDraft}
          creatingNote={creatingNote}
          onAddNote={handleAddNote}
          creatingTab={creatingTab}
          onStartCreateTab={() => setCreatingTab(true)}
          newTabTitle={newTabTitle}
          onNewTabTitleChange={setNewTabTitle}
          onCreateTab={handleCreateTab}
          onCancelCreateTab={() => {
            setCreatingTab(false);
            setNewTabTitle("");
          }}
        />
        <CenterReaderPanel
          view={centerView}
          onSelectSource={handleSelectNote}
          onPromote={selectedNote ? () => promoteNote(selectedNote.id) : undefined}
          onDelete={
            selectedNote
              ? () => {
                  setCenterView(null);
                  deleteNote(selectedNote.id);
                }
              : undefined
          }
          onRevalidate={selectedNote ? () => revalidateNote(selectedNote.id) : undefined}
          revalidating={revalidating}
        />
        <OriginalPanel note={selectedNote} />
      </div>
    </div>
  );
}

/** Persistent ask bar — always visible above the 3 panels. */
function AskBar({ onAsk, asking }: { onAsk: (question: string) => void; asking: boolean }) {
  const [question, setQuestion] = useState("");

  const submit = () => {
    const q = question.trim();
    if (!q || asking) return;
    onAsk(q);
  };

  return (
    <div className="flex shrink-0 items-center gap-2 border-b border-border bg-card/20 px-3 py-2">
      <input
        value={question}
        onChange={(e) => setQuestion(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") submit();
        }}
        placeholder="Ask a question — answered from your notes, self-healed against the live code…"
        className="h-8 flex-1 rounded-md border border-input bg-background px-3 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
      />
      <Button size="sm" disabled={!question.trim() || asking} onClick={submit}>
        <Search className="mr-1.5 h-3.5 w-3.5" />
        {asking ? "asking…" : "ask"}
      </Button>
    </div>
  );
}

const STATUS_DOT: Record<Note["status"], string> = {
  draft: "bg-amber-400 animate-pulse",
  processed: "bg-emerald-400",
  promoted: "bg-sky-400",
  failed: "bg-rose-400",
};

/** LEFT — the notes library: tab filter, collapsible add-note, click-to-load list. */
function NotesListPanel({
  tabs,
  notes,
  notesLoading,
  selectedTab,
  onSelectTab,
  selectedNoteId,
  onSelectNote,
  showAddNote,
  onToggleAddNote,
  draft,
  onDraftChange,
  creatingNote,
  onAddNote,
  creatingTab,
  onStartCreateTab,
  newTabTitle,
  onNewTabTitleChange,
  onCreateTab,
  onCancelCreateTab,
}: {
  tabs: { id: string; title: string; note_ids: string[] }[];
  notes: Note[];
  notesLoading: boolean;
  selectedTab: string;
  onSelectTab: (id: string) => void;
  selectedNoteId: string | null;
  onSelectNote: (id: string) => void;
  showAddNote: boolean;
  onToggleAddNote: () => void;
  draft: string;
  onDraftChange: (v: string) => void;
  creatingNote: boolean;
  onAddNote: () => void;
  creatingTab: boolean;
  onStartCreateTab: () => void;
  newTabTitle: string;
  onNewTabTitleChange: (v: string) => void;
  onCreateTab: () => void;
  onCancelCreateTab: () => void;
}) {
  return (
    <div className="flex w-64 shrink-0 flex-col border-r border-border bg-card/10">
      <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-border/70 px-2 py-1.5">
        <button
          type="button"
          onClick={() => onSelectTab(ALL_TABS)}
          className={cn(
            "rounded-md px-2 py-1 text-[10px] font-medium whitespace-nowrap transition-colors",
            selectedTab === ALL_TABS
              ? "bg-accent text-accent-foreground"
              : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
          )}
        >
          all
        </button>
        {tabs.map((t) => (
          <button
            key={t.id}
            type="button"
            onClick={() => onSelectTab(t.id)}
            title={`${t.note_ids.length} note${t.note_ids.length === 1 ? "" : "s"}`}
            className={cn(
              "rounded-md px-2 py-1 text-[10px] font-medium whitespace-nowrap transition-colors",
              selectedTab === t.id
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
            )}
          >
            {t.title}
            <span className="ml-1 text-muted-foreground/60">{t.note_ids.length}</span>
          </button>
        ))}
        {creatingTab ? (
          <input
            autoFocus
            value={newTabTitle}
            onChange={(e) => onNewTabTitleChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") onCreateTab();
              if (e.key === "Escape") onCancelCreateTab();
            }}
            onBlur={onCancelCreateTab}
            placeholder="name…"
            className="h-6 w-20 rounded border border-input bg-background px-1 text-[10px] focus:outline-none focus:ring-1 focus:ring-ring"
          />
        ) : (
          <button
            type="button"
            onClick={onStartCreateTab}
            title="New tab"
            className="rounded-md p-1 text-muted-foreground hover:bg-accent/50"
          >
            <Plus className="h-3 w-3" />
          </button>
        )}
      </div>

      <div className="shrink-0 border-b border-border/70 p-2">
        {showAddNote ? (
          <div>
            <textarea
              autoFocus
              value={draft}
              onChange={(e) => onDraftChange(e.target.value)}
              placeholder="Paste a note…"
              rows={4}
              className="w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-[11px] font-mono focus:outline-none focus:ring-1 focus:ring-ring"
            />
            <div className="mt-1.5 flex items-center justify-end gap-1">
              <Button size="sm" variant="ghost" className="h-6 px-2 text-[10px]" onClick={onToggleAddNote}>
                cancel
              </Button>
              <Button
                size="sm"
                className="h-6 px-2 text-[10px]"
                disabled={!draft.trim() || creatingNote}
                onClick={onAddNote}
              >
                <Upload className="mr-1 h-3 w-3" />
                {creatingNote ? "adding…" : "add"}
              </Button>
            </div>
          </div>
        ) : (
          <Button
            size="sm"
            variant="outline"
            className="h-7 w-full text-[11px]"
            onClick={onToggleAddNote}
          >
            <Plus className="mr-1.5 h-3.5 w-3.5" />
            paste note
          </Button>
        )}
      </div>

      <div className="flex-1 overflow-y-auto">
        {notesLoading ? (
          <p className="p-3 text-[11px] text-muted-foreground">loading…</p>
        ) : notes.length === 0 ? (
          <p className="p-3 text-[11px] text-muted-foreground">
            no notes {selectedTab === ALL_TABS ? "yet" : "in this tab"}.
          </p>
        ) : (
          notes.map((n) => (
            <button
              key={n.id}
              type="button"
              onClick={() => onSelectNote(n.id)}
              className={cn(
                "block w-full min-w-0 border-b border-border/40 px-3 py-2 text-left transition-colors",
                selectedNoteId === n.id ? "bg-accent" : "hover:bg-accent/40",
              )}
            >
              <div className="flex items-center gap-1.5">
                <span
                  className={cn("h-1.5 w-1.5 shrink-0 rounded-full", STATUS_DOT[n.status])}
                  title={n.status}
                />
                <span className="truncate text-xs font-medium">{n.title}</span>
              </div>
              <p className="mt-0.5 truncate pl-3 text-[10px] text-muted-foreground">
                {new Date(n.updated_at).toLocaleDateString()}
                {n.history_count > 0 ? ` · healed ×${n.history_count}` : ""}
              </p>
            </button>
          ))
        )}
      </div>
    </div>
  );
}

function relativeTime(iso: string): string {
  const diffMin = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

const STATUS_BADGE: Record<
  Note["status"],
  { label: string; variant: "outline" | "secondary" | "default" | "destructive" }
> = {
  draft: { label: "processing…", variant: "outline" },
  processed: { label: "processed", variant: "secondary" },
  promoted: { label: "promoted", variant: "default" },
  failed: { label: "structuring failed", variant: "destructive" },
};

/** CENTER — either the selected note's structured read, or the latest ask answer. */
function CenterReaderPanel({
  view,
  onSelectSource,
  onPromote,
  onDelete,
  onRevalidate,
  revalidating,
}: {
  view: CenterView;
  onSelectSource: (id: string) => void;
  onPromote?: () => void;
  onDelete?: () => void;
  onRevalidate?: () => void;
  revalidating: boolean;
}) {
  const [section, setSection] = useState<"summary" | "technical" | "plain">("summary");

  if (!view) {
    return (
      <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
        Select a note from the left, or ask a question above.
      </div>
    );
  }

  if (view.kind === "answer") {
    const { data } = view;
    return (
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="shrink-0 border-b border-border/70 px-4 py-2">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            answer
          </h3>
        </div>
        <div className="min-w-0 flex-1 overflow-y-auto p-4">
          <MarkdownView content={data.answer} />
          {data.sources.length > 0 || data.healed.length > 0 ? (
            <div className="mt-3 flex flex-wrap items-center gap-1.5 border-t border-border/50 pt-3">
              {data.sources.map((id) => (
                <button key={id} type="button" onClick={() => onSelectSource(id)}>
                  <Badge
                    variant="outline"
                    className="cursor-pointer text-[10px] hover:bg-accent/50"
                  >
                    {id}
                  </Badge>
                </button>
              ))}
              {data.healed.length > 0 ? (
                <Badge variant="warning" className="text-[10px]">
                  healed {data.healed.length} note{data.healed.length === 1 ? "" : "s"} vs current
                  code
                </Badge>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>
    );
  }

  return <NoteStructuredReader
    noteId={view.noteId}
    section={section}
    onSectionChange={setSection}
    onPromote={onPromote}
    onDelete={onDelete}
    onRevalidate={onRevalidate}
    revalidating={revalidating}
  />;
}

/**
 * The actual structured-note render — pulled out so it re-subscribes to the
 * live note list by id (list polling keeps it fresh: a heal or a status
 * flip shows up here without a manual refresh).
 */
function NoteStructuredReader({
  noteId,
  section,
  onSectionChange,
  onPromote,
  onDelete,
  onRevalidate,
  revalidating,
}: {
  noteId: string;
  section: "summary" | "technical" | "plain";
  onSectionChange: (s: "summary" | "technical" | "plain") => void;
  onPromote?: () => void;
  onDelete?: () => void;
  onRevalidate?: () => void;
  revalidating: boolean;
}) {
  const { data: notesData } = useListNotesQuery();
  const note = notesData?.notes.find((n) => n.id === noteId);

  if (!note) {
    return (
      <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
        Note not found — it may have been deleted.
      </div>
    );
  }

  const badge = STATUS_BADGE[note.status];
  const validationLabel = note.last_validated_at
    ? note.history_count > 0
      ? `healed ×${note.history_count} · checked ${relativeTime(note.last_validated_at)}`
      : `current as of ${relativeTime(note.last_validated_at)}`
    : "never validated vs code";

  return (
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden border-r border-border">
      <div className="flex shrink-0 items-start justify-between gap-2 border-b border-border/70 px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium" title={note.title}>
            {note.title}
          </h3>
          <p
            className={cn(
              "mt-0.5 text-[10px]",
              note.history_count > 0 ? "text-amber-400/80" : "text-muted-foreground/70",
            )}
            title={`repo: ${note.repo}`}
          >
            {validationLabel}
          </p>
        </div>
        <Badge variant={badge.variant} className="shrink-0 text-[10px]">
          {badge.label}
        </Badge>
      </div>

      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {note.pipeline ? (
          <>
            <div className="mb-3 flex gap-1">
              {(["summary", "technical", "plain"] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => onSectionChange(s)}
                  className={cn(
                    "rounded px-2 py-1 text-[10px] font-medium uppercase tracking-wide transition-colors",
                    section === s
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent/50",
                  )}
                >
                  {s}
                </button>
              ))}
            </div>
            <MarkdownView content={note.pipeline[section]} />
            {note.pipeline.tags.length > 0 ? (
              <div className="mt-3 flex flex-wrap gap-1">
                {note.pipeline.tags.map((tag) => (
                  <Badge key={tag} variant="outline" className="text-[9px]">
                    {tag}
                  </Badge>
                ))}
              </div>
            ) : null}
          </>
        ) : (
          <p className="text-xs text-muted-foreground/70">
            Still processing — the original is shown on the right; this panel fills in once
            structuring completes.
          </p>
        )}
      </div>

      <div className="flex shrink-0 items-center justify-end gap-1 border-t border-border/50 px-3 py-1.5">
        <Button
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-[10px] text-muted-foreground"
          title="Re-ground this note against the current code — heals it if the code moved on"
          disabled={revalidating}
          onClick={onRevalidate}
        >
          <RefreshCw className={cn("mr-1 h-3 w-3", revalidating && "animate-spin")} />
          {revalidating ? "checking…" : "re-check vs code"}
        </Button>
        {!note.training.promoted ? (
          <Button
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-[10px]"
            title="Mark as good for training — feeds the mnemosyne distiller"
            onClick={onPromote}
          >
            promote
          </Button>
        ) : (
          <span className="px-2 text-[10px] text-muted-foreground/60">promoted for training</span>
        )}
        <Button
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-destructive/70 hover:text-destructive"
          title="Delete note"
          onClick={onDelete}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
    </div>
  );
}

/** RIGHT — the note's immutable original (raw_text), rendered the same way. */
function OriginalPanel({ note }: { note: Note | null }) {
  return (
    <div className="flex w-[420px] shrink-0 flex-col overflow-hidden bg-card/10">
      <div className="shrink-0 border-b border-border/70 px-4 py-2.5">
        <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          original
        </h3>
      </div>
      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {note ? (
          <MarkdownView content={note.raw_text} />
        ) : (
          <p className="text-xs text-muted-foreground/70">
            The immutable original paste shows here once a note is selected.
          </p>
        )}
      </div>
    </div>
  );
}
