/**
 * Library — file-manager presentation over the auto-organized guide store
 * (Grimoire FILE-MANAGER, tasks/grimoire/FILE-MANAGER.md). Collections are a
 * TREE now (`NotebookTab.parent_id`), not a flat list — hierarchy is a
 * PRESENTATION layer over the existing content-based self-organization, not
 * a manual filesystem: a drag-move sets `tab_id` AND `pinned_placement=true`
 * (the organizer skips pinned notes and keeps auto-filing everything else).
 *
 * Two states, like Notebook's own grid/reader split: browse (this file's
 * FileManager — sidebar rails + tree + list/grid main pane) and read
 * (clickable TOC + the full guide via the shared MarkdownView). Reuses
 * Notebook's SidePanelShell/usePersistedBoolean/relativeTime for layout
 * consistency instead of re-inventing collapsible panels or time formatting
 * here.
 */

import { useMemo, useRef, useState } from "react";
import { Grid3x3, List, Star } from "lucide-react";
import { ChevronLeft, Search } from "lucide-react";

import {
  useCreateNoteMutation,
  useListNotesQuery,
  useListTabsQuery,
  useUpdateNoteMutation,
} from "@/api";
import { CopyRawMarkdownButton } from "@/components/notebook/CopyRawMarkdownButton";
import { IdChip } from "@/components/notebook/IdChip";
import {
  ChatBody,
  ChatHeaderControls,
  useRecordChat,
} from "@/components/notebook/ChatPanel";
import {
  ancestorChain,
  DRAG_MIME,
  filterRecordsByRail,
  filterRecordsByTags,
  FileManagerSidebar,
  type Rail,
  TagFilterInput,
  useTabTree,
} from "@/components/notebook/FileManagerSidebar";
import { MarkdownView } from "@/components/notebook/MarkdownView";
import {
  NoteCaptureBox,
  relativeTime,
  SidePanelShell,
  usePersistedBoolean,
} from "@/components/notebook/Notebook";
import {
  GENERAL_REPO,
  isStudyGuidePipeline,
  type Note,
  type NotebookTab,
  type NotePriority,
} from "@/components/notebook/notebookTypes";
import { PrioritySelector } from "@/components/notebook/PrioritySelector";
import { SensitiveBanner } from "@/components/notebook/SensitiveBadge";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const READER_TOC_WIDTH = 240;
const READER_CHAT_WIDTH = 380;

export function Library() {
  const [selectedGuideId, setSelectedGuideId] = useState<string | null>(null);

  // Guides are global (cross-repo housing is the point of the grimoire), so
  // no repo scope here — unlike the regular note list, which follows the
  // project route it's mounted under.
  const { data: guidesData, isLoading } = useListNotesQuery(
    { kind: "study_guide" },
    { pollingInterval: 5000 },
  );
  const { data: tabsData } = useListTabsQuery();

  const guides = guidesData?.notes ?? [];
  const collections = (tabsData?.tabs ?? []).filter(
    (t) => t.kind === "collection",
  );
  const selectedGuide = selectedGuideId
    ? (guides.find((g) => g.id === selectedGuideId) ?? null)
    : null;

  if (selectedGuideId && !selectedGuide && !isLoading) {
    // Guide vanished from underneath us (deleted elsewhere) — fall back to
    // the file manager rather than showing a dead reader.
    setSelectedGuideId(null);
  }

  return selectedGuide ? (
    <GuideReader
      guide={selectedGuide}
      onBack={() => setSelectedGuideId(null)}
    />
  ) : (
    <FileManager
      guides={guides}
      collections={collections}
      isLoading={isLoading}
      onOpenGuide={setSelectedGuideId}
    />
  );
}

/** Housed = imported/created but the organizer hasn't placed/checked it yet
 *  (`organized_at` null). Organized = the organizer has run on it at least
 *  once. This is the guide's OWN 2-state lifecycle — distinct from the note
 *  model's draft/processed/promoted/failed, which doesn't apply to guides. */
function GuideStatusBadge({ guide }: { guide: Note }) {
  const organized = guide.organized_at !== null;
  return (
    <Badge
      variant="outline"
      className={cn(
        "text-[9px]",
        organized
          ? "border-emerald-500/40 text-emerald-400"
          : "border-amber-500/40 text-amber-400",
      )}
    >
      {organized ? "organized" : "housed"}
    </Badge>
  );
}

// ---------------------------------------------------------------------------
// File manager — sidebar rails + tree, breadcrumb main pane, list/grid,
// multi-select, drag-to-move.
// ---------------------------------------------------------------------------

function FileManager({
  guides,
  collections,
  isLoading,
  onOpenGuide,
}: {
  guides: Note[];
  collections: NotebookTab[];
  isLoading: boolean;
  onOpenGuide: (id: string) => void;
}) {
  const [rail, setRail] = useState<Rail>({ kind: "tab", tabId: null });
  const [grid, setGrid] = usePersistedBoolean("library-fm-grid", false);
  const [search, setSearch] = useState("");
  const [priorityFilter, setPriorityFilter] = useState<NotePriority | "">("");
  const [selectedTags, setSelectedTags] = useState<string[]>([]);
  const [selected, setSelected] = useState<Set<string>>(new Set());

  const [updateNote] = useUpdateNoteMutation();
  const [createNote, { isLoading: creatingGuide }] = useCreateNoteMutation();

  const { tabsById } = useTabTree(collections);

  const allTags = useMemo(() => {
    const tags = new Set<string>();
    for (const g of guides) {
      const pipeline = isStudyGuidePipeline(g.pipeline) ? g.pipeline : null;
      pipeline?.tags.forEach((t) => tags.add(t));
    }
    return Array.from(tags).sort();
  }, [guides]);

  const breadcrumbs =
    rail.kind === "tab" ? ancestorChain(rail.tabId, tabsById) : [];

  const items = useMemo(() => {
    let filtered = filterRecordsByRail(guides, rail);
    filtered = filterRecordsByTags(filtered, selectedTags);

    const query = search.trim().toLowerCase();
    if (query) {
      filtered = filtered.filter((g) => {
        const pipeline = isStudyGuidePipeline(g.pipeline) ? g.pipeline : null;
        return (
          g.title.toLowerCase().includes(query) ||
          (pipeline?.abstract.toLowerCase().includes(query) ?? false) ||
          (pipeline?.tags.some((t) => t.toLowerCase().includes(query)) ?? false)
        );
      });
    }
    if (priorityFilter)
      filtered = filtered.filter((g) => g.priority === priorityFilter);

    return filtered;
  }, [rail, guides, selectedTags, search, priorityFilter]);

  const toggleSelect = (id: string) => {
    setSelected((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const clearSelection = () => setSelected(new Set());

  const moveGuidesToTab = async (guideIds: string[], targetTabId: string) => {
    await Promise.all(
      guideIds.map((id) =>
        updateNote({ id, tab_id: targetTabId, pinned_placement: true })
          .unwrap()
          .catch(() => {
            /* one guide failing to move shouldn't block the others */
          }),
      ),
    );
    clearSelection();
  };

  const handleDragStartGuide = (e: React.DragEvent, guideId: string) => {
    const ids =
      selected.has(guideId) && selected.size > 0
        ? Array.from(selected)
        : [guideId];
    e.dataTransfer.setData(DRAG_MIME, JSON.stringify(ids));
    e.dataTransfer.effectAllowed = "move";
  };

  // Manual guide authoring (FILE-MANAGER, 2026-07-04) — guides are normally
  // roster-authored (notebook_create_study_guide), but Joseph needs a
  // by-hand path too. Same create endpoint as notes, kind:"study_guide"
  // routes the backend to the guide pipeline (abstract/tags only, raw_text
  // never re-expressed) instead of note structuring.
  const handleAddGuide = async (rawText: string, sensitive: boolean) => {
    await createNote({
      raw_text: rawText,
      tab_id: rail.kind === "tab" ? (rail.tabId ?? undefined) : undefined,
      sensitive,
      kind: "study_guide",
    }).unwrap();
  };

  const railLabel =
    rail.kind === "tab"
      ? (breadcrumbs.at(-1)?.title ?? "all collections")
      : rail.kind;

  return (
    <div className="flex flex-1 overflow-hidden">
      <div className="flex w-52 shrink-0 flex-col overflow-hidden border-r border-border bg-card/10">
        <FileManagerSidebar
          tabKind="collection"
          tabs={collections}
          rail={rail}
          onRailChange={setRail}
          onDropRecords={(ids, tabId) => void moveGuidesToTab(ids, tabId)}
          newTabRepo={GENERAL_REPO}
        />
      </div>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex shrink-0 flex-col gap-2 border-b border-border/70 px-4 py-2.5">
          <div className="flex items-center justify-between gap-2">
            <Breadcrumbs
              rail={rail}
              breadcrumbs={breadcrumbs}
              railLabel={railLabel}
              onSelect={(tabId) => setRail({ kind: "tab", tabId })}
            />
            <div className="flex shrink-0 items-center gap-2">
              <TagFilterInput
                allTags={allTags}
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
            creating={creatingGuide}
            defaultSensitive={rail.kind === "vault"}
            triggerLabel="new guide"
            placeholder="Paste the guide's content…"
            onSubmit={handleAddGuide}
          />
          <div className="flex items-center gap-2">
            <div className="relative max-w-sm flex-1">
              <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
              <input
                value={search}
                onChange={(e) => setSearch(e.target.value)}
                placeholder="search guides…"
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
          </div>
          {selected.size > 0 ? (
            <BulkActionBar
              count={selected.size}
              collections={collections}
              onMove={(tabId) =>
                void moveGuidesToTab(Array.from(selected), tabId)
              }
              onClear={clearSelection}
            />
          ) : null}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-4">
          {isLoading ? (
            <p className="text-xs text-muted-foreground">loading…</p>
          ) : items.length === 0 ? (
            <p className="text-xs text-muted-foreground">
              {search
                ? `no guides match "${search}".`
                : "nothing here — the jeevy roster authors guides in, or import existing files."}
            </p>
          ) : grid ? (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
              {items.map((g) => (
                <GuideCard
                  key={g.id}
                  guide={g}
                  selected={selected.has(g.id)}
                  onOpen={() => onOpenGuide(g.id)}
                  onToggleSelect={() => toggleSelect(g.id)}
                  onDragStart={(e) => handleDragStartGuide(e, g.id)}
                />
              ))}
            </div>
          ) : (
            <div className="divide-y divide-border/50 rounded-md border border-border/50">
              {items.map((g) => (
                <GuideListRow
                  key={g.id}
                  guide={g}
                  selected={selected.has(g.id)}
                  onOpen={() => onOpenGuide(g.id)}
                  onToggleSelect={() => toggleSelect(g.id)}
                  onDragStart={(e) => handleDragStartGuide(e, g.id)}
                />
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

function Breadcrumbs({
  rail,
  breadcrumbs,
  railLabel,
  onSelect,
}: {
  rail: Rail;
  breadcrumbs: NotebookTab[];
  railLabel: string;
  onSelect: (tabId: string | null) => void;
}) {
  if (rail.kind !== "tab") {
    return (
      <h3 className="truncate text-sm font-medium capitalize">{railLabel}</h3>
    );
  }
  return (
    <div className="flex min-w-0 items-center gap-1 text-sm">
      <button
        type="button"
        onClick={() => onSelect(null)}
        className={cn(
          "shrink-0 font-medium",
          rail.tabId === null
            ? "text-foreground"
            : "text-muted-foreground hover:text-foreground",
        )}
      >
        all collections
      </button>
      {breadcrumbs.map((tab) => (
        <span key={tab.id} className="flex min-w-0 items-center gap-1">
          <span className="text-muted-foreground/50">/</span>
          <button
            type="button"
            onClick={() => onSelect(tab.id)}
            className={cn(
              "min-w-0 truncate font-medium",
              rail.tabId === tab.id
                ? "text-foreground"
                : "text-muted-foreground hover:text-foreground",
            )}
            title={tab.title}
          >
            {tab.title}
          </button>
        </span>
      ))}
    </div>
  );
}

function BulkActionBar({
  count,
  collections,
  onMove,
  onClear,
}: {
  count: number;
  collections: NotebookTab[];
  onMove: (tabId: string) => void;
  onClear: () => void;
}) {
  return (
    <div className="flex items-center gap-2 rounded-md border border-border bg-accent/30 px-2 py-1.5 text-[10px]">
      <span className="font-medium text-foreground">{count} selected</span>
      <select
        value=""
        onChange={(e) => {
          if (e.target.value) onMove(e.target.value);
        }}
        className="h-6 rounded border border-input bg-background px-1 text-[10px] text-muted-foreground focus:outline-none focus:ring-1 focus:ring-ring"
      >
        <option value="">move to…</option>
        {collections.map((c) => (
          <option key={c.id} value={c.id}>
            {c.title}
          </option>
        ))}
      </select>
      <button
        type="button"
        onClick={onClear}
        className="ml-auto text-muted-foreground hover:text-foreground"
      >
        clear
      </button>
    </div>
  );
}

function GuideMetaRow({ guide }: { guide: Note }) {
  return (
    <div className="flex shrink-0 items-center gap-1">
      {guide.starred ? (
        <Star className="h-3 w-3 shrink-0 fill-amber-400 text-amber-400" />
      ) : null}
      {guide.sensitive ? (
        <span title="Sensitive — the assistant sees a redacted copy">🔒</span>
      ) : null}
      <IdChip id={guide.id} />
    </div>
  );
}

function GuideListRow({
  guide,
  selected,
  onOpen,
  onToggleSelect,
  onDragStart,
}: {
  guide: Note;
  selected: boolean;
  onOpen: () => void;
  onToggleSelect: () => void;
  onDragStart: (e: React.DragEvent) => void;
}) {
  const pipeline = isStudyGuidePipeline(guide.pipeline) ? guide.pipeline : null;
  const [updateNote] = useUpdateNoteMutation();
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
      <span className="min-w-0 flex-1 truncate font-medium" title={guide.title}>
        {guide.title}
      </span>
      {pipeline && pipeline.tags.length > 0 ? (
        <div className="hidden shrink-0 items-center gap-1 sm:flex">
          {pipeline.tags.slice(0, 2).map((tag) => (
            <Badge key={tag} variant="outline" className="text-[9px]">
              {tag}
            </Badge>
          ))}
        </div>
      ) : null}
      <span className="hidden shrink-0 text-[10px] text-muted-foreground md:inline">
        {relativeTime(guide.updated_at)}
      </span>
      <div onClick={(e) => e.stopPropagation()} className="shrink-0">
        <PrioritySelector
          priority={guide.priority}
          onChange={(priority) => updateNote({ id: guide.id, priority })}
        />
      </div>
      <GuideStatusBadge guide={guide} />
      <GuideMetaRow guide={guide} />
    </div>
  );
}

function GuideCard({
  guide,
  selected,
  onOpen,
  onToggleSelect,
  onDragStart,
}: {
  guide: Note;
  selected: boolean;
  onOpen: () => void;
  onToggleSelect: () => void;
  onDragStart: (e: React.DragEvent) => void;
}) {
  const pipeline = isStudyGuidePipeline(guide.pipeline) ? guide.pipeline : null;
  const [updateNote] = useUpdateNoteMutation();
  return (
    <Card
      draggable
      onDragStart={onDragStart}
      className={cn(
        "relative min-w-0 cursor-pointer transition-colors hover:border-ring/60",
        selected && "border-ring",
      )}
      onClick={onOpen}
    >
      <input
        type="checkbox"
        checked={selected}
        onClick={(e) => e.stopPropagation()}
        onChange={onToggleSelect}
        className="absolute left-2 top-2 z-10"
      />
      <CardHeader className="flex-row items-start justify-between gap-2 space-y-0 p-3 pb-1.5 pl-7">
        <h4
          className="min-w-0 truncate text-xs font-medium"
          title={guide.title}
        >
          {guide.title}
        </h4>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <GuideMetaRow guide={guide} />
          <PrioritySelector
            priority={guide.priority}
            onChange={(priority) => updateNote({ id: guide.id, priority })}
          />
          <GuideStatusBadge guide={guide} />
        </div>
      </CardHeader>
      <CardContent className="p-3 pt-0">
        {pipeline ? (
          <p className="line-clamp-3 text-[11px] leading-relaxed text-muted-foreground">
            {pipeline.abstract}
          </p>
        ) : (
          <p className="text-[11px] italic text-muted-foreground/60">
            not yet structured…
          </p>
        )}
        {pipeline && pipeline.tags.length > 0 ? (
          <div className="mt-2 flex flex-wrap gap-1">
            {pipeline.tags.slice(0, 4).map((tag) => (
              <Badge key={tag} variant="outline" className="text-[9px]">
                {tag}
              </Badge>
            ))}
          </div>
        ) : null}
      </CardContent>
    </Card>
  );
}

/** The guide detail read — clickable TOC (left), the full guide (center),
 *  the per-guide research chat (right, expanded by default — it's the
 *  primary interaction now per the Phase 3 chat redesign). No raw-source
 *  panel here (unlike the note reader) — the guide IS the deliverable;
 *  Joseph only needs raw/original for NOTES, where comparing structured-
 *  vs-original matters. */
function GuideReader({ guide, onBack }: { guide: Note; onBack: () => void }) {
  const [tocCollapsed, setTocCollapsed] = usePersistedBoolean(
    "library-toc-collapsed",
    false,
  );
  const [chatCollapsed, setChatCollapsed] = usePersistedBoolean(
    "library-chat-collapsed",
    false,
  );
  const pipeline = isStudyGuidePipeline(guide.pipeline) ? guide.pipeline : null;
  const contentRef = useRef<HTMLDivElement>(null);
  const chat = useRecordChat(guide.id);
  const [updateNote] = useUpdateNoteMutation();

  const handleTocClick = (anchor: string) => {
    const el = contentRef.current?.querySelector(`#${CSS.escape(anchor)}`);
    el?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="flex flex-1 overflow-hidden">
      <SidePanelShell
        side="left"
        label="contents"
        width={READER_TOC_WIDTH}
        collapsed={tocCollapsed}
        onToggleCollapsed={() => setTocCollapsed(!tocCollapsed)}
        resizable={false}
      >
        <div className="shrink-0 border-b border-border/50 p-1.5">
          <Button
            type="button"
            size="sm"
            variant="ghost"
            className="h-6 gap-1 px-1.5 text-[10px]"
            onClick={onBack}
          >
            <ChevronLeft className="h-3 w-3" />
            library
          </Button>
        </div>
        <nav className="min-h-0 flex-1 overflow-y-auto p-2">
          {pipeline && pipeline.toc.length > 0 ? (
            <ul className="space-y-0.5">
              {pipeline.toc.map((entry) => (
                <li key={entry.anchor}>
                  <button
                    type="button"
                    onClick={() => handleTocClick(entry.anchor)}
                    title={entry.title}
                    style={{ paddingLeft: `${(entry.level - 1) * 10 + 6}px` }}
                    className="block w-full truncate rounded py-1 pr-1.5 text-left text-[11px] text-muted-foreground hover:bg-accent hover:text-foreground"
                  >
                    {entry.title}
                  </button>
                </li>
              ))}
            </ul>
          ) : (
            <p className="px-1.5 py-1 text-[10px] text-muted-foreground/60">
              no table of contents.
            </p>
          )}
        </nav>
      </SidePanelShell>

      <div className="flex min-w-0 flex-1 flex-col overflow-hidden">
        <div className="flex shrink-0 items-start justify-between gap-2 border-b border-border/70 px-4 py-2.5">
          <div className="min-w-0">
            <h3 className="truncate text-sm font-medium" title={guide.title}>
              {guide.title}
            </h3>
            {pipeline && pipeline.tags.length > 0 ? (
              <div className="mt-1 flex flex-wrap gap-1">
                {pipeline.tags.map((tag) => (
                  <Badge key={tag} variant="outline" className="text-[9px]">
                    {tag}
                  </Badge>
                ))}
              </div>
            ) : null}
          </div>
          <div className="flex shrink-0 flex-col items-end gap-1">
            <div className="flex items-center gap-1">
              <button
                type="button"
                title={guide.starred ? "Unstar" : "Star"}
                onClick={() =>
                  updateNote({ id: guide.id, starred: !guide.starred })
                }
              >
                <Star
                  className={cn(
                    "h-3.5 w-3.5",
                    guide.starred
                      ? "fill-amber-400 text-amber-400"
                      : "text-muted-foreground",
                  )}
                />
              </button>
              {guide.sensitive ? (
                <span title="Sensitive — the assistant sees a redacted copy">
                  🔒
                </span>
              ) : null}
              <CopyRawMarkdownButton text={guide.raw_text} />
              <IdChip id={guide.id} />
            </div>
            <PrioritySelector
              priority={guide.priority}
              onChange={(priority: NotePriority) =>
                updateNote({ id: guide.id, priority })
              }
            />
            <GuideStatusBadge guide={guide} />
          </div>
        </div>
        <div
          ref={contentRef}
          className="min-w-0 min-h-0 flex-1 overflow-y-auto p-4"
        >
          {pipeline ? (
            <>
              {guide.sensitive ? (
                <SensitiveBanner redactions={guide.redactions} />
              ) : null}
              <MarkdownView content={guide.raw_text} slugHeadings />
            </>
          ) : (
            <p className="text-xs italic text-muted-foreground/60">
              not yet structured…
            </p>
          )}
        </div>
      </div>

      <SidePanelShell
        side="right"
        label="chat"
        width={READER_CHAT_WIDTH}
        collapsed={chatCollapsed}
        onToggleCollapsed={() => setChatCollapsed(!chatCollapsed)}
        resizable={false}
        extraHeader={<ChatHeaderControls state={chat} />}
      >
        <ChatBody state={chat} mode="record" sensitive={!!guide.sensitive} />
      </SidePanelShell>
    </div>
  );
}
