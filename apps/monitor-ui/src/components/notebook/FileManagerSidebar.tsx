/**
 * FileManagerSidebar — the kind-generic rails + collection/folder tree
 * shared by BOTH sides of the grimoire's file manager (Grimoire FILE-
 * MANAGER, tasks/grimoire/FILE-MANAGER.md, scope extended to notes
 * 2026-07-04 per Joseph): guides browse via `kind:"collection"` tabs,
 * notes browse via `kind:"folder"` tabs — same tree shape, same rails
 * (Recent/Starred/Vault), same drag-to-move-and-pin target. The tab
 * NAMESPACE stays split (a folder never nests under a collection or vice
 * versa — enforced server-side too), so one sidebar instance is scoped to
 * ONE `tabKind`; a page that needs both mounts two instances.
 *
 * Tags are NOT a sidebar rail (Joseph, 2026-07-04 revision) — a flat list
 * of dozens of tags competed with real navigation. Tags are a header-row
 * multi-select FILTER instead (`TagFilterInput` below), applied on top of
 * whatever rail is active — see its own doc comment.
 *
 * This file owns tab CRUD (create/rename/delete) internally — identical
 * mutations regardless of which side is asking. It does NOT own record
 * (note/guide) mutations — `onDropRecords` bubbles a drag-drop up so the
 * caller decides what "move" means for its own record set (still always
 * `updateNote({tab_id, pinned_placement:true})` today, but the caller owns
 * that call so this file stays record-kind-agnostic).
 */

import { useMemo, useRef, useState } from "react";
import { Clock, Folder, FolderPlus, Lock, Pencil, Star, Trash2, X } from "lucide-react";

import { useCreateTabMutation, useDeleteTabMutation, useUpdateTabMutation } from "@/api";
import type { Note, NotebookTab, NotebookTabKind } from "@/components/notebook/notebookTypes";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";

/** The drag payload's MIME type — namespaced so a stray OS/browser drag
 *  (e.g. dragging text) never accidentally triggers a move. Shared across
 *  every drag SOURCE (guide/note rows+cards) and drop TARGET (this file's
 *  tree rows) so both sides interop through the same contract. */
export const DRAG_MIME = "application/x-khimaira-record-ids";

/** Which slice of a record store the main pane shows. `tab` is the only
 *  drill-down rail (tree navigation via `tabId`); the others are flat,
 *  single-level lists — a record store is a set of orthogonal views over
 *  the same records, not a single hierarchy (FILE-MANAGER.md principle #2:
 *  virtual folders, never real directories). Tags are a separate,
 *  orthogonal filter (`TagFilterInput`) — not a rail — since they narrow
 *  ON TOP of whichever rail is active rather than being a view of their own. */
export type Rail =
  | { kind: "tab"; tabId: string | null }
  | { kind: "recent" }
  | { kind: "starred" }
  | { kind: "vault" };

export function ancestorChain(tabId: string | null, tabsById: Map<string, NotebookTab>): NotebookTab[] {
  const chain: NotebookTab[] = [];
  const seen = new Set<string>();
  let current = tabId;
  while (current) {
    const tab = tabsById.get(current);
    if (!tab || seen.has(tab.id)) break;
    seen.add(tab.id);
    chain.unshift(tab);
    current = tab.parent_id;
  }
  return chain;
}

/** Resolves `records` for the current rail — the flat card list a Files-
 *  mode main pane renders. `kind:"tab"` with `tabId:null` ("all
 *  collections"/"all folders", the default landing) is the full-corpus
 *  triage view — every record, not just orphans — per Joseph's fold-Grid-
 *  into-Files decision (2026-07-04): the default must show cards
 *  immediately, not a collapsed tree the user expands one-by-one. Selecting
 *  a specific tab scopes to its direct members; the other rails
 *  (recent/starred/vault) are flat, single-level views. Pure — no
 *  fetching, no mutation; callers layer their own search/priority/tag
 *  filters on the result after this. */
export function filterRecordsByRail(records: Note[], rail: Rail): Note[] {
  if (rail.kind === "tab") {
    if (rail.tabId === null) return records;
    return records.filter((r) => r.tab_id === rail.tabId);
  }
  if (rail.kind === "recent") {
    return [...records].sort((a, b) => b.updated_at.localeCompare(a.updated_at)).slice(0, 40);
  }
  if (rail.kind === "starred") {
    return records.filter((r) => r.starred);
  }
  return records.filter((r) => r.sensitive);
}

/** Tag filter — narrows an already-rail-filtered list to records carrying
 *  ANY of `selectedTags` (union semantics: picking more tags widens the
 *  match, matching the common "has any of these labels" convention). A
 *  no-op when `selectedTags` is empty. Both NotePipeline and
 *  StudyGuidePipeline carry `tags`, so this applies uniformly. */
export function filterRecordsByTags(records: Note[], selectedTags: string[]): Note[] {
  if (selectedTags.length === 0) return records;
  return records.filter((r) => selectedTags.some((t) => r.pipeline?.tags.includes(t)));
}

export function useTabTree(tabs: NotebookTab[]) {
  return useMemo(() => {
    const tabsById = new Map(tabs.map((t) => [t.id, t]));
    const childrenByParent = new Map<string | "root", NotebookTab[]>();
    for (const t of tabs) {
      const key = t.parent_id ?? "root";
      const list = childrenByParent.get(key) ?? [];
      list.push(t);
      childrenByParent.set(key, list);
    }
    for (const list of childrenByParent.values()) list.sort((a, b) => a.title.localeCompare(b.title));
    return { tabsById, childrenByParent };
  }, [tabs]);
}

export function errDetail(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "data" in err) {
    const data = (err as { data?: unknown }).data;
    if (data && typeof data === "object" && "detail" in data) {
      const detail = (data as { detail?: unknown }).detail;
      if (typeof detail === "string") return detail;
    }
  }
  return fallback;
}

export function FileManagerSidebar({
  tabKind,
  tabs,
  rail,
  onRailChange,
  onDropRecords,
  newTabRepo,
  compact = false,
}: {
  tabKind: NotebookTabKind;
  tabs: NotebookTab[];
  rail: Rail;
  onRailChange: (r: Rail) => void;
  onDropRecords: (ids: string[], tabId: string) => void;
  /** North-star (2026-07-16): which repo to stamp on a brand-new TOP-LEVEL
   *  tab (no parent) — the backend now requires `repo` on every /tabs
   *  mutation. Anything targeting an EXISTING tab derives its repo from
   *  `tabsById` instead (must match the stored value exactly). */
  newTabRepo: string;
  /** Narrower rendering for the notes-side sidebar (embedded in a ~240px
   *  panel alongside other controls) vs the guide Library's full-width rail. */
  compact?: boolean;
}) {
  const { tabsById, childrenByParent } = useTabTree(tabs);
  const [dragOverTabId, setDragOverTabId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  const [createTab] = useCreateTabMutation();
  const [updateTab] = useUpdateTabMutation();
  const [deleteTab] = useDeleteTabMutation();

  const handleCreateChild = async (parentId: string | null) => {
    setActionError(null);
    // child tab's repo MUST equal the parent's (backend get_tab match); top-level uses newTabRepo
    const repo = parentId ? (tabsById.get(parentId)?.repo ?? newTabRepo) : newTabRepo;
    try {
      await createTab({ title: `New ${tabKind}`, kind: tabKind, parent_id: parentId, repo }).unwrap();
    } catch (err) {
      setActionError(errDetail(err, `couldn't create the ${tabKind}.`));
    }
  };

  const handleRenameTab = async (id: string, title: string) => {
    setActionError(null);
    const repo = tabsById.get(id)?.repo ?? newTabRepo;
    try {
      await updateTab({ id, title, repo }).unwrap();
    } catch (err) {
      setActionError(errDetail(err, "couldn't rename — a sibling may already use that name."));
    }
  };

  const handleDeleteTab = async (id: string) => {
    setActionError(null);
    const repo = tabsById.get(id)?.repo ?? newTabRepo;
    try {
      await deleteTab({ id, repo }).unwrap();
      if (rail.kind === "tab" && rail.tabId === id) {
        onRailChange({ kind: "tab", tabId: tabsById.get(id)?.parent_id ?? null });
      }
    } catch (err) {
      setActionError(errDetail(err, `couldn't delete that ${tabKind}.`));
    }
  };

  const handleDropOnTab = (e: React.DragEvent, targetTabId: string) => {
    e.preventDefault();
    setDragOverTabId(null);
    const raw = e.dataTransfer.getData(DRAG_MIME);
    if (!raw) return;
    try {
      const ids: string[] = JSON.parse(raw);
      if (ids.length > 0) onDropRecords(ids, targetTabId);
    } catch {
      // malformed payload — not a khimaira drag, ignore
    }
  };

  const rootLabel = tabKind === "collection" ? "all collections" : "all folders";
  const sectionLabel = tabKind === "collection" ? "Collections" : "Folders";

  return (
    <nav className={cn("min-h-0 flex-1 space-y-3 overflow-y-auto", compact ? "p-1.5" : "p-2")}>
      <div className="space-y-0.5">
        <RailButton
          icon={<Clock className="h-3.5 w-3.5" />}
          label="Recent"
          active={rail.kind === "recent"}
          onClick={() => onRailChange({ kind: "recent" })}
        />
        <RailButton
          icon={<Star className="h-3.5 w-3.5" />}
          label="Starred"
          active={rail.kind === "starred"}
          onClick={() => onRailChange({ kind: "starred" })}
        />
        <RailButton
          icon={<Lock className="h-3.5 w-3.5" />}
          label="Vault"
          active={rail.kind === "vault"}
          onClick={() => onRailChange({ kind: "vault" })}
        />
      </div>

      <div>
        <div className="mb-1 flex items-center justify-between px-1.5">
          <span className="text-[9px] font-medium uppercase tracking-wide text-muted-foreground/70">
            {sectionLabel}
          </span>
          <button
            type="button"
            title={`New top-level ${tabKind}`}
            onClick={() => void handleCreateChild(null)}
            className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
          >
            <FolderPlus className="h-3 w-3" />
          </button>
        </div>
        <RailButton
          icon={<Folder className="h-3.5 w-3.5" />}
          label={rootLabel}
          active={rail.kind === "tab" && rail.tabId === null}
          onClick={() => onRailChange({ kind: "tab", tabId: null })}
        />
        <TabTree
          childrenByParent={childrenByParent}
          parentId="root"
          depth={0}
          currentTabId={rail.kind === "tab" ? rail.tabId : undefined}
          onSelect={(tabId) => onRailChange({ kind: "tab", tabId })}
          dragOverTabId={dragOverTabId}
          setDragOverTabId={setDragOverTabId}
          onDropOnTab={handleDropOnTab}
          onCreateChild={handleCreateChild}
          onRenameTab={handleRenameTab}
          onDeleteTab={handleDeleteTab}
        />
      </div>

      {actionError ? (
        <div className="mx-1.5 rounded border border-destructive/40 bg-destructive/5 px-2 py-1 text-[9px] text-destructive">
          {actionError}
        </div>
      ) : null}
    </nav>
  );
}

function RailButton({
  icon,
  label,
  active,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={cn(
        "flex w-full min-w-0 items-center gap-1.5 rounded px-1.5 py-1 text-left text-[11px] transition-colors",
        active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
      )}
    >
      <span className="shrink-0">{icon}</span>
      <span className="min-w-0 truncate">{label}</span>
    </button>
  );
}

function TabTree({
  childrenByParent,
  parentId,
  depth,
  currentTabId,
  onSelect,
  dragOverTabId,
  setDragOverTabId,
  onDropOnTab,
  onCreateChild,
  onRenameTab,
  onDeleteTab,
}: {
  childrenByParent: Map<string | "root", NotebookTab[]>;
  parentId: string | "root";
  depth: number;
  currentTabId: string | null | undefined;
  onSelect: (tabId: string) => void;
  dragOverTabId: string | null;
  setDragOverTabId: (id: string | null) => void;
  onDropOnTab: (e: React.DragEvent, tabId: string) => void;
  onCreateChild: (parentId: string) => void;
  onRenameTab: (id: string, title: string) => void;
  onDeleteTab: (id: string) => void;
}) {
  const nodes = childrenByParent.get(parentId) ?? [];
  if (nodes.length === 0) return null;
  return (
    <ul className={depth > 0 ? "ml-3 border-l border-border/40 pl-1.5" : ""}>
      {nodes.map((tab) => (
        <li key={tab.id}>
          <TabTreeRow
            tab={tab}
            active={currentTabId === tab.id}
            dragOver={dragOverTabId === tab.id}
            onSelect={() => onSelect(tab.id)}
            onDragOver={(e) => {
              e.preventDefault();
              setDragOverTabId(tab.id);
            }}
            onDragLeave={() => setDragOverTabId(null)}
            onDrop={(e) => onDropOnTab(e, tab.id)}
            onCreateChild={() => onCreateChild(tab.id)}
            onRename={(title) => onRenameTab(tab.id, title)}
            onDelete={() => onDeleteTab(tab.id)}
          />
          <TabTree
            childrenByParent={childrenByParent}
            parentId={tab.id}
            depth={depth + 1}
            currentTabId={currentTabId}
            onSelect={onSelect}
            dragOverTabId={dragOverTabId}
            setDragOverTabId={setDragOverTabId}
            onDropOnTab={onDropOnTab}
            onCreateChild={onCreateChild}
            onRenameTab={onRenameTab}
            onDeleteTab={onDeleteTab}
          />
        </li>
      ))}
    </ul>
  );
}

function TabTreeRow({
  tab,
  active,
  dragOver,
  onSelect,
  onDragOver,
  onDragLeave,
  onDrop,
  onCreateChild,
  onRename,
  onDelete,
}: {
  tab: NotebookTab;
  active: boolean;
  dragOver: boolean;
  onSelect: () => void;
  onDragOver: (e: React.DragEvent) => void;
  onDragLeave: () => void;
  onDrop: (e: React.DragEvent) => void;
  onCreateChild: () => void;
  onRename: (title: string) => void;
  onDelete: () => void;
}) {
  const [renaming, setRenaming] = useState(false);
  const [draft, setDraft] = useState(tab.title);

  if (renaming) {
    return (
      <div className="flex items-center gap-1 px-1.5 py-1">
        <input
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && draft.trim()) {
              onRename(draft.trim());
              setRenaming(false);
            }
            if (e.key === "Escape") setRenaming(false);
          }}
          onBlur={() => setRenaming(false)}
          className="min-w-0 flex-1 rounded border border-ring bg-background px-1 py-0.5 text-[11px] outline-none"
        />
      </div>
    );
  }

  return (
    <div
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
      className={cn("group flex items-center gap-0.5 rounded", dragOver && "bg-accent/60 ring-1 ring-ring")}
    >
      <button
        type="button"
        onClick={onSelect}
        className={cn(
          "flex min-w-0 flex-1 items-center gap-1.5 rounded px-1.5 py-1 text-left text-[11px] transition-colors",
          active ? "bg-accent text-foreground" : "text-muted-foreground hover:bg-accent/50 hover:text-foreground",
        )}
      >
        <Folder className="h-3.5 w-3.5 shrink-0" />
        <span className="min-w-0 truncate" title={tab.title}>
          {tab.title}
        </span>
      </button>
      <span className="shrink-0 pr-1.5 text-[9px] text-muted-foreground/50 group-hover:hidden">
        {tab.note_ids.length}
      </span>
      <div className="hidden shrink-0 items-center gap-0.5 pr-1 group-hover:flex">
        <button
          type="button"
          title={`New ${tab.kind === "collection" ? "sub-collection" : "subfolder"} in "${tab.title}"`}
          onClick={onCreateChild}
          className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <FolderPlus className="h-3 w-3" />
        </button>
        <button
          type="button"
          title="Rename"
          onClick={() => {
            setDraft(tab.title);
            setRenaming(true);
          }}
          className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-foreground"
        >
          <Pencil className="h-3 w-3" />
        </button>
        <button
          type="button"
          title="Delete (children + records re-file to its parent)"
          onClick={onDelete}
          className="rounded p-0.5 text-muted-foreground hover:bg-accent hover:text-destructive"
        >
          <Trash2 className="h-3 w-3" />
        </button>
      </div>
    </div>
  );
}

/** Header-row tag filter (Joseph, 2026-07-04 revision) — a searchable
 *  multi-select that narrows whatever the sidebar rail already shows
 *  (`filterRecordsByTags` above is the actual filter; this is just the
 *  picker UI). Lives in the content-pane header next to the list/grid
 *  toggle, NOT the sidebar — dozens of tags as sidebar rail buttons
 *  competed with real navigation. Union semantics (matches ANY selected
 *  tag) — see `filterRecordsByTags`'s doc comment for why. */
export function TagFilterInput({
  allTags,
  selected,
  onChange,
}: {
  allTags: string[];
  selected: string[];
  onChange: (tags: string[]) => void;
}) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const containerRef = useRef<HTMLDivElement>(null);

  const matches = allTags
    .filter((t) => !selected.includes(t))
    .filter((t) => t.toLowerCase().includes(query.trim().toLowerCase()))
    .slice(0, 8);

  const addTag = (tag: string) => {
    onChange([...selected, tag]);
    setQuery("");
  };
  const removeTag = (tag: string) => onChange(selected.filter((t) => t !== tag));

  return (
    <div ref={containerRef} className="relative">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className={cn(
          "flex h-8 items-center gap-1 rounded-md border border-input bg-background px-2 text-[11px] text-muted-foreground hover:text-foreground",
          selected.length > 0 && "border-ring text-foreground",
        )}
      >
        tags{selected.length > 0 ? ` (${selected.length})` : ""}
      </button>

      {selected.length > 0 ? (
        <div className="mt-1 flex max-w-[220px] flex-wrap gap-1">
          {selected.map((tag) => (
            <Badge key={tag} variant="secondary" className="gap-1 py-0.5 pr-1 text-[9px]">
              <span className="max-w-[100px] truncate">{tag}</span>
              <button
                type="button"
                onClick={() => removeTag(tag)}
                title="Remove tag filter"
                className="rounded-full p-0.5 hover:bg-background/60"
              >
                <X className="h-2.5 w-2.5" />
              </button>
            </Badge>
          ))}
          <button
            type="button"
            onClick={() => onChange([])}
            className="text-[9px] text-muted-foreground hover:text-foreground"
          >
            clear
          </button>
        </div>
      ) : null}

      {open ? (
        <div className="absolute right-0 top-full z-20 mt-1 w-56 rounded-md border border-border bg-card shadow-lg">
          <input
            autoFocus
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") setOpen(false);
              if (e.key === "Enter" && matches[0]) addTag(matches[0]);
            }}
            placeholder="search tags…"
            className="w-full border-b border-border bg-transparent px-2 py-1.5 text-[11px] outline-none"
          />
          <div className="max-h-48 overflow-y-auto">
            {matches.length === 0 ? (
              <p className="px-2 py-2 text-[10px] text-muted-foreground/60">no matching tags.</p>
            ) : (
              matches.map((tag) => (
                <button
                  key={tag}
                  type="button"
                  onClick={() => addTag(tag)}
                  className="block w-full truncate px-2 py-1.5 text-left text-[11px] hover:bg-accent"
                >
                  {tag}
                </button>
              ))
            )}
          </div>
          <button
            type="button"
            onClick={() => setOpen(false)}
            className="block w-full border-t border-border px-2 py-1 text-[10px] text-muted-foreground hover:bg-accent/50"
          >
            done
          </button>
        </div>
      ) : null}
    </div>
  );
}
