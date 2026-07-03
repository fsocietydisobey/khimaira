/**
 * Notebook — AI-structured note capture (Phase 1b frontend).
 *
 * Paste → draft note → (Phase 1c, async) auto-structured into
 * summary/technical/plain sections. Tabs group related notes; note_ids
 * are derived server-side by grouping on tab_id, so switching tabs is
 * just a filtered re-query, not separate storage.
 *
 * Global daemon state (not project-scoped) mounted under a per-project
 * route for nav consistency with the other observability views — every
 * project's Notebook tab shows the same notes.
 */

import { useState } from "react";
import { useParams } from "react-router-dom";
import { Plus, RefreshCw, Trash2, Upload } from "lucide-react";

import {
  useCreateNoteMutation,
  useCreateTabMutation,
  useDeleteNoteMutation,
  useListNotesQuery,
  useListTabsQuery,
  usePromoteNoteMutation,
  useRevalidateNoteMutation,
} from "@/api";
import type { Note } from "@/components/notebook/notebookTypes";
import { ProjectNavTabs } from "@/components/project/ProjectNavTabs";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { cn } from "@/lib/utils";

const ALL_TABS = "__all__";

export function Notebook() {
  const { name } = useParams<{ name: string }>();
  const projectName = name ?? "";

  const [selectedTab, setSelectedTab] = useState<string>(ALL_TABS);
  const [draft, setDraft] = useState("");
  const [creatingTab, setCreatingTab] = useState(false);
  const [newTabTitle, setNewTabTitle] = useState("");

  const { data: tabsData } = useListTabsQuery();
  const { data: notesData, isLoading: notesLoading } = useListNotesQuery(
    selectedTab === ALL_TABS ? undefined : { tabId: selectedTab },
    { pollingInterval: 3000 },
  );
  const [createNote, { isLoading: creatingNote }] = useCreateNoteMutation();
  const [createTab] = useCreateTabMutation();
  const [promoteNote] = usePromoteNoteMutation();
  const [deleteNote] = useDeleteNoteMutation();

  const tabs = tabsData?.tabs ?? [];
  const notes = notesData?.notes ?? [];

  const handleAddNote = async () => {
    const text = draft.trim();
    if (!text) return;
    await createNote({
      raw_text: text,
      tab_id: selectedTab === ALL_TABS ? undefined : selectedTab,
    }).unwrap();
    setDraft("");
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
            <p className="text-[11px] text-muted-foreground mt-0.5">
              paste → auto-structure → review → promote into the mnemosyne knowledge loop
            </p>
          </div>
          <ProjectNavTabs projectName={projectName} />
        </div>
      </header>

      <div className="flex shrink-0 items-center gap-1 overflow-x-auto border-b border-border bg-card/20 px-3 py-1.5">
        <button
          type="button"
          onClick={() => setSelectedTab(ALL_TABS)}
          className={cn(
            "rounded-md px-2.5 py-1 text-xs font-medium whitespace-nowrap transition-colors",
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
            onClick={() => setSelectedTab(t.id)}
            title={`${t.note_ids.length} note${t.note_ids.length === 1 ? "" : "s"}`}
            className={cn(
              "rounded-md px-2.5 py-1 text-xs font-medium whitespace-nowrap transition-colors",
              selectedTab === t.id
                ? "bg-accent text-accent-foreground"
                : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
            )}
          >
            {t.title}
            <span className="ml-1.5 text-muted-foreground/60">{t.note_ids.length}</span>
          </button>
        ))}

        {creatingTab ? (
          <div className="flex items-center gap-1">
            <input
              autoFocus
              value={newTabTitle}
              onChange={(e) => setNewTabTitle(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter") void handleCreateTab();
                if (e.key === "Escape") {
                  setCreatingTab(false);
                  setNewTabTitle("");
                }
              }}
              placeholder="tab name…"
              className="h-6 w-28 rounded border border-input bg-background px-1.5 text-xs focus:outline-none focus:ring-1 focus:ring-ring"
            />
            <Button size="sm" variant="ghost" className="h-6 px-1.5" onClick={handleCreateTab}>
              add
            </Button>
          </div>
        ) : (
          <Button
            size="sm"
            variant="ghost"
            className="h-6 px-1.5 text-muted-foreground"
            title="New tab"
            onClick={() => setCreatingTab(true)}
          >
            <Plus className="h-3.5 w-3.5" />
          </Button>
        )}
      </div>

      <div className="shrink-0 border-b border-border bg-card/10 p-3">
        <textarea
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Paste a note (e.g. a Claude Code response) — it'll be auto-structured into summary/technical/plain sections…"
          rows={4}
          className="w-full resize-y rounded-md border border-input bg-background px-3 py-2 text-xs font-mono focus:outline-none focus:ring-1 focus:ring-ring"
        />
        <div className="mt-2 flex items-center justify-between">
          <span className="text-[11px] text-muted-foreground">
            {selectedTab === ALL_TABS
              ? "adds to the default tab"
              : `adds to "${tabs.find((t) => t.id === selectedTab)?.title ?? selectedTab}"`}
          </span>
          <Button size="sm" disabled={!draft.trim() || creatingNote} onClick={handleAddNote}>
            <Upload className="mr-1.5 h-3.5 w-3.5" />
            {creatingNote ? "adding…" : "add note"}
          </Button>
        </div>
      </div>

      <div className="flex-1 overflow-auto p-3">
        {notesLoading ? (
          <p className="text-xs text-muted-foreground">loading notes…</p>
        ) : notes.length === 0 ? (
          <p className="text-xs text-muted-foreground">
            no notes {selectedTab === ALL_TABS ? "yet" : "in this tab"}. Paste one above.
          </p>
        ) : (
          <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
            {notes.map((n) => (
              <NoteCard
                key={n.id}
                note={n}
                onPromote={() => promoteNote(n.id)}
                onDelete={() => deleteNote(n.id)}
              />
            ))}
          </div>
        )}
      </div>
    </div>
  );
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

function relativeTime(iso: string): string {
  const diffMin = Math.floor((Date.now() - new Date(iso).getTime()) / 60_000);
  if (diffMin < 1) return "just now";
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.floor(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  return `${Math.floor(diffHr / 24)}d ago`;
}

function NoteCard({
  note,
  onPromote,
  onDelete,
}: {
  note: Note;
  onPromote: () => void;
  onDelete: () => void;
}) {
  const [section, setSection] = useState<"summary" | "technical" | "plain">("summary");
  const [revalidateNote, { isLoading: revalidating }] = useRevalidateNoteMutation();
  const badge = STATUS_BADGE[note.status];
  const pipeline = note.pipeline;

  // North-star validation indicator: "healed ×N" when a revalidate pass ever
  // changed the note, else "current as of <time>" once checked, else
  // "never validated". Not a live git-diff check — that's what the
  // "re-check vs code" button triggers on demand.
  const validationLabel = note.last_validated_at
    ? note.history_count > 0
      ? `healed ×${note.history_count} · checked ${relativeTime(note.last_validated_at)}`
      : `current as of ${relativeTime(note.last_validated_at)}`
    : "never validated vs code";

  return (
    <Card className="flex flex-col">
      <CardHeader className="flex-row items-start justify-between gap-2 pb-2">
        <div className="min-w-0">
          <h3 className="truncate text-sm font-medium" title={note.title}>
            {note.title}
          </h3>
          <p className="mt-0.5 text-[10px] text-muted-foreground">
            {new Date(note.updated_at).toLocaleString()}
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
      <CardContent className="flex-1 pt-0 text-xs">
        {pipeline ? (
          <>
            <div className="mb-2 flex gap-1">
              {(["summary", "technical", "plain"] as const).map((s) => (
                <button
                  key={s}
                  type="button"
                  onClick={() => setSection(s)}
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
            <p className="whitespace-pre-wrap text-muted-foreground">{pipeline[section]}</p>
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
          <p className="whitespace-pre-wrap text-muted-foreground/70">
            {note.raw_text.slice(0, 240)}
            {note.raw_text.length > 240 ? "…" : ""}
          </p>
        )}
      </CardContent>
      <div className="flex items-center justify-end gap-1 border-t border-border/50 px-3 py-1.5">
        <Button
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
          <span className="px-2 text-[10px] text-muted-foreground/60">
            promoted for training
          </span>
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
    </Card>
  );
}
