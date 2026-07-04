/**
 * Library — the grimoire's browsing + reading surface for study guides
 * (Phase 1f). A study guide is a distinct note KIND (`kind: "study_guide"`):
 * a finished deliverable to be housed + rendered, never re-expressed into
 * the note model's summary/technical/plain triple. Guides are grouped by
 * COLLECTION (a tab with `kind: "collection"`; `tab_id` on the guide IS its
 * collection) rather than the note model's flat tab filter.
 *
 * Two states, like Notebook's own grid/reader split: browse (card grid per
 * collection, searchable) and read (clickable TOC + the full guide via the
 * shared MarkdownView). Reuses Notebook's SidePanelShell/usePersistedBoolean
 * for layout consistency instead of re-inventing collapsible panels here.
 */

import { useRef, useState } from "react";
import { ChevronLeft, Search } from "lucide-react";

import { useListNotesQuery, useListTabsQuery } from "@/api";
import { MarkdownView } from "@/components/notebook/MarkdownView";
import { SidePanelShell, usePersistedBoolean } from "@/components/notebook/Notebook";
import { isStudyGuidePipeline, type Note, type NotebookTab } from "@/components/notebook/notebookTypes";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const READER_TOC_WIDTH = 240;
const READER_RAW_WIDTH = 360;

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
  const collections = (tabsData?.tabs ?? []).filter((t) => t.kind === "collection");
  const selectedGuide = selectedGuideId ? (guides.find((g) => g.id === selectedGuideId) ?? null) : null;

  if (selectedGuideId && !selectedGuide && !isLoading) {
    // Guide vanished from underneath us (deleted elsewhere) — fall back to
    // the grid rather than showing a dead reader.
    setSelectedGuideId(null);
  }

  return selectedGuide ? (
    <GuideReader guide={selectedGuide} onBack={() => setSelectedGuideId(null)} />
  ) : (
    <LibraryGrid
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
        organized ? "border-emerald-500/40 text-emerald-400" : "border-amber-500/40 text-amber-400",
      )}
    >
      {organized ? "organized" : "housed"}
    </Badge>
  );
}

function LibraryGrid({
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
  const [search, setSearch] = useState("");
  const query = search.trim().toLowerCase();

  const filtered = query
    ? guides.filter((g) => {
        const pipeline = isStudyGuidePipeline(g.pipeline) ? g.pipeline : null;
        return (
          g.title.toLowerCase().includes(query) ||
          (pipeline?.abstract.toLowerCase().includes(query) ?? false) ||
          (pipeline?.tags.some((t) => t.toLowerCase().includes(query)) ?? false)
        );
      })
    : guides;

  const byCollection = new Map<string, Note[]>();
  const uncollected: Note[] = [];
  for (const g of filtered) {
    const inCollection = collections.some((c) => c.id === g.tab_id);
    if (inCollection) {
      const list = byCollection.get(g.tab_id) ?? [];
      list.push(g);
      byCollection.set(g.tab_id, list);
    } else {
      uncollected.push(g);
    }
  }

  const hasAnyGuides = guides.length > 0;

  return (
    <div className="flex flex-1 flex-col overflow-hidden">
      <div className="shrink-0 border-b border-border/70 px-4 py-2.5">
        <div className="relative max-w-sm">
          <Search className="pointer-events-none absolute left-2 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-muted-foreground" />
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="search guides…"
            className="w-full rounded-md border border-border bg-card/40 py-1.5 pl-7 pr-2 text-xs outline-none focus:border-ring"
          />
        </div>
      </div>
      <div className="min-h-0 flex-1 overflow-y-auto p-4">
        {isLoading ? (
          <p className="text-xs text-muted-foreground">loading…</p>
        ) : !hasAnyGuides ? (
          <p className="text-xs text-muted-foreground">
            no study guides yet — the jeevy roster authors them in, or import existing files.
          </p>
        ) : filtered.length === 0 ? (
          <p className="text-xs text-muted-foreground">no guides match "{search}".</p>
        ) : (
          <div className="space-y-6">
            {collections.map((c) => {
              const list = byCollection.get(c.id) ?? [];
              if (list.length === 0) return null;
              return (
                <CollectionSection key={c.id} title={c.title} guides={list} onOpenGuide={onOpenGuide} />
              );
            })}
            {uncollected.length > 0 ? (
              <CollectionSection title="uncollected" guides={uncollected} onOpenGuide={onOpenGuide} />
            ) : null}
          </div>
        )}
      </div>
    </div>
  );
}

function CollectionSection({
  title,
  guides,
  onOpenGuide,
}: {
  title: string;
  guides: Note[];
  onOpenGuide: (id: string) => void;
}) {
  return (
    <section>
      <h3 className="mb-2 flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
        {title}
        <span className="text-[10px] font-normal normal-case text-muted-foreground/60">
          ({guides.length})
        </span>
      </h3>
      <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
        {guides.map((g) => (
          <GuideCard key={g.id} guide={g} onOpen={() => onOpenGuide(g.id)} />
        ))}
      </div>
    </section>
  );
}

function GuideCard({ guide, onOpen }: { guide: Note; onOpen: () => void }) {
  const pipeline = isStudyGuidePipeline(guide.pipeline) ? guide.pipeline : null;
  return (
    <Card
      className="min-w-0 cursor-pointer transition-colors hover:border-ring/60"
      onClick={onOpen}
    >
      <CardHeader className="flex-row items-start justify-between gap-2 space-y-0 p-3 pb-1.5">
        <h4 className="min-w-0 truncate text-xs font-medium" title={guide.title}>
          {guide.title}
        </h4>
        <GuideStatusBadge guide={guide} />
      </CardHeader>
      <CardContent className="p-3 pt-0">
        {pipeline ? (
          <p className="line-clamp-3 text-[11px] leading-relaxed text-muted-foreground">
            {pipeline.abstract}
          </p>
        ) : (
          <p className="text-[11px] italic text-muted-foreground/60">not yet structured…</p>
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

/** The guide detail read — clickable TOC (left) + the full guide (center),
 *  raw source available in a right panel COLLAPSED by default: the guide
 *  IS the deliverable, so unlike a note's original/structured split, the
 *  rendered view is the primary (and usually only) thing anyone wants. */
function GuideReader({ guide, onBack }: { guide: Note; onBack: () => void }) {
  const [tocCollapsed, setTocCollapsed] = usePersistedBoolean("library-toc-collapsed", false);
  const [rawCollapsed, setRawCollapsed] = usePersistedBoolean("library-raw-collapsed", true);
  const pipeline = isStudyGuidePipeline(guide.pipeline) ? guide.pipeline : null;
  const contentRef = useRef<HTMLDivElement>(null);

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
            <p className="px-1.5 py-1 text-[10px] text-muted-foreground/60">no table of contents.</p>
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
          <GuideStatusBadge guide={guide} />
        </div>
        <div ref={contentRef} className="min-w-0 flex-1 overflow-y-auto p-4">
          {pipeline ? (
            <MarkdownView content={guide.raw_text} slugHeadings />
          ) : (
            <p className="text-xs italic text-muted-foreground/60">not yet structured…</p>
          )}
        </div>
      </div>

      <SidePanelShell
        side="right"
        label="raw source"
        width={READER_RAW_WIDTH}
        collapsed={rawCollapsed}
        onToggleCollapsed={() => setRawCollapsed(!rawCollapsed)}
        resizable={false}
      >
        <pre className="min-h-0 flex-1 overflow-auto whitespace-pre-wrap break-words p-3 font-mono text-[10px] text-muted-foreground [overflow-wrap:anywhere]">
          {guide.raw_text}
        </pre>
      </SidePanelShell>
    </div>
  );
}
