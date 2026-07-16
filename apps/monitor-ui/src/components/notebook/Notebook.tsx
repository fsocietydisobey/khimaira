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

import { skipToken } from "@reduxjs/toolkit/query";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useParams } from "react-router-dom";
import {
  BookMarked,
  BookOpen,
  CheckCircle2,
  ChevronLeft,
  ChevronRight,
  Folder,
  Grid3x3,
  LayoutGrid,
  List,
  Plus,
  RefreshCw,
  Search,
  Star,
  Ticket as TicketIcon,
  Trash2,
  Upload,
} from "lucide-react";

import {
  useCreateNoteMutation,
  useCreateTabMutation,
  useDeleteNoteMutation,
  useGetNoteQuery,
  useListNotesQuery,
  useListProjectsQuery,
  useListTabsQuery,
  usePromoteNoteMutation,
  useRevalidateNoteMutation,
  useUpdateNoteMutation,
} from "@/api";
import {
  ChatBody,
  ChatHeaderControls,
  useNotebookChat,
  useRecordChat,
} from "@/components/notebook/ChatPanel";
import {
  DRAG_MIME,
  FileManagerSidebar,
  filterRecordsByRail,
  filterRecordsByTags,
  type Rail,
  TagFilterInput,
} from "@/components/notebook/FileManagerSidebar";
import { CopyRawMarkdownButton } from "@/components/notebook/CopyRawMarkdownButton";
import { IdChip } from "@/components/notebook/IdChip";
import { Library } from "@/components/notebook/LibraryView";
import { MarkdownView } from "@/components/notebook/MarkdownView";
import { TicketsView } from "@/components/notebook/TicketsView";
import {
  GENERAL_REPO,
  isStudyGuidePipeline,
  type Note,
  type NotebookTab,
  type NotePriority,
  type NoteTestStatus,
} from "@/components/notebook/notebookTypes";
import { PrioritySelector } from "@/components/notebook/PrioritySelector";
import { SensitiveBanner } from "@/components/notebook/SensitiveBadge";
import { TestStatusSelector } from "@/components/notebook/TestStatusSelector";
import { ProjectNavTabs } from "@/components/project/ProjectNavTabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const ALL_TABS = "__all__";
const CHAT_WIDTH = 380;
/** Well-known tab_id for the Personal/Behavior folder — a distinct left-list
 *  section, independent of the regular tab filter / repo scope. The
 *  pipeline reads notes in this tab as behavioral context for every LLM
 *  call (structuring, revalidation, ask-synthesis). */
const PERSONAL_TAB_ID = "personal";

// Grid (Joseph, 2026-07-04): folded into Files' "all folders" default —
// the full-corpus card triage view IS Files' landing state now, not a
// separate mode. Two modes only: Files (browse) + Reader (focus).
type ViewMode = "files" | "reader";

/** Grimoire (Phase 1f): top-level note⇄guide switch, independent of ViewMode
 *  (which only applies within "notes"). "library" mounts <Library /> in place
 *  of the whole notes grid/reader area. "tickets" (2026-07-11) mounts
 *  <TicketsView /> the same way — the local Linear-issue mirror, a
 *  completely separate resource from notes/guides. */
type NotebookSection = "notes" | "library" | "tickets";

// CHAT-UNIFY: answers used to be a special CenterView kind rendered by
// CenterReaderPanel; they're chat bubbles in the sidebar now, so this is
// just "which note is open".
type CenterView = { kind: "note"; noteId: string } | null;

// ---------------------------------------------------------------------------
// Persisted layout preferences — lazy-init from localStorage, guarded against
// unavailability (private browsing), mirroring KgMapper's palette/glow prefs.
// ---------------------------------------------------------------------------

export function usePersistedBoolean(key: string, defaultValue: boolean) {
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
    return Number.isFinite(saved) && saved >= min && saved <= max
      ? saved
      : defaultWidth;
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
export function SidePanelShell({
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
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {children}
      </div>
    </div>
  );

  return (
    <div className="flex shrink-0" style={{ width }}>
      {side === "right" && resizable ? (
        <ResizeHandle onResize={onResize!} />
      ) : null}
      {panel}
      {side === "left" && resizable ? (
        <ResizeHandle onResize={onResize!} />
      ) : null}
    </div>
  );
}

function ViewModeToggle({
  mode,
  onChange,
}: {
  mode: ViewMode;
  onChange: (m: ViewMode) => void;
}) {
  const options: { key: ViewMode; label: string; Icon: typeof LayoutGrid }[] = [
    { key: "files", label: "files", Icon: Folder },
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

  const [section, setSection] = useState<NotebookSection>("notes");
  const [viewMode, setViewMode] = useState<ViewMode>("files");
  const [selectedTab, setSelectedTab] = useState<string>(ALL_TABS);
  // Files mode (2026-07-04 FM) is a THIRD, independent lens — its own
  // folder-drill-down state, never scoping Grid's "everything" default
  // (Joseph: Grid is the triage/scan-all view, Files is organize-by-folder).
  const [rail, setRail] = useState<Rail>({ kind: "tab", tabId: null });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [creatingTab, setCreatingTab] = useState(false);
  const [newTabTitle, setNewTabTitle] = useState("");
  const [priorityFilter, setPriorityFilter] = useState<NotePriority | "">("");
  const [testStatusFilter, setTestStatusFilter] = useState<NoteTestStatus | "">("");
  const [showAddPersonalNote, setShowAddPersonalNote] = useState(false);
  const [personalDraft, setPersonalDraft] = useState("");
  const [centerView, setCenterView] = useState<CenterView>(null);
  const [originalRawMode, setOriginalRawMode] = useState(false);

  const [leftCollapsed, setLeftCollapsed] = usePersistedBoolean(
    "notebook-left-collapsed",
    false,
  );
  const [rightCollapsed, setRightCollapsed] = usePersistedBoolean(
    "notebook-right-collapsed",
    false,
  );
  const [leftWidth, resizeLeft] = useResizableWidth(
    "notebook-left-width",
    256,
    200,
    480,
    1,
  );
  const [rightWidth, resizeRight] = useResizableWidth(
    "notebook-right-width",
    420,
    280,
    720,
    -1,
  );
  const [chatCollapsed, setChatCollapsed] = usePersistedBoolean(
    "notebook-chat-collapsed",
    false,
  );
  // "All projects" — default OFF (scoped to the current project + General).
  // Doesn't hard-hide anything; it's a toggle, not a filter you can't undo.
  const [allProjects, setAllProjects] = usePersistedBoolean(
    "notebook-all-projects",
    false,
  );
  const repoScope = allProjects ? undefined : projectName || undefined;

  const { data: tabsData } = useListTabsQuery();
  const { data: notesData, isLoading: notesLoading } = useListNotesQuery(
    {
      tabId: selectedTab === ALL_TABS ? undefined : selectedTab,
      repo: repoScope,
      priority: priorityFilter || undefined,
      testStatus: testStatusFilter || undefined,
      sort: "-priority",
    },
    { pollingInterval: 3000 },
  );
  // Files mode (2026-07-04 FM) — a genuinely separate query: it does its own
  // folder drill-down + rail filtering over the FULL repo-scoped set, so it
  // must NOT be narrowed by Grid/Reader's selectedTab or priorityFilter.
  // skipToken when Files isn't the active mode — no wasted fetch.
  const { data: filesNotesData, isLoading: filesNotesLoading } =
    useListNotesQuery(viewMode === "files" ? { repo: repoScope } : skipToken, {
      pollingInterval: 5000,
    });
  // Personal/Behavior folder — independent of the tab filter / repo scope
  // above; always the same notes regardless of what the regular list is
  // scoped to.
  const { data: personalNotesData } = useListNotesQuery(
    { tabId: PERSONAL_TAB_ID },
    { pollingInterval: 5000 },
  );
  const { data: projectsData } = useListProjectsQuery();
  const [createNote, { isLoading: creatingNote }] = useCreateNoteMutation();
  const [createTab] = useCreateTabMutation();
  const [promoteNote] = usePromoteNoteMutation();
  const [deleteNote] = useDeleteNoteMutation();
  const [updateNote] = useUpdateNoteMutation();
  const [revalidateNote, { isLoading: revalidating }] =
    useRevalidateNoteMutation();

  const tabs = tabsData?.tabs ?? [];
  // Folders are notes' tab namespace (guide collections live in the
  // Library) — kind-scoped so the two never intermix in the FM tree.
  const folders = tabs.filter((t) => t.kind !== "collection");
  // Personal/Behavior notes are a distinct section (below) — never mixed
  // into the regular list/grid/reader/@-mention flow, matching the backend
  // excluding them from embedding + ask retrieval. Study guides are a
  // distinct KIND (grimoire) — housed in the Library, kind-filtered out here
  // the same way personal notes are. Tickets (2026-07-11) are a third
  // distinct KIND — housed in the Tickets view — same exclusion; this filter
  // predates tickets and was leaking them into the regular notes list/grid
  // until Joseph caught it live (they render with status="draft" since
  // tickets never enter the structuring pipeline, which read as "stuck
  // processing" in the UI).
  const notesExcludingPersonal = (notesData?.notes ?? []).filter(
    (n) => n.tab_id !== PERSONAL_TAB_ID,
  );
  // `Note.kind` types as `NoteKind` ("note" | "study_guide") — it doesn't
  // model "ticket" because a ticket isn't conceptually a note kind, it's a
  // separate record type that happens to share the same unscoped /api/notes
  // storage. The runtime data can still contain kind="ticket" here (this
  // query has no kind filter), so the comparison is cast to string rather
  // than widening NoteKind itself (which would wrongly imply "ticket" is a
  // valid notebook_create kind param).
  const notes = notesExcludingPersonal.filter(
    (n) => n.kind !== "study_guide" && (n.kind as string) !== "ticket",
  );
  // Files-mode's own record set — same personal/guide/ticket exclusions,
  // sourced from the unscoped query above.
  const filesNotes = (filesNotesData?.notes ?? []).filter(
    (n) =>
      n.tab_id !== PERSONAL_TAB_ID &&
      n.kind !== "study_guide" &&
      (n.kind as string) !== "ticket",
  );
  // Guides ARE askable/@-mentionable (unlike personal notes) — only excluded
  // from the browsing grid/list above, per void-null's settled contract.
  const mentionableNotes = notesExcludingPersonal;
  const personalNotes = personalNotesData?.notes ?? [];
  const repoOptions = [
    ...(projectsData ?? []).map((p) => p.name),
    GENERAL_REPO,
  ];
  const selectedNote =
    viewMode === "reader" && centerView?.kind === "note"
      ? (notes.find((n) => n.id === centerView.noteId) ?? null)
      : null;

  // CHAT-UNIFY: scope follows what's open — a note open scopes the sidebar
  // to that note (per-record chat, same route as guide chat); nothing open
  // is the notebook-wide ask. Both hooks are always called (never behind a
  // conditional) so the rules of hooks hold regardless of which is active —
  // useRecordChat(null) is inert (skipToken on every query).
  const chatNoteId = selectedNote?.id ?? null;
  const recordChat = useRecordChat(chatNoteId);
  const notebookChat = useNotebookChat();
  const activeChat = chatNoteId ? recordChat : notebookChat;
  const chatMode: "record" | "notebook" = chatNoteId ? "record" : "notebook";

  // Shared across both modes — a click on a card, a list row, or a cited
  // source always opens that note in the deep-read Reader.
  const handleSelectNote = (id: string) => {
    setViewMode("reader");
    setCenterView({ kind: "note", noteId: id });
  };

  const handleChangeRepo = (noteId: string, repo: string) =>
    updateNote({ id: noteId, repo });
  const handleChangePriority = (noteId: string, priority: NotePriority) =>
    updateNote({ id: noteId, priority });
  const handleChangeTestStatus = (noteId: string, testStatus: NoteTestStatus) =>
    updateNote({ id: noteId, test_status: testStatus });
  const handleToggleStarred = (noteId: string, starred: boolean) =>
    updateNote({ id: noteId, starred });

  // Shared by the Reader-mode capture box (defaults to the flat selectedTab
  // filter) AND Files mode (defaults to whatever folder the rail is
  // currently scoped to) — see NoteCaptureBox below. `tabId` undefined =
  // no folder (root/uncategorized).
  const handleAddNote = async (
    rawText: string,
    sensitive: boolean,
    tabId?: string,
  ) => {
    await createNote({
      raw_text: rawText,
      tab_id: tabId,
      // Quick-win default (Joseph, 2026-07-03): scope new notes to the
      // project they were pasted under, instead of the backend's hardcoded
      // "khimaira" fallback — a full repo-set/change UI is a separate spec.
      repo: projectName || undefined,
      sensitive,
    }).unwrap();
  };

  const handleAddPersonalNote = async () => {
    const text = personalDraft.trim();
    if (!text) return;
    await createNote({
      raw_text: text,
      tab_id: PERSONAL_TAB_ID,
      repo: GENERAL_REPO,
    }).unwrap();
    setPersonalDraft("");
    setShowAddPersonalNote(false);
  };

  const handleCreateTab = async () => {
    const title = newTabTitle.trim();
    const created = await createTab({ title, repo: projectName }).unwrap();
    setNewTabTitle("");
    setCreatingTab(false);
    setSelectedTab(created.id);
  };

  // Files mode only — multi-select + drag-move-pin over the unscoped
  // filesNotes set (Grid/Reader's selectedTab-scoped notes never touch this).
  const clearNoteSelection = () => setSelected(new Set());
  const moveNotesToTab = async (noteIds: string[], targetTabId: string) => {
    await Promise.all(
      noteIds.map((id) =>
        updateNote({ id, tab_id: targetTabId, pinned_placement: true })
          .unwrap()
          .catch(() => {
            /* one note failing to move shouldn't block the others */
          }),
      ),
    );
    clearNoteSelection();
  };

  const notesListPanel = (
    <NotesListPanel
      tabs={tabs}
      notes={notes}
      notesLoading={notesLoading}
      selectedTab={selectedTab}
      onSelectTab={setSelectedTab}
      priorityFilter={priorityFilter}
      onPriorityFilterChange={setPriorityFilter}
      testStatusFilter={testStatusFilter}
      onTestStatusFilterChange={setTestStatusFilter}
      selectedNoteId={
        viewMode === "reader" && centerView?.kind === "note"
          ? centerView.noteId
          : null
      }
      onSelectNote={handleSelectNote}
      creatingNote={creatingNote}
      onAddNote={(text, sensitive) =>
        handleAddNote(
          text,
          sensitive,
          selectedTab === ALL_TABS ? undefined : selectedTab,
        )
      }
      creatingTab={creatingTab}
      onStartCreateTab={() => setCreatingTab(true)}
      newTabTitle={newTabTitle}
      onNewTabTitleChange={setNewTabTitle}
      onCreateTab={handleCreateTab}
      onCancelCreateTab={() => {
        setCreatingTab(false);
        setNewTabTitle("");
      }}
      allProjects={allProjects}
      onToggleAllProjects={() => setAllProjects(!allProjects)}
    />
  );

  const notesListWithPersonal = (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
        {notesListPanel}
      </div>
      <PersonalFolderSection
        notes={personalNotes}
        selectedNoteId={
          viewMode === "reader" && centerView?.kind === "note"
            ? centerView.noteId
            : null
        }
        onSelectNote={handleSelectNote}
        showAddNote={showAddPersonalNote}
        onToggleAddNote={() => setShowAddPersonalNote((v) => !v)}
        draft={personalDraft}
        onDraftChange={setPersonalDraft}
        creatingNote={creatingNote}
        onAddNote={handleAddPersonalNote}
      />
    </div>
  );

  return (
    <div className="flex h-full flex-col overflow-hidden">
      <header className="shrink-0 border-b border-border bg-card/40 px-4 py-3">
        <div className="flex items-center justify-between gap-3">
          <div>
            <h2 className="text-sm font-semibold">Grimoire — {projectName}</h2>
            <p className="mt-0.5 text-[11px] text-muted-foreground">
              paste → auto-structure → ask → self-heal against the live code
            </p>
          </div>
          <div className="flex items-center gap-3">
            <NotebookSectionToggle section={section} onChange={setSection} />
            {section === "notes" ? (
              <ViewModeToggle mode={viewMode} onChange={setViewMode} />
            ) : null}
            <ProjectNavTabs projectName={projectName} />
          </div>
        </div>
      </header>

      {section === "library" ? (
        <Library />
      ) : section === "tickets" ? (
        <TicketsView />
      ) : viewMode === "files" ? (
        <NotesFileManager
          folders={folders}
          notes={filesNotes}
          isLoading={filesNotesLoading}
          rail={rail}
          onRailChange={setRail}
          newTabRepo={projectName}
          selected={selected}
          onToggleSelect={(id) =>
            setSelected((prev) => {
              const next = new Set(prev);
              if (next.has(id)) next.delete(id);
              else next.add(id);
              return next;
            })
          }
          onClearSelect={clearNoteSelection}
          onMoveToTab={(ids, tabId) => void moveNotesToTab(ids, tabId)}
          onOpenNote={handleSelectNote}
          onPromote={(id) => promoteNote(id)}
          onDelete={(id) => deleteNote(id)}
          repoOptions={repoOptions}
          onChangeRepo={handleChangeRepo}
          onChangePriority={handleChangePriority}
          onChangeTestStatus={handleChangeTestStatus}
          creatingNote={creatingNote}
          onAddNote={handleAddNote}
        />
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
            {notesListWithPersonal}
          </SidePanelShell>
          <CenterReaderPanel
            view={centerView}
            onPromote={
              selectedNote ? () => promoteNote(selectedNote.id) : undefined
            }
            onDelete={
              selectedNote
                ? () => {
                    setCenterView(null);
                    deleteNote(selectedNote.id);
                  }
                : undefined
            }
            onRevalidate={
              selectedNote ? () => revalidateNote(selectedNote.id) : undefined
            }
            revalidating={revalidating}
            repoOptions={repoOptions}
            onChangeRepo={handleChangeRepo}
            onChangePriority={handleChangePriority}
            onChangeTestStatus={handleChangeTestStatus}
            onToggleStarred={handleToggleStarred}
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
                  title={
                    originalRawMode
                      ? "Show rendered markdown"
                      : "Show raw pasted text"
                  }
                  className="rounded px-1.5 py-0.5 text-[9px] font-medium uppercase tracking-wide text-muted-foreground hover:bg-accent hover:text-foreground"
                >
                  {originalRawMode ? "rendered" : "raw"}
                </button>
              ) : null
            }
          >
            <OriginalPanel
              noteId={selectedNote?.id ?? null}
              rawMode={originalRawMode}
            />
          </SidePanelShell>
          <SidePanelShell
            side="right"
            label="chat"
            width={CHAT_WIDTH}
            collapsed={chatCollapsed}
            onToggleCollapsed={() => setChatCollapsed(!chatCollapsed)}
            resizable={false}
            extraHeader={<ChatHeaderControls state={activeChat} />}
          >
            <ChatBody
              state={activeChat}
              mode={chatMode}
              notes={mentionableNotes}
              sensitive={!!selectedNote?.sensitive}
            />
          </SidePanelShell>
        </div>
      )}
    </div>
  );
}

function NotebookSectionToggle({
  section,
  onChange,
}: {
  section: NotebookSection;
  onChange: (s: NotebookSection) => void;
}) {
  const options: {
    key: NotebookSection;
    label: string;
    Icon: typeof BookOpen;
  }[] = [
    { key: "notes", label: "notes", Icon: LayoutGrid },
    { key: "library", label: "library", Icon: BookMarked },
    { key: "tickets", label: "tickets", Icon: TicketIcon },
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
            section === key
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

const STATUS_DOT: Record<Note["status"], string> = {
  draft: "bg-amber-400 animate-pulse",
  processed: "bg-emerald-400",
  promoted: "bg-sky-400",
  failed: "bg-rose-400",
};

/** Repo selector — set/change which codebase a note validates against.
 *  "general" (GENERAL_REPO) means no codebase (cross-cutting notes);
 *  revalidate/ask code-grounding skip it entirely. Changing repo re-anchors
 *  future validation (the backend clears validated_git_sha/last_validated_at). */
function RepoSelector({
  repo,
  options,
  onChange,
}: {
  repo: string;
  options: string[];
  onChange: (repo: string) => void;
}) {
  return (
    <select
      value={repo}
      onChange={(e) => {
        e.stopPropagation();
        onChange(e.target.value);
      }}
      onClick={(e) => e.stopPropagation()}
      title="Repo this note validates against — change it if mis-tagged"
      className="rounded border border-border/50 bg-transparent px-1 py-0.5 text-[9px] uppercase tracking-wide text-muted-foreground hover:border-border hover:text-foreground focus:outline-none"
    >
      {options.map((r) => (
        <option key={r} value={r} className="bg-card text-foreground">
          {r === GENERAL_REPO ? "general" : r}
        </option>
      ))}
      {!options.includes(repo) ? (
        <option value={repo} className="bg-card text-foreground">
          {repo}
        </option>
      ) : null}
    </select>
  );
}

/**
 * PERSONAL — the Behavior/voice folder. Distinct from the regular
 * notes/tabs above: independent of the tab filter and repo scope, never
 * mixed into the grid/reader/@-mention flow (the backend never embeds or
 * retrieves these either — see notebook_retrieval.upsert_note). The
 * pipeline reads every note here as behavioral context, prepended to
 * every LLM call (structuring, revalidation, ask-synthesis).
 */
function PersonalFolderSection({
  notes,
  selectedNoteId,
  onSelectNote,
  showAddNote,
  onToggleAddNote,
  draft,
  onDraftChange,
  creatingNote,
  onAddNote,
}: {
  notes: Note[];
  selectedNoteId: string | null;
  onSelectNote: (id: string) => void;
  showAddNote: boolean;
  onToggleAddNote: () => void;
  draft: string;
  onDraftChange: (v: string) => void;
  creatingNote: boolean;
  onAddNote: () => void;
}) {
  return (
    <div className="shrink-0 border-t border-border">
      <div className="flex items-center justify-between px-2 py-1.5">
        <span className="text-[9px] font-medium uppercase tracking-wide text-muted-foreground/70">
          personal · voice &amp; behavior
        </span>
        <button
          type="button"
          onClick={onToggleAddNote}
          title="Add a behavioral-context note — read as instructions for every LLM call, never surfaced as an ask source"
          className="rounded p-0.5 text-muted-foreground hover:bg-accent/50 hover:text-foreground"
        >
          <Plus className="h-3 w-3" />
        </button>
      </div>
      {showAddNote ? (
        <div className="px-2 pb-2">
          <textarea
            autoFocus
            value={draft}
            onChange={(e) => onDraftChange(e.target.value)}
            placeholder="A rule for how notes get structured and answers get written…"
            rows={3}
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
              {creatingNote ? "adding…" : "add"}
            </Button>
          </div>
        </div>
      ) : null}
      <div className="max-h-40 overflow-y-auto">
        {notes.length === 0 ? (
          <p className="px-3 pb-2 text-[10px] text-muted-foreground/60">
            no behavioral notes yet.
          </p>
        ) : (
          notes.map((n) => (
            <button
              key={n.id}
              type="button"
              onClick={() => onSelectNote(n.id)}
              className={cn(
                "block w-full min-w-0 truncate px-3 py-1.5 text-left text-[11px] transition-colors",
                selectedNoteId === n.id ? "bg-accent" : "hover:bg-accent/40",
              )}
              title={n.title}
            >
              {n.title}
            </button>
          ))
        )}
      </div>
    </div>
  );
}

/** Files mode (2026-07-04 FM, revised same day) — notes' unified browse
 *  view: rails (Recent/Starred/Vault/Tags) + a folder tree in the sidebar
 *  for organizing, and a main pane that's ALWAYS a flat card grid/list (no
 *  breadcrumb drill-down, no folder-rows) — "all folders" (the default
 *  landing) shows the FULL corpus as cards, exactly what the old standalone
 *  Grid mode did (Joseph folded Grid into Files: the default must show
 *  cards immediately, not a tree to expand one-by-one). Selecting a
 *  specific folder in the sidebar narrows the same card grid to its direct
 *  members; Recent/Starred/Vault/a tag narrow it their own way. Reuses the
 *  real `NoteCard` (summary/technical/plain tabs + re-check/promote/delete)
 *  for the rich view and a compact `NoteFMListRow` for the dense one. */
function NotesFileManager({
  folders,
  notes,
  isLoading,
  rail,
  onRailChange,
  newTabRepo,
  selected,
  onToggleSelect,
  onClearSelect,
  onMoveToTab,
  onOpenNote,
  onPromote,
  onDelete,
  repoOptions,
  onChangeRepo,
  onChangePriority,
  onChangeTestStatus,
  creatingNote,
  onAddNote,
}: {
  folders: NotebookTab[];
  notes: Note[];
  isLoading: boolean;
  rail: Rail;
  onRailChange: (r: Rail) => void;
  newTabRepo: string;
  selected: Set<string>;
  onToggleSelect: (id: string) => void;
  onClearSelect: () => void;
  onMoveToTab: (ids: string[], tabId: string) => void;
  onOpenNote: (id: string) => void;
  onPromote: (id: string) => void;
  onDelete: (id: string) => void;
  repoOptions: string[];
  onChangeRepo: (noteId: string, repo: string) => void;
  onChangePriority: (noteId: string, priority: NotePriority) => void;
  onChangeTestStatus: (noteId: string, testStatus: NoteTestStatus) => void;
  creatingNote: boolean;
  onAddNote: (
    rawText: string,
    sensitive: boolean,
    tabId?: string,
  ) => Promise<void>;
}) {
  const [grid, setGrid] = usePersistedBoolean("notebook-fm-grid", true);
  const [search, setSearch] = useState("");
  const [priorityFilter, setPriorityFilter] = useState<NotePriority | "">("");
  const [testStatusFilter, setTestStatusFilter] = useState<NoteTestStatus | "">("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);

  const noteTags = useMemo(() => {
    const tags = new Set<string>();
    for (const n of notes) n.pipeline?.tags.forEach((t) => tags.add(t));
    return Array.from(tags).sort();
  }, [notes]);

  const items = useMemo(() => {
    let filtered = filterRecordsByRail(notes, rail);
    filtered = filterRecordsByTags(filtered, selectedTags);

    const query = search.trim().toLowerCase();
    if (query) {
      filtered = filtered.filter(
        (n) =>
          n.title.toLowerCase().includes(query) ||
          (n.pipeline?.tags.some((t) => t.toLowerCase().includes(query)) ??
            false),
      );
    }
    if (priorityFilter)
      filtered = filtered.filter((n) => n.priority === priorityFilter);
    if (testStatusFilter)
      filtered = filtered.filter((n) => n.test_status === testStatusFilter);

    return filtered;
  }, [rail, notes, selectedTags, search, priorityFilter, testStatusFilter]);

  const railLabel =
    rail.kind === "tab"
      ? rail.tabId === null
        ? "all folders"
        : (folders.find((f) => f.id === rail.tabId)?.title ?? "all folders")
      : rail.kind;

  const handleDragStartNote = (e: React.DragEvent, noteId: string) => {
    const ids =
      selected.has(noteId) && selected.size > 0
        ? Array.from(selected)
        : [noteId];
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(ids));
    e.dataTransfer.effectAllowed = "move";
  };

  return (
    <div className="flex flex-1 overflow-hidden">
      <div className="flex w-52 shrink-0 flex-col overflow-hidden border-r border-border bg-card/10">
        <FileManagerSidebar
          tabKind="folder"
          tabs={folders}
          rail={rail}
          onRailChange={onRailChange}
          onDropRecords={onMoveToTab}
          newTabRepo={newTabRepo}
        />
      </div>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex shrink-0 flex-col gap-2 border-b border-border/70 px-4 py-2.5">
          <div className="flex items-center justify-between gap-2">
            <h3 className="truncate text-sm font-medium capitalize">
              {railLabel}
            </h3>
            <div className="flex items-center gap-2">
              <TagFilterInput
                allTags={noteTags}
                selected={selectedTags}
                onChange={setSelectedTags}
              />
              <div className="flex items-center gap-0.5 rounded-md border border-border p-0.5">
                <button
                  type="button"
                  title="List view"
                  onClick={() => setGrid(false)}
                  className={cn(
                    "rounded p-1",
                    !grid
                      ? "bg-accent text-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  <List className="h-3.5 w-3.5" />
                </button>
                <button
                  type="button"
                  title="Grid view"
                  onClick={() => setGrid(true)}
                  className={cn(
                    "rounded p-1",
                    grid
                      ? "bg-accent text-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  <Grid3x3 className="h-3.5 w-3.5" />
                </button>
              </div>
            </div>
          </div>
          <NoteCaptureBox
            creating={creatingNote}
            defaultSensitive={rail.kind === "vault"}
            triggerLabel="new note"
            onSubmit={(text, sensitive) =>
              onAddNote(
                text,
                sensitive,
                rail.kind === "tab" ? (rail.tabId ?? undefined) : undefined,
              )
            }
          />
          <div className="flex items-center gap-2">
            <div className="relative max-w-sm flex-1">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="search notes…"
                className="w-full rounded-md border border-border bg-card/40 py-1.5 pl-7 pr-2 text-xs outline-none focus:border-ring"
              />
            </div>
            <select
              value={priorityFilter}
              onChange={(e) =>
                setPriorityFilter(e.target.value as NotePriority | "")
              }
              title="Filter by priority"
              className="h-8 shrink-0 rounded-md border border-input bg-background px-1.5 text-[11px] text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">any priority</option>
              <option value="urgent">🔴 urgent</option>
              <option value="high">🟠 high</option>
              <option value="normal">⚪ normal</option>
              <option value="low">⚫ low</option>
            </select>
            <select
              value={testStatusFilter}
              onChange={(e) =>
                setTestStatusFilter(e.target.value as NoteTestStatus | "")
              }
              title="Filter by testing status"
              className="h-8 shrink-0 rounded-md border border-input bg-background px-1.5 text-[11px] text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
            >
              <option value="">any test status</option>
              <option value="untested">⚪ untested</option>
              <option value="needs_testing">🟡 needs testing</option>
              <option value="in_review">🔵 in review</option>
              <option value="tested">🟢 tested</option>
            </select>
          </div>
          {selected.size > 0 ? (
            <div className="flex items-center gap-2 rounded-md border border-border bg-accent/30 px-2 py-1.5 text-[10px]">
              <span className="font-medium text-foreground">
                {selected.size} selected
              </span>
              <select
                value=""
                onChange={(e) => {
                  if (e.target.value)
                    onMoveToTab(Array.from(selected), e.target.value);
                }}
                className="h-6 rounded border border-input bg-background px-1 text-[10px] text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
              >
                <option value="">move to…</option>
                {folders.map((f) => (
                  <option key={f.id} value={f.id}>
                    {f.title}
                  </option>
                ))}
              </select>
              <button
                type="button"
                onClick={onClearSelect}
                className="ml-auto text-muted-foreground hover:text-foreground"
              >
                clear
              </button>
            </div>
          ) : null}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {isLoading ? (
            <p className="text-xs text-muted-foreground">loading…</p>
          ) : items.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {search ? `no notes match "${search}".` : "nothing here yet."}
            </p>
          ) : grid ? (
            <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-3">
              {items.map((n) => (
                <NoteCard
                  key={n.id}
                  note={n}
                  onOpen={() => onOpenNote(n.id)}
                  onPromote={() => onPromote(n.id)}
                  onDelete={() => onDelete(n.id)}
                  repoOptions={repoOptions}
                  onChangeRepo={(repo) => onChangeRepo(n.id, repo)}
                  onChangePriority={(p) => onChangePriority(n.id, p)}
                  onChangeTestStatus={(s) => onChangeTestStatus(n.id, s)}
                  selected={selected.has(n.id)}
                  onToggleSelect={() => onToggleSelect(n.id)}
                  onDragStart={(e) => handleDragStartNote(e, n.id)}
                />
              ))}
            </div>
          ) : (
            <div className="divide-y divide-border/50 rounded-md border border-border/50">
              {items.map((n) => (
                <NoteFMListRow
                  key={n.id}
                  note={n}
                  selected={selected.has(n.id)}
                  onOpen={() => onOpenNote(n.id)}
                  onToggleSelect={() => onToggleSelect(n.id)}
                  onDragStart={(e) => handleDragStartNote(e, n.id)}
                  onChangePriority={(p) => onChangePriority(n.id, p)}
                  onChangeTestStatus={(s) => onChangeTestStatus(n.id, s)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function NoteFMListRow({
  note,
  selected,
  onOpen,
  onToggleSelect,
  onDragStart,
  onChangePriority,
  onChangeTestStatus,
}: {
  note: Note;
  selected: boolean;
  onOpen: () => void;
  onToggleSelect: () => void;
  onDragStart: (e: React.DragEvent) => void;
  onChangePriority: (p: NotePriority) => void;
  onChangeTestStatus: (s: NoteTestStatus) => void;
}) {
  const badge = STATUS_BADGE[note.status];
  return (
    <div
      draggable
      onDragStart={onDragStart}
      onClick={onOpen}
      className={cn(
        "flex w-full min-w-0 cursor-pointer items-center gap-2 px-3 py-2 text-left text-xs transition-colors hover:bg-accent/40",
        selected && "bg-accent/50",
      )}
    >
      <input
        type="checkbox"
        checked={selected}
        onClick={(e) => e.stopPropagation()}
        onChange={onToggleSelect}
        className="shrink-0"
      />
      <span className="min-w-0 flex-1 truncate font-medium" title={note.title}>
        {note.title}
      </span>
      <span className="hidden shrink-0 text-[10px] text-muted-foreground md:inline">
        {relativeTime(note.updated_at)}
      </span>
      <div
        onClick={(e) => e.stopPropagation()}
        className="flex shrink-0 items-center gap-1"
      >
        <PrioritySelector
          priority={note.priority}
          onChange={onChangePriority}
        />
        <TestStatusSelector
          testStatus={note.test_status}
          onChange={onChangeTestStatus}
        />
      </div>
      <Badge variant={badge.variant} className="shrink-0 text-[9px]">
        {badge.label}
      </Badge>
      <div className="flex shrink-0 items-center gap-1">
        {note.starred ? (
          <Star className="h-3 w-3 shrink-0 fill-amber-400 text-amber-400" />
        ) : null}
        {note.sensitive ? (
          <span title="Sensitive — the assistant sees a redacted copy">🔒</span>
        ) : null}
        <IdChip id={note.id} />
      </div>
    </div>
  );
}

/** Shared create affordance for notes AND guides (FILE-MANAGER regression
 *  fix, 2026-07-04 — Files mode dropped the only create button when it
 *  replaced the flat notes sidebar; this is now mounted in BOTH Reader
 *  mode's left panel and Files mode's header so the create path is never
 *  lost again). Collapsed = a single trigger button; expanded = raw_text
 *  textarea + 🔒 sensitive toggle (the vault-add path — Files mode pre-
 *  toggles this on when the Vault rail is active). Owns its own draft/
 *  sensitive/expanded state; the caller only supplies where the note lands
 *  (`onSubmit`'s job) and what to call it. */
export function NoteCaptureBox({
  creating,
  onSubmit,
  defaultSensitive = false,
  triggerLabel = "paste note",
  triggerClassName,
  placeholder = "Paste a note…",
}: {
  creating: boolean;
  onSubmit: (rawText: string, sensitive: boolean) => Promise<void>;
  defaultSensitive?: boolean;
  triggerLabel?: string;
  triggerClassName?: string;
  placeholder?: string;
}) {
  const [expanded, setExpanded] = useState(false);
  const [draft, setDraft] = useState("");
  const [sensitive, setSensitive] = useState(defaultSensitive);

  // Re-sync to the caller's default (e.g. Vault rail → pre-toggle on) while
  // collapsed — once the user opens the box and touches the checkbox
  // themselves, their choice shouldn't get silently overwritten by a rail
  // change happening behind it.
  useEffect(() => {
    if (!expanded) setSensitive(defaultSensitive);
  }, [defaultSensitive, expanded]);

  const handleSubmit = async () => {
    const text = draft.trim();
    if (!text || creating) return;
    await onSubmit(text, sensitive);
    setDraft("");
    setExpanded(false);
  };

  if (!expanded) {
    return (
      <Button
        type="button"
        size="sm"
        variant="outline"
        className={cn("h-7 text-[11px]", triggerClassName)}
        onClick={() => setExpanded(true)}
      >
        <Plus className="mr-1.5 h-3.5 w-3.5" />
        {triggerLabel}
      </Button>
    );
  }

  return (
    <div>
      <textarea
        autoFocus
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        placeholder={placeholder}
        rows={4}
        className="w-full resize-y rounded-md border border-input bg-background px-2 py-1.5 text-[11px] font-mono focus:outline-none focus:ring-1 focus:ring-ring"
      />
      <div className="mt-1.5 flex items-center justify-between gap-1">
        <label className="flex items-center gap-1 text-[10px] text-muted-foreground">
          <input
            type="checkbox"
            checked={sensitive}
            onChange={(e) => setSensitive(e.target.checked)}
            className="h-3 w-3"
          />
          🔒 sensitive
        </label>
        <div className="flex items-center gap-1">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-[10px]"
            onClick={() => setExpanded(false)}
          >
            cancel
          </Button>
          <Button
            type="button"
            size="sm"
            className="h-6 px-2 text-[10px]"
            disabled={!draft.trim() || creating}
            onClick={() => void handleSubmit()}
          >
            <Upload className="mr-1 h-3 w-3" />
            {creating ? "adding…" : "add"}
          </Button>
        </div>
      </div>
    </div>
  );
}

/** The notes library: tab filter, collapsible add-note, click-to-load list. */
function NotesListPanel({
  tabs,
  notes,
  notesLoading,
  selectedTab,
  onSelectTab,
  priorityFilter,
  onPriorityFilterChange,
  testStatusFilter,
  onTestStatusFilterChange,
  selectedNoteId,
  onSelectNote,
  creatingNote,
  onAddNote,
  creatingTab,
  onStartCreateTab,
  newTabTitle,
  onNewTabTitleChange,
  onCreateTab,
  onCancelCreateTab,
  allProjects,
  onToggleAllProjects,
}: {
  tabs: { id: string; title: string; note_ids: string[] }[];
  notes: Note[];
  notesLoading: boolean;
  selectedTab: string;
  onSelectTab: (id: string) => void;
  priorityFilter: NotePriority | "";
  onPriorityFilterChange: (p: NotePriority | "") => void;
  testStatusFilter: NoteTestStatus | "";
  onTestStatusFilterChange: (s: NoteTestStatus | "") => void;
  selectedNoteId: string | null;
  onSelectNote: (id: string) => void;
  creatingNote: boolean;
  onAddNote: (rawText: string, sensitive: boolean) => Promise<void>;
  creatingTab: boolean;
  onStartCreateTab: () => void;
  newTabTitle: string;
  onNewTabTitleChange: (v: string) => void;
  onCreateTab: () => void;
  onCancelCreateTab: () => void;
  allProjects: boolean;
  onToggleAllProjects: () => void;
}) {
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="flex shrink-0 items-center gap-1.5 border-b border-border/70 px-2 py-1.5">
        <select
          value={selectedTab}
          onChange={(e) => onSelectTab(e.target.value)}
          title="Filter by collection"
          className="h-7 min-w-0 flex-1 rounded-md border border-input bg-background px-2 text-[11px] text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        >
          <option value={ALL_TABS}>all</option>
          {tabs.map((t) => (
            <option key={t.id} value={t.id}>
              {t.title} ({t.note_ids.length})
            </option>
          ))}
        </select>
        <select
          value={priorityFilter}
          onChange={(e) =>
            onPriorityFilterChange(e.target.value as NotePriority | "")
          }
          title="Filter by priority"
          className="h-7 shrink-0 rounded-md border border-input bg-background px-1.5 text-[11px] text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        >
          <option value="">any priority</option>
          <option value="urgent">🔴 urgent</option>
          <option value="high">🟠 high</option>
          <option value="normal">⚪ normal</option>
          <option value="low">⚫ low</option>
        </select>
        <select
          value={testStatusFilter}
          onChange={(e) =>
            onTestStatusFilterChange(e.target.value as NoteTestStatus | "")
          }
          title="Filter by testing status"
          className="h-7 shrink-0 rounded-md border border-input bg-background px-1.5 text-[11px] text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring"
        >
          <option value="">any test status</option>
          <option value="untested">⚪ untested</option>
          <option value="needs_testing">🟡 needs testing</option>
          <option value="in_review">🔵 in review</option>
          <option value="tested">🟢 tested</option>
        </select>
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
            title="New top-level folder"
            className="rounded-md p-1 text-muted-foreground hover:bg-accent/50"
          >
            <Plus className="h-3 w-3" />
          </button>
        )}
      </div>

      <div className="flex shrink-0 items-center justify-between border-b border-border/70 px-2 py-1">
        <span className="text-[9px] uppercase tracking-wide text-muted-foreground/60">
          {allProjects ? "all projects" : "current project + general"}
        </span>
        <button
          type="button"
          onClick={onToggleAllProjects}
          className={cn(
            "rounded-md px-1.5 py-0.5 text-[9px] font-medium transition-colors",
            allProjects
              ? "bg-accent text-accent-foreground"
              : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
          )}
        >
          all projects
        </button>
      </div>

      <div className="shrink-0 border-b border-border/70 p-2">
        <NoteCaptureBox
          creating={creatingNote}
          onSubmit={onAddNote}
          triggerClassName="w-full"
        />
      </div>

      <div className="min-h-0 flex-1 overflow-y-auto">
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
                  className={cn(
                    "h-1.5 w-1.5 shrink-0 rounded-full",
                    STATUS_DOT[n.status],
                  )}
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

export function relativeTime(iso: string): string {
  const diffMin = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

const STATUS_BADGE: Record<
  Note["status"],
  {
    label: string;
    variant: "outline" | "secondary" | "default" | "destructive";
  }
> = {
  draft: { label: "processing…", variant: "outline" },
  processed: { label: "processed", variant: "secondary" },
  promoted: { label: "promoted", variant: "default" },
  failed: { label: "structuring failed", variant: "destructive" },
};

/** Files' rich card (Grid mode's original render, reused verbatim there
 *  after Grid folded into Files 2026-07-04) — `selected`/`onToggleSelect`/
 *  `onDragStart` are optional so Files mode can layer multi-select +
 *  drag-to-move on top without a second card type. */
function NoteCard({
  note,
  onOpen,
  onPromote,
  onDelete,
  repoOptions,
  onChangeRepo,
  onChangePriority,
  onChangeTestStatus,
  selected,
  onToggleSelect,
  onDragStart,
}: {
  note: Note;
  onOpen: () => void;
  onPromote: () => void;
  onDelete: () => void;
  repoOptions: string[];
  onChangeRepo: (repo: string) => void;
  onChangePriority: (priority: NotePriority) => void;
  onChangeTestStatus: (testStatus: NoteTestStatus) => void;
  selected?: boolean;
  onToggleSelect?: () => void;
  onDragStart?: (e: React.DragEvent) => void;
}) {
  const [section, setSection] = useState<"summary" | "technical" | "plain">(
    "summary",
  );
  const [revalidateNote, { isLoading: revalidating }] =
    useRevalidateNoteMutation();
  const badge = STATUS_BADGE[note.status];
  // Files/Grid only ever render notes (study guides are excluded upstream
  // and live in the Library), so pipeline here is always NotePipeline —
  // narrow explicitly since Note.pipeline's static type is the
  // discriminated union.
  const pipeline =
    note.pipeline && !isStudyGuidePipeline(note.pipeline)
      ? note.pipeline
      : null;

  const validationLabel = note.last_validated_at
    ? note.history_count > 0
      ? `healed ×${note.history_count} · checked ${relativeTime(note.last_validated_at)}`
      : `current as of ${relativeTime(note.last_validated_at)}`
    : "never validated vs code";

  return (
    <Card
      draggable={!!onDragStart}
      onDragStart={onDragStart}
      className={cn(
        "relative flex min-w-0 flex-col overflow-hidden",
        selected && "border-ring",
      )}
    >
      {onToggleSelect ? (
        <input
          type="checkbox"
          checked={!!selected}
          onClick={(e) => e.stopPropagation()}
          onChange={onToggleSelect}
          className="absolute left-2 top-2 z-10"
        />
      ) : null}
      <CardHeader
        className={cn(
          "flex-row cursor-pointer items-start justify-between gap-2 pb-2 transition-colors hover:bg-accent/30",
          onToggleSelect && "pl-7",
        )}
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
              note.history_count > 0
                ? "text-amber-400/80"
                : "text-muted-foreground/70",
            )}
          >
            {validationLabel}
          </p>
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <div className="flex items-center gap-1">
            {note.starred ? (
              <Star className="h-3 w-3 shrink-0 fill-amber-400 text-amber-400" />
            ) : null}
            {note.sensitive ? (
              <span title="Sensitive — the assistant sees a redacted copy">
                🔒
              </span>
            ) : null}
            <IdChip id={note.id} />
          </div>
          <PrioritySelector
            priority={note.priority}
            onChange={onChangePriority}
          />
          <TestStatusSelector
            testStatus={note.test_status}
            onChange={onChangeTestStatus}
          />
          <Badge variant={badge.variant} className="text-[10px]">
            {badge.label}
          </Badge>
          {note.resolution ? (
            <Badge
              variant="outline"
              className="border-emerald-500/40 text-[10px] text-emerald-400"
            >
              ✅ resolved
            </Badge>
          ) : null}
          <RepoSelector
            repo={note.repo}
            options={repoOptions}
            onChange={onChangeRepo}
          />
        </div>
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
            content={
              note.raw_text.length > 240
                ? `${note.raw_text.slice(0, 240)}…`
                : note.raw_text
            }
          />
        )}
      </CardContent>
      <div className="flex shrink-0 items-center justify-end gap-1 border-t border-border/50 px-3 py-1.5">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-[10px] text-muted-foreground"
          title="Re-ground this note against the current code — heals it if the code moved on"
          disabled={revalidating}
          onClick={() => revalidateNote(note.id)}
        >
          <RefreshCw
            className={cn("mr-1 h-3 w-3", revalidating && "animate-spin")}
          />
          {revalidating ? "checking…" : "re-check vs code"}
        </Button>
        {!note.training.promoted ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-[10px]"
            title="Mark as good for training — feeds the mnemosyne distiller"
            onClick={onPromote}
          >
            promote
          </Button>
        ) : (
          <span className="px-2 text-[10px] text-muted-foreground/60">
            promoted for training
          </span>
        )}
        <Button
          type="button"
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
  onPromote,
  onDelete,
  onRevalidate,
  revalidating,
  repoOptions,
  onChangeRepo,
  onChangePriority,
  onChangeTestStatus,
  onToggleStarred,
}: {
  view: CenterView;
  onPromote?: () => void;
  onDelete?: () => void;
  onRevalidate?: () => void;
  revalidating: boolean;
  repoOptions: string[];
  onChangeRepo: (noteId: string, repo: string) => void;
  onChangePriority: (noteId: string, priority: NotePriority) => void;
  onChangeTestStatus: (noteId: string, testStatus: NoteTestStatus) => void;
  onToggleStarred: (noteId: string, starred: boolean) => void;
}) {
  const [section, setSection] = useState<"summary" | "technical" | "plain">(
    "summary",
  );

  if (!view) {
    return (
      <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
        Select a note from the left, or ask in the chat sidebar.
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
      repoOptions={repoOptions}
      onChangeRepo={onChangeRepo}
      onChangePriority={onChangePriority}
      onChangeTestStatus={onChangeTestStatus}
      onToggleStarred={onToggleStarred}
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
  repoOptions,
  onChangeRepo,
  onChangePriority,
  onChangeTestStatus,
  onToggleStarred,
}: {
  noteId: string;
  section: "summary" | "technical" | "plain";
  onSectionChange: (s: "summary" | "technical" | "plain") => void;
  onPromote?: () => void;
  onDelete?: () => void;
  onRevalidate?: () => void;
  revalidating: boolean;
  repoOptions: string[];
  onChangeRepo: (noteId: string, repo: string) => void;
  onChangePriority: (noteId: string, priority: NotePriority) => void;
  onChangeTestStatus: (noteId: string, testStatus: NoteTestStatus) => void;
  onToggleStarred: (noteId: string, starred: boolean) => void;
}) {
  // Always polls (2026-07-07, Joseph report: an agent's `notebook_add_
  // resolution` MCP-tool call — a write from a totally different process,
  // never dispatched through this app's own RTK Query store — never showed
  // up in an already-open reader; only a full reload picked it up, since
  // gating polling to status==="draft" meant a settled note stopped
  // refetching the moment its first structuring pass finished).
  //
  // This used to be gated to status==="draft" only, because unconditional
  // polling previously caused a REAL flicker regression (2026-07-04 report):
  // every poll tick re-rendered MarkdownView, remounting MermaidBlock and
  // re-running mermaid.render, even when the note's content hadn't changed.
  // Root cause (fixed alongside this change, not worked around): (a) this
  // hook had no `selectFromResult`, so the component re-rendered on every
  // tick's isFetching/fulfilledTimeStamp churn even though RTK Query's
  // structural sharing kept `data` referentially stable; (b)
  // MarkdownView's `components` map for ReactMarkdown was a fresh object
  // literal every render, which forces React to remount every heading/
  // code-block/MermaidBlock at that position instead of diffing them.
  // Both are fixed now (see this hook's `selectFromResult` below and
  // MarkdownView.tsx's memoized `components`), so unconditional polling no
  // longer flickers — verify this holds if either fix is ever reverted.
  //
  // Single-note fetch (`getNote`), NOT the bulk `listNotes` — live-verified
  // (2026-07-04) the list endpoint MASKS raw_text to a placeholder for
  // sensitive notes ("[sensitive note — open it to view the real content]"),
  // per the sensitive-notes spec's own "list never returns raw_text of a
  // sensitive note in bulk" rule. The reader must show the REAL text, so it
  // can't source from the list — using the single-note endpoint (which
  // returns the real raw_text) is what the security boundary requires, not
  // a stylistic choice.
  const { data: note } = useGetNoteQuery(noteId, {
    pollingInterval: 3000,
    selectFromResult: ({ data }) => ({ data }),
  });
  const stillProcessing = !!note && note.status === "draft";
  // A note that already has tabs but is back in "draft" is being reprocessed
  // (raw_text changed) — the old tabs stay visible, badged as reprocessing
  // rather than the plain "processing…" a first-time structuring pass gets.
  const reprocessing = stillProcessing && !!note.pipeline;

  if (!note) {
    return (
      <div className="flex flex-1 items-center justify-center text-xs text-muted-foreground">
        Note not found — it may have been deleted.
      </div>
    );
  }
  const badge = STATUS_BADGE[note.status];
  // This reader only ever shows notes (study guides live in the Library and
  // are excluded from the list this component's caller reads from) — narrow
  // explicitly since Note.pipeline's static type is the discriminated union.
  const pipeline =
    note.pipeline && !isStudyGuidePipeline(note.pipeline)
      ? note.pipeline
      : null;
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
              note.history_count > 0
                ? "text-amber-400/80"
                : "text-muted-foreground/70",
            )}
          >
            {validationLabel}
          </p>
          {note.structured_at ? (
            <p className="mt-0.5 text-[10px] text-muted-foreground/70">
              structured {relativeTime(note.structured_at)}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <div className="flex items-center gap-1">
            <button
              type="button"
              title={note.starred ? "Unstar" : "Star"}
              onClick={() => onToggleStarred(note.id, !note.starred)}
            >
              <Star
                className={cn(
                  "h-3.5 w-3.5",
                  note.starred
                    ? "fill-amber-400 text-amber-400"
                    : "text-muted-foreground",
                )}
              />
            </button>
            {note.sensitive ? (
              <span title="Sensitive — the assistant sees a redacted copy">
                🔒
              </span>
            ) : null}
            <IdChip id={note.id} />
          </div>
          <PrioritySelector
            priority={note.priority}
            onChange={(priority) => onChangePriority(note.id, priority)}
          />
          <TestStatusSelector
            testStatus={note.test_status}
            onChange={(testStatus) => onChangeTestStatus(note.id, testStatus)}
          />
          {reprocessing ? (
            <Badge
              variant="outline"
              className="animate-pulse border-amber-500/40 text-[10px] text-amber-400"
            >
              ⟳ reprocessing…
            </Badge>
          ) : (
            <Badge variant={badge.variant} className="text-[10px]">
              {badge.label}
            </Badge>
          )}
          {note.resolution ? (
            <Badge
              variant="outline"
              className="border-emerald-500/40 text-[10px] text-emerald-400"
            >
              ✅ resolved
            </Badge>
          ) : null}
          <RepoSelector
            repo={note.repo}
            options={repoOptions}
            onChange={(repo) => onChangeRepo(note.id, repo)}
          />
        </div>
      </div>

      <div className="min-w-0 min-h-0 flex-1 overflow-y-auto p-4">
        {note.sensitive ? (
          <SensitiveBanner redactions={note.redactions} />
        ) : null}
        {pipeline ? (
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
            <MarkdownView content={pipeline[section]} />
            {pipeline.tags.length > 0 ? (
              <div className="mt-3 flex flex-wrap gap-1">
                {pipeline.tags.map((tag) => (
                  <Badge key={tag} variant="outline" className="text-[9px]">
                    {tag}
                  </Badge>
                ))}
              </div>
            ) : null}
          </>
        ) : note.tab_id === PERSONAL_TAB_ID ? (
          <p className="text-xs text-muted-foreground/70">
            Behavioral context — read as raw_text directly (see the original
            panel), not structured into sections. Never surfaced as an ask
            source.
          </p>
        ) : (
          <p className="text-xs text-muted-foreground/70">
            Still processing — the original is shown on the right; this panel
            fills in once structuring completes.
          </p>
        )}
        {note.resolution ? (
          <div className="mt-4 rounded-md border border-emerald-500/30 bg-emerald-500/5 p-3">
            <div className="mb-1.5 flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-emerald-400">
              <CheckCircle2 className="h-3.5 w-3.5" />
              Resolution
            </div>
            <MarkdownView content={note.resolution} />
            <p className="mt-2 text-[10px] text-muted-foreground/70">
              resolved by {note.resolved_by || "(unattributed)"}
              {note.resolved_at
                ? ` · ${new Date(note.resolved_at).toLocaleString()}`
                : ""}
            </p>
          </div>
        ) : null}
      </div>

      <div className="flex shrink-0 items-center justify-end gap-1 border-t border-border/50 px-3 py-1.5">
        <Button
          type="button"
          size="sm"
          variant="ghost"
          className="h-6 px-2 text-[10px] text-muted-foreground"
          title="Re-ground this note against the current code — heals it if the code moved on"
          disabled={revalidating}
          onClick={onRevalidate}
        >
          <RefreshCw
            className={cn("mr-1 h-3 w-3", revalidating && "animate-spin")}
          />
          {revalidating ? "checking…" : "re-check vs code"}
        </Button>
        {!note.training.promoted ? (
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 px-2 text-[10px]"
            title="Mark as good for training — feeds the mnemosyne distiller"
            onClick={onPromote}
          >
            promote
          </Button>
        ) : (
          <span className="px-2 text-[10px] text-muted-foreground/60">
            promoted for training
          </span>
        )}
        <Button
          type="button"
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
/** Fetches the single note directly (`getNote`), NOT the masked bulk list —
 *  same live-verified reason as `NoteStructuredReader`: the list endpoint
 *  replaces a sensitive note's raw_text with a placeholder, and this panel's
 *  whole job is showing the real, immutable original. */
function OriginalPanel({
  noteId,
  rawMode,
}: {
  noteId: string | null;
  rawMode: boolean;
}) {
  // Polls too (see NoteStructuredReader's comment) — an externally-added
  // resolution or raw_text edit should show up here without a reload too.
  const { data: note } = useGetNoteQuery(noteId ?? skipToken, {
    pollingInterval: 3000,
    selectFromResult: ({ data }) => ({ data }),
  });
  return (
    <div className="flex min-h-0 flex-1 flex-col overflow-hidden">
      {note ? (
        <div className="flex shrink-0 justify-end border-b border-border/50 px-2 py-1">
          <CopyRawMarkdownButton text={note.raw_text} />
        </div>
      ) : null}
      <div className="min-w-0 min-h-0 flex-1 overflow-y-auto p-4">
        {note ? (
          rawMode ? (
            // whitespace-pre-wrap (not break-words/overflow-wrap:anywhere,
            // 2026-07-10): prose still wraps softly at word boundaries as
            // before, but a run with no break opportunity — a pasted
            // box-drawing ASCII table, a long token — now overflows into
            // the horizontal scrollbar (overflow-x-auto, already present)
            // instead of being force-broken mid-character. Forced breaking
            // was shredding box-drawing tables (┌─┬─┐ style) into an
            // unreadable mess, since it has no problem breaking mid-glyph.
            <pre className="min-w-0 overflow-x-auto whitespace-pre-wrap text-[11px] text-muted-foreground">
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
