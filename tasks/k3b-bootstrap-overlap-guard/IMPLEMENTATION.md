# K3b — Server-side bootstrap-overlap guard

**Alpha-close item.** Status: ⬜ not started. Priority: P1 (only lived-damage item still
structurally open). Owner: dispatch to a roster (architect → agent → verifier).

## Problem (from ROSTER_ISSUES K3b — caused real damage 2026-06-05)

A second master ran `/khimaira-bootstrap-roster` while a roster chat already existed.
The bootstrap created a **second** chat (`chat-e619024f2b92`) that re-invited the **same**
agent sessions already in the first chat (`chat-5dae92cf6221`). Result: agents were members
of two chats and cross-received invites/notifications — Joseph had to terminate the roster.

The bootstrap **skill** has a guard (Step 5.5: detect existing roster chat → incremental-add)
but it **didn't fire / wasn't enforced** (title mismatch, member-overlap computed before
invites landed, or the master called `chat_create_room` directly and skipped the skill).

**Root cause:** the idempotency check lives in the *skill* (advisory, skippable). The durable
fix is **server-side**: `chats.create_room` itself must refuse to fork a chat whose member-set
overlaps a live roster.

Tonight's manual-default bootstrap *reduces* the trigger (master stands by; user bootstraps
once) but does **not** close the class — a careless or duplicated bootstrap can still fork.

## Approach (server-side, in `chats.create_room`)

`packages/khimaira/src/khimaira/monitor/chats.py:708 create_room(creator_session_id, member_session_ids, ...)`

Before writing the new room's member records:

1. Compute `new_members = set(resolved_members)`.
2. Scan existing chats (reuse the `_chat_dir().glob("chat-*.jsonl")` + `load_room` pattern
   already used by `my_chats`) for a **live** chat where:
   - **member-overlap** `|existing_accepted_or_pending ∩ new_members| / |new_members| ≥ 0.5`, AND
   - the chat is **live** — has ≥1 member NOT in state `left`/`removed`, and last activity
     within a freshness window (reuse the alive-guard horizon, e.g. `KHIMAIRA_ALIVE_DELETE_GUARD_S`).
3. If such a chat exists → **raise a structured conflict** (not a silent proceed):
   `raise RosterOverlapError(existing_chat_id, overlap_members)` — surfaced by the API layer
   as **HTTP 409** with body `{existing_chat_id, overlap_count, overlap_members}`.
4. **Escape hatch:** `create_room(..., allow_overlap: bool = False)`. `allow_overlap=True`
   bypasses the guard for the rare deliberate parallel-chat case. The API endpoint exposes it
   as a query param; default off.

### Caller change (bootstrap skill)
`/khimaira-bootstrap-roster` must catch the 409 and route to its **incremental-add** path
(Step 6b): `chat_invite` the *missing* members into `existing_chat_id` instead of creating a
new chat. This makes the skill's Step 5.5 a convenience, with the server as the real backstop.

## Acceptance criteria

- `create_room` with members overlapping a live roster by ≥50% → raises (API 409) carrying
  `existing_chat_id`; **no second chat file is written.**
- `create_room(allow_overlap=True)` → creates the second chat anyway (override works).
- `create_room` with <50% overlap, or where the overlapping chat is fully `left`/stale →
  proceeds normally (no false block on a genuinely new roster or a dead one).
- Bootstrap skill: given a 409, invites only the missing members into the existing chat;
  no duplicate chat; no agent ends up in two live roster chats.

## Tests (`packages/khimaira/tests/test_chats.py` + `test_chats_api.py`)

- `test_create_room_rejects_overlapping_live_roster` — create roster A (members X,Y,Z),
  attempt create roster B with {X,Y,Z} → 409 with A's chat_id; assert only one chat exists.
- `test_create_room_allow_overlap_override` — same, with `allow_overlap=True` → second chat created.
- `test_create_room_no_overlap_proceeds` — disjoint members → created.
- `test_create_room_stale_overlap_proceeds` — overlapping chat all-`left` / past freshness → created.
- **Class-invariant test:** `test_no_two_live_chats_share_majority_members` — after any
  create sequence, assert no two non-stale chats share ≥50% members. This catches the *class*
  (duplicate-roster) regardless of which path (skill vs direct create) triggered it.

## Out of scope
- K3c name auto-suffix (separate beta item).
- The skill's title-match heuristic — superseded by the server-side member-overlap check.
