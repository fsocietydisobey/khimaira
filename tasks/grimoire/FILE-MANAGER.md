# Grimoire — File-manager presentation over the auto-organized substrate

## Goal

Give the grimoire a file-manager-style UX — nested folders, sidebar quick-access
rails, breadcrumb navigation, drag-to-move, list/grid toggle — **as a presentation
layer over the existing auto-organized store, NOT as a manual filesystem.** The win
Joseph wants is hierarchy + familiar navigation + quick-access; the thing we must
NOT lose is content-based self-organization (a literal "drag files into folders you
maintain" model is exactly what rotted `shared-docs/`).

## The five load-bearing principles

1. **Hierarchy is the real win.** Collections today are FLAT (one level; a note has
   one `tab_id`). Upgrade collections to a **tree** — that single change delivers
   most of "more organization." The organizer files into the tree instead of a flat
   list.
2. **Virtual folders, never real directories.** A note is not a file — it has tags,
   an abstract, embeddings, a resolution. A note surfaces under its collection AND
   its tags AND Starred AND Recent simultaneously. Real dirs can't do that; the
   record model already can. Do NOT create on-disk folders.
3. **Manual + auto coexist (the tension-resolver).** A manual move (drag into a
   folder) sets `tab_id` AND pins it (`pinned_placement=true`); the organizer SKIPS
   pinned notes and keeps auto-filing the rest. "Propose, don't dispose" applied to
   filing — the system proposes placement, the human overrides where they care, and
   overrides are durable.
4. **Search + chat stay first-class.** At 130+ guides, semantic search + the
   per-guide chat + `notebook_ask` are often faster than clicking drawers. The
   file-manager is ONE view; it does not demote search.
5. **Compact list/tree default, grid optional.** Big-icon grids are space-hungry for
   a text corpus. Default to a dense list/tree (VS Code explorer / Nautilus list
   view); offer the icon grid as a toggle.

## Data model (needs architect sign-off before build — see below)

- **Tab record** `+ parent_id: str | None` — nesting. Existing flat tabs migrate to
  `parent_id=None` (root level); purely additive, no data rewrite. Tabs already
  carry `kind: "folder" | "collection"` — clarify how nesting interacts with kind.
- **Note record** `+ pinned_placement: bool` (default False) — a manual move sets
  `tab_id` and flips this true; `organize_library` skips pinned notes.
- **Note record** `+ starred: bool` (default False) — the Starred rail.
- **Recent** is derived (sort by `updated_at`) — no new field.

## Organizer changes

- `organize_library` **skips notes with `pinned_placement=true`** (user-owned
  placement, same principle as priority being user-owned).
- **v1 scope bound:** the organizer files into EXISTING tree leaves; it does NOT
  invent nested hierarchy on its own in v1 (humans create the tree; a later pass can
  propose sub-collections). This bounds the risk of the organizer churning the tree.

## API

- **Tabs:** `parent_id` accepted on create/update; reparent via PATCH. `GET /tabs`
  returns the tree (or flat list + client builds it). Cycle prevention: a tab cannot
  be its own ancestor (reject on reparent).
- **Notes:** PATCH `pinned_placement`, `starred`. A "move" = PATCH `tab_id` +
  `pinned_placement=true`. List filters: `?starred=true`, by collection (decide:
  direct members only vs. include descendants), sort by recent.
- Deleting a parent collection: decide reparent-children-to-root vs. cascade
  (recommend reparent-to-root — never silently delete guides).

## Frontend — the file-manager LibraryView

- **Sidebar rails:** Recent · Starred · Collections (tree) · Tags · 🔒 Vault
  (the sensitive-notes collection).
- **Main pane:** breadcrumb path + list/tree view (default) or icon grid (toggle);
  multi-select; drag-to-move (sets `tab_id` + pins). Priority = a colored dot + a
  sort axis; status badge as today.
- Per-guide chat + global search remain accessible from every view.

## Sequencing

Queue **after** sensitive-notes + priority-flags land (they touch the same
`notes.py` record + LibraryView; doing FM first would mean thrashing those files
three times). `starred` + `priority` compose directly into the FM list, so landing
them first is the right order.

## Verify

- A nested collection round-trips (create child under parent, cycle rejected).
- A note moved manually STAYS PUT across an organize sweep (pin respected); an
  un-pinned note still auto-files.
- Starred + Recent rails populate; drag-move sets tab_id + pin; delete-parent
  reparents children (no guide lost).
- Search + chat still reachable and primary; list view is the default, grid toggles.

---

## Architect review — build requirements (FOLDED IN, decision b1f7c1b45b5f)

Inspection-grade review of the real `notes.py` / `notebook_organizer.py` /
`api/notebook.py`. Storage layer is SAFE; the risk is entirely in the
**resolve-a-tab-by-title** layer, which nesting silently breaks. These are
mandatory for the build — void-null must satisfy each.

### Data-model shape (final)

```python
# tab record — adjacency list (NOT materialized path: append-only fold would
# rewrite every descendant on reparent; NOT closure table: overkill at ~130 items)
{ "id", "title", "kind": "folder"|"collection",
  "parent_id": str | None,        # None = root
  "created_at", "updated_at", "deleted": False }

# note record (+ _index_stub + _NOTE_MUTABLE_FIELDS + UpdateNoteReq)
{ ..., "pinned_placement": False, "starred": False }
```

### Tab invariants — ALL enforced in the `update_tab` reparent write path

1. **No cycle** — a tab is never its own ancestor. Missing check = infinite-loop /
   stack-overflow DoS on every tree-build / breadcrumb / descendant walk. Walk
   ancestors of the proposed parent; reject (422) if `tab_id` appears.
2. **Homogeneous subtree** — `parent.kind == child.kind` (folders don't nest under
   collections or vice versa; preserves the notes/guides namespace split).
3. **Sibling-unique** — `(kind, parent_id, title_norm)` unique among live tabs.
   This is the invariant that replaces the old global title-uniqueness assumption.
4. **Parent exists** — reject a dangling `parent_id` on BOTH create and update.

### The load-bearing change — parent-scope the get-or-create-by-title layer (Q6)

Bug class: *any path that resolves a collection/folder by `title` alone returns an
arbitrary same-named sibling once titles are non-unique across the tree — silently
misfiling guides on every organize sweep.* BROKEN paths (all title-keyed today):
`_get_or_create_tab_by_kind` (notes.py:829-840), `get_or_create_collection/folder`
(843-854), `organize_library` `existing_collections/folders` title-sets
(organizer:196-197), organizer re-file `get_or_create_*(location)` (organizer:234),
`assign_deterministic`→`get_or_create_collection` (organizer:89), `create_note`
collection resolve (api/notebook.py:176).

Fix: `_get_or_create_tab_by_kind(title, kind, parent_id=None)` matches on
`(kind, parent_id, title_norm)`. **v1 organizer passes `parent_id=None`** (auto-files
into ROOT-level collections only; nesting is human-authored per the v1 bound) — so a
human-created nested "API" is deliberately invisible to the organizer's auto-create
until a v2 tree-aware pass. Documented safe limitation, NOT a silent misfile.

### Pin must be respected at EVERY `mark_organized` caller (Q4) — not just one

`organize_library`: add `and not n.get("pinned_placement")` to the `organizable`
filter (organizer:~189) — one guard covers both the sweep and the post-structuring
hook. BUT there's a side path: `assign_deterministic`→`mark_organized`
(organizer:89-90, called from `notebook_import.py:159`) does NOT check the pin — a
re-import of a pinned guide's `source_path` silently overrides the manual placement.
**Guard all three `mark_organized` call sites** (organizer:90, organizer:236/238,
import:159). Also required or the feature can't work: add `pinned_placement`+`starred`
to `_NOTE_MUTABLE_FIELDS` (else `update_note` rejects them), to `UpdateNoteReq`, and
to `_index_stub` (`?starred=true` filters on the folded STUB — unprojected = invisible
to the filter).

### `delete_tab` is greenfield — must re-file TWO things (Q3)

No `delete_tab` / `DELETE /tabs` exists yet. "Never lose a guide" is NOT enforced by
reparenting child tabs alone: member notes carry a dead `tab_id` and become
tree-unreachable (not deleted from disk, but invisible). Delete-parent must re-file
**child tabs → root (or the deleted tab's parent)** AND **direct member notes → the
deleted tab's parent / `_DEFAULT_TAB_ID`**. The member-note re-filing is the actual
enforcement and the easy thing a naive impl omits.

### Reader changes (everything else is unchanged — additive)

`_with_note_ids`: add `setdefault("parent_id", None)` (mirrors the existing `kind`
setdefault) so pre-migration tabs read as root. `list_tabs` stays FLAT — client builds
the tree; `?tab_id=` stays direct-members-only (descendant queries are a query-time
tree walk, never a stored field). `_fold_tabs` / `_index_stub` (tab side) untouched.

### The 3 invariant tests that MUST exist (class-level, not path-level)

1. **Sibling-name isolation** (closes the whole Q6 class): two `collection` tabs both
   titled "API" under different parents → `get_or_create_collection("API", parent_id=P1)`
   vs `(..., P2)` return DISTINCT ids; a guide filed "into API" lands in the shown tab.
2. **Pin survives sweep AND re-import** (closes Q4's side path): pin a guide → run
   `organize_library()` → `tab_id` unchanged → fire `assign_deterministic`/re-import →
   `tab_id` STILL unchanged (the second half is what a naive impl fails).
3. **Delete-parent loses nothing** (closes Q3): parent→child collections w/ a member
   guide in each → delete parent → (a) no infinite loop on tree-build, (b) child tabs
   reparented, (c) every member note resolves to a live `tab_id` (list_notes count
   unchanged, no dead tab_id). Assert reparent-under-own-descendant → 422 here too.

### Files that change

`notes.py` (tab shape, `_with_note_ids` setdefault, `update_tab` allowed-fields +
cycle/kind/uniqueness/parent-exists checks, new `delete_tab`, parent-scoped
`_get_or_create_tab_by_kind`, note `_NOTE_MUTABLE_FIELDS` + `_index_stub`) ·
`notebook_organizer.py` (`organizable` pin filter, `assign_deterministic` pin guard,
root-scoped get-or-create) · `api/notebook.py` (`UpdateNoteReq`/`CreateTabReq`/
`UpdateTabReq` + `parent_id`, `DELETE /tabs/{id}`) · `notebook_import.py:159` (pin
guard). Daemon restart required to go live (`monitor/**`); check `/api/version`
`stale` before any live test.
