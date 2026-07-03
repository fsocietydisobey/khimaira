/**
 * Notebook — self-healing, code-grounded knowledge base (Phase 1-2c backend).
 *
 * Two view modes, sharing the same left notes list + tab filter:
 *   GRID   (default) — full-width multi-card overview: every note as a card
 *          (title, status, section tabs, tags, re-check/promote/delete),
 *          for scanning/comparing many notes at once. Click a card to open
 *          it in Reader mode.
 *   READER — the NotebookLM-style 3-panel deep-read: LEFT notes list |
 *          CENTER the selected note's structured read (summary/technical/
 *          plain) OR the latest ask answer | RIGHT the note's immutable
 *          ORIGINAL (raw_text). Clicking a cited source in an answer loads
 *          that note into center+right, same as a left-panel click.
 *
 * The left (Reader+Grid) and right (Reader-only) panels are resizable by
 * drag handle and collapsible to a thin rail; both preferences persist to
 * localStorage (mirrors KgMapper's palette/glow persistence pattern).
 *
 * Global daemon state (not project-scoped) mounted under a per-project route
 * for nav consistency with the other observability views.
 */

import { useCallback, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import {
  BookMarked,
  BookOpen,
  ChevronLeft,
  ChevronRight,
  LayoutGrid,
  Plus,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from "lucide-react";

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
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const ALL_TABS = "__all__";
const GRID_LEFT_WIDTH = 240;

const SUGGESTED_QUESTIONS = [
  "What's changed recently in the code this notebook tracks?",
  "Summarize everything tagged as a bug or incident.",
  "What's still unresolved or open?",
];

type ViewMode = "grid" | "reader";

type CenterView =
  | { kind: "note"; noteId: string }
  | { kind: "answer"; data: AskAnswer; question: string }
  | null;

// ---------------------------------------------------------------------------
// Persisted layout preferences — lazy-init from localStorage, guarded against
// unavailability (private browsing), mirroring KgMapper's palette/glow prefs.
// ---------------------------------------------------------------------------

function usePersistedBoolean(key: string, defaultValue: boolean) {
  const [value, setValue] = useState<boolean>(() => {
    if (typeof localStorage === "undefined") return defaultValue;
    const saved = localStorage.getItem(key);
    return saved === null ? defaultValue : saved === "1";
  });
  const update = useCallback(
    (next: boolean) => {
      setValue(next);
      try {
        localStorage.setItem(key, next ? "1" : "0");
      } catch {
        // non-fatal — preference just won't persist across reloads
      }
    },
    [key],
  );
  return [value, update] as const;
}

function useResizableWidth(
  key: string,
  defaultWidth: number,
  min: number,
  max: number,
  sign: 1 | -1,
) {
  const [width, setWidth] = useState<number>(() => {
    if (typeof localStorage === "undefined") return defaultWidth;
    const saved = Number(localStorage.getItem(key));
    return Number.isFinite(saved) && saved >= min && saved <= max ? saved : defaultWidth;
  });
  const resize = useCallback(
    (deltaPx: number) => {
      setWidth((w) => {
        const next = Math.min(max, Math.max(min, w + sign * deltaPx));
        try {
          localStorage.setItem(key, String(next));
        } catch {
          // non-fatal
        }
        return next;
      });
    },
    [key, max, min, sign],
  );
  return [width, resize] as const;
}

/** Drag handle between a side panel and the center — reports pixel delta only. */
function ResizeHandle({ onResize }: { onResize: (deltaPx: number) => void }) {
  const draggingRef = useRef(false);
  const lastXRef = useRef(0);

  return (
    <div
      onPointerDown={(e) => {
        draggingRef.current = true;
        lastXRef.current = e.clientX;
        e.currentTarget.setPointerCapture(e.pointerId);
      }}
      onPointerMove={(e) => {
        if (!draggingRef.current) return;
        onResize(e.clientX - lastXRef.current);
        lastXRef.current = e.clientX;
      }}
      onPointerUp={(e) => {
        draggingRef.current = false;
        e.currentTarget.releasePointerCapture(e.pointerId);
      }}
      className="group relative z-10 w-1.5 shrink-0 cursor-col-resize select-none"
    >
      <div className="absolute inset-y-0 left-1/2 w-px -translate-x-1/2 bg-border transition-colors group-hover:bg-ring group-active:bg-ring" />
    </div>
  );
}

/**
 * A collapsible (and optionally resizable) side panel shell — left or right.
 * Collapsed state renders a thin rail with a re-expand affordance, like
 * NotebookLM's Sources/Studio panels.
 */
function SidePanelShell({
  side,
  label,
  width,
  collapsed,
  onToggleCollapsed,
  resizable,
  onResize,
  extraHeader,
  children,
}: {
  side: "left" | "right";
  label: string;
  width: number;
  collapsed: boolean;
  onToggleCollapsed: () => void;
  resizable: boolean;
  onResize?: (deltaPx: number) => void;
  extraHeader?: React.ReactNode;
  children: React.ReactNode;
}) {
  if (collapsed) {
    return (
      <div
        className={cn(
          "flex w-8 shrink-0 flex-col items-center bg-card/10 py-2",
          side === "left" ? "border-r border-border" : "border-l border-border",
        )}
      >
        <button
          type="button"
          onClick={onToggleCollapsed}
          title={`Show ${label}`}
          className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          {side === "left" ? (
            <ChevronRight className="h-4 w-4" />
          ) : (
            <ChevronLeft className="h-4 w-4" />
          )}
        </button>
      </div>
    );
  }

  const panel = (
    <div
      className={cn(
        "flex min-w-0 flex-1 flex-col overflow-hidden bg-card/10",
        side === "left" ? "border-r border-border" : "border-l border-border",
      )}
    >
      <div className="flex shrink-0 items-center justify-between gap-1 border-b border-border/50 px-2 py-1">
        <span className="pl-0.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground/70">
          {label}
        </span>
        <div className="flex items-center gap-0.5">
          {extraHeader}
          <button
            type="button"
            onClick={onToggleCollapsed}
            title={`Hide ${label}`}
            className="rounded p-1 text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            {side === "left" ? (
              <ChevronLeft className="h-3.5 w-3.5" />
            ) : (
              <ChevronRight className="h-3.5 w-3.5" />
            )}
          </button>
        </div>
      </div>
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">{children}</div>
    </div>
  );

  return (
    <div className="flex shrink-0" style={{ width }}>
      {side === "right" && resizable ? <ResizeHandle onResize={onResize!} /> : null}
      {panel}
      {side === "left" && resizable ? <ResizeHandle onResize={onResize!} /> : null}
    </div>
  );
}

function ViewModeToggle({ mode, onChange }: { mode: ViewMode; onChange: (m: ViewMode) => void }) {
  const options: { key: ViewMode; label: string; Icon: typeof LayoutGrid }[] = [
    { key: "grid", label: "grid", Icon: LayoutGrid },
    { key: "reader", label: "reader", Icon: BookOpen },
  ];
  return (
    <div className="flex items-center gap-0.5 rounded-md border border-border bg-card/40 p-0.5">
      {options.map(({ key, label, Icon }) => (
        <button
          key={key}
          type="button"
          onClick={() => onChange(key)}
          className={cn(
            "flex items-center gap-1 rounded px-2 py-1 text-[11px] font-medium transition-colors",
            mode === key
              ? "bg-accent text-accent-foreground"
              : "text-muted-foreground hover:text-foreground",
          )}
        >
          <Icon className="h-3.5 w-3.5" />
          {label}
        </button>
      ))}
    </div>
  );
}

export function Notebook() {
  const { name } = useParams<{ name: string }>();
  const projectName = name ?? "";

  const [viewMode, setViewMode] = useState<ViewMode>("grid");
  const [selectedTab, setSelectedTab] = useState<string>(ALL_TABS);
  const [showAddNote, setShowAddNote] = useState(false);
  const [draft, setDraft] = useState("");
  const [creatingTab, setCreatingTab] = useState(false);
  const [newTabTitle, setNewTabTitle] = useState("");
  const [centerView, setCenterView] = useState<CenterView>(null);
  const [savedAnswerRef, setSavedAnswerRef] = useState<AskAnswer | null>(null);
  const [originalRawMode, setOriginalRawMode] = useState(false);

  const [leftCollapsed, setLeftCollapsed] = usePersistedBoolean("notebook-left-collapsed", false);
  const [rightCollapsed, setRightCollapsed] = usePersistedBoolean(
    "notebook-right-collapsed",
    false,
  );
  const [leftWidth, resizeLeft] = useResizableWidth("notebook-left-width", 256, 200, 480, 1);
  const [rightWidth, resizeRight] = useResizableWidth("notebook-right-width", 420, 280, 720, -1);

  const { data: tabsData } = useListTabsQuery();
  const { data: notesData, isLoading: notesLoading } = useListNotesQuery(
    selectedTab === ALL_TABS ? undefined : { tabId: selectedTab },
    { pollingInterval: 3000 },
  );
  const [createNote, { isLoading: creatingNote }] = useCreateNoteMutation();
  const [saveAnswerAsNote, { isLoading: savingAnswer }] = useCreateNoteMutation();
  const [createTab] = useCreateTabMutation();
  const [promoteNote] = usePromoteNoteMutation();
  const [deleteNote] = useDeleteNoteMutation();
  const [revalidateNote, { isLoading: revalidating }] = useRevalidateNoteMutation();
  const [askNotebook, { isLoading: asking }] = useAskNotebookMutation();

  const tabs = tabsData?.tabs ?? [];
  const notes = notesData?.notes ?? [];
  const selectedNote =
    viewMode === "reader" && centerView?.kind === "note"
      ? (notes.find((n) => n.id === centerView.noteId) ?? null)
      : null;

  // Shared across both modes — a click on a card, a list row, or a cited
  // source always opens that note in the deep-read Reader.
  const handleSelectNote = (id: string) => {
    setViewMode("reader");
    setCenterView({ kind: "note", noteId: id });
  };

  const handleAsk = async (question: string, noteIds: string[]) => {
    const result = await askNotebook({ question, note_ids: noteIds }).unwrap();
    setViewMode("reader");
    setCenterView({ kind: "answer", data: result, question });
  };

  const handleSaveAnswer = async () => {
    if (centerView?.kind !== "answer") return;
    const { data, question } = centerView;
    await saveAnswerAsNote({ raw_text: data.answer, title: `Answer: ${question}` }).unwrap();
    setSavedAnswerRef(data);
  };

  const handleAddNote = async () => {
    const text = draft.trim();
    if (!text) return;
    await createNote({
      raw_text: text,
      tab_id: selectedTab === ALL_TABS ? undefined : selectedTab,
      // Quick-win default (Joseph, 2026-07-03): scope new notes to the
      // project they were pasted under, instead of the backend's hardcoded
      // "khimaira" fallback — a full repo-set/change UI is a separate spec.
      repo: projectName || undefined,
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

  const notesListPanel = (
    <NotesListPanel
      tabs={tabs}
      notes={notes}
      notesLoading={notesLoading}
      selectedTab={selectedTab}
      onSelectTab={setSelectedTab}
      selectedNoteId={viewMode === "reader" && centerView?.kind === "note" ? centerView.noteId : null}
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
  );

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
          <div className="flex items-center gap-3">
            <ViewModeToggle mode={viewMode} onChange={setViewMode} />
            <ProjectNavTabs projectName={projectName} />
          </div>
        </div>
      </header>

      <AskBar onAsk={handleAsk} asking={asking} notes={notes} />

      {viewMode === "grid" ? (
        <div className="flex flex-1 overflow-hidden">
          <SidePanelShell
            side="left"
            label="notes"
            width={GRID_LEFT_WIDTH}
            collapsed={leftCollapsed}
            onToggleCollapsed={() => setLeftCollapsed(!leftCollapsed)}
            resizable={false}
          >
            {notesListPanel}
          </SidePanelShell>
          <GridView
            notes={notes}
            notesLoading={notesLoading}
            selectedTab={selectedTab}
            onOpenNote={handleSelectNote}
            onPromote={(id) => promoteNote(id)}
            onDelete={(id) => deleteNote(id)}
          />
        </div>
      ) : (
        <div className="flex flex-1 overflow-hidden">
          <SidePanelShell
            side="left"
            label="notes"
            width={leftWidth}
            collapsed={leftCollapsed}
            onToggleCollapsed={() => setLeftCollapsed(!leftCollapsed)}
            resizable
            onResize={resizeLeft}
          >
            {notesListPanel}
          </SidePanelShell>
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
            onSaveAnswer={handleSaveAnswer}
            savingAnswer={savingAnswer}
            answerSaved={centerView?.kind === "answer" && savedAnswerRef === centerView.data}
          />
          <SidePanelShell
            side="right"
            label="original"
            width={rightWidth}
            collapsed={rightCollapsed}
            onToggleCollapsed={() => setRightCollapsed(!rightCollapsed)}
            resizable
            onResize={resizeRight}
            extraHeader={
              selectedNote ? (
                <button
                  type="button"
                  onClick={() => setOriginalRawMode((v) => !v)}
                  title={originalRawMode ? "Show rendered markdown" : "Show raw pasted text"}
                  className="rounded px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-muted-foreground hover:bg-accent hover:text-foreground"
                >
                  {originalRawMode ? "rendered" : "raw"}
                </button>
              ) : null
            }
          >
            <OriginalPanel note={selectedNote} rawMode={originalRawMode} />
          </SidePanelShell>
        </div>
      )}
    </div>
  );
}

type Mention = { id: string; title: string };

/** Persistent ask bar — always visible above the panels. Type `@` to reference
 *  a specific note (autocomplete over the already-loaded notes list, modeled
 *  on Claude Code's @-file mentions) — @-referenced notes always join the
 *  answer's sources (prioritized default; see answer_question's `exclusive`
 *  param for the future flip). Shows a few suggested-question chips when
 *  idle to nudge first-time use. */
function AskBar({
  onAsk,
  asking,
  notes,
}: {
  onAsk: (question: string, noteIds: string[]) => void;
  asking: boolean;
  notes: Note[];
}) {
  const [question, setQuestion] = useState("");
  const [mentions, setMentions] = useState<Mention[]>([]);
  const [mentionQuery, setMentionQuery] = useState<string | null>(null);
  const [mentionMatchStart, setMentionMatchStart] = useState<number | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  const mentionMatches =
    mentionQuery !== null
      ? notes
          .filter((n) => n.title.toLowerCase().includes(mentionQuery.toLowerCase()))
          .slice(0, 6)
      : [];

  const updateMentionState = (value: string, cursorPos: number) => {
    const match = /@([^\s@]*)$/.exec(value.slice(0, cursorPos));
    if (match) {
      setMentionQuery(match[1]);
      setMentionMatchStart(cursorPos - match[0].length);
    } else {
      setMentionQuery(null);
      setMentionMatchStart(null);
    }
  };

  const selectMention = (note: Note) => {
    if (mentionMatchStart === null || mentionQuery === null) return;
    const before = question.slice(0, mentionMatchStart);
    const after = question.slice(mentionMatchStart + 1 + mentionQuery.length);
    setQuestion(`${before}${after}`);
    setMentions((prev) => (prev.some((m) => m.id === note.id) ? prev : [...prev, { id: note.id, title: note.title }]));
    setMentionQuery(null);
    setMentionMatchStart(null);
    requestAnimationFrame(() => inputRef.current?.focus());
  };

  const removeMention = (id: string) => setMentions((prev) => prev.filter((m) => m.id !== id));

  const submit = (text?: string) => {
    const q = (text ?? question).trim();
    if (!q || asking) return;
    onAsk(
      q,
      mentions.map((m) => m.id),
    );
    if (text) {
      setQuestion("");
      setMentions([]);
    }
  };

  return (
    <div className="shrink-0 border-b border-border bg-card/20 px-4 py-3">
      <div className="relative mx-auto w-full max-w-3xl">
        {mentions.length > 0 ? (
          <div className="mb-1.5 flex flex-wrap gap-1">
            {mentions.map((m) => (
              <Badge key={m.id} variant="secondary" className="gap-1 py-0.5 pr-1 text-[10px]">
                <span className="max-w-[180px] truncate">@{m.title}</span>
                <button
                  type="button"
                  onClick={() => removeMention(m.id)}
                  title="Remove mention"
                  className="rounded-full p-0.5 hover:bg-background/60"
                >
                  <X className="h-2.5 w-2.5" />
                </button>
              </Badge>
            ))}
          </div>
        ) : null}
        <div className="flex items-center gap-2 rounded-full border border-input bg-background py-1 pl-3 pr-1 shadow-sm focus-within:ring-1 focus-within:ring-ring">
          <Search className="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
          <input
            ref={inputRef}
            value={question}
            onChange={(e) => {
              setQuestion(e.target.value);
              updateMentionState(e.target.value, e.target.selectionStart ?? e.target.value.length);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                if (mentionQuery !== null && mentionMatches.length > 0) {
                  e.preventDefault();
                  selectMention(mentionMatches[0]);
                } else if (mentionQuery === null) {
                  e.preventDefault();
                  submit();
                }
              }
              if (e.key === "Escape" && mentionQuery !== null) {
                setMentionQuery(null);
                setMentionMatchStart(null);
              }
            }}
            placeholder="Ask a question, or type @ to reference a note — self-healed against the live code…"
            className="h-7 flex-1 bg-transparent text-xs focus:outline-none"
          />
          <Button
            type="button"
            size="sm"
            className="h-7 rounded-full px-3 text-[11px]"
            disabled={!question.trim() || asking}
            onClick={() => submit()}
          >
            {asking ? "asking…" : "ask"}
          </Button>
        </div>
        {mentionQuery !== null && mentionMatches.length > 0 ? (
          <div className="absolute left-0 right-16 top-full z-20 mt-1 max-h-56 overflow-y-auto rounded-md border border-border bg-card shadow-lg">
            {mentionMatches.map((n) => (
              <button
                key={n.id}
                type="button"
                onClick={() => selectMention(n)}
                className="block w-full truncate px-3 py-1.5 text-left text-xs hover:bg-accent"
              >
                {n.title}
              </button>
            ))}
          </div>
        ) : null}
      </div>
      {!question && !asking && mentions.length === 0 ? (
        <div className="mx-auto mt-1.5 flex w-full max-w-3xl flex-wrap gap-1.5">
          {SUGGESTED_QUESTIONS.map((q) => (
            <button
              key={q}
              type="button"
              onClick={() => submit(q)}
              className="rounded-full border border-border/70 px-2.5 py-1 text-[10px] text-muted-foreground transition-colors hover:border-accent-foreground/30 hover:bg-accent/40 hover:text-foreground"
            >
              {q}
            </button>
          ))}
        </div>
      ) : null}
    </div>
  );
}

const STATUS_DOT: Record<Note["status"], string> = {
  draft: "bg-amber-400 animate-pulse",
  processed: "bg-emerald-400",
  promoted: "bg-sky-400",
  failed: "bg-rose-400",
};

/** The notes library: tab filter, collapsible add-note, click-to-load list. */
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
    <div className="flex min-h-0 flex-1 flex-col">
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
              if (e.key === "Enter") {
                e.preventDefault();
                onCreateTab();
              }
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
              <Button
                type="button"
                size="sm"
                variant="ghost"
                className="h-6 px-2 text-[10px]"
                onClick={onToggleAddNote}
              >
                cancel
              </Button>
              <Button
                type="button"
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
            type="button"
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
                {new Date(n.created_at).toLocaleString()}
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

/** GRID — full-width multi-card overview, for scanning/comparing many notes. */
function GridView({
  notes,
  notesLoading,
  selectedTab,
  onOpenNote,
  onPromote,
  onDelete,
}: {
  notes: Note[];
  notesLoading: boolean;
  selectedTab: string;
  onOpenNote: (id: string) => void;
  onPromote: (id: string) => void;
  onDelete: (id: string) => void;
}) {
  return (
    <div className="min-w-0 flex-1 overflow-y-auto p-4">
      {notesLoading ? (
        <p className="text-xs text-muted-foreground">loading notes…</p>
      ) : notes.length === 0 ? (
        <p className="text-xs text-muted-foreground">
          no notes {selectedTab === ALL_TABS ? "yet" : "in this tab"}. Paste one from the left panel.
        </p>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {notes.map((n) => (
            <NoteCard
              key={n.id}
              note={n}
              onOpen={() => onOpenNote(n.id)}
              onPromote={() => onPromote(n.id)}
              onDelete={() => onDelete(n.id)}
            />
          ))}
        </div>
      )}
    </div>
  );
}

function NoteCard({
  note,
  onOpen,
  onPromote,
  onDelete,
}: {
  note: Note;
  onOpen: () => void;
  onPromote: () => void;
  onDelete: () => void;
}) {
  const [section, setSection] = useState<"summary" | "technical" | "plain">("summary");
  const [revalidateNote, { isLoading: revalidating }] = useRevalidateNoteMutation();
  const badge = STATUS_BADGE[note.status];
  const pipeline = note.pipeline;

  const validationLabel = note.last_validated_at
    ? note.history_count > 0
      ? `healed ×${note.history_count} · checked ${relativeTime(note.last_validated_at)}`
      : `current as of ${relativeTime(note.last_validated_at)}`
    : "never validated vs code";

  return (
    <Card className="flex min-w-0 flex-col overflow-hidden">
      <CardHeader
        className="flex-row cursor-pointer items-start justify-between gap-2 pb-2 transition-colors hover:bg-accent/30"
        onClick={onOpen}
        title="Open in reader"
      >
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium" title={note.title}>
            {note.title}
          </h3>
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            {new Date(note.created_at).toLocaleString()}
          </p>
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
      </CardHeader>
      <CardContent className="min-w-0 flex-1 pt-0 text-xs">
        {pipeline ? (
          <>
            <div className="mb-2 flex gap-1">
              {(["summary", "technical", "plain"] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={(e) => {
                    e.stopPropagation();
                    setSection(s);
                  }}
                  className={cn(
                    "rounded px-1.5 py-0.5 text-[10px] font-medium uppercase tracking-wide transition-colors",
                    section === s
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:bg-accent/50",
                  )}
                >
                  {s}
                </button>
              ))}
            </div>
            <MarkdownView content={pipeline[section]} />
            {pipeline.tags.length > 0 ? (
              <div className="mt-2 flex flex-wrap gap-1">
                {pipeline.tags.map((tag) => (
                  <Badge key={tag} variant="outline" className="text-[9px]">
                    {tag}
                  </Badge>
                ))}
              </div>
            ) : null}
          </>
        ) : (
          <MarkdownView
            content={note.raw_text.length > 240 ? `${note.raw_text.slice(0, 240)}…` : note.raw_text}
          />
        )}
      </CardContent>
      <div className="flex shrink-0 items-center justify-end gap-1 border-t border-border/50 px-3 py-1.5">
        <Button type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-[10px] text-muted-foreground"
          title="Re-ground this note against the current code — heals it if the code moved on"
          disabled={revalidating}
          onClick={() => revalidateNote(note.id)}
        >
          <RefreshCw className={cn("mr-1 h-3 w-3", revalidating && "animate-spin")} />
          {revalidating ? "checking…" : "re-check vs code"}
        </Button>
        {!note.training.promoted ? (
          <Button type="button"
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
        <Button type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-destructive/70 hover:text-destructive"
          title="Delete note"
          onClick={onDelete}
        >
          <Trash2 className="h-3.5 w-3.5" />
        </Button>
      </div>
    </Card>
  );
}

/** CENTER (Reader mode) — either the selected note's structured read, or the latest ask answer. */
function CenterReaderPanel({
  view,
  onSelectSource,
  onPromote,
  onDelete,
  onRevalidate,
  revalidating,
  onSaveAnswer,
  savingAnswer,
  answerSaved,
}: {
  view: CenterView;
  onSelectSource: (id: string) => void;
  onPromote?: () => void;
  onDelete?: () => void;
  onRevalidate?: () => void;
  revalidating: boolean;
  onSaveAnswer: () => void;
  savingAnswer: boolean;
  answerSaved: boolean;
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
    const hasSources = data.sources.length > 0;
    return (
      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex shrink-0 items-center justify-between gap-2 border-b border-border/70 px-4 py-2.5">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
            answer
          </h3>
          {hasSources ? (
            <Button type="button"
              size="sm"
              variant="ghost"
              className="h-6 px-2 text-[10px]"
              disabled={savingAnswer || answerSaved}
              onClick={onSaveAnswer}
            >
              <BookMarked className="mr-1 h-3 w-3" />
              {answerSaved ? "saved as note" : savingAnswer ? "saving…" : "save as note"}
            </Button>
          ) : null}
        </div>
        <div className="min-w-0 flex-1 overflow-y-auto p-4">
          <MarkdownView content={data.answer} />
          {hasSources || data.healed.length > 0 ? (
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
          {data.code_sources.length > 0 ? (
            <div className="mt-2 flex flex-wrap items-center gap-1.5">
              {data.code_sources.map((c, i) => (
                <Badge
                  key={`${c.repo}/${c.file_path}:${c.start_line}-${i}`}
                  variant="outline"
                  className="font-mono text-[10px] text-sky-300/90"
                  title={`${c.repo}/${c.file_path}:${c.start_line}-${c.end_line}`}
                >
                  {c.file_path}:{c.start_line}-{c.end_line}
                </Badge>
              ))}
            </div>
          ) : null}
          {data.code_unavailable.length > 0 ? (
            <p className="mt-2 text-[10px] text-muted-foreground/70">
              code-grounding unavailable for: {data.code_unavailable.join(", ")}
            </p>
          ) : null}
        </div>
      </div>
    );
  }

  return (
    <NoteStructuredReader
      noteId={view.noteId}
      section={section}
      onSectionChange={setSection}
      onPromote={onPromote}
      onDelete={onDelete}
      onRevalidate={onRevalidate}
      revalidating={revalidating}
    />
  );
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
    <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
      <div className="flex shrink-0 items-start justify-between gap-2 border-b border-border/70 px-4 py-2.5">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium" title={note.title}>
            {note.title}
          </h3>
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            {new Date(note.created_at).toLocaleString()}
          </p>
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
        <Button type="button"
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
          <Button type="button"
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
        <Button type="button"
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

/** RIGHT (Reader mode) — the note's immutable original (raw_text). */
function OriginalPanel({ note, rawMode }: { note: Note | null; rawMode: boolean }) {
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      <div className="min-w-0 flex-1 overflow-y-auto p-4">
        {note ? (
          rawMode ? (
            <pre className="min-w-0 overflow-x-auto whitespace-pre-wrap break-words text-[11px] text-muted-foreground [overflow-wrap:anywhere]">
              {note.raw_text}
            </pre>
          ) : (
            <MarkdownView content={note.raw_text} />
          )
        ) : (
          <p className="text-xs text-muted-foreground/70">
            The immutable original paste shows here once a note is selected.
          </p>
        )}
      </div>
    </div>
  );
}
